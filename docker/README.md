# Clio in Docker

Runs Teams for Linux on a virtual display inside a container, with Clio as its
incoming-call command hook. Each incoming Teams call is reported to Aurora,
which shows it on the info screens. No desktop session is needed.

The container is one more signed-in Teams client: Teams rings every client of
the account, so calls are still answered on the usual phone or desktop. The
container only watches.

> **Untested.** This setup has not run against a real Teams account yet. Check
> the prerequisites below before relying on it.

## Prerequisites

- A Linux host with Docker Engine and Compose v2 (`docker compose`).
- The Microsoft account whose calls should be shown, and someone who can do its
  sign-in (including MFA) once.
- **Check with the tenant's IT admins first**: Conditional Access or device
  compliance rules may block, or frequently re-prompt, a sign-in from an
  unmanaged Linux machine.
- Aurora with the caller ID set up (see the main README, "Aurora side"): the
  `InfoScreen.CallerId` setting on, and an integration key with the
  `setInfoCallerRing` and `setInfoCallerEnd` scopes.

## Setup

All commands run from the `docker/` directory.

1. **Configure Aurora's address.**

   ```sh
   cp .env.example .env
   ```

   Set `AURORA_URL` in `.env` to the Aurora core as the container reaches it.
   For an Aurora running on the same host, use the host's LAN address, not
   `localhost`, which is the container itself.

2. **Store the secrets.** The container runs as uid 1000, which must be able to
   read them, and Clio warns about a key file others can read.

   ```sh
   mkdir -p secrets
   printf '%s' 'PASTE-THE-AURORA-INTEGRATION-KEY' > secrets/aurora_api_key
   printf '%s' 'CHOOSE-A-VNC-PASSWORD' > secrets/vnc_password
   sudo chown 1000:1000 secrets/*
   sudo chmod 600 secrets/*
   ```

   VNC only uses the first 8 characters of its password, which is why noVNC is
   published on localhost only (step 4).

3. **Build and start.**

   ```sh
   docker compose build
   docker compose up -d
   docker compose logs -f
   ```

   Wait until the log shows `noVNC is listening on port 6080`.

4. **Sign in to Teams, once.** Open <http://localhost:6080/vnc.html> on the
   host and enter the VNC password. From another machine, tunnel the port
   first:

   ```sh
   ssh -L 6080:localhost:6080 user@docker-host
   ```

   Sign in to the Teams account in the window shown, including MFA. The session
   is kept in the `teams-session` volume, so this survives restarts and image
   rebuilds.

5. **Test.** Call the account's number. Within a second or two:

   ```sh
   docker compose exec clio tail -f /var/log/clio/calls.log /var/log/clio/clio.log
   ```

   `calls.log` shows `Incoming call from ...` and `... stopped ringing ...`.
   `clio.log` shows Clio's own output, including any Aurora rejection with its
   likely cause: 409 means `InfoScreen.CallerId` is off, 401/403 a wrong key or
   missing scopes, 404 an Aurora without the caller endpoints.

## Running it

- `docker compose up -d` / `docker compose down`: start and stop. The restart
  policy brings the container back after a crash or reboot.
- `docker compose ps`: the health check reports whether Teams is running.
- Updating Teams for Linux: change `TEAMS_FOR_LINUX_VERSION` in the
  `Dockerfile` (2.24.0 or newer), then `docker compose build` and
  `docker compose up -d`.
- Local names on top of Aurora's directory: uncomment the `phonebook.csv` mount
  in `compose.yaml` and put a `phonebook.csv` next to it. A phonebook name is
  shown even when Aurora is set to hide unknown numbers.

## Keeping an eye on it

A signed-out container looks exactly like one nobody calls. Microsoft sessions
expire eventually, so check the noVNC view now and then, or after a quiet
period, and sign in again when Teams asks.

## Security

- The `teams-session` volume holds a live sign-in to the Teams account: treat
  it like a password, and remove it (`docker compose down -v`) when retiring
  the setup.
- The logs contain caller names and numbers; `docker compose down -v` removes
  them too.
- Keep noVNC on localhost; reach it through an SSH tunnel or VPN only.
- Chromium's own sandbox is off (`--no-sandbox`), because Docker's default
  seccomp profile does not allow it. The container is the isolation boundary.
