#!/bin/bash
# Runs inside the chroot. Restarts only shairport-sync so avahi and nqptp keep running.
NAME="эйрплей2 тв"
LOG=/root/logs
pkill -x shairport-sync 2>/dev/null
# Let avahi finish sending its "goodbye" first, or it can cancel the new announcement on other devices.
sleep 6
nohup shairport-sync -v -p 7010 -a "$NAME" -o alsa > /dev/null 2> "$LOG/shairport.log" &
sleep 3
printf 'shairport-sync: '; pgrep -x shairport-sync | tr '\n' ' '; echo
grep -E 'audio_backend_latency_offset|latency offset' "$LOG/shairport.log" | cut -c1-120
