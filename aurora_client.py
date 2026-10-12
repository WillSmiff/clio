"""Report incoming Teams calls to Aurora, so the info screen shows the caller.

Only the standard library is used, which keeps the `--incoming-call` command
hook free of dependencies: `dbus-next` stays a requirement of the notification
watcher alone.

Aurora resolves the number against its own caller directory, so the number is
what matters here; the name Clio worked out locally is sent along as a fallback
for a caller Aurora does not know.

Reporting is best-effort by design. A screen that does not light up is a worse
outcome than a missed call log, but an unreachable screen must never stop Clio
from handling the call, so every failure is logged and swallowed.
"""

import json
import logging
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

from phonebook import normalize_number

LOGGER = logging.getLogger("clio.aurora")

RING_PATH = "/api/handler/screen/info/caller-ring"
END_PATH = "/api/handler/screen/info/caller-end"

#: Kept short: this runs on the path that announces a call, and a screen that
#: updates late is of no use anyway.
DEFAULT_TIMEOUT_SECONDS = 2.0

#: E.164 allows 15 digits; 7 is about the shortest number worth believing.
MIN_DIGITS = 7
MAX_DIGITS = 15


def to_e164(number):
    """
    Render a reported number as `+<digits>`, or None when it does not look like
    a phone number.

    Aurora compares numbers with spacing and punctuation stripped but the `+`
    intact, so a directory entry of "+31 6 1234 5678" only matches if the `+`
    survives. `normalize_number` drops it (it strips every non-digit), hence
    this step rather than reusing it alone. A `00` international prefix becomes
    `+`, which is the form Teams and the phonebook both use.
    """
    digits = normalize_number(number or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        return None
    return f"+{digits}"


def read_api_key(environ):
    """
    The key from `AURORA_API_KEY`, or read from `AURORA_API_KEY_FILE`.

    The file is preferred on NixOS: anything in a Nix store path is
    world-readable, so the key belongs in a file managed by agenix/sops-nix and
    referenced by path.
    """
    inline = (environ.get("AURORA_API_KEY") or "").strip()
    if inline:
        return inline

    key_path = (environ.get("AURORA_API_KEY_FILE") or "").strip()
    if not key_path:
        return None

    path = Path(key_path)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        LOGGER.error("Could not read AURORA_API_KEY_FILE %s: %s", path, error)
        return None

    if not key:
        LOGGER.error("AURORA_API_KEY_FILE %s is empty", path)
        return None

    try:
        mode = path.stat().st_mode
    except OSError:
        mode = 0
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        LOGGER.warning(
            "AURORA_API_KEY_FILE %s is readable by other users; chmod 600 it",
            path,
        )
    return key


def _describe_http_error(error):
    """Turn a status code into something that names the likely fix."""
    status = error.code
    if status in (401, 403):
        return (
            f"{status} {error.reason} - check AURORA_API_KEY and that its integration "
            "user has the setInfoCallerRing and setInfoCallerEnd scopes"
        )
    if status == 409:
        return (
            f"{status} {error.reason} - the InfoScreen.CallerId setting is switched off "
            "in Aurora, so no call is shown"
        )
    if status == 404:
        return (
            f"{status} {error.reason} - this Aurora has no caller endpoints; it predates "
            "the caller ID overlay"
        )
    return f"{status} {error.reason}"


class AuroraClient:
    """Pushes incoming-call events to one Aurora core."""

    def __init__(self, base_url, api_key, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key

    @classmethod
    def from_env(cls, environ=None):
        """
        Build a client from the environment, or None when Aurora is not
        configured. Reporting is optional: without it Clio still logs calls, so
        a missing configuration is not an error.
        """
        environ = os.environ if environ is None else environ
        base_url = (environ.get("AURORA_URL") or "").strip()
        api_key = read_api_key(environ)

        if not base_url or not api_key:
            # Half a configuration is a mistake worth pointing at, rather than
            # silently never showing a caller on the screen.
            if base_url or api_key:
                missing = "AURORA_URL" if not base_url else "AURORA_API_KEY/AURORA_API_KEY_FILE"
                LOGGER.warning(
                    "Aurora reporting is disabled: %s is not set",
                    missing,
                )
            return None

        timeout = DEFAULT_TIMEOUT_SECONDS
        raw_timeout = (environ.get("AURORA_TIMEOUT_SECONDS") or "").strip()
        if raw_timeout:
            try:
                timeout = float(raw_timeout)
            except ValueError:
                LOGGER.warning(
                    "Ignoring invalid AURORA_TIMEOUT_SECONDS %r; using %.1fs",
                    raw_timeout,
                    timeout,
                )

        return cls(base_url, api_key, timeout=timeout)

    def ring(self, number=None, name=None):
        """
        Show the incoming call on the screens.

        Aurora needs the `number` field, so an absent number is sent as an empty
        string and `name` carries the caller. With neither, Aurora reports the
        call as not ringing and nothing appears.
        """
        payload = {"number": number or ""}
        if name:
            payload["name"] = name
        if not payload["number"] and not name:
            LOGGER.info("Not reporting a call to Aurora: no caller number or name")
            return False
        return self._post(RING_PATH, payload)

    def end(self):
        """
        Dismiss the overlay because the call stopped ringing. Aurora also times
        the overlay out on its own, so a lost `end` costs a few seconds of a
        stale name rather than a screen stuck on it.
        """
        return self._post(END_PATH)

    def _post(self, path, payload=None):
        """POST to Aurora. Returns True when it was accepted."""
        url = f"{self.base_url}{path}"
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("x-api-key", self._api_key)
        if payload is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return True
        except urllib.error.HTTPError as error:
            # An HTTPError is also a URLError, so it has to be caught first.
            LOGGER.warning("Aurora rejected POST %s: %s", path, _describe_http_error(error))
        except urllib.error.URLError as error:
            LOGGER.warning("Could not reach Aurora at %s: %s", url, error.reason)
        except OSError as error:
            LOGGER.warning("Could not reach Aurora at %s: %s", url, error)
        return False
