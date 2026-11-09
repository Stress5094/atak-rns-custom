"""Synthetic traffic for measuring what this mesh costs.

Shared deliberately between the laptop node (`tools/mesh_node.py`) and the app
itself, so a bench run on the phones and a bench run on a laptop are offering
the SAME thing. Two copies of a generator drift, and the first sign of the
drift is a preset comparison that disagrees with the previous one for reasons
nobody can name.

The load is a room chat because that is the traffic acknowledgement applies to
-- positions are fire-and-forget and self-healing, so a flood of them measures
the radio rather than the mesh. See docs/routing.md.
"""

import random
import time

# Real message words rather than filler bytes. The compact path compresses
# against a dictionary trained on real ATAK traffic, and a run of repeated
# characters would shrink to almost nothing and understate every frame size in
# the test -- an instrument that flatters the thing it measures.
WORDS = (
    "moving to phase line bravo eta ten mikes copy roger contact north ridge "
    "holding position request resupply ammo water casualty evac grid "
    "checkpoint alpha charlie delta sitrep negative affirmative standby"
).split()


def body(seq, body_bytes):
    """Message text of roughly body_bytes, deterministic for a given seq.

    Deterministic so two runs at different presets offer byte-identical
    traffic: a preset comparison where the payloads differ is comparing two
    things at once.
    """
    rng = random.Random(seq)
    words, length = [], 0
    while length < max(1, body_bytes):
        word = rng.choice(WORDS)
        words.append(word)
        length += len(word) + 1
    return " ".join(words)[:max(1, body_bytes)]


def geochat(uid, callsign, seq, body_bytes, now=None):
    """A room chat shaped like the ones ATAK emits, so it takes the compact
    path rather than the zlib fallback -- measuring the fallback would measure
    a path real traffic does not use."""
    now = time.time() if now is None else now
    text = body(seq, body_bytes)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".000Z"
    stale = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 60)) + ".000Z"
    guid = "%08x-1c4d-4e2a-9f88-%012x" % (seq & 0xffffffff, seq & 0xffffffffffff)
    return (
        '<event version="2.0" uid="GeoChat.%s.All Chat Rooms.%s" type="b-t-f" '
        'time="%s" start="%s" stale="%s" how="h-g-i-g-o">'
        '<point lat="35.1234567" lon="-79.7654321" hae="123.45" ce="9.5" '
        'le="9999999.0"/>'
        '<detail><__chat parent="RootContactGroup" groupOwner="false" '
        'messageId="%s" chatroom="All Chat Rooms" id="All Chat Rooms" '
        'senderCallsign="%s">'
        '<chatgrp uid0="%s" uid1="All Chat Rooms" id="All Chat Rooms"/>'
        '</__chat><link uid="%s" type="a-f-G-U-C" relation="p-p"/>'
        '<remarks source="BAO.F.ATAK.%s" to="All Chat Rooms" time="%s">%s'
        '</remarks>'
        '</detail></event>'
        % (uid, guid, stamp, stamp, stale, guid, callsign, uid, uid, uid,
           stamp, text)
    ).encode("utf-8")


def interval_s(rate_per_min):
    """Seconds between messages. 0 or less means "do not send"."""
    if rate_per_min <= 0:
        return 0.0
    return 60.0 / float(rate_per_min)


def jittered(interval, rng=random):
    """Spread the offered load, for the reason every other periodic sender
    here is jittered: two nodes on one fixed interval collide forever, which
    the 2026-08-15 bench measured directly."""
    return interval * rng.uniform(0.75, 1.25)
