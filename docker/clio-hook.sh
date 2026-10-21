#!/bin/sh
# The incoming-call command Teams for Linux runs. Teams discards the command's
# output, so it goes to a log file instead: that is where Aurora warnings (a
# wrong key, the feature switched off) end up. `exec` keeps the process id, so
# the SIGTERM Teams sends when the call stops ringing reaches Clio itself.
exec python3 /opt/clio/teams_nixos_monitor.py --incoming-call "$@" \
    >> /var/log/clio/clio.log 2>&1
