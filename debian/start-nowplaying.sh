#!/bin/bash
# Runs inside the chroot. Starts the now-playing page service.
mkdir -p /root/logs
pkill -f '^python3 /root/nowplaying' 2>/dev/null
pkill -x arecord 2>/dev/null
sleep 1
nohup python3 /root/nowplaying.py > /root/logs/nowplaying.log 2>&1 &
