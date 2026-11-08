#!/system/bin/sh
# Magisk late-start script: brings up the AirPlay 2 receiver from the Debian 12 tree at boot.
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done

n=0
while ! ip -4 addr | grep 'inet ' | grep -qv '127.0.0.1'; do
  sleep 2
  n=$((n + 1))
  [ "$n" -gt 90 ] && break
done
sleep 5

sh /data/local/debian-mount.sh
env -i HOME=/root TERM=dumb LANG=C.UTF-8 \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  chroot /data/local/debian12 /root/start-airplay2.sh > /data/local/airplay2-boot.log 2>&1
