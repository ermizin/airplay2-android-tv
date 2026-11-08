#!/system/bin/sh
# Keeps the AirPlay 2 receiver running while Android sleeps, wakes Android when it starts playing,
# and shows the now-playing screen when nothing else is on the TV.
# Android still sleeps normally and turns the TV off over HDMI-CEC; only kernel suspend is blocked.
# This script never puts the box to sleep. To turn it off: delete this file and reboot.

PCM=/proc/asound/card0/pcm4p/sub0/status
PIDFILE=/dev/airplay2-wake.pid
LOG=/data/local/airplay2-wake.log
APP=local.airplay.nowplaying
LAUNCHER=com.google.android.tvlauncher
LINGER=30

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  exit 0
fi
echo $$ > "$PIDFILE"

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done

: > "$LOG"
log() { echo "$(date '+%d.%m %H:%M:%S') $1" >> "$LOG"; }

echo airplay2 > /sys/power/wake_lock
log "started, wake lock held"

wakefulness() { dumpsys power | grep -m1 'mWakefulness=' | sed 's/.*=//'; }
focus() { dumpsys window | grep -m1 'mCurrentFocus='; }
now() { cut -d. -f1 /proc/uptime; }

show_screen() {
  am start -n "$APP/.Main" >/dev/null 2>&1
  shown=1
  log "screen opened"
}

playing=0
shown=0
last_play=0

# Music that was already playing when this script started is not a new start of music.
if grep -q RUNNING "$PCM" 2>/dev/null; then
  playing=1
  last_play=$(now)
fi
if focus | grep -q "$APP"; then
  shown=1
fi

while true; do
  if grep -q RUNNING "$PCM" 2>/dev/null; then
    last_play=$(now)
    if [ "$playing" = "0" ]; then
      playing=1
      state=$(wakefulness)
      log "music started, android is $state"
      case "$state" in
        Asleep|Dozing|Dreaming)
          input keyevent KEYCODE_WAKEUP
          log "woke android"
          sleep 2
          show_screen
          ;;
        *)
          if focus | grep -q "$LAUNCHER"; then
            show_screen
          else
            log "something else is on screen, leaving it"
          fi
          ;;
      esac
    fi
  else
    playing=0
    if [ "$shown" = "1" ] && [ $(( $(now) - last_play )) -ge "$LINGER" ]; then
      am force-stop "$APP"
      shown=0
      log "screen closed"
    fi
  fi
  sleep 1
done
