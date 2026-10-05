import logging
import os
import re
import time
from ctypes import windll

from pywinauto import Desktop

from phonebook import load_phonebook, normalize_number


LOGGER = logging.getLogger("teams-desktop-monitor")
CALL_STATE_MARKERS = (
    "incoming call",
    "incoming audio",
    "incoming video",
    "accept audio call",
    "accept video call",
    "answer audio call",
    "answer video call",
)
CALL_ACTION_PATTERN = re.compile(
    r"\b(?:accept|answer)\s+(?:audio|video)\s+call\b", re.IGNORECASE
)
ACCEPT_BUTTON_PATTERN = re.compile(r"\b(?:accept|answer)\b", re.IGNORECASE)
DECLINE_BUTTON_PATTERN = re.compile(r"\b(?:decline|reject|ignore)\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+|00)\s*\d[\d\s().-]{4,}\d(?!\w)")


def is_incoming_call_action(control_type, text):
    return (
        control_type == "Button"
        and bool(CALL_ACTION_PATTERN.search(text or ""))
    )


def extract_incoming_caller(texts, phonebook):
    visible_text = " ".join(text.strip() for text in texts if text).casefold()
    if not any(marker in visible_text for marker in CALL_STATE_MARKERS):
        return None

    for match in PHONE_PATTERN.finditer(visible_text):
        number = match.group().strip()
        normalized_number = normalize_number(number)
        if 7 <= len(normalized_number) <= 15:
            return phonebook.get(normalized_number, number)

    return "Caller ID unavailable"


def visible_texts(window):
    texts = [window.window_text()]
    for control in window.descendants():
        text = control.window_text()
        if text:
            texts.append(text)
    return texts


def is_bottom_right_popup(rectangle, screen_width, screen_height):
    width = rectangle.right - rectangle.left
    height = rectangle.bottom - rectangle.top
    return (
        180 <= width <= 900
        and 80 <= height <= 600
        and rectangle.left >= screen_width // 2
        and rectangle.top >= screen_height // 3
        and rectangle.right >= screen_width - 700
        and rectangle.bottom >= screen_height - 500
    )


def is_incoming_call_popup(texts, controls):
    visible_text = " ".join(text for text in texts if text).casefold()
    if any(marker in visible_text for marker in CALL_STATE_MARKERS):
        return True

    button_texts = [
        control.window_text()
        for control in controls
        if control.element_info.control_type == "Button"
    ]
    has_accept = any(ACCEPT_BUTTON_PATTERN.search(text or "") for text in button_texts)
    has_decline = any(DECLINE_BUTTON_PATTERN.search(text or "") for text in button_texts)
    return has_accept and has_decline


def find_incoming_callers(desktop, phonebook):
    callers = set()
    screen_width = windll.user32.GetSystemMetrics(0)
    screen_height = windll.user32.GetSystemMetrics(1)
    for window in desktop.windows():
        try:
            if not window.is_visible():
                continue
            if not is_bottom_right_popup(window.rectangle(), screen_width, screen_height):
                continue

            controls = window.descendants()
            texts = visible_texts(window)
            if not is_incoming_call_popup(texts, controls):
                continue

            caller_texts = texts
            if not any(
                marker in " ".join(texts).casefold()
                for marker in CALL_STATE_MARKERS
            ):
                caller_texts = ["Incoming call", *texts]
            caller = extract_incoming_caller(caller_texts, phonebook)
            if caller:
                callers.add(caller)
        except Exception as error:
            LOGGER.debug("Could not inspect a Teams window: %s", error)
    return callers


def call_transition_messages(previous_callers, current_callers):
    stopped = sorted(previous_callers - current_callers)
    started = sorted(current_callers - previous_callers)
    return [
        *(f"Incoming call stopped ringing from {caller}" for caller in stopped),
        *(f"Incoming call from {caller}" for caller in started),
    ]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    phonebook_path = os.environ.get("PHONEBOOK_PATH", "phonebook.csv")
    poll_seconds = float(os.environ.get("CALL_POLL_SECONDS", "1"))
    phonebook = load_phonebook(phonebook_path)
    desktop = Desktop(backend="uia")
    active_callers = set()

    LOGGER.info("Watching the signed-in Teams desktop UI; press Ctrl+C to stop")
    try:
        while True:
            callers = find_incoming_callers(desktop, phonebook)
            for message in call_transition_messages(active_callers, callers):
                print(message, flush=True)
            active_callers = callers
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Stopping Teams desktop monitor")


if __name__ == "__main__":
    main()