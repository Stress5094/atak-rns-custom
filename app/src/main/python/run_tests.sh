#!/usr/bin/env bash
# Run the desktop test suite WITH the AEAD backend.
#
# Why this exists: mesh.py needs `cryptography` for AES-CCM, and without it 47
# tests -- every one covering authentication, forgery rejection and nonce
# uniqueness -- skip rather than fail. `unittest` then prints
#
#     OK (skipped=47)
#
# which reads as success. That was the state of this repo for two milestones
# before anyone noticed. The venv below is cheap, so there is no reason to run
# the suite the weak way.
#
# WHERE THE VENV LIVES, and why it is not next to this script. Everything under
# app/src/main/python is declared as a Chaquopy source directory, so anything
# left here is packaged into the APK and shipped to the phone. A venv here put
# 97 MB of desktop-built `cryptography` into every build -- silently, from the
# moment someone first ran this script. The repo root is outside that sweep.
# (build.gradle.kts also excludes it, so a venv created here by an IDE cannot
# reintroduce the problem.)
#
# Usage:  ./run_tests.sh [unittest args...]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../../../.." && pwd)"
venv="${RTAK_TEST_VENV:-$repo/.venv}"

if [ ! -x "$venv/bin/python" ]; then
    echo "creating test venv at $venv" >&2
    if command -v uv >/dev/null 2>&1; then
        uv venv "$venv" >&2
        uv pip install --python "$venv/bin/python" cryptography >&2
    else
        python3 -m venv "$venv" >&2
        "$venv/bin/pip" install --quiet --upgrade pip cryptography >&2
    fi
fi

"$venv/bin/python" -c 'import cryptography' 2>/dev/null || {
    echo "ERROR: $venv has no cryptography; delete it and re-run" >&2
    exit 1
}

# test_mt_port drives the REAL meshtastic package (the protocol client is the
# point of that module, so a stub would test nothing). The version is the one
# app/build.gradle.kts pins for the phone; keep the two in step. Installed
# lazily so an existing venv from before this module picks it up.
MESHTASTIC_PIN="meshtastic==2.7.11"
"$venv/bin/python" -c 'import meshtastic' 2>/dev/null || {
    echo "installing $MESHTASTIC_PIN into $venv" >&2
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$venv/bin/python" "$MESHTASTIC_PIN" >&2
    else
        "$venv/bin/pip" install --quiet "$MESHTASTIC_PIN" >&2
    fi
}

cd "$here"
"$venv/bin/python" -m unittest discover -p 'test_*.py' "$@" 2>&1 | tee /tmp/rtak-tests.$$ >&2
status=${PIPESTATUS[0]}

# A skip here means the run was weaker than it looks -- say so loudly rather
# than let "OK (skipped=N)" pass for a clean sweep.
if grep -q "skipped=" /tmp/rtak-tests.$$; then
    echo >&2
    echo "WARNING: tests were SKIPPED -- this run did not cover everything." >&2
    status=1
fi
rm -f /tmp/rtak-tests.$$
exit "$status"
