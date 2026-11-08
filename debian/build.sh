#!/bin/bash
# Runs inside the Debian chroot. Installs build deps, builds nqptp and shairport-sync.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# No init system here: stop package scripts from trying to start services.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d

echo "=== apt install"
apt-get install -y --no-install-recommends \
  build-essential git autoconf automake libtool pkg-config xxd ca-certificates \
  libpopt-dev libconfig-dev libasound2-dev alsa-utils \
  avahi-daemon libavahi-client-dev dbus \
  libssl-dev libsoxr-dev libplist-dev libplist-utils libsodium-dev \
  libavutil-dev libavcodec-dev libavformat-dev libswresample-dev \
  uuid-dev libgcrypt20-dev libglib2.0-dev avahi-utils strace

# Android only lets members of these groups open network sockets.
getent group aid_inet >/dev/null || groupadd -g 3003 aid_inet
getent group aid_net_raw >/dev/null || groupadd -g 3004 aid_net_raw
usermod -aG aid_inet,aid_net_raw avahi
usermod -aG aid_inet,aid_net_raw messagebus

mkdir -p /root/src
cd /root/src

echo "=== nqptp"
rm -rf nqptp
git clone --depth 1 --branch 1.2.9 https://github.com/mikebrady/nqptp.git
cd nqptp
autoreconf -fi
./configure
make -j4
make install
cd ..

echo "=== shairport-sync"
rm -rf shairport-sync
git clone --depth 1 --branch 5.6.2 https://github.com/mikebrady/shairport-sync.git
cd shairport-sync
autoreconf -fi
echo "--- configure options"
./configure --help | grep -iE 'airplay|alsa|pipe|stdout|soxr|avahi|ssl|ffmpeg' || true
./configure --sysconfdir=/etc --with-alsa --with-pipe --with-stdout --with-soxr --with-avahi \
  --with-ssl=openssl --with-metadata --with-airplay-2
make -j4
make install

echo "=== versions"
command -v nqptp
nqptp -V || true
shairport-sync -V
