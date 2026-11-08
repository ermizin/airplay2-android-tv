#!/bin/sh
# Builds and signs the now-playing app. Needs JDK 17 on PATH.
# ANDROID_JAR: path to android.jar of platform 30. BUILD_TOOLS: folder with aapt2, d8, zipalign, apksigner.
set -eu
: "${ANDROID_JAR:?set ANDROID_JAR to the path of android.jar}"
: "${BUILD_TOOLS:?set BUILD_TOOLS to the build-tools folder}"

cd "$(dirname "$0")"
rm -rf build
mkdir -p build/classes

javac -Xlint:-options -source 8 -target 8 -bootclasspath "$ANDROID_JAR" -classpath "$ANDROID_JAR" \
  -d build/classes src/local/airplay/nowplaying/Main.java
"$BUILD_TOOLS/d8" --release --lib "$ANDROID_JAR" --min-api 23 --output build build/classes/local/airplay/nowplaying/*.class
"$BUILD_TOOLS/aapt2" link -o build/base.apk --manifest AndroidManifest.xml -I "$ANDROID_JAR" \
  --min-sdk-version 23 --target-sdk-version 30 --version-code 1 --version-name 1.0
(cd build && zip -q -j base.apk classes.dex)
"$BUILD_TOOLS/zipalign" -f -p 4 build/base.apk build/aligned.apk

# Local signing key, kept out of git. Reuse it so that updates install over the old version.
KEY_PASS="${KEY_PASS:-android}"
if [ ! -f signing.jks ]; then
  keytool -genkeypair -keystore signing.jks -storepass "$KEY_PASS" -keypass "$KEY_PASS" -alias np \
    -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=local" >/dev/null 2>&1
fi
"$BUILD_TOOLS/apksigner" sign --ks signing.jks --ks-pass "pass:$KEY_PASS" --key-pass "pass:$KEY_PASS" \
  --out build/nowplaying.apk build/aligned.apk
"$BUILD_TOOLS/apksigner" verify build/nowplaying.apk
echo "Built android/build/nowplaying.apk"
