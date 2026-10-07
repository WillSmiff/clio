import asyncio
import html
import logging
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

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
CALL_TEXT_PATTERN = re.compile(
    r"\b(?:incoming\s+(?:(?:audio|video)\s+)?call|"
    r"(?:(?:audio|video)\s+)?call\s+from|is\s+calling|calling\s+you)\b",
    re.IGNORECASE,
)
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+|00)\s*\d[\d\s().-]{4,}\d(?!\w)")
CALLER_PREFIX_PATTERN = re.compile(
    r"^\s*(?:incoming\s+(?:(?:audio|video)\s+)?call\s+from|"
    r"(?:(?:audio|video)\s+)?call\s+from)\s+",
    re.IGNORECASE,
)
CALLER_SUFFIX_PATTERN = re.compile(r"\s+(?:is\s+calling(?:\s+you)?|calling\s+you)[.!]?\s*$", re.IGNORECASE)
GENERIC_CALL_TEXT = {
    "incoming call",
    "incoming audio call",
    "incoming video call",
    "audio call",
    "video call",
    "microsoft teams",
    "teams",
}


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value or ""))).strip()


def full_phone_number(value):
    value = clean_text(value)
    if not PHONE_PATTERN.fullmatch(value):
        return None
    normalized = normalize_number(value)
    return normalized if 7 <= len(normalized) <= 15 else None


def caller_from_notification(summary, body, phonebook):
    summary = clean_text(summary)
    body = clean_text(body)
    combined_text = f"{summary} {body}"

    for match in PHONE_PATTERN.finditer(combined_text):
        number = match.group().strip()
        normalized = normalize_number(number)
        if 7 <= len(normalized) <= 15:
            return phonebook.get(normalized, number)

    for text in (body, summary):
        caller_match = CALLER_PREFIX_PATTERN.match(text)
        if caller_match:
            text = text[caller_match.end():]
        text = CALLER_SUFFIX_PATTERN.sub("", text).strip(" .,:;-–—")
        if text and text.casefold() not in GENERIC_CALL_TEXT:
            return text

    return "Caller ID unavailable"


def incoming_call_caller(caller, text, phonebook):
    caller = clean_text(caller)
    text = clean_text(text)

    for candidate in (caller, text):
        for match in PHONE_PATTERN.finditer(candidate):
            number = match.group().strip()
            normalized = normalize_number(number)
            if not 7 <= len(normalized) <= 15:
                continue
            known_name = phonebook.get(normalized)
            if known_name:
                return known_name
            if candidate == caller and text and normalize_number(text) != normalized:
                return text
            return number

    if caller:
        return caller
    if text:
        return text
    return "Caller ID unavailable"


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

    app_name, replaces_id, _icon, summary, body, actions = arguments[:6]
    summary = clean_text(summary)
    body = clean_text(body)
    has_call_text = bool(CALL_TEXT_PATTERN.search(f"{summary} {body}"))
    app_name_text = clean_text(app_name).casefold()
    title_number = full_phone_number(summary)
    body_number = full_phone_number(body)
    has_repeated_caller_number = (
        "teams" in app_name_text
        and "linux" in app_name_text
        and title_number is not None
        and title_number == body_number
    )

    action_labels = [str(action).casefold() for action in actions]
    has_answer_action = any(
        re.search(r"\b(?:accept|answer)\b", action) for action in action_labels
    )
    has_decline_action = any(
        re.search(r"\b(?:decline|reject|ignore)\b", action)
        for action in action_labels
    )
    if not (
        has_call_text
        or (has_answer_action and has_decline_action)
        or has_repeated_caller_number
    ):
        return None

    caller = caller_from_notification(summary, body, phonebook)
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

    has_call_text = bool(CALL_TEXT_PATTERN.search(f"{summary} {body}"))
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
    if not (
        has_call_text
        or has_incoming_category
        or (has_answer_action and has_decline_action)
    ):
        return None

    return caller_from_notification(summary, body, phonebook)


def debug_notifications_enabled():
    return os.environ.get("CALL_MONITOR_DEBUG_NOTIFICATIONS") == "1"


class NotificationCallTracker:
    def __init__(self, phonebook):
        self.phonebook = phonebook
        self.pending_notifications = {}
        self.active_notifications = {}

    def start_notification(self, key, caller):
        already_active = caller in self.active_notifications.values()
        self.active_notifications[key] = caller
        if already_active:
            return []
        return [f"Incoming call from {caller}"]

    def stop_notification(self, key):
        caller = self.active_notifications.pop(key, None)
        if not caller:
            return []

        for active_key, active_caller in list(self.active_notifications.items()):
            if active_caller == caller:
                self.active_notifications.pop(active_key)
        return [f"Incoming call stopped ringing from {caller}"]

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


async def run_monitor(phonebook):
    from dbus_next import Message, MessageType
    from dbus_next.aio import MessageBus

    print("Connecting to the session D-Bus...", flush=True)
    bus = await MessageBus().connect()
    print("Connected to the session D-Bus.", flush=True)
    tracker = NotificationCallTracker(phonebook)
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


def run_incoming_call_command(arguments, phonebook):
    caller = arguments[0] if arguments else ""
    text = arguments[1] if len(arguments) > 1 else ""
    display_name = incoming_call_caller(caller, text, phonebook)
    log_call_message(f"Incoming call from {display_name}")

    def report_call_ended(_signum, _frame):
        log_call_message(f"Incoming call stopped ringing from {display_name}")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, report_call_ended)
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
    if len(sys.argv) > 1 and sys.argv[1] == "--incoming-call":
        run_incoming_call_command(sys.argv[2:], phonebook)
        return

    try:
        asyncio.run(run_monitor(phonebook))
    except KeyboardInterrupt:
        LOGGER.info("Stopping NixOS Teams call monitor")
    except Exception as error:
        raise SystemExit(f"Could not monitor the session D-Bus: {error}") from error


if __name__ == "__main__":
    main()