"""Passive survey of the local band, and what to conclude from it.

Deliberately free of RNS and radio imports, exactly like tx_policy and relay:
everything here is arithmetic over observations, so it can be tested on a
desktop. rns_bridge owns the part that cannot be -- retuning the radio and
waiting for it to report.

WHAT A SURVEY CAN AND CANNOT SETTLE. It ranks channels against each other. It
does NOT produce a trustworthy absolute noise figure, and this module never
claims one: on 2026-08-14 the bench radio read -101 dBm, was power-cycled by a
USB knock, and came back reading -92 dBm at what may or may not have been a
different location. Whether that 9 dB was the room or the recalibration could
not be determined. A sweep is immune to that question precisely because every
channel in one pass is measured by the same radio in the same session, so a
calibration offset is common-mode and cancels out of the comparison. Rankings
survive it; absolute numbers do not, and are reported only as evidence.

WHY THE WORST SAMPLE RANKS, NOT THE MEAN. Interference is bursty. A channel
that is silent for nine passes and unusable on the tenth is not a good channel,
but its mean says it is. Ranking on the worst reading is what makes a
recommendation hold up on the day rather than on average, and it is the whole
reason multiple passes are worth the time.
"""

# Band edges. Receive-only sweeping is unrestricted, but the RECOMMENDATION has
# to land somewhere the radio may legally transmit, so the planner works in
# band limits rather than sweeping whatever the chip can tune.
BANDS = {
    "us915": (902000000, 928000000),
    "eu868": (863000000, 870000000),
}

# Below this, the quietest and loudest channels are not meaningfully different
# and the honest answer is "changing frequency will not help you". 6 dB is
# roughly a factor of two in range under free-space loss, and comfortably
# larger than the 1-2 dB of jitter the bench radio shows when nothing is
# happening -- so a difference this big is a real feature of the band rather
# than the measurement breathing.
MEANINGFUL_DB = 6.0

# A channel needs this many observations before it can be recommended. One
# reading is an anecdote, and recommending off an anecdote is how a survey
# sends you to the one channel that happened to be quiet for three seconds.
MIN_OBSERVATIONS = 2


def plan_channels(low_hz, high_hz, bandwidth_hz, step_hz=None):
    """Channel centres to visit, lowest first.

    A centre is only included if the whole channel fits inside the band --
    centre +/- half the bandwidth. Recommending a centre whose skirt hangs over
    the band edge would be recommending an illegal transmission, and the band
    edge is exactly where a naive range() lands you.

    Steps by one bandwidth by default, so the survey tiles the band without
    gaps and without measuring the same energy twice.
    """
    if bandwidth_hz <= 0:
        raise ValueError("bandwidth must be positive")
    if high_hz <= low_hz:
        raise ValueError("band must be non-empty")

    step = step_hz or bandwidth_hz
    if step <= 0:
        raise ValueError("step must be positive")

    half = bandwidth_hz // 2
    first = low_hz + half
    channels = []
    centre = first
    while centre + half <= high_hz:
        channels.append(centre)
        centre += step
    return channels


class Sweep(object):
    """Observations accumulated across one or more passes over the band."""

    def __init__(self, channels=(), bandwidth_hz=125000):
        self.bandwidth_hz = bandwidth_hz
        self.channels = list(channels)
        self.passes = 0
        # freq -> list of noise floor readings, one per pass
        self._floors = {}
        # freq -> list of channel load readings
        self._loads = {}

    def observe(self, freq_hz, noise_floor, load=None):
        """Record one reading. Ignores a missing floor rather than raising --
        the radio can legitimately have nothing to report yet, and a survey
        that aborts on the first such channel is worse than one with a gap."""
        if noise_floor is None:
            return
        self._floors.setdefault(freq_hz, []).append(float(noise_floor))
        if load is not None:
            self._loads.setdefault(freq_hz, []).append(float(load))

    def note_pass_complete(self):
        self.passes += 1

    def observed(self):
        """Frequencies with at least one reading, lowest first."""
        return sorted(self._floors)

    def worst_floor(self, freq_hz):
        """The loudest reading seen on this channel. See the module docstring
        for why this, and not the mean, is what ranks."""
        readings = self._floors.get(freq_hz)
        return max(readings) if readings else None

    def spread_db(self, freq_hz):
        """How much this channel varied between passes. Large means bursty,
        which is a reason to distrust a single quiet reading on it."""
        readings = self._floors.get(freq_hz)
        if not readings or len(readings) < 2:
            return None
        return max(readings) - min(readings)

    def mean_load(self, freq_hz):
        readings = self._loads.get(freq_hz)
        return sum(readings) / len(readings) if readings else None

    def ranked(self):
        """Channels quietest first, as (freq, worst_floor) -- only those with
        enough observations to be worth acting on."""
        usable = [
            (freq, self.worst_floor(freq))
            for freq in self._floors
            if len(self._floors[freq]) >= MIN_OBSERVATIONS
        ]
        # Frequency as the tie-break so the output is stable run to run rather
        # than reordering on dict iteration.
        usable.sort(key=lambda entry: (entry[1], entry[0]))
        return usable

    def recommendation(self, current_hz=None):
        """What to do, as a dict, or None if nothing can honestly be said.

        `verdict` is one of:
          "no_data"    -- not enough observations to rank anything
          "uniform"    -- the band is flat; changing frequency will not help
          "move"       -- there is a meaningfully quieter channel
          "stay"       -- the current channel is already among the quietest
        """
        ranked = self.ranked()
        if not ranked:
            return {"verdict": "no_data", "passes": self.passes}

        best_hz, best_floor = ranked[0]
        worst_floor = ranked[-1][1]
        span = worst_floor - best_floor

        result = {
            "passes": self.passes,
            "channels": len(ranked),
            "best_hz": best_hz,
            "best_floor": best_floor,
            "worst_floor": worst_floor,
            "span_db": span,
            "current_hz": current_hz,
            "current_floor": self.worst_floor(current_hz) if current_hz else None,
        }

        # Said before any winner is announced, because a flat band is the case
        # where naming a winner does active harm: it is noise the whole way
        # across, the "best" channel is a coin toss, and moving to it buys
        # nothing while costing a reconfiguration of every node.
        if span < MEANINGFUL_DB:
            result["verdict"] = "uniform"
            return result

        current_floor = result["current_floor"]
        if current_floor is not None and current_floor - best_floor < MEANINGFUL_DB:
            result["verdict"] = "stay"
        else:
            result["verdict"] = "move"
        return result


def format_recommendation(result, top=None):
    """The recommendation as log lines. Separate from recommendation() so the
    wording lives in one place and the decision stays testable without it."""
    if result is None or result["verdict"] == "no_data":
        return ["survey: not enough data to recommend anything"]

    mhz = lambda hz: "%.3f MHz" % (hz / 1000000.0)
    lines = ["survey: %d channels over %d pass(es), span %.0f dB "
             "(quietest %d dBm, loudest %d dBm)"
             % (result["channels"], result["passes"], result["span_db"],
                result["best_floor"], result["worst_floor"])]

    if result["verdict"] == "uniform":
        lines.append(
            "survey: the band is flat to within %.0f dB -- this is broadband "
            "noise, not a busy channel, so changing frequency will not help. "
            "Height, antenna or a relay will." % MEANINGFUL_DB)
        return lines

    if result["verdict"] == "stay":
        lines.append("survey: stay on %s -- nothing is meaningfully quieter"
                     % mhz(result["current_hz"]))
    else:
        gain = ("" if result["current_floor"] is None else
                ", %.0f dB quieter than %s" % (
                    result["current_floor"] - result["best_floor"],
                    mhz(result["current_hz"])))
        lines.append("survey: move to %s (%d dBm%s)"
                     % (mhz(result["best_hz"]), result["best_floor"], gain))

    for freq, floor in (top or []):
        lines.append("survey:   %s  %d dBm" % (mhz(freq), floor))
    return lines
