"""Regenerate COMPACT_ZDICT, the dictionary the compact path compresses against.

Run it as `python3 train_zdict.py > compact_zdict.py` from this directory.

WHY THIS EXISTS AS A SCRIPT

The dictionary is part of the wire format: zlib embeds its Adler-32 in the
stream header, so two builds with different dictionaries fail loudly at
decompress() rather than silently corrupting -- but they do fail, so changing it
means bumping PROTOCOL_VERSION. Keeping the generator in the repository is what
makes that a deliberate act with a reproducible result rather than a blob
somebody pasted in once.

WHAT IT IS TRAINED ON, AND WHY THAT MATTERS

Synthetic variations of the test fixtures: real ATAK shapes, with uids,
callsigns, coordinates, icon paths and message text randomised. That is
honest-but-optimistic. Real traffic is more varied than four templates, so the
compression ratios measured against this corpus are an upper bound.

**Retraining on captured frames is worth doing and costs a version bump.** The
mechanism is safe against a bad dictionary either way: encode() emits the
compressed form only when it is actually smaller, so a dictionary that suits
the traffic badly costs nothing beyond the attempt.
"""
from __future__ import print_function

import base64
import random
import sys
import textwrap

import cot_codec as c
from test_cot_codec import MARKER, GEOCHAT, DIRECT_CHAT, self_sa

DICT_BYTES = 16384
SEED = 3

CALLSIGNS = ["BRAVO01", "pixel", "OnePlus", "ALPHA2", "CHARLIE-6", "DELTA11",
             "ECHO4", "hq", "medic-1"]
TEXTS = ["moving to phase line bravo, eta 10 mikes", "ok", "roger that",
         "hold position, over", "contact front, 200m", "rally point alpha in 5",
         "need casevac at grid", "negative, say again", "on station",
         "returning to base", "eyes on target", "wilco"]
ICONS = ["COT_MAPPING_2525B/a-u/a-u-G", "COT_MAPPING_2525B/a-h/a-h-G",
         "COT_MAPPING_2525B/a-f/a-f-G", "COT_MAPPING_2525B/a-n/a-n-G",
         "COT_MAPPING_SPOTMAP/b-m-p-s-m/spotmap"]
MTYPES = [b"a-u-G", b"a-h-G", b"a-n-G", b"a-f-G", b"a-u-G-U-C", b"a-h-G-U-C"]


def build(seed=SEED, size=DICT_BYTES):
    rng = random.Random(seed)

    def uuid4():
        h = "%032x" % rng.getrandbits(128)
        return "-".join((h[:8], h[8:12], h[12:16], h[16:20], h[20:]))

    def android_uid():
        return ("ANDROID-%016x" % rng.getrandbits(64)).encode()

    frames = []

    # Positions as they are actually sent: one session per node, so the corpus
    # contains keyframes and deltas in the ratio the air will see.
    for _ in range(60):
        tx = c.SaTx()
        uid = ("ANDROID-%015d" % rng.getrandbits(48))
        callsign = rng.choice(CALLSIGNS)
        lat, lon = rng.uniform(-60, 60), rng.uniform(-170, 170)
        for minute in range(10):
            frames.append(c.encode(
                self_sa(minute, lat=lat + 0.000757 * minute, lon=lon,
                        callsign=callsign, uid=uid), 448, tx))

    for _ in range(300):
        raw = MARKER
        raw = raw.replace(b"0ef9c3a9-bf54-46e5-9655-29956a0d80ba", uuid4().encode())
        raw = raw.replace(b"ANDROID-0123456789abcdef", android_uid())
        raw = raw.replace(b"COT_MAPPING_2525B/a-u/a-u-G", rng.choice(ICONS).encode())
        raw = raw.replace(b'type="a-u-G"', b'type="%s"' % rng.choice(MTYPES))
        raw = raw.replace(b"pixel", rng.choice(CALLSIGNS).encode())
        raw = raw.replace(b"observed movement north", rng.choice(TEXTS).encode())
        raw = raw.replace(b"20.9089800", ("%.7f" % rng.uniform(-60, 60)).encode())
        raw = raw.replace(b"-140.5829100", ("%.7f" % rng.uniform(-170, 170)).encode())
        frames.append(c.encode(raw))

    for base in (GEOCHAT, DIRECT_CHAT):
        for _ in range(150):
            raw = base
            for old in (b"ANDROID-990000111111111", b"ANDROID-0123456789abcdef",
                        b"ANDROID-fedcba9876543210"):
                raw = raw.replace(old, android_uid())
            raw = raw.replace(b"7f3a2b19-1c4d-4e2a-9f88-2b7c1d0e5a63", uuid4().encode())
            raw = raw.replace(b"ee992581-4e37-4218-801a-e71feec6c397", uuid4().encode())
            raw = raw.replace(b"moving to phase line bravo, eta 10 mikes",
                              rng.choice(TEXTS).encode())
            raw = raw.replace(b"direct message test", rng.choice(TEXTS).encode())
            raw = raw.replace(b"BRAVO01", rng.choice(CALLSIGNS).encode())
            frames.append(c.encode(raw))

    # Shuffled so the tail -- which is all a zlib dictionary is -- carries every
    # frame kind rather than whichever happened to be generated last. Getting
    # this wrong is not subtle: an all-chat tail measured markers at 141 B
    # instead of 75.
    rng.shuffle(frames)
    return b"".join(frames)[-size:]


def main():
    raw = build()
    encoded = base64.b64encode(raw).decode("ascii")
    print('"""The compact path\'s compression dictionary. GENERATED -- see')
    print("train_zdict.py, which is what to edit and re-run.")
    print()
    print("Part of the wire format: zlib embeds its Adler-32 in the stream")
    print("header, so a build with a different dictionary fails loudly at")
    print("decompress() instead of corrupting quietly. Changing it therefore")
    print('needs a PROTOCOL_VERSION bump."""')
    print()
    print("import base64")
    print()
    print("_B64 = (")
    for line in textwrap.wrap(encoded, 72):
        print('    "%s"' % line)
    print(")")
    print()
    print("COMPACT_ZDICT = base64.b64decode(_B64)")
    print("assert len(COMPACT_ZDICT) == %d" % len(raw))
    print("[%d bytes]" % len(raw), file=sys.stderr)


if __name__ == "__main__":
    main()
