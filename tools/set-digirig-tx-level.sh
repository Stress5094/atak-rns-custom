#!/bin/sh
# Sets a Digirig-class (C-Media CM108/CM119) sound card's TX output level to
# the measured sweet spot -- see docs/open-questions.md, "FOUND IT: the
# laptop's own TX playback level..." (2026-09-20). Non-monotonic: too quiet
# (the OS default, ~38%) and too loud (100%, overdriven per Digirig's own
# "use ~80%, not 100%" guidance) both measurably hurt per-copy reliability
# on real hardware; 30/37 (~81%) was the best of three levels tested,
# confirmed across two independent 10-trial batches (87.5% combined).
#
# Called by 99-digirig.rules on every card-add event for this vendor, so
# the level survives reboots and cable swaps instead of depending on
# whatever the OS/ALSA last had it at. $1 is the kernel control device name
# udev passes via %k, e.g. "controlC6" -- the trailing digits are the ALSA
# card number amixer wants.
#
# One-time install (needs root once, same pattern as this file's sibling
# 99-digirig.rules):
#   sudo cp tools/set-digirig-tx-level.sh /usr/local/bin/
#   sudo cp tools/99-digirig.rules /etc/udev/rules.d/
#   sudo udevadm control --reload-rules && sudo udevadm trigger
# Then unplug and replug the cable -- udev only applies rules on connect.

card="${1#controlC}"
case "$card" in
    ''|*[!0-9]*) exit 0 ;;  # not a "controlC<N>" name -- nothing to do
esac

# By NAME, not numid -- numids are a per-device enumeration order, not a
# stable identity across every C-Media CM108/CM119 firmware/kernel-driver
# revision, even within "the same family". A name match fails safely
# (nothing happens) on a device that doesn't have this exact control,
# where a numid match could silently hit some unrelated control instead.
# "Speaker" (not "Speaker Playback Volume") is deliberate -- amixer's
# simple-mixer interface (`sset`) merges the volume/switch pair the raw
# numid interface exposes separately (numid=5/6, "Speaker Playback
# Switch"/"Speaker Playback Volume") under one simple name; confirmed
# against `amixer -c <card> scontrols` on the actual hardware before
# shipping this, not guessed.
amixer -c "$card" sset 'Speaker' 30,30 >/dev/null 2>&1 || true
