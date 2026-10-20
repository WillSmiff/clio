import asyncio
import html
import logging
import os
import re
import signal
import sys
from collections import namedtuple
from datetime import datetime
from pathlib import Path

from aurora_client import AuroraClient, to_e164
from phonebook import load_phonebook, normalize_number


LOGGER = logging.getLogger("teams-nixos-monitor")
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"
PORTAL_INTERFACE = "org.freedesktop.portal.Notification"
MONITOR_RULES = [
    "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
    "type='method_return'",
    "type='signal',interface='org.freedesktop.Notifications',"
    "member='NotificationClosed'",
    "type='method_call',interface='org.freedesktop.portal.Notification',member='AddNotification'",
    "type='method_call',interface='org.freedesktop.portal.Notification',member='RemoveNotification'",
    "type='signal',interface='org.freedesktop.portal.Notification',"
    "member='ActionInvoked'",
]
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+|00)\s*\d[\d\s().-]{4,}\d(?!\w)")
#: A number Teams for Linux passed to the command hook with its `+` removed.
STRIPPED_INTERNATIONAL_PATTERN = re.compile(r"[1-9][\d\s().-]*")
TEAMS_FOR_LINUX_NAMES = {"microsoft teams for linux", "teams for linux"}
GENERIC_CALLER_LABELS = {
    "incoming call",
    "incoming audio call",
    "incoming video call",
}
CALLER_ID_UNAVAILABLE = "Caller ID unavailable"
#: Strings that report a ringing phone without identifying anybody.
NON_NAMES = GENERIC_CALLER_LABELS | {CALLER_ID_UNAVAILABLE.casefold()}

#: A caller as Clio understands them: `display` is what gets printed and logged
#: (a phonebook name where there is one), `number` is the E.164 number Aurora
#: resolves against its own directory, or None when the call carried no number.
#: Both are needed because the two consumers want different things, and the
#: printed output must not change.
Caller = namedtuple("Caller", ("display", "number"))


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


def full_phone_number(value):
    value = clean_text(value)
    if not PHONE_PATTERN.fullmatch(value):
        return None
    normalized = normalize_number(value)
    return normalized if 7 <= len(normalized) <= 15 else None


def caller_from_notification_fields(summary, body, phonebook):
    summary = clean_text(summary)
    body = clean_text(body)
    for value in (summary, body):
        number = full_phone_number(value)
        if number:
            return Caller(phonebook.get(number, value), to_e164(number))

    for value in (summary, body):
        if meaningful_name(value):
            return Caller(value, None)
    return Caller(CALLER_ID_UNAVAILABLE, None)


def meaningful_name(value):
    """
    Whether a string actually names somebody, rather than being empty, one of
    Teams' generic "Incoming call" labels, or Clio's own "unavailable" sentinel.
    Those say only that the phone is ringing, which the overlay conveys by
    itself, so none of them should be shown as though it were a caller.
    """
    value = (value or "").strip()
    return bool(value) and value.casefold() not in NON_NAMES


def incoming_call_identity(caller, text, phonebook):
    """
    Resolve the caller and text Teams for Linux passes to the command hook.

    Returns both the display name and the number, so the log keeps reading the
    way it always has while Aurora still receives something it can match
    against its directory.
    """
    caller = clean_text(caller)
    text = clean_text(text)

    for candidate in (caller, text):
        for match in PHONE_PATTERN.finditer(candidate):
            number = match.group().strip()
            normalized = normalize_number(number)
            if not 7 <= len(normalized) <= 15:
                continue
            e164 = to_e164(number)
            known_name = phonebook.get(normalized)
            if known_name:
                return Caller(known_name, e164)
            # Teams puts a richer caller string in `text` for some calls, which
            # beats showing a bare number -- but only when it names somebody.
            if (
                candidate == caller
                and meaningful_name(text)
                and normalize_number(text) != normalized
            ):
                return Caller(text, e164)
            return Caller(number, e164)

    # No number anywhere: a Teams-to-Teams call, which carries only a name.
    if meaningful_name(caller):
        return Caller(caller, None)
    if meaningful_name(text):
        return Caller(text, None)
    return Caller(CALLER_ID_UNAVAILABLE, None)


def restore_plus(value):
    """
    Put back the `+` that Teams for Linux strips from command-hook arguments.

    It drops every Unicode symbol, `+` included, before running the command,
    so "+31 6 1234 5678" arrives as "31 6 1234 5678". A value of nothing but
    digits and separators that does not start with 0 can only be such a number;
    national (`0...`) and `00...` numbers keep their digits and are left alone.
    """
    value = (value or "").strip()
    if STRIPPED_INTERNATIONAL_PATTERN.fullmatch(value) and 7 <= len(
        normalize_number(value)
    ) <= 15:
        return f"+{value}"
    return value


def incoming_call_caller(caller, text, phonebook):
    """The display name alone, kept for callers that do not need the number."""
    return incoming_call_identity(caller, text, phonebook).display


def reportable_name(caller):
    """
    The display name to hand Aurora, which is nothing when Clio could not work
    out who is calling: a status like "Caller ID unavailable" does not belong on
    a wall-mounted screen as if it were the caller's name.
    """
    return caller.display if meaningful_name(caller.display) else None


def log_call_message(message):
    print(message, flush=True)
    default_log_path = Path.home() / ".local" / "state" / "callerid" / "teams-calls.log"
    log_path = Path(os.environ.get("CALL_MONITOR_LOG", default_log_path))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as log_file:
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            log_file.write(f"{timestamp} {message}\n")
    except OSError as error:
        LOGGER.error("Could not write call log %s: %s", log_path, error)


def parse_call_notification(arguments, phonebook):
    if len(arguments) < 8:
        return None

    app_name, replaces_id, _icon, summary, body = arguments[:5]
    app_name_text = clean_text(app_name).casefold()
    if app_name_text not in TEAMS_FOR_LINUX_NAMES:
        return None

    title_number = full_phone_number(summary)
    body_number = full_phone_number(body)
    if title_number is None or title_number != body_number:
        return None

    caller = Caller(
        phonebook.get(title_number, clean_text(summary)),
        to_e164(title_number),
    )
    return {
        "app_name": str(app_name),
        "replaces_id": int(replaces_id),
        "caller": caller,
    }


def unwrap_variant(value):
    return value.value if hasattr(value, "value") else value


def parse_portal_call_notification(notification, phonebook):
    notification = {
        key: unwrap_variant(value)
        for key, value in notification.items()
    }
    summary = clean_text(notification.get("title", ""))
    body = clean_text(
        notification.get("markup-body", notification.get("body", ""))
    )
    button_labels = []
    for button in notification.get("buttons", []):
        button = {key: unwrap_variant(value) for key, value in button.items()}
        button_labels.extend(
            str(button.get(key, ""))
            for key in ("label", "purpose", "action")
        )

    has_incoming_category = notification.get("category") == "call.incoming"
    labels = [label.casefold() for label in button_labels]
    has_answer_action = any(
        re.search(r"\b(?:accept|answer|call\.accept)\b", label)
        for label in labels
    )
    has_decline_action = any(
        re.search(r"\b(?:decline|reject|ignore|call\.decline)\b", label)
        for label in labels
    )
    title_number = full_phone_number(summary)
    body_number = full_phone_number(body)
    has_repeated_caller_number = (
        title_number is not None and title_number == body_number
    )
    if not (has_incoming_category or has_repeated_caller_number or (has_answer_action and has_decline_action)):
        return None

    return caller_from_notification_fields(summary, body, phonebook)


def debug_notifications_enabled():
    return os.environ.get("CALL_MONITOR_DEBUG_NOTIFICATIONS") == "1"


class NotificationCallTracker:
    def __init__(self, phonebook, aurora=None):
        self.phonebook = phonebook
        self.aurora = aurora
        self.pending_notifications = {}
        self.active_notifications = {}

    def start_notification(self, key, caller):
        already_active = caller in self.active_notifications.values()
        self.active_notifications[key] = caller
        if already_active:
            # Teams replaces its own notification while a call rings on; the
            # screen is already showing this caller.
            return []
        if self.aurora:
            self.aurora.ring(caller.number, reportable_name(caller))
        return [f"Incoming call from {caller.display}"]

    def stop_notification(self, key):
        caller = self.active_notifications.pop(key, None)
        if not caller:
            return []

        for active_key, active_caller in list(self.active_notifications.items()):
            if active_caller == caller:
                self.active_notifications.pop(active_key)
        # Only dismiss once nothing is ringing any more, so hanging up one call
        # does not clear the overlay of another that is still coming in.
        if self.aurora and not self.active_notifications:
            self.aurora.end()
        return [f"Incoming call stopped ringing from {caller.display}"]

    def handle_message(self, message):
        from dbus_next import MessageType

        if (
            message.message_type == MessageType.METHOD_CALL
            and message.interface == NOTIFICATIONS_INTERFACE
            and message.member == "Notify"
        ):
            notification = parse_call_notification(message.body, self.phonebook)
            if debug_notifications_enabled():
                app_name = message.body[0] if len(message.body) > 0 else ""
                summary = message.body[3] if len(message.body) > 3 else ""
                body = message.body[4] if len(message.body) > 4 else ""
                print(
                    "D-Bus Notify observed: "
                    f"app={app_name!r} title={clean_text(summary)!r} "
                    f"body={clean_text(body)!r} "
                    f"call_match={notification is not None}",
                    flush=True,
                )
            if notification:
                self.pending_notifications[(message.sender, message.serial)] = notification
            return []

        if (
            message.message_type == MessageType.METHOD_CALL
            and message.interface == PORTAL_INTERFACE
            and message.member == "AddNotification"
            and len(message.body) >= 2
        ):
            notification_id = str(message.body[0])
            caller = parse_portal_call_notification(message.body[1], self.phonebook)
            if debug_notifications_enabled():
                print(
                    "XDG portal notification observed: "
                    f"id={notification_id!r} call_match={caller is not None}",
                    flush=True,
                )
            if caller:
                return self.start_notification(("portal", notification_id), caller)
            return []

        if (
            message.message_type == MessageType.METHOD_CALL
            and message.interface == PORTAL_INTERFACE
            and message.member == "RemoveNotification"
            and message.body
        ):
            return self.stop_notification(("portal", str(message.body[0])))

        if (
            message.message_type == MessageType.METHOD_RETURN
            and (message.destination, message.reply_serial) in self.pending_notifications
            and message.body
        ):
            notification = self.pending_notifications.pop(
                (message.destination, message.reply_serial)
            )
            notification_id = int(message.body[0])
            replaced_id = notification["replaces_id"]
            existing_caller = self.active_notifications.pop(
                ("freedesktop", replaced_id), None
            )
            if existing_caller:
                self.active_notifications[("freedesktop", notification_id)] = existing_caller
                return []
            return self.start_notification(
                ("freedesktop", notification_id), notification["caller"]
            )

        if (
            message.message_type == MessageType.SIGNAL
            and message.interface == NOTIFICATIONS_INTERFACE
            and message.member == "NotificationClosed"
            and len(message.body) >= 2
        ):
            return self.stop_notification(("freedesktop", int(message.body[0])))

        if (
            message.message_type == MessageType.SIGNAL
            and message.interface == PORTAL_INTERFACE
            and message.member == "ActionInvoked"
            and len(message.body) >= 2
        ):
            action = str(message.body[1]).casefold()
            if re.search(r"\b(?:accept|answer|decline|reject|ignore)\b", action):
                return self.stop_notification(("portal", str(message.body[0])))

        return []


def handle_monitor_message(message, tracker):
    from dbus_next import MessageType

    try:
        for output in tracker.handle_message(message):
            print(output, flush=True)
    except Exception:
        LOGGER.exception("Failed to process a monitored D-Bus message")

    # A monitor connection cannot reply to observed method calls. Returning
    # True consumes those messages; method returns must pass through so
    # MessageBus.call() can complete the BecomeMonitor handshake.
    return message.message_type == MessageType.METHOD_CALL


async def run_monitor(phonebook, aurora=None):
    from dbus_next import Message, MessageType
    from dbus_next.aio import MessageBus

    print("Connecting to the session D-Bus...", flush=True)
    bus = await MessageBus().connect()
    print("Connected to the session D-Bus.", flush=True)
    tracker = NotificationCallTracker(phonebook, aurora)
    bus.add_message_handler(lambda message: handle_monitor_message(message, tracker))
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    try:
        reply = await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus.Monitoring",
                member="BecomeMonitor",
                signature="asu",
                body=[MONITOR_RULES, 0],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(
                "The session D-Bus rejected BecomeMonitor; this desktop bus may not "
                "allow notification monitoring."
            )

        print(
            "Session D-Bus monitor active; waiting for desktop notifications.",
            flush=True,
        )
        await stop_event.wait()
        LOGGER.info("Stopping NixOS Teams call monitor")
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        bus.disconnect()
        try:
            await bus.wait_for_disconnect()
        except EOFError:
            LOGGER.debug("Session D-Bus connection closed")


def run_incoming_call_command(arguments, phonebook, aurora=None):
    caller = restore_plus(arguments[0] if arguments else "")
    text = restore_plus(arguments[1] if len(arguments) > 1 else "")
    identity = incoming_call_identity(caller, text, phonebook)
    log_call_message(f"Incoming call from {identity.display}")

    # Teams runs one of these per call, so this process never sees the others.
    # Dismissing an overlay this call never raised would clear the overlay of a
    # second call that is still ringing, hence the flag.
    reported = False

    def report_call_ended(_signum, _frame):
        log_call_message(f"Incoming call stopped ringing from {identity.display}")
        if aurora and reported:
            aurora.end()
        raise SystemExit(0)

    # Installed before the call is reported, so a call answered while Aurora is
    # still being told about it is not missed.
    signal.signal(signal.SIGTERM, report_call_ended)
    if aurora:
        reported = aurora.ring(identity.number, reportable_name(identity))

    try:
        while True:
            signal.pause()
    except KeyboardInterrupt:
        LOGGER.info("Stopping incoming-call command")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    phonebook_path = os.environ.get(
        "PHONEBOOK_PATH", str(Path(__file__).with_name("phonebook.csv"))
    )
    phonebook = load_phonebook(phonebook_path)
    aurora = AuroraClient.from_env()
    if aurora:
        LOGGER.info("Reporting incoming calls to Aurora at %s", aurora.base_url)

    if len(sys.argv) > 1 and sys.argv[1] == "--incoming-call":
        run_incoming_call_command(sys.argv[2:], phonebook, aurora)
        return

    try:
        asyncio.run(run_monitor(phonebook, aurora))
    except KeyboardInterrupt:
        LOGGER.info("Stopping NixOS Teams call monitor")
    except Exception as error:
        raise SystemExit(f"Could not monitor the session D-Bus: {error}") from error


if __name__ == "__main__":
    main()