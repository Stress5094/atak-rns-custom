"""What the Mesh tab draws: who is out there, on which radio, and how healthy.

The tab is a picture, and a picture says more than it knows unless something
decides in advance what each mark is allowed to claim. That is this module. The
Kotlin side only draws; every grade and every reason comes from here, for the
same reason fm_link.audio_plan() exists -- two copies of a rule drift, and a
diagram that disagrees with the node list next to it is worse than no diagram.

WHAT A LINE MEANS. One line per (peer, radio kind) this node has heard the peer
on. Kinds: "rnode" / "meshtastic" (the board in the LoRa slot when the frame
arrived -- they cannot hear each other, so the board names the radio), "fm",
"wifi". A line is DIRECT when that radio has carried a first-hand frame from the
peer; otherwise it is RELAYED, meaning only forwarded copies came in on it, and
it is drawn as "via mesh" because the relay that carried it is not known.

WHAT A GRADE MEANS. Three, decided on the line's own facts:

  good  -- heard within WEAK_AFTER_INTERVALS beacon intervals, signal fine
  weak  -- going quiet, or the signal is at or under SNR_WARN_DB
  gone  -- not heard within the peer table's connected window

THE THRESHOLDS ARE NOT MEASURED. SNR_WARN_DB/SNR_BAD_DB equal the node list's
(MainActivity.SNR_WARN/SNR_BAD) and a test holds them equal. Lowered from 10/3
to 0/-7 on 2026-10-14: 10/3 graded the desk Meshtastic link (SNR ~6) weak, and
the one real-distance reading is a beacon decoded at -11.25 dB on SF8 (the
2026-08-21 drive). One figure for every preset, though the floors differ
(SF8 ~-10 dB, Long Fast/SF11 ~-17.5). The quiet threshold is a first guess.
FM lines carry no signal reading at all -- fm_rx measures level per audio
chunk, not per frame -- so an FM grade is age only, and its reason says so
rather than implying a signal was judged.

PER-RADIO LOSS is passed through as peers.py reports it, which is None whenever
a peer has been heard on more than one radio (one sequence counter spans all the
sender's radios). It is shown, never graded: no loss threshold has evidence.

Deliberately free of RNS and Android imports, and of any clock: every age
arrives already computed. Same shape as fm_health.py.

IMPORTANT: the on-device interpreter is CPython 3.9 (Chaquopy). Keep this module
3.8-compatible anyway, like its siblings.
"""

# The node list's values, so the two views agree; see the module docstring for
# where they come from. TestKotlinAgrees holds them equal to MainActivity's.
SNR_WARN_DB = 0.0
SNR_BAD_DB = -7.0

# A peer beacons once per interval, so one interval of silence is ordinary.
# Half another interval is allowance for a beacon slot drawn late, which
# fm_link's slot grid and the 0-2 s back-off both do. A first guess.
WEAK_AFTER_INTERVALS = 1.5

GOOD, WEAK, GONE = "good", "weak", "gone"
_RANK = {GOOD: 0, WEAK: 1, GONE: 2}

# Every radio kind the tab knows how to draw: (kind, legend label, line tag).
# Kotlin's legend is held to this list by a test, so a kind added here and not
# there is a failure rather than a line drawn in a default style.
KINDS = (
    ("rnode", "RNode", "RN"),
    ("meshtastic", "Meshtastic", "MT"),
    ("fm", "FM", "FM"),
    ("wifi", "WiFi", "WiFi"),
)
_SIGNAL_KINDS = ("rnode", "meshtastic")


def grade_line(age_s, snr, kind, connected_s, beacon_interval_s):
    """(grade, reason) for one line. `age_s` is how long ago the line's own
    evidence arrived: the last DIRECT frame for a direct line, the last frame
    of any kind for a relayed one."""
    heard = "heard %s ago" % _ago(age_s)
    if age_s is None or age_s > connected_s:
        return GONE, "not %s" % (
            "heard in %s" % _ago(connected_s) if age_s is None
            else heard)
    quiet_after = beacon_interval_s * WEAK_AFTER_INTERVALS
    if age_s > quiet_after:
        return WEAK, "%s -- going quiet (expected every %s)" % (
            heard, _ago(beacon_interval_s))
    if kind in _SIGNAL_KINDS and snr is not None:
        if snr <= SNR_BAD_DB:
            return WEAK, "%s, SNR %.1f dB -- very weak signal" % (heard, snr)
        if snr <= SNR_WARN_DB:
            return WEAK, "%s, SNR %.1f dB -- weak signal" % (heard, snr)
        return GOOD, "%s, SNR %.1f dB" % (heard, snr)
    if kind == "fm":
        return GOOD, "%s (FM: no signal reading, judged on age only)" % heard
    return GOOD, heard


def build(me, peers, connected_s, beacon_interval_s, probable=None):
    """The whole picture as plain data.

    `me`: {"node_id", "name", "callsign", "radios": [kinds this node has]}.
    `peers`: peers.PeerTable.snapshot().
    `probable`: [(node_a, node_b), ...] remote pairs one of them claims to
    hear -- see the module docstring of relay for why a claim is all it is.
    """
    nodes, lines = [], []
    for peer in peers:
        node_id = peer["node_id"]
        peer_lines = []
        for row in peer.get("radios") or []:
            direct = row.get("direct_age_s") is not None
            age = row["direct_age_s"] if direct else row.get("age_s")
            snr = row.get("snr") if direct else None
            grade, reason = grade_line(age, snr, row.get("radio"),
                                       connected_s, beacon_interval_s)
            if not direct:
                reason = "via mesh, %s" % reason
            peer_lines.append({
                "node_id": node_id,
                "radio": row.get("radio"),
                "direct": direct,
                "grade": grade,
                "reason": reason,
                "age_s": age,
                "rssi": row.get("rssi") if direct else None,
                "snr": snr,
                "heard": row.get("heard", 0),
                "lost": row.get("lost"),
            })
        lines.extend(peer_lines)
        nodes.append({
            "node_id": node_id,
            "label": peer.get("name") or node_id,
            "callsign": peer.get("callsign") or "",
            "age_s": peer.get("age_s"),
            # Inner ring for a node heard first-hand on ANY radio within the
            # connected window; everything else, including a direct neighbour
            # that has gone silent, is placed by its last known distance.
            "ring": 1 if peer.get("direct") or (
                not peer.get("connected") and peer.get("hops_away", 1) <= 1
            ) else 2,
            "hops": peer.get("hops_away", 1),
            "grade": _best(l["grade"] for l in peer_lines) if peer_lines
                     else (GOOD if peer.get("connected") else GONE),
        })

    known = {n["node_id"] for n in nodes}
    pairs = []
    for a, b in probable or []:
        # Only between nodes already on the picture, once per pair.
        if a in known and b in known and a != b:
            pair = tuple(sorted((a, b)))
            if pair not in pairs:
                pairs.append(pair)

    me_id = me.get("node_id")
    return {
        "me": {
            "node_id": me_id,
            "label": me.get("name") or me_id or "",
            "callsign": me.get("callsign") or "",
            "radios": list(me.get("radios") or []),
        },
        "nodes": nodes,
        "lines": lines,
        "probable": [{"a": a, "b": b} for a, b in pairs],
        "kinds": [{"kind": k, "label": l, "tag": t} for k, l, t in KINDS],
    }


def _best(grades):
    return min(grades, key=lambda g: _RANK[g])


def _ago(seconds):
    if seconds is None:
        return "never"
    seconds = int(round(seconds))
    if seconds < 90:
        return "%d s" % seconds
    if seconds < 5400:
        return "%d min" % round(seconds / 60.0)
    return "%.1f h" % (seconds / 3600.0)
