"""Tests for fm_burst: the layer that refuses to build an illegal transmission.

fm_burst composes fm_framing with an fm_modem and exists for one reason: the
duration ceiling has to be enforced at the single place a burst is built, not
left as a property the configuration constants have to be trusted to add up to
by hand. The previous transport checked it too and it caught real regressions.

This suite is deliberately light on the modem -- test_fm_modem owns that
contract. What it owns is the arithmetic above the modem (plan, goodput, the
ceiling) and the two pure byte<->sample conversions at the platform boundary.
One integration test ties the whole stack together: build() with the shipped
defaults must produce a burst the modem can decode.
"""

import unittest

import fm_burst
import fm_config
import fm_framing
import fm_modem

_HAVE_CODEC2 = fm_modem.available()
requires_codec2 = unittest.skipUnless(
    _HAVE_CODEC2, "libcodec2 is not installed")


# The operating point, at the sample rate the desktop bench measured it at.
# 8000 Hz is the phone's; both are legal for this placement, and the modem is
# constructed from whichever the profile carries.
def bench_profile():
    op = fm_config.OPERATING_POINT.profile
    return fm_config.Profile(
        name=op.name, first_tone_hz=op.first_tone_hz,
        tone_spacing_hz=op.tone_spacing_hz, tones=op.tones,
        symbol_rate_bd=op.symbol_rate_bd, chain=op.chain,
        sample_rate_hz=48000)


class LongerThanTheModel(fm_modem.Modem):
    """A modem that frames correctly but runs far longer than the arithmetic
    says. Stands in for a codec2 build whose framing changed, or a replacement
    waveform whose duration model was never written."""

    def __init__(self, config):
        self.frame_payload_bytes = config.frame_payload_bytes()
        self.sample_rate_hz = config.profile.sample_rate_hz
        self.modulated = False

    def burst_duration_s(self, frames, lead_in_s=0.0):
        return 99.0

    def modulate_burst(self, frames, lead_in_s=0.0, hop_lead_in=False):
        self.modulated = True
        return [0.0]


class TestPlan(unittest.TestCase):
    """What a burst WOULD cost, without building it -- the number the sweeps
    and the startup path use to choose a configuration.

    NO MODEM AND NO LIBRARY NOW. plan() takes a LinkConfig, which knows the
    code, the rate, the tone count, the repeats and the lead-in, and burst
    duration is a function of exactly those. That is the phase's point: the
    ceiling is checkable when a configuration is chosen, not only after a
    native modem has been built from it.
    """

    def test_capacity_agrees_with_fm_framing(self):
        # plan() must not carry its own idea of the length prefix. The two
        # disagreeing is an off-by-one that only shows up at exactly the
        # full-payload boundary -- which is where beacons sit, 29 B of content
        # in 29 B of capacity.
        cfg = fm_config.OPERATING_POINT
        plan = fm_burst.plan(cfg, payload_bytes=1)
        self.assertEqual(plan["capacity_bytes"],
                         fm_framing.capacity_for(cfg.frame_payload_bytes()))

    def test_goodput_is_payload_over_the_whole_transmission(self):
        # The honest throughput figure: real content, over the whole burst
        # including the lead-in and every redundant copy. This is the number
        # to quote; the raw symbol rate is how a 90 bps link gets called 500.
        plan = fm_burst.plan(fm_config.OPERATING_POINT, payload_bytes=29)
        self.assertAlmostEqual(plan["goodput_bps"], 29 * 8.0 / plan["burst_s"])

    def test_lead_in_fraction_is_surfaced(self):
        # Small at the operating point, and that is the finding: on the
        # previous transport it was most of a short burst and invisible.
        plan = fm_burst.plan(fm_config.OPERATING_POINT, payload_bytes=1)
        self.assertAlmostEqual(plan["lead_in_fraction"],
                               0.10 / plan["burst_s"])

    def test_the_operating_point_fits(self):
        plan = fm_burst.plan(fm_config.OPERATING_POINT, payload_bytes=29)
        self.assertTrue(plan["fits"])
        self.assertAlmostEqual(plan["burst_s"], 2.95, places=6)


class TestCeiling(unittest.TestCase):
    """TWO CHECKS, AND THEY ARE DIFFERENT CHECKS.

    fm_config.LinkConfig refuses an over-long burst at construction, from
    arithmetic -- so an illegal configuration cannot reach this module at all,
    and test_fm_config owns that. What build() owns is the case the arithmetic
    cannot see: a modem whose real framing disagrees with the model. A codec2
    build with a different unique word, a profile whose sample rate changed
    after the config was built, a replacement waveform whose duration model was
    never written.
    """

    @requires_codec2
    def test_a_modem_built_from_a_different_code_is_refused(self):
        """Any code that is not the configuration's own -- picked from the table
        rather than named, so this cannot silently become a no-op the way it did
        when the operating point moved onto the code the test had hard-coded."""
        cfg = fm_config.OPERATING_POINT
        other = next(c for c in fm_config.CODES if c != cfg.codename)
        with fm_modem.Codec2Fsk(bench_profile(), other) as m:
            with self.assertRaises(fm_burst.BurstTooLong) as caught:
                fm_burst.build(m, b"x", cfg)
        msg = str(caught.exception)
        self.assertIn("different code", msg)
        self.assertIn("%d B" % (fm_config.code_bytes(other)
                                - fm_config.CRC_BYTES), msg)

    def test_a_modem_longer_than_the_model_is_refused_before_modulating(self):
        """A modem that disagrees with the arithmetic, which is the ONLY case
        this check can catch -- a config long enough to trip the ceiling by
        arithmetic cannot be constructed, so reaching this needs a modem whose
        real framing differs from the model.

        That is not hypothetical. The duration model is three measured claims
        about codec2's framing (a 32-bit unique word, a 50-symbols-per-tone-pair
        preamble, no postamble), and a codec2 build that changed any of them
        would land exactly here. test_fm_modem is what checks they still hold;
        this is what happens when they do not.
        """
        cfg = fm_config.OPERATING_POINT
        modem = LongerThanTheModel(cfg)
        with self.assertRaises(fm_burst.BurstTooLong) as caught:
            fm_burst.build(modem, b"x", cfg)
        msg = str(caught.exception)
        # The message must point at repeats/code/lead-in, never at raising the
        # ceiling -- the ceiling is a measured property of the channel, not a
        # knob to turn to make a payload fit.
        self.assertIn("Reduce repeats", msg)
        self.assertIn("do NOT raise the ceiling", msg)
        # And it must report BOTH numbers, since a disagreement between them is
        # the thing this check exists to catch.
        self.assertIn("by the modem's own count", msg)
        self.assertIn("2.95 s", msg)
        self.assertFalse(modem.modulated,
                         "the check must come BEFORE modulation -- modulating "
                         "a 34 s burst to then refuse it wastes exactly the "
                         "time the check exists to protect")


class TestPcm16(unittest.TestCase):
    """The float<->PCM16 boundary to the platform's audio API. Pure bytes in,
    pure bytes out; no modem involved."""

    def test_round_trip_within_one_lsb(self):
        samples = [0.0, 0.5, -0.5, 0.999, -0.999, 0.25]
        back = fm_burst.from_pcm16(fm_burst.to_pcm16(samples))
        self.assertEqual(len(back), len(samples))
        for a, b in zip(samples, back):
            self.assertAlmostEqual(a, b, delta=1.0 / 32768.0)

    def test_clamping_not_wrapping(self):
        # An overshoot must clamp to +/-full-scale, not wrap to the opposite
        # sign: wrapping turns an overshoot into a loud discontinuity, which on
        # this link reads as a decode failure. int16 is asymmetric -- positive
        # full scale is +32767 (= 32767/32768 = 0.99997 as a float), negative
        # is -32768 (= -1.0 exactly) -- so the two sides clamp differently.
        out = fm_burst.from_pcm16(fm_burst.to_pcm16([1.5, -1.5]))
        self.assertAlmostEqual(out[0], 32767.0 / 32768.0, delta=1e-6)
        self.assertAlmostEqual(out[1], -32768.0 / 32768.0, delta=1e-6)

    def test_empty_round_trips_to_empty(self):
        self.assertEqual(fm_burst.to_pcm16([]), b"")
        self.assertEqual(fm_burst.from_pcm16(b""), [])

    def test_pcm16_is_little_endian_s16(self):
        # The platform boundary is a specific wire format; a mismatch here
        # produces garbage that decodes nowhere.
        raw = fm_burst.to_pcm16([1.0])
        self.assertEqual(len(raw), 2)
        self.assertEqual(raw, b"\xff\x7f")  # +32767 little-endian


@requires_codec2
class TestBuildDecodes(unittest.TestCase):
    """The whole foundation stack in one shot: build() at the shipped operating
    point must produce a burst a fresh modem can demodulate back to the exact
    payload."""

    def test_build_at_the_operating_point_decodes_byte_exact(self):
        payload = b"a foundation stack check"
        cfg = fm_config.OPERATING_POINT
        prof = bench_profile()
        with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
            samples = fm_burst.build(tx, payload, cfg)
            with fm_modem.Codec2Fsk(prof, cfg.codename) as rx:
                got = rx.demodulate(samples)
        recovered = {fm_framing.unpack(g, tx.frame_payload_bytes) for g in got}
        self.assertIn(payload, recovered)

    def test_build_repeats_the_frame_and_both_copies_decode(self):
        """BOUNDED RETRY, for the reason test_fm_modem's docstring gives: codec2
        can fail to decode the FIRST frame after the preamble about 0.4% of the
        time even on clean loopback, while the sync state machine settles. That
        is real modem behaviour and precisely why the transport never trusts a
        single copy -- but it makes an every-copy assertion flaky, so this
        mirrors the transport and retries rather than absorbing the miss
        silently.
        """
        payload = b"repeated copy"
        cfg = fm_config.link_config("fast", lead_in_s=0.0)
        prof = bench_profile()
        for _attempt in range(3):
            with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
                samples = fm_burst.build(tx, payload, cfg)
                with fm_modem.Codec2Fsk(prof, cfg.codename) as rx:
                    got = rx.demodulate(samples)
            recovered = [fm_framing.unpack(g, tx.frame_payload_bytes)
                         for g in got]
            if recovered.count(payload) == cfg.repeats:
                return
        self.fail("only %d of %d copies decoded on three attempts"
                  % (recovered.count(payload), cfg.repeats))

    def test_the_operating_point_round_trips_at_the_phones_sample_rate(self):
        """8000 Hz, which is what fm_config.FAST actually carries and what the
        phone will run -- the bench measured this placement at 48000.

        Worth its own test because the two rates are not interchangeable:
        codec2 requires Fs %% Rs == 0, so a symbol rate legal at one can be
        unconstructible at the other, and confusing the two is one of the three
        errors Phase 0 found in the record. 400 Bd divides both; 375 and 480
        divide only 48000.
        """
        cfg = fm_config.OPERATING_POINT
        self.assertEqual(cfg.profile.sample_rate_hz, 8000)
        payload = b"phone rate round trip"
        with fm_modem.Codec2Fsk(cfg.profile, cfg.codename) as tx:
            samples = fm_burst.build(tx, payload, cfg)
            with fm_modem.Codec2Fsk(cfg.profile, cfg.codename) as rx:
                got = rx.demodulate(samples)
        recovered = [fm_framing.unpack(g, tx.frame_payload_bytes) for g in got]
        self.assertIn(payload, recovered)
        self.assertAlmostEqual(len(samples) / 8000.0, 2.95, places=3)

    def test_a_scrambled_frame_survives_the_real_modem(self):
        """The scrambling change, end to end against libcodec2 rather than only
        as bytes. An all-zero payload is the case random padding could not
        cover: it is the shape that decoded ZERO frames on real radios, and it
        has to survive both the scrambler and the modem.
        """
        cfg = fm_config.link_config("fast", lead_in_s=0.0, repeats=1)
        payload = bytes(cfg.capacity_bytes())
        prof = bench_profile()
        with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
            frame = fm_framing.pack(payload, tx.frame_payload_bytes)
            # Whitened before the encoder ever sees it -- the IL2P ordering
            # rule, which is what keeps the LDPC decoder's error correction
            # intact.
            self.assertNotIn(b"\x00" * 2, frame)
            samples = fm_burst.build(tx, payload, cfg)
            with fm_modem.Codec2Fsk(prof, cfg.codename) as rx:
                got = rx.demodulate(samples)
        recovered = [fm_framing.unpack(g, tx.frame_payload_bytes) for g in got]
        self.assertIn(payload, recovered)

    def test_the_burst_is_the_length_the_config_promised(self):
        """The arithmetic against actual samples, which is the claim everything
        above fm_burst rests on."""
        cfg = fm_config.OPERATING_POINT
        prof = bench_profile()
        with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
            samples = fm_burst.build(tx, b"length check", cfg)
        self.assertAlmostEqual(len(samples) / float(prof.sample_rate_hz),
                               cfg.burst_duration_s(), places=3)


if __name__ == "__main__":
    unittest.main()


class TestVoxLeadInHops(unittest.TestCase):
    """2026-10-08, Pixel 7 + K1 PRO + Baofeng Mini: a STEADY lead-in tone was
    turned down ~1 s into the burst and the receiver lost the start of its
    preamble (0 of 3 decoded off the phone's raw recording); a lead-in hopping
    through the tones decoded 3 of 3. So VOX lead-ins hop and CM108's does not."""

    @staticmethod
    def _tone_energy(samples, fs, f):
        import math
        w = 2.0 * math.pi * f / fs
        c = 2.0 * math.cos(w)
        a1 = a2 = 0.0
        for v in samples:
            a1, a2 = v + c * a1 - a2, a1
        return math.sqrt(max(a1 * a1 + a2 * a2 - c * a1 * a2, 0.0)) / len(samples)

    def _lead(self, cfg):
        prof = bench_profile()
        with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
            samples = fm_burst.build(tx, b"hop", cfg)
        n = int(cfg.lead_in_s * prof.sample_rate_hz)
        return samples[:n], prof

    def test_a_vox_lead_in_contains_every_tone(self):
        cfg = fm_config.link_config("fast", keying=fm_config.KEYING_VOX,
                                    lead_in_s=fm_config.VOX_CABLE_LEAD_IN_S)
        lead, prof = self._lead(cfg)
        top = max(self._tone_energy(lead, prof.sample_rate_hz,
                                    prof.first_tone_hz + k * prof.tone_spacing_hz)
                  for k in range(prof.tones))
        for k in range(prof.tones):
            f = prof.first_tone_hz + k * prof.tone_spacing_hz
            self.assertGreater(self._tone_energy(lead, prof.sample_rate_hz, f),
                               0.3 * top, "tone %d Hz missing from a VOX lead-in" % f)

    def test_a_cm108_lead_in_stays_a_single_tone(self):
        cfg = fm_config.OPERATING_POINT
        lead, prof = self._lead(cfg)
        first = self._tone_energy(lead, prof.sample_rate_hz, prof.first_tone_hz)
        other = self._tone_energy(lead, prof.sample_rate_hz,
                                  prof.first_tone_hz + 2 * prof.tone_spacing_hz)
        self.assertGreater(first, 10 * other)

    def test_hopping_does_not_change_the_burst_length(self):
        cfg = fm_config.link_config("fast", keying=fm_config.KEYING_VOX,
                                    lead_in_s=fm_config.VOX_CABLE_LEAD_IN_S)
        prof = bench_profile()
        with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
            n = len(fm_burst.build(tx, b"len", cfg))
            self.assertAlmostEqual(n / prof.sample_rate_hz,
                                   tx.burst_duration_s(cfg.repeats, cfg.lead_in_s),
                                   delta=0.01)

    def test_a_hopping_burst_still_decodes_byte_exact(self):
        payload = b"vox lead-in hops"
        cfg = fm_config.link_config("fast", keying=fm_config.KEYING_VOX,
                                    lead_in_s=fm_config.VOX_CABLE_LEAD_IN_S)
        prof = bench_profile()
        for _attempt in range(3):
            with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
                samples = fm_burst.build(tx, payload, cfg)
                with fm_modem.Codec2Fsk(prof, cfg.codename) as rx:
                    got = rx.demodulate(samples)
            if payload in {fm_framing.unpack(g, tx.frame_payload_bytes) for g in got}:
                return
        self.fail("hopping-lead-in burst never decoded")
