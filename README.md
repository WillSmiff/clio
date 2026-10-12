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

The NixOS version can also report each call to an
[Aurora](https://github.com/GEWIS/aurora) info screen, which shows the caller
full-screen while the phone rings and gives the screen back afterwards. See
[Showing the caller on an Aurora info screen](#showing-the-caller-on-an-aurora-info-screen).

## Windows
```powershell
python teams_desktop_monitor.py
```

The initial version of Clio. This uses Windows UI Automation to inspect accessible text in Teams windows;
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
by Teams, then the number. Teams' own generic labels ("Incoming call",
"Incoming audio call", "Incoming video call") are never treated as a caller
name. This mode needs only Python's standard library; `dbus-next` is required
only for the notification-bus mode above. Use a Teams for Linux version that
includes its incoming PSTN/call-queue detection fix (v2.24.0 or newer).

Teams for Linux captures child stdout/stderr, so command-mode events are also
appended to `~/.local/state/callerid/teams-calls.log` with private file
permissions. Follow the log with:

```sh
tail -f ~/.local/state/callerid/teams-calls.log
```

Set `CALL_MONITOR_LOG` to choose another log path. Caller names and numbers are
personal data; keep the log private and remove it when no longer needed.

## Showing the caller on an Aurora info screen

Clio can report each call to [Aurora](https://github.com/GEWIS/aurora), which
shows the caller full-screen on the narrowcasting screens and takes the screen
back when the call is answered, declined or cancelled. Clio sends the number;
Aurora resolves it against its own caller directory to pick the name and photo,
so a caller only has to be named once, in the backoffice.

This works in both NixOS modes (the command hook and the notification watcher), and needs only the standard library.

### Aurora side

Aurora's caller ID is off by default. In the backoffice:

1. Turn on `InfoScreen.CallerId` under **Server settings**.
2. Add the callers you want shown by name under **Info screen → Caller ID**.
   Use E.164 numbers, as in `phonebook.csv`.
3. Add the overlay to each screen under **Manage layout → Overlays → Incoming
   call (overlay)**.
4. Create an integration user with the `setInfoCallerRing` and
   `setInfoCallerEnd` scopes, and copy its API key.

The **Test call** and **Hang up** buttons next to the directory drive the
overlay without a real call, which is the quickest way to confirm the screen
side before involving Teams.

### Clio side

Two environment variables, read at startup:

| Variable | Required | Description |
| --- | --- | --- |
| `AURORA_URL` | yes | Aurora core's base URL, e.g. `https://aurora.gewis.nl` |
| `AURORA_API_KEY` | | The integration key, inline |
| `AURORA_API_KEY_FILE` | | Path to a file holding the key; preferred over the above |
| `AURORA_TIMEOUT_SECONDS` | | Request timeout, default `2.0` |

With neither the URL nor a key set, reporting is simply off and Clio behaves
exactly as before. Setting one but not the other logs a warning, so a typo does
not look like a silently dark screen.

Prefer `AURORA_API_KEY_FILE` on NixOS: anything in a Nix store path is
world-readable, so keep the key in a file managed by
[agenix](https://github.com/ryantm/agenix) or
[sops-nix](https://github.com/Mic92/sops-nix) and point at it by path. Clio
warns if the file is group- or world-readable; `chmod 600` it.

Because Teams for Linux may be launched from a desktop entry rather than a
shell, set these where Teams itself will see them rather than in `~/.bashrc`.
A home-manager fragment:

```nix
{
  # Both modes read these; the command hook inherits Teams' own environment.
  home.sessionVariables = {
    AURORA_URL = "https://aurora.gewis.nl";
    AURORA_API_KEY_FILE = "/run/user/1000/secrets/aurora-caller-key";
  };
}
```

Check it reaches the process with `tr '\0' '\n' < /proc/$(pgrep -f teams-for-linux | head -1)/environ | grep AURORA`.

### Behaviour and limits

- Reporting is best-effort. An unreachable or misconfigured Aurora is logged as
  a warning and never stops Clio from handling or logging the call.
- The overlay times out on Aurora's side after about 20 seconds, so a lost
  hang-up costs a few seconds of a stale name rather than a stuck screen.
- A hang-up is only reported for a call Clio actually announced, so a second
  call that is still ringing keeps its overlay.
- Clio cannot tell a pickup from a decline or a timeout, and does not need to:
  all three dismiss the overlay.
- A call with no number and no usable name is not reported at all, since there
  would be nothing to show.
- Warnings name the likely cause: a 409 means `InfoScreen.CallerId` is off, and
  a 401/403 means the key or its scopes are wrong.

### Tests

The Aurora path is covered by standard-library tests:

```sh
python3 -m unittest discover
```

## Teams Bot
An implementation of Clio for use as a registered Microsoft Teams calling bot.
Microsoft exposes incoming-call notifications to the bot when a call is addressed it. 

In order for Clio to work in this form, a Teams administrator must configure the
bot, enable calling, and publish the callback as a publicly reachable HTTPS
URL. This monitor does not answer, reject, or otherwise control calls.

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

## Phonebook

Edit `phonebook.csv` and keep its header as `phone,name`. Use E.164 numbers,
including the country code, to avoid ambiguous local-number matches:

```csv
phone,name
+12065550100,Avery Chen
+442079460123,Sam Taylor
```

Microsoft setup references:

- [Register a Teams calls and meetings bot](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/registering-calling-bot)
- [Configure incoming call notifications](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/call-notifications)
