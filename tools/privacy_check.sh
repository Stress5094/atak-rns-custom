#!/usr/bin/env bash
# Pre-publication privacy check. Exits non-zero if anything that looks like
# real personal data is found in the files git would publish (tracked files
# plus untracked, non-ignored ones).
#
# This script holds only *shapes*, never the real values -- publishing a list
# of the real values would defeat it. Put exact strings to hunt for (a home
# coordinate prefix, a town name, a device serial), one extended regex per
# line, in tools/privacy_patterns.local. That file is gitignored.
# Hits reviewed and judged harmless go in tools/privacy_allow.txt.
#
# Deliberate synthetic fixtures are allowed through; do not "fix" them, tests
# depend on their exact values:
#   ANDROID-0123456789abcdef  ANDROID-fedcba9876543210
#   ANDROID-1111111111111111  ANDROID-2222222222222222
#   20.9089800,-140.5829100   35.1234567,-79.7654321 (and jittered 35.123x,-79.765x)
#   MACs beginning 02:00:00   (locally administered, made up)
set -u
cd "$(git rev-parse --show-toplevel)" || exit 2

files() { git ls-files -z --cached --others --exclude-standard; }
fail=0
check() {  # name, regex, allow-regex
    local hits
    hits=$(files | xargs -0 grep -IHnoE -e "$2" 2>/dev/null | grep -vE -e "${3:-^\$^}" | grep -vE '^tools/privacy_(check\.sh|allow\.txt):' | grep -vF -f <(grep -v '^#' tools/privacy_allow.txt | grep .))
    if [ -n "$hits" ]; then
        fail=1
        echo "== $1: $(echo "$hits" | wc -l) hit(s)"
        echo "$hits" | head -20
    fi
}

check "phone UID" 'ANDROID-[0-9a-f]{16}' \
    'ANDROID-(0123456789abcdef|fedcba9876543210|1111111111111111|2222222222222222)'
check "MAC address" '\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b' ':02:00:00:'
check "home path" '/home/[A-Za-z0-9_.-]+' '/home/user\b'
check "email" '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.(com|net|org|io|edu|gov|us|dev|me|co)\b' \
    'noreply|@example\.|@invalid\.test|git@github\.com'
check "adb wireless serial" 'adb-[0-9A-Za-z]{6,}-[0-9A-Za-z]{4,}'
check "mesh key fingerprint" 'mesh key `?[0-9a-f]{4}\b'
# Both halves at least 10 in magnitude, so audio levels and ratios ("0.25, 0.45")
# do not count. Allowed: the synthetic test area (20-21 N, 140-141 W, open
# Pacific) in code, tools and docs only, and range-edge test values.
# Published results carry no positions at all, so any pair there is a hit.
check "lat,lon pair" '-?[1-9][0-9]{1,2}\.[0-9]{4,} *, *-?[1-9][0-9]{1,2}\.[0-9]{4,}' \
    '^(app|tools|docs|server)/[^:]*:[0-9]+:(-?2[01]\.[0-9]+ *, *-?14[01]\.[0-9]+|-?14[01]\.[0-9]+ *, *-?2[01]\.[0-9]+)|:35\.12[0-9]* *, *-79\.76|:35\.22[0-9]* *, *-80\.84|:35\.0+ *, *-79\.0+|:-?85\.0511|:-?89\.9+ *, *-?179\.9+'

if [ -f tools/privacy_patterns.local ]; then
    while IFS= read -r p; do
        case "$p" in ''|'#'*) continue ;; esac
        check "local pattern" "$p"
    done < tools/privacy_patterns.local
else
    echo "note: tools/privacy_patterns.local not found -- exact real values not checked"
fi

[ "$fail" = 0 ] && echo "privacy check: clean"
exit "$fail"
