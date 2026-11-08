#!/bin/bash
# Runs inside the Debian chroot. Starts dbus, avahi, nqptp and shairport-sync.
# Audio goes to stdout -> /dev/null for now: this stage only tests discovery and AirPlay 2 support.
NAME="эйрплей2 тв"
LOG=/root/logs
mkdir -p "$LOG" /run/dbus /run/avahi-daemon

for p in shairport-sync nqptp avahi-daemon dbus-daemon; do pkill -x "$p" 2>/dev/null; done
sleep 1
rm -f /run/dbus/pid /run/avahi-daemon/pid

dbus-uuidgen --ensure
dbus-daemon --system --fork

sed -i 's/^#\?host-name=.*/host-name=tox3/' /etc/avahi/avahi-daemon.conf
nohup avahi-daemon --no-rlimits --no-chroot --no-drop-root > "$LOG/avahi.log" 2>&1 &
sleep 2

nohup nqptp > "$LOG/nqptp.log" 2>&1 &
sleep 1

nohup shairport-sync -v -p 7010 -a "$NAME" -o alsa > /dev/null 2> "$LOG/shairport.log" &
sleep 4

/root/start-nowplaying.sh

for p in dbus-daemon avahi-daemon nqptp shairport-sync; do
  printf '%s: ' "$p"; pgrep -x "$p" | tr '\n' ' '; echo
done
echo "--- avahi"; tail -6 "$LOG/avahi.log"
echo "--- nqptp"; tail -6 "$LOG/nqptp.log"
echo "--- shairport"; tail -15 "$LOG/shairport.log"
