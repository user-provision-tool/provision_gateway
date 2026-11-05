#!/bin/sh
# The cross resolver's supervisor.
#
# dnsmasq does NOT re-read its configuration on SIGHUP (SIGHUP clears the cache
# and re-reads /etc/hosts and resolv.conf only), so a new `address=` line needs
# a process restart. The gateway owns the file and rewrites it atomically on a
# grant change; this loop notices the mtime move and restarts dnsmasq. That is
# the reload mechanism the ruling leaves to the implementer.
set -u

CONF=/etc/dnsmasq.d/cross.conf
DEFAULT=/etc/dnsmasq-default.conf
PID=""

start_dnsmasq() {
    conf="$DEFAULT"
    [ -f "$CONF" ] && conf="$CONF"
    dnsmasq --no-daemon --conf-file="$conf" --log-facility=- &
    PID=$!
    echo "[cross-resolver] dnsmasq started (pid $PID, conf $conf)" >&2
}

stop_dnsmasq() {
    [ -n "$PID" ] || return 0
    kill -TERM "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
    PID=""
}

trap 'stop_dnsmasq; exit 0' TERM INT

last=""
start_dnsmasq
last=$(stat -c %Y "$CONF" 2>/dev/null || echo missing)
last_exists=0
[ -f "$CONF" ] && last_exists=1

while true; do
    sleep 1
    exists=0
    [ -f "$CONF" ] && exists=1
    current=$(stat -c %Y "$CONF" 2>/dev/null || echo missing)
    if [ "$current" != "$last" ] || [ "$exists" != "$last_exists" ]; then
        last="$current"
        last_exists="$exists"
        echo "[cross-resolver] config changed; restarting dnsmasq" >&2
        stop_dnsmasq
        start_dnsmasq
    fi
    # If dnsmasq died on its own (bad config), restart it with the last good one.
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "[cross-resolver] dnsmasq exited; restarting" >&2
        sleep 1
        start_dnsmasq
    fi
done
