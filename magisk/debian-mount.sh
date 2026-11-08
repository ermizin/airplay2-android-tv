#!/system/bin/sh
# Prepare the Debian tree: safe to run repeatedly. Must run as root in the global mount namespace.
R=/data/local/debian12

is_mounted() { grep -q " $1 " /proc/mounts; }

if ! is_mounted "$R"; then
  mount --bind "$R" "$R"
  mount -o remount,bind,suid,dev,exec "$R"
fi
is_mounted "$R/proc"    || mount -t proc proc "$R/proc"
is_mounted "$R/sys"     || mount -t sysfs sysfs "$R/sys"
is_mounted "$R/dev"     || mount --bind /dev "$R/dev"
is_mounted "$R/dev/pts" || mount --bind /dev/pts "$R/dev/pts"
mkdir -p /dev/shm
is_mounted "$R/dev/shm" || mount -t tmpfs -o mode=1777 tmpfs "$R/dev/shm"
is_mounted "$R/run"     || mount -t tmpfs -o mode=755 tmpfs "$R/run"
mkdir -p "$R/run/dbus" "$R/run/avahi-daemon" "$R/run/shairport-sync"
mkdir -p "$R/tmp"
chmod 1777 "$R/tmp"
