#!/bin/sh
# Starts the virtual display, the password-protected noVNC view of it, and
# Teams for Linux. Teams runs in the foreground: when it exits, so does the
# container, and the restart policy brings it back.
set -eu

Xvfb "$DISPLAY" -screen 0 1280x900x24 -nolisten tcp &

# Wait for the display socket before starting anything that draws on it.
display_socket="/tmp/.X11-unix/X${DISPLAY#:}"
for _ in $(seq 1 50); do
    [ -S "$display_socket" ] && break
    sleep 0.1
done

if [ -s "${VNC_PASSWORD_FILE:-}" ]; then
    # Bound to localhost: only websockify, in this container, connects to it.
    x11vnc -display "$DISPLAY" -localhost -rfbport 5900 -forever -shared \
        -passwdfile "$VNC_PASSWORD_FILE" -quiet -bg -o /var/log/clio/x11vnc.log
    websockify --web /usr/share/novnc 6080 localhost:5900 \
        >> /var/log/clio/websockify.log 2>&1 &
    echo "noVNC is listening on port 6080 (open /vnc.html)"
else
    echo "VNC_PASSWORD_FILE is not set or empty: the noVNC view is disabled" >&2
fi

# Chromium's sandbox needs user namespaces, which Docker's default seccomp
# profile does not grant. The container is the isolation boundary instead.
exec dbus-run-session -- teams-for-linux --no-sandbox
