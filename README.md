# Clio - Teams Incoming Call Monitor

This Python daemon receives authenticated incoming-call callbacks for a
Microsoft Teams calling bot and prints the caller's phone number. If the number
matches `phonebook.csv`, it prints the caller's name instead.

Clio currently has three versions, with each one being tailored to different use cases:

| Version   | Current status |
|-----------|----------------|
| Windows   | Tested, works  |
| NixOS     | Tested, works  |
| Teams Bot | Untested       |

## Local desktop fallback

If you cannot install a custom Teams app, try `teams_desktop_monitor.py` on the
Windows computer where you are signed in to the Teams desktop client:

```powershell
python teams_desktop_monitor.py
```

This uses Windows UI Automation to inspect accessible text in Teams windows;
it needs no Teams app registration, public URL, or callback credentials. Keep
Teams running and signed in. The UI can change between Teams versions, and
caller details might not be exposed to UI Automation, so this is an
experimental fallback rather than a supported Teams API. It prints
`Caller ID unavailable` if it detects ringing but cannot read the caller, and
may not detect a call if Teams exposes no incoming-call state in its UI tree.
To reduce false positives, it only examines small, visible windows in the
lower-right of the screen and requires incoming-call text or accessible
Accept/Answer and Decline/Reject controls. It only treats international-format
numbers (`+...` or `00...`) as caller IDs. If the toast isn't exposed to UI
Automation, the fallback won't report it. When a detected popup disappears, it
prints `Incoming call stopped ringing from <caller>`. That UI transition can't
reliably distinguish pickup from caller cancellation or timeout.

## NixOS desktop notification fallback

On NixOS, use `teams_nixos_monitor.py` instead of the Windows UI monitor. It
listens on the logged-in user's session D-Bus for desktop notification
requests through either `org.freedesktop.Notifications` or the XDG Desktop
Portal. For Teams for Linux's standard notification, it requires the app name
`Microsoft Teams for Linux` and matching phone numbers in the title and message,
allowing different spacing. Portal notifications use their incoming-call
category or accept/decline action metadata. It tracks notification closure or
removal. It does not use `journalctl`, Teams app permissions, a public URL, or
bot credentials.

Run it from the graphical login session so it can access that session's D-Bus:

```sh
nix-shell -p 'python3.withPackages (ps: [ ps.dbus-next ])' --run 'python3 teams_nixos_monitor.py'
```

Teams (or the browser hosting Teams) must have desktop notifications enabled
and include caller information in its notification. This does not read a
call-only overlay inside a browser tab. The session bus must permit the
`BecomeMonitor` operation; the program reports an error if the bus disallows
it. A portal removal or `NotificationClosed` means the desktop notification
closed, which does not prove whether the call was answered, cancelled, or
timed out.

For troubleshooting, set `CALL_MONITOR_DEBUG_NOTIFICATIONS=1` before starting
the daemon. Startup progress is printed to stdout. Debug mode also prints each
standard/portal notification observed and whether it matched the call filter.
Debug output includes notification title/body and may contain caller details;
keep it private and unset the variable after testing. If launched by a user
service, read stdout/stderr from that service's journal.

### Teams for Linux Integration

If you use Teams for Linux, prefer its incoming-call command hook over the
notification-bus watcher. Add this to its user `config.json`, replacing the
paths with the actual interpreter and script locations:

```json
{
	"incomingCalls": {
		"command": "/run/current-system/sw/bin/python3",
		"commandArgs": [
			"/home/YOUR_USER/CallerID/teams_nixos_monitor.py",
			"--incoming-call"
		]
	}
}
```

Teams for Linux appends the caller and text as arguments. The script prints the
incoming event, then prints the stopped-ringing event when Teams terminates
the child process. It prefers a phonebook match, then the caller name supplied
by Teams, then the number. This mode needs only Python's standard library;
`dbus-next` is required only for the notification-bus mode above. Use a
Teams for Linux version that includes its incoming PSTN/call-queue detection
fix (v2.24.0 or newer).

Teams for Linux captures child stdout/stderr, so command-mode events are also
appended to `~/.local/state/callerid/teams-calls.log` with private file
permissions. Follow the log with:

```sh
tail -f ~/.local/state/callerid/teams-calls.log
```

Set `CALL_MONITOR_LOG` to choose another log path. Caller names and numbers are
personal data; keep the log private and remove it when no longer needed.

## Phonebook

Edit `phonebook.csv` and keep its header as `phone,name`. Use E.164 numbers,
including the country code, to avoid ambiguous local-number matches:

```csv
phone,name
+12065550100,Avery Chen
+442079460123,Sam Taylor
```

## Teams Bot

Use Python 3.9 or later. From this directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MICROSOFT_APP_ID = "<calling-bot-application-id>"
python call_monitor.py
```

The listener defaults to `0.0.0.0:3978` at `/api/calls`. Configure the Teams
calling bot's **Webhook (for calling)** URL to the externally reachable HTTPS
address that forwards to this listener, for example
`https://calls.example.com/api/calls`. Terminate TLS at a trusted reverse
proxy or host the listener behind an HTTPS gateway. Do not expose the plain
HTTP listener directly to the public internet.

Optional environment variables:

- `PHONEBOOK_PATH`: phonebook CSV path (default `phonebook.csv`).
- `CALLBACK_HOST`: bind host (default `0.0.0.0`).
- `CALLBACK_PORT`: bind port (default `3978`).
- `MICROSOFT_TENANT_ID`: additionally restrict callbacks to this tenant.

The callback bearer token is checked against Microsoft's published signing
keys, issuer, expiration, and the configured app ID audience before its JSON
body is processed. The callback responds with HTTP 204, as required by the
calling notification protocol.

Microsoft setup references:

- [Register a Teams calls and meetings bot](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/registering-calling-bot)
- [Configure incoming call notifications](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/call-notifications)
