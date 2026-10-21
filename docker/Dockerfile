# Teams for Linux on a virtual display, with Clio as its incoming-call command
# hook. Build from the repository root:
#   docker compose -f docker/compose.yaml build
FROM debian:bookworm-slim

# 2.24.0 or newer is required: it detects phone (PSTN) and call-queue calls,
# and reads the nested `incomingCalls` config keys used below.
ARG TEAMS_FOR_LINUX_VERSION=2.26.0
ARG CLIO_UID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    TZ=Europe/Amsterdam \
    PHONEBOOK_PATH=/opt/clio/phonebook.csv \
    CALL_MONITOR_LOG=/var/log/clio/calls.log

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl tzdata python3 procps \
        xvfb x11vnc novnc websockify dbus-x11 \
        fonts-noto-core fonts-noto-color-emoji \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL -o /tmp/teams-for-linux.deb \
        "https://github.com/IsmaelMartinez/teams-for-linux/releases/download/v${TEAMS_FOR_LINUX_VERSION}/teams-for-linux_${TEAMS_FOR_LINUX_VERSION}_${arch}.deb" \
    && apt-get install -y --no-install-recommends /tmp/teams-for-linux.deb \
    && rm -rf /tmp/teams-for-linux.deb /var/lib/apt/lists/*

RUN useradd --create-home --uid "${CLIO_UID}" --shell /usr/sbin/nologin clio \
    && mkdir -p /home/clio/.config/teams-for-linux /var/log/clio /opt/clio \
    && chown -R clio:clio /home/clio /var/log/clio

COPY teams_nixos_monitor.py aurora_client.py phonebook.py /opt/clio/
COPY docker/clio-hook.sh docker/entrypoint.sh /opt/clio/
COPY docker/teams-config.json /etc/teams-for-linux/config.json

# An empty phonebook: Aurora's caller directory names the callers. Mount a
# phonebook.csv over this file to add local names on top.
RUN printf 'phone,name\n' > /opt/clio/phonebook.csv

USER clio
WORKDIR /home/clio
EXPOSE 6080

HEALTHCHECK --interval=1m --timeout=5s --start-period=2m \
    CMD pgrep -f teams-for-linux > /dev/null || exit 1

ENTRYPOINT ["sh", "/opt/clio/entrypoint.sh"]
