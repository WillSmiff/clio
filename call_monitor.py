import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import urlopen

import jwt
from jwt import PyJWKClient

from phonebook import load_phonebook, normalize_number


CALLBACK_PATH = "/api/calls"
OPENID_CONFIGURATION_URL = (
    "https://api.aps.skype.com/v1/.well-known/OpenIdConfiguration"
)
LOGGER = logging.getLogger("teams-call-monitor")


def caller_number(call):
    source = call.get("source") or {}
    identity = source.get("identity") or {}
    phone = identity.get("phone") or {}
    number = phone.get("id")
    return number if isinstance(number, str) else None


def incoming_call_messages(payload, phonebook):
    messages = []
    for notification in payload.get("value", []):
        call = notification.get("resourceData") or {}
        if call.get("state", "").casefold() != "incoming":
            continue

        number = caller_number(call)
        if not number:
            LOGGER.warning("Incoming call notification did not include a phone identity")
            continue

        caller = phonebook.get(normalize_number(number), number)
        messages.append(f"Incoming call from {caller}")
    return messages


def load_signing_keys():
    try:
        with urlopen(OPENID_CONFIGURATION_URL, timeout=10) as response:
            configuration = json.load(response)
    except (OSError, URLError, json.JSONDecodeError) as error:
        raise RuntimeError("Could not load Microsoft callback signing-key metadata") from error

    jwks_uri = configuration.get("jwks_uri")
    if not jwks_uri:
        raise RuntimeError("Microsoft callback signing-key metadata has no jwks_uri")
    return PyJWKClient(jwks_uri)


def create_handler(phonebook, signing_keys, app_id, tenant_id=None):
    class CallWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != CALLBACK_PATH:
                self.send_error(404)
                return

            authorization = self.headers.get("Authorization", "")
            scheme, separator, token = authorization.partition(" ")
            if not separator or scheme.casefold() != "bearer" or not token:
                self.send_error(401)
                return

            try:
                signing_key = signing_keys.get_signing_key_from_jwt(token).key
                claims = jwt.decode(
                    token,
                    signing_key,
                    algorithms=["RS256"],
                    audience=app_id,
                    issuer="https://api.botframework.com",
                    options={"require": ["exp", "iss", "aud"]},
                )
                if tenant_id and claims.get("tid") != tenant_id:
                    self.send_error(401)
                    return
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0 or content_length > 1_000_000:
                    self.send_error(400)
                    return
                payload = json.loads(self.rfile.read(content_length))
                if not isinstance(payload, dict):
                    self.send_error(400)
                    return
            except (jwt.PyJWTError, ValueError, json.JSONDecodeError):
                self.send_error(401)
                return

            for message in incoming_call_messages(payload, phonebook):
                print(message, flush=True)

            self.send_response(204)
            self.end_headers()

        def log_message(self, format_string, *args):
            LOGGER.info("%s - %s", self.address_string(), format_string % args)

    return CallWebhookHandler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app_id = os.environ.get("MICROSOFT_APP_ID")
    if not app_id:
        raise SystemExit("Set MICROSOFT_APP_ID to the calling bot's application ID.")

    phonebook_path = os.environ.get("PHONEBOOK_PATH", "phonebook.csv")
    host = os.environ.get("CALLBACK_HOST", "0.0.0.0")
    port = int(os.environ.get("CALLBACK_PORT", "3978"))
    tenant_id = os.environ.get("MICROSOFT_TENANT_ID")
    phonebook = load_phonebook(phonebook_path)
    signing_keys = load_signing_keys()
    server = ThreadingHTTPServer(
        (host, port), create_handler(phonebook, signing_keys, app_id, tenant_id)
    )
    LOGGER.info("Listening for Teams calling callbacks on %s:%s%s", host, port, CALLBACK_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping call monitor")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()