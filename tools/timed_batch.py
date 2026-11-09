"""Run 10 otair_test.py trials at random points across the next hour.

One-off driver for a time-of-day/RF-conditions question: does reliability
vary depending on when the test runs, not just how many trials pile up back
to back. Each result is logged immediately, with a wall-clock timestamp, so
partial progress survives if this gets interrupted.
"""

import datetime
import os
import random
import subprocess
import sys
import time

N = 10
WINDOW_S = 3600

# ALSA card IDS, not indexes and not PipeWire node names (2026-09-13, when
# otair_test.py stopped going through PipeWire by default -- see its own
# backend comment for why).
#
# `hw:CARD=<id>,DEV=0` is the stable spelling: a card's INDEX moves whenever
# another USB audio device is plugged or unplugged, and a stale index does
# not fail loudly -- it silently addresses whatever card landed there (the
# dock, a webcam) and records a silent file that looks exactly like a radio
# that never transmitted. The ids come from `arecord -l`/`aplay -l`; both K1
# PRO cables enumerate as "USB Audio", which ALSA disambiguates as Audio and
# Audio_1 in plug order.
#
# WHICH CARD HOLDS WHICH RADIO IS NOT KNOWABLE FROM SOFTWARE -- the two are
# identical devices. Check it against the physical bench before trusting a
# direction-labelled result, and override per run if they are the other way
# round.
PLAY_DEV = os.environ.get("RTAK_PLAY_DEV", "hw:CARD=Audio,DEV=0")
REC_DEV = os.environ.get("RTAK_REC_DEV", "hw:CARD=Audio_1,DEV=0")


def main():
    start = time.monotonic()
    offsets = sorted(random.uniform(0, WINDOW_S) for _ in range(N))
    with open("timed_batch_schedule.log", "w") as f:
        for i, off in enumerate(offsets, 1):
            f.write("%d: +%.0fs\n" % (i, off))

    for i, off in enumerate(offsets, 1):
        wait = off - (time.monotonic() - start)
        if wait > 0:
            time.sleep(wait)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        rec_out = "timed_%d.wav" % i
        log_path = "timed_%d.log" % i
        with open(log_path, "w") as logf:
            logf.write("started %s (t+%.0fs planned)\n" % (now, off))
            logf.flush()
            subprocess.run(
                ["python3", "otair_test.py", "--run", "--trials", "1", "--vox",
                 "--play-dev", PLAY_DEV, "--rec-dev", REC_DEV,
                 "--rec-out", rec_out],
                stdout=logf, stderr=subprocess.STDOUT)
        print("trial %d done at %s (planned t+%.0fs)" % (i, now, off))
        sys.stdout.flush()

    print("TIMED_BATCH_DONE")


if __name__ == "__main__":
    main()
