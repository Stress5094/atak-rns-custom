#!/usr/bin/env bash
# Build the settings server's copy: the Python files it needs (the same ones
# the app ships, so the server and the phones run one implementation), this
# directory's README and systemd unit, in one tarball.
#
# Usage:  server/package.sh [OUT_DIR]      (default: server/dist)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$here/../app/src/main/python"
out="${1:-$here/dist}"
name="rtak-settings-server"
stage="$out/$name"

files="$(cd "$py" && python3 -c 'import settings_server; print(" ".join(settings_server.SERVER_FILES))')"
rm -rf "$stage"
mkdir -p "$stage"
for f in $files; do
    cp "$py/$f" "$stage/"
done
cp "$here/README.md" "$here/rtak-settings.service" "$stage/"
tar -C "$out" -czf "$out/$name.tar.gz" "$name"
echo "$out/$name.tar.gz"
