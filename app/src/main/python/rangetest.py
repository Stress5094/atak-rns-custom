"""Range test recording: link quality against distance, as a CSV you can plot.

Modelled on Meshtastic's Range Test module, which writes one row per received
packet carrying both ends' positions and the signal stats, so the result can be
mapped rather than read. The same idea fits here with one difference worth
stating: Meshtastic's module transmits its own numbered test packets, and this
does not need to. The mesh beacon already goes out on a fixed interval carrying
a sequence number, which is exactly what a range test sender is -- so this
records what is already on the air and adds nothing to it.

WHERE THE POSITIONS COME FROM. A book of last-known fixes, joined against:
our own from every self-SA ATAK hands the bridge (sent on air or not), the
peer's from its position reports -- and, since 2026-11-08, from the fix a
range-testing peer puts on every beacon (cot_codec.encode_beacon's `fix`).
That last one was refused here originally because it cost bytes in every
beacon forever; it is now carried only while the sender runs a test. The
2026-11-08 drive is why it was needed: on air, the self-SA is rate-limited to
about one a minute, so at 30 km/h the far end's copy was 300-400 m stale and
4 rows of 25 were usable.

The join is the part that needs care, and it is why every row carries the AGE
of both fixes. A position that arrived 90 seconds ago is up to 100 m stale at
walking pace, which is the same order as the distances being measured. A row
whose fixes are old is not wrong so much as unusable, and the only honest thing
is to say so per row rather than silently average it into the result.
"""

import collections

# Kept in the same order as the header, and the header is what a spreadsheet
# or a plotting script binds to -- so this tuple IS the file format.
COLUMNS = (
    "time",             # ISO-8601 UTC, so it sorts as text
    "peer",             # human name, for reading
    "peer_id",          # node id, for grouping
    "link",             # which radio this frame arrived on -- see note_frame's
                         # own docstring for why this exists and what a blank
                         # one alongside blank rssi_dbm/snr_db/noise_floor_dbm
                         # means (FM, which has no signal metering at all)
    "seq",              # the sender's frame sequence -- gaps are lost frames
    "rssi_dbm",
    "snr_db",
    "noise_floor_dbm",
    "margin_db",        # rssi - noise floor, the "how close to the edge" figure
    "rx_lat", "rx_lon", "rx_fix_age_s",
    "tx_lat", "tx_lon", "tx_fix_age_s",
    "distance_m",
    "fix_error_m",      # how far the two ends could have moved since their fixes
    "usable",           # distance trustworthy: see _uncertainty_m
)

# WHAT MAKES A FIX TOO OLD IS SPEED, NOT AGE.
#
# This was a flat 60-second cutoff, and the 2026-08-21 drive showed why that is
# the wrong test: it threw away 26 of 29 rows on the STATIONARY node, whose
# every fix was perfectly correct and merely old. A parked node's position does
# not rot. Meanwhile a node doing 30 mph is 800 m adrift after the same 60
# seconds, and 59 seconds would have passed the old test.
#
# So the question asked here is "how far could this node have moved since we
# last saw it" -- its recent speed times the age of its fix. A stationary
# reference station stays usable for as long as it stays still, which is what a
# range test wants, and a moving node is rejected as soon as its uncertainty
# grows to matter.
#
# 50 m because that is comfortably inside the distances a range test is trying
# to resolve, and roughly twice the movement threshold that triggers a position
# report in the first place (tx_policy.DEFAULT_MIN_DISTANCE_M).
DEFAULT_MAX_FIX_ERROR_M = 50.0

# ...OR this fraction of the distance, whichever is larger (2026-11-08).
#
# A flat 50 m is a walking-pace rule. In a car at 10 m/s, five seconds of fix
# age is already 50 m -- and the 2026-11-08 drive showed the bridge's own fix
# is 3-7 s old at the best of times, a beacon fix older again by its burst and
# its wait for a clear channel. So a flat 50 m would refuse nearly every row
# of a drive, including the ones 2 km out where 50 m is noise. What a range
# test needs is the distance to within a tenth of itself, which is finer than
# the link's own scatter at any one distance has ever been. Walking-scale rows
# are unchanged: below 500 m the 50 m floor is the larger and still decides.
# fix_error_m is in every row, so a stricter rule can still be applied later.
DEFAULT_MAX_FIX_ERROR_FRAC = 0.10

# Consumer GPS wanders by a few metres while sitting perfectly still, and over
# a short gap between fixes that wander divides into a real-looking speed: 3 m
# of jitter 5 s apart reads as 0.6 m/s, which over a quarter of an hour would
# condemn a parked node to 500 m of imaginary uncertainty. So movement below
# the jitter floor is not movement. 10 m is the top of the ~5-10 m consumer
# range, and well under the 25 m that tx_policy calls a real move.
_GPS_JITTER_M = 10.0

# Until a node has been seen twice there is no speed to reason about, and the
# start of every run is exactly that case. Rather than discard it -- which is
# the failure note_frame's own docstring warns against -- fall back to the flat
# age rule this replaced. It is the weaker test, used only where the better one
# has nothing to work with.
DEFAULT_PROVISIONAL_AGE_S = 60.0

# A backstop for the case speed cannot cover: a node that sat still, went
# somewhere, and came back would look stationary from two identical fixes taken
# an hour apart. Nothing this old is evidence of anything.
DEFAULT_MAX_FIX_AGE_S = 900.0

# A report's own timestamp can run ahead of the receiver's wall clock by the two
# phones' clock disagreement. Within this, it is treated as "now"; beyond it the
# stamp is not believed at all and the arrival time is used instead, because a
# phone with a wildly wrong clock would otherwise make every fix look fresh.
_CLOCK_SKEW_ALLOWANCE_S = 30.0

# `speed` is metres per second measured from the PREVIOUS fix of the same node,
# or None when there is no previous fix to measure against.
#
# `at` is when the fix was TAKEN, on the injected (monotonic) clock -- not when
# it arrived. `stamp` is the report's own wall-clock time, or None when the
# caller had none; it is what tells a resent or reordered report from a new one.
Fix = collections.namedtuple("Fix", "lat lon at speed stamp")


def _valid(lat, lon):
    """Null Island is what a device with no fix reports, and it is a real
    coordinate 5000 km from anywhere -- so it has to be rejected explicitly or
    it becomes the most distant contact of every test."""
    if lat is None or lon is None:
        return False
    if abs(lat) < 1e-7 and abs(lon) < 1e-7:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _uncertainty_m(rx, rx_age, tx, tx_age):
    """How far the two ends could have moved since their fixes were taken.

    None when either end has never been located, or when a node's speed is not
    yet known -- one fix is a position, but it takes two to know whether that
    position is going anywhere. Treating an unmeasured speed as zero would let
    the very first fix of a moving node look authoritative.
    """
    if rx is None or tx is None or rx_age is None or tx_age is None:
        return None
    if rx.speed is None or tx.speed is None:
        return None
    return rx.speed * rx_age + tx.speed * tx_age


class RangeTest(object):
    """A book of last-known positions, and a row per frame heard."""

    def __init__(self, distance_fn, clock,
                 max_fix_error_m=DEFAULT_MAX_FIX_ERROR_M,
                 max_fix_error_frac=DEFAULT_MAX_FIX_ERROR_FRAC,
                 provisional_age_s=DEFAULT_PROVISIONAL_AGE_S,
                 max_fix_age_s=DEFAULT_MAX_FIX_AGE_S,
                 max_nodes=64, wall_clock=None):
        # Injected rather than imported so this module stays free of the rest:
        # distance_fn is tx_policy.haversine_m in the bridge and a stub here.
        self.distance_fn = distance_fn
        self.clock = clock
        # time.time in the bridge. Needed only to turn a report's wall-clock
        # stamp into an age; without it, stamps still order reports but every
        # fix is aged from its arrival, as before.
        self.wall_clock = wall_clock
        self.max_fix_error_m = max_fix_error_m
        self.max_fix_error_frac = max_fix_error_frac
        self.provisional_age_s = provisional_age_s
        self.max_fix_age_s = max_fix_age_s
        self.max_nodes = max_nodes
        self._fixes = collections.OrderedDict()
        self.rows = 0
        self.skipped_no_fix = 0
        # Reports refused as no newer than the fix already held -- the
        # resends that put the walker back at its start on 2026-11-07.
        # Counted so the summary shows the guard working, not just silent.
        self.stale_refused = 0

    def note_position(self, node_id, lat, lon, stamp=None):
        """Record where a node was. Called for our own self-SA and for every
        position report received from a peer -- the same book serves both,
        because the join does not care which side of the link a fix came from.

        `stamp` is the report's own time (CoT `time`, epoch seconds). Pass it
        whenever there is one. The 2026-11-07 FM walk is why: the walking
        phone re-sent its start-up keyframe blind, three times over two
        minutes, while it was already 70 m away. Booked by arrival time, each
        copy put the walker back at its starting point with a fresh fix, and
        a later resend of an unchanged position measured a speed of zero --
        so the stationary phone wrote rows saying 1 m, usable, while the walker
        was 143-170 m off. A report no newer than the one already held is now
        refused, and a fix is aged from when it was taken.
        """
        if not _valid(lat, lon):
            return False
        lat, lon, now = float(lat), float(lon), self.clock()

        previous = self._fixes.get(node_id)
        if (stamp is not None and previous is not None
                and previous.stamp is not None and stamp <= previous.stamp):
            # A resend, a duplicate, or a report overtaken in the queue. It
            # says nothing newer about where the node is, and booking it would
            # both move the node backwards and reset its age.
            self.stale_refused += 1
            return False

        taken = now
        if stamp is not None and self.wall_clock is not None:
            lag = self.wall_clock() - stamp
            if -_CLOCK_SKEW_ALLOWANCE_S <= lag <= self.max_fix_age_s:
                taken = now - max(0.0, lag)
            # Otherwise the stamp is not believable as a time, though it is
            # still kept below for ordering, which needs only the sender's own
            # clock to agree with itself.

        # Speed from the previous fix of this node, which is what decides how
        # fast its position goes stale. Measured rather than assumed: a node
        # cannot tell us it is parked, but two fixes in the same place can.
        speed = None
        if previous is not None:
            elapsed = taken - previous.at
            if elapsed > 0:
                try:
                    moved = self.distance_fn(previous.lat, previous.lon, lat, lon)
                    speed = max(0.0, moved - _GPS_JITTER_M) / elapsed
                except Exception:
                    speed = None

        self._fixes[node_id] = Fix(lat, lon, taken, speed, stamp)
        self._fixes.move_to_end(node_id)
        while len(self._fixes) > self.max_nodes:
            self._fixes.popitem(last=False)
        return True

    def fix_for(self, node_id):
        return self._fixes.get(node_id)

    def note_frame(self, local_id, peer_id, peer_name, seq, rssi, snr,
                   noise_floor, link=None):
        """One heard frame -> one row, or None if there is nothing to say.

        Returns a dict keyed by COLUMNS. Written even when a position is
        missing, because the signal reading is the measurement and the position
        is the annotation -- a range test that discarded readings taken before
        the first GPS lock would throw away the start of every walk.

        `link` records which of this node's own radios the frame arrived on
        (e.g. "lora"/"fm"/"wifi") -- added because a range test is only ever
        run on one link at a time in practice (LoRa or FM, not both), but the
        column still says so explicitly rather than leaving a future reader to
        infer it from rssi_dbm being blank. Optional and defaults to None
        (written as an empty field) for callers that don't know or don't care.
        """
        now = self.clock()
        rx = self._fixes.get(local_id)
        tx = self._fixes.get(peer_id)

        rx_age = None if rx is None else now - rx.at
        tx_age = None if tx is None else now - tx.at

        distance = None
        if rx is not None and tx is not None:
            try:
                distance = self.distance_fn(rx.lat, rx.lon, tx.lat, tx.lon)
            except Exception:
                distance = None

        error = _uncertainty_m(rx, rx_age, tx, tx_age)
        if error is not None:
            # See DEFAULT_MAX_FIX_ERROR_FRAC. The distance is the one measured
            # from these same fixes, so it is itself off by up to `error` -- an
            # error large enough to matter makes the fraction moot anyway.
            allowed = self.max_fix_error_m
            if distance is not None:
                allowed = max(allowed, self.max_fix_error_frac * distance)
            trustworthy = error <= allowed
        else:
            # No speed measured for one end or the other. See
            # DEFAULT_PROVISIONAL_AGE_S.
            trustworthy = (rx_age is not None and tx_age is not None
                           and rx_age <= self.provisional_age_s
                           and tx_age <= self.provisional_age_s)
        usable = (distance is not None
                  and trustworthy
                  and rx_age is not None and rx_age <= self.max_fix_age_s
                  and tx_age is not None and tx_age <= self.max_fix_age_s)
        if distance is None:
            self.skipped_no_fix += 1

        margin = None
        if rssi is not None and noise_floor is not None:
            margin = rssi - noise_floor

        self.rows += 1
        return {
            "peer": peer_name or "",
            "peer_id": peer_id or "",
            "link": link,
            "seq": seq,
            "rssi_dbm": rssi,
            "snr_db": snr,
            "noise_floor_dbm": noise_floor,
            "margin_db": margin,
            "rx_lat": None if rx is None else rx.lat,
            "rx_lon": None if rx is None else rx.lon,
            "rx_fix_age_s": None if rx_age is None else round(rx_age, 1),
            "tx_lat": None if tx is None else tx.lat,
            "tx_lon": None if tx is None else tx.lon,
            "tx_fix_age_s": None if tx_age is None else round(tx_age, 1),
            "distance_m": None if distance is None else round(distance, 1),
            "fix_error_m": None if error is None else round(error, 1),
            "usable": "yes" if usable else "no",
        }

    def summary_line(self):
        return ("range test: %d row(s), %d without a distance, %d stale "
                "report(s) ignored"
                % (self.rows, self.skipped_no_fix, self.stale_refused))


def header_line():
    return ",".join(COLUMNS)


def format_row(row, timestamp):
    """A row as one CSV line.

    Coordinates keep 6 decimal places -- about 0.1 m, far finer than any fix
    this will ever be given, and short enough that a long test stays a file you
    can open. Missing values are empty rather than 0, since a 0 in a latitude
    column is a place.
    """
    out = [timestamp]
    for column in COLUMNS[1:]:
        value = row.get(column)
        if value is None:
            out.append("")
        elif column in ("rx_lat", "rx_lon", "tx_lat", "tx_lon"):
            out.append("%.6f" % value)
        elif isinstance(value, float):
            out.append("%.2f" % value)
        else:
            # Names are the only free text here, and a callsign with a comma
            # would otherwise shift every column after it.
            text = str(value)
            out.append('"%s"' % text.replace('"', '""')
                       if ("," in text or '"' in text) else text)
    return ",".join(out)
