#!/usr/bin/env bash
# Cross-compiles libcodec2 for Android (arm64-v8a) and drops it where the
# Gradle build already looks: app/src/main/jniLibs/arm64-v8a/libcodec2.so.
#
# Pinned to the same codec2 version (1.2.0) already installed on this desktop
# and bench-tested all night, via tools/fsk_modem.py -- a different version
# on the phone than what every over-the-air figure in this project was
# measured against would be its own untested variable.
#
# Needs the Android SDK's own cmake and NDK packages (not a system cmake):
#   sdkmanager --install "cmake;4.1.2" "ndk;28.2.13676358"
# Both installed under $ANDROID_HOME/{cmake,ndk}/<version>/ already if you
# built this project before; set ANDROID_HOME if it isn't at ~/android-sdk.

set -euo pipefail

CODEC2_VERSION="1.2.0"
NDK_VERSION="28.2.13676358"
CMAKE_VERSION="4.1.2"
ABI="arm64-v8a"
PLATFORM="android-26"   # matches minSdk in app/build.gradle.kts

ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
NDK="$ANDROID_HOME/ndk/$NDK_VERSION"
CMAKE="$ANDROID_HOME/cmake/$CMAKE_VERSION/bin/cmake"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ ! -x "$CMAKE" ]; then
    echo "cmake not found at $CMAKE -- run:" >&2
    echo "  sdkmanager --install \"cmake;$CMAKE_VERSION\"" >&2
    exit 1
fi
if [ ! -f "$NDK/build/cmake/android.toolchain.cmake" ]; then
    echo "NDK not found at $NDK -- run:" >&2
    echo "  sdkmanager --install \"ndk;$NDK_VERSION\"" >&2
    exit 1
fi

echo "cloning codec2 $CODEC2_VERSION..."
git clone --quiet --depth 1 --branch "$CODEC2_VERSION" \
    https://github.com/drowe67/codec2.git "$WORK/codec2"

echo "configuring for $ABI / $PLATFORM..."
"$CMAKE" -G "Unix Makefiles" \
    -S "$WORK/codec2" -B "$WORK/codec2/build-android" \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI="$ABI" \
    -DANDROID_PLATFORM="$PLATFORM" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=ON \
    >/dev/null

echo "building..."
make -C "$WORK/codec2/build-android" -j"$(nproc)" codec2 >/dev/null

DEST="$REPO_ROOT/app/src/main/jniLibs/$ABI"
mkdir -p "$DEST"
cp "$WORK/codec2/build-android/src/libcodec2.so" "$DEST/libcodec2.so"
echo "done: $DEST/libcodec2.so"
file "$DEST/libcodec2.so"
