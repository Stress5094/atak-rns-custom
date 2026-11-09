"""That the FM link's two halves agree with each other and with the layers
below them.

What is actually at risk here, and what each group of tests is for:

  * THE MTU. `hw_mtu()` being asked rather than assumed is what stops a beacon
    larger than the whole code sailing past RNS and only failing at the modem,
    which took the link offline on its first ever send. Tested as arithmetic,
    with no libcodec2 anywhere near it, because that is the property that lets
    startup ask the question at all.
  * THE SOCKET FRAMING. Kotlin implements the other half of it by hand, so the
    cases that matter are a frame split across reads and several frames in one.
  * THE OPERATING POINT BEING SINGLE. Transmit and receive reading one config
    is the only thing standing between a profile change and a receiver that
    hears nothing with nothing in the logs to say why.
  * THE WHOLE PATH, against the real libcodec2 at the PHONE's 8000 Hz rather
    than the bench's 48000 -- `build_tx_burst` to `feed_audio` and back,
    byte-exact, which is the one test that would catch the two halves having
    drifted into different formats.
"""

import json
import math
import os
import re
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()

import airtime
import fm_carrier
import fm_config
import fm_framing
import fm_modem
import fm_link

_HAVE_CODEC2 = fm_modem.available()
requires_codec2 = unittest.skipUnless(
    _HAVE_CODEC2, "libcodec2 is not installed -- these tests need the real "
                  "library, because a mock would only test the wrapper")


class _Owner(object):
    """Stands in for RNS.Transport, which is all FmInterface wants from one."""

    def __init__(self):
        self.inbound_calls = []

    def inbound(self, data, iface=None):
        self.inbound_calls.append(data)


def _iface(case, deliver=None, reconnect=None):
    """An FmInterface on one end of a socket pair, with the other end returned
    so a test can be the Kotlin side.

    Both ends are closed on cleanup, including the one the interface swapped in
    after a reconnect -- a reader thread blocked in recv() on a socket nobody
    closed outlives the test and reports as a ResourceWarning somewhere else
    entirely.
    """
    ours, theirs = socket.socketpair()
    owner = _Owner()
    iface = fm_link.FmInterface(owner, ours, "", deliver_frame=deliver,
                                reconnect=reconnect)
    case.addCleanup(lambda: _close(ours, theirs, iface.sock))
    return iface, theirs, owner


def _close(*socks):
    for s in socks:
        try:
            s.close()
        except Exception:
            pass


def _wait(predicate, timeout_s=1.0):
    """Spin until `predicate` or the timeout -- the read/reconnect loops are
    real threads, so every assertion about them is inherently a wait."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class TestSocketFraming(unittest.TestCase):
    """The loopback framing, which exists only because a TCP stream has no
    message boundaries -- nothing here goes on the air."""

    def test_a_frame_round_trips(self):
        p = fm_link.FrameParser()
        self.assertEqual(p.feed(fm_link.pack_frame(b"hello")), [b"hello"])

    def test_several_frames_in_one_read(self):
        p = fm_link.FrameParser()
        chunk = (fm_link.pack_frame(b"one") + fm_link.pack_frame(b"two")
                 + fm_link.pack_frame(b"three"))
        self.assertEqual(p.feed(chunk), [b"one", b"two", b"three"])

    def test_a_frame_split_across_reads(self):
        """The case the framing exists for: one process_outgoing() has no
        relationship to how many bytes arrive in one recv()."""
        p = fm_link.FrameParser()
        whole = fm_link.pack_frame(b"a frame in pieces")
        got = []
        for i in range(0, len(whole), 3):
            got.extend(p.feed(whole[i:i + 3]))
        self.assertEqual(got, [b"a frame in pieces"])

    def test_a_partial_frame_yields_nothing_yet(self):
        p = fm_link.FrameParser()
        whole = fm_link.pack_frame(b"incomplete")
        self.assertEqual(p.feed(whole[:-1]), [])
        self.assertEqual(p.feed(whole[-1:]), [b"incomplete"])

    def test_an_empty_frame_is_a_frame(self):
        p = fm_link.FrameParser()
        self.assertEqual(p.feed(fm_link.pack_frame(b"")), [b""])

    def test_a_frame_too_large_for_the_prefix_is_refused(self):
        with self.assertRaises(ValueError):
            fm_link.pack_frame(b"x" * 0x10000)

    def test_trailing_bytes_of_the_next_frame_are_kept(self):
        p = fm_link.FrameParser()
        chunk = fm_link.pack_frame(b"first") + fm_link.pack_frame(b"second")[:2]
        self.assertEqual(p.feed(chunk), [b"first"])
        self.assertEqual(p.feed(fm_link.pack_frame(b"second")[2:]), [b"second"])


class TestTheMtuIsAskedNotAssumed(unittest.TestCase):
    """The regression that took the link down on its first live send."""

    def test_it_is_the_configs_own_capacity(self):
        self.assertEqual(fm_link.hw_mtu(),
                         fm_link.config().capacity_bytes())

    def test_it_needs_no_native_library(self):
        """Pure arithmetic, so startup can ask before any modem exists -- which
        is the whole reason _compute_max_packet() can ask at all."""
        self.assertIsInstance(fm_link.hw_mtu(), int)
        self.assertGreater(fm_link.hw_mtu(), 0)

    def test_it_is_well_below_plain_mdu(self):
        """RNS.Packet.PLAIN_MDU is LoRa's ~464 B and describes a different
        interface entirely. Sizing FM against it is what let a 42 B beacon past
        a 30 B code with no complaint from RNS and only fail at the modem.

        A QUARTER rather than "nowhere near": the operating point moved to the
        128 B code on 2026-10-02, so this is 125 B against 464. The gap being
        smaller does not make asking instead of assuming any less necessary --
        it is still 3.7x, and a frame between the two sizes is exactly the one
        that would sail past RNS unnoticed.
        """
        import RNS
        self.assertLess(fm_link.hw_mtu(), RNS.Packet.PLAIN_MDU // 3)

    def test_it_agrees_with_the_framing_layers_arithmetic(self):
        cfg = fm_link.config()
        self.assertEqual(
            fm_link.hw_mtu(),
            fm_framing.capacity_for(cfg.frame_payload_bytes()))

    def test_the_interface_publishes_it(self):
        iface, theirs, _ = _iface(self)
        self.assertEqual(iface.HW_MTU, fm_link.hw_mtu())


def fm_carrier_hold():
    import fm_carrier
    return fm_carrier.HOLD_S


class TestTheOperatingPointIsSingleAndShared(unittest.TestCase):
    """Two ends that disagree recover nothing, and the link cannot report it."""

    def tearDown(self):
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)

    def test_the_default_is_the_measured_operating_point(self):
        self.assertIs(fm_link.config().profile,
                      fm_config.OPERATING_POINT.profile)
        self.assertEqual(fm_link.config().codename,
                         fm_config.OPERATING_POINT.codename)
        self.assertEqual(fm_link.config().repeats,
                         fm_config.OPERATING_POINT.repeats)

    def test_a_known_profile_takes_effect(self):
        name, substituted = fm_link.set_profile("narrow")
        self.assertEqual(name, "narrow")
        self.assertFalse(substituted)
        self.assertEqual(fm_link.profile_name(), "narrow")
        self.assertFalse(fm_link.profile_is_default())

    def test_an_unknown_profile_substitutes_and_says_so(self):
        """It arrives from SharedPreferences, so a stale value must leave a
        working link rather than take startup down -- but silently substituting
        it would be a link that works with nobody the operator expected."""
        name, substituted = fm_link.set_profile("a profile from last month")
        self.assertTrue(substituted)
        self.assertEqual(name, fm_config.DEFAULT_PROFILE)

    def test_an_empty_name_is_the_default_not_an_error(self):
        name, substituted = fm_link.set_profile("")
        self.assertEqual(name, fm_config.DEFAULT_PROFILE)
        self.assertFalse(substituted)

    def test_a_vox_cable_gets_the_long_lead_in_and_vox_keying(self):
        """K1 PRO class: no PTT pin, the audio keys the radio. Measured
        2026-10-06 the Mini starts 0.34-0.69 s after the audio, so the 0.10 s
        CM108 lead-in loses the whole front of the burst (0/3 decoded)."""
        fm_link.set_profile(fm_config.DEFAULT_PROFILE, vox=True)
        cfg = fm_link.config()
        self.assertEqual(cfg.keying, fm_config.KEYING_VOX)
        self.assertEqual(cfg.lead_in_s, fm_config.VOX_CABLE_LEAD_IN_S)
        self.assertGreater(fm_config.VOX_CABLE_LEAD_IN_S, 0.69)
        self.assertEqual(fm_config.VOX_CABLE_LEAD_IN_S, 2.2)

    def test_a_vox_cable_receives_with_the_k1pro_deemphasis(self):
        fm_link.set_profile(fm_config.DEFAULT_PROFILE, vox=True)
        self.assertEqual(fm_link.config().deemphasis_us(),
                         fm_config.VOX_CABLE_DEEMPHASIS_US)
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)
        self.assertEqual(fm_link.config().deemphasis_us(),
                         fm_config.OPERATING_POINT.deemphasis_us())

    def test_the_digirig_path_is_untouched_by_the_vox_option(self):
        """vox=False after vox=True must restore CM108 keying and 0.10 s --
        the toggle exists so nobody on a Digirig Lite has to think about it."""
        fm_link.set_profile(fm_config.DEFAULT_PROFILE, vox=True)
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)
        cfg = fm_link.config()
        self.assertEqual(cfg.keying, fm_config.KEYING_CM108)
        self.assertEqual(cfg.lead_in_s,
                         fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_CM108])
        self.assertEqual(cfg.burst_duration_s(),
                         fm_config.OPERATING_POINT.burst_duration_s())

    def test_vox_adds_exactly_the_extra_lead_in_to_the_burst(self):
        base = fm_config.OPERATING_POINT.burst_duration_s()
        fm_link.set_profile(fm_config.DEFAULT_PROFILE, vox=True)
        extra = (fm_config.VOX_CABLE_LEAD_IN_S
                 - fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_CM108])
        self.assertAlmostEqual(fm_link.config().burst_duration_s(),
                               base + extra, places=6)

    def test_every_tone_profile_still_fits_the_ceiling_under_vox(self):
        """The longest-burst placements (narrow, wide) are the ones a longer
        lead-in could push over MAX_BURST_S; LinkConfig refuses at build."""
        for name in fm_config.PROFILES:
            fm_link.set_profile(name, vox=True)
            self.assertLessEqual(fm_link.config().burst_duration_s(),
                                 fm_config.MAX_BURST_S, name)

    def test_the_audio_plan_carries_the_inter_burst_gap(self):
        """The service spaces its own back-to-back bursts by this much plus a
        random 0..backoff. It must exceed the carrier detector's hold, or a
        polite peer that has just seen our burst end is still holding busy when
        our next one starts and never gets a turn (2026-10-08, ten-minute run)."""
        import json
        plan = json.loads(fm_link.audio_plan())
        self.assertEqual(plan["inter_burst_gap_s"], fm_link.INTER_BURST_GAP_S)
        self.assertGreater(plan["inter_burst_gap_s"], fm_carrier_hold())

    def test_the_audio_plan_tells_kotlin_not_to_key_ptt_under_vox(self):
        """Kotlin claims the CM108 GPIO only when keying == "cm108". Under vox
        it must not wait key-up before playing (the audio IS the keying)."""
        import json
        fm_link.set_profile(fm_config.DEFAULT_PROFILE, vox=True)
        plan = json.loads(fm_link.audio_plan())
        self.assertEqual(plan["keying"], "vox")
        self.assertEqual(plan["ptt_keyup_s"], 0.0)
        self.assertEqual(plan["lead_in_s"], fm_config.VOX_CABLE_LEAD_IN_S)

    def test_changing_profile_drops_the_receive_path(self):
        """The modem holds its tones from construction. Without this the
        receiver stays on the old placement and simply stops hearing."""
        sentinel = object()
        fm_link._RX[0] = sentinel
        fm_link.set_profile("narrow")
        self.assertIsNone(fm_link._RX[0])

    def test_the_summary_line_names_everything_that_must_match(self):
        line = fm_link.summary_line()
        cfg = fm_link.config()
        self.assertIn(cfg.codename, line)
        self.assertIn(str(cfg.profile.symbol_rate_bd), line)
        self.assertIn(str(cfg.profile.first_tone_hz), line)
        self.assertIn("both ends must match", line)

    def test_status_lines_carry_the_placements_evidence(self):
        """An operator on handhelds running the desktop-measured default is
        choosing an unqualified placement, and this is where they find out."""
        lines = fm_link.status_lines()
        self.assertTrue(any(fm_link.evidence_line() == l for l in lines))

    def test_status_lines_work_before_any_audio_has_arrived(self):
        fm_link.reset_rx()
        self.assertTrue(fm_link.status_lines())

    def test_no_health_reading_is_none_not_a_zeroed_one(self):
        """So a status screen can tell 'nothing arrived' from 'there is no
        audio path at all'."""
        fm_link.reset_rx()
        self.assertIsNone(fm_link.health_snapshot())


class TestTheAirtimeModel(unittest.TestCase):

    def test_a_full_frame_charges_the_real_burst(self):
        link = airtime.fm(fm_link.bitrate_bps())
        self.assertAlmostEqual(link.frame_s(fm_link.hw_mtu()),
                               fm_link.burst_duration_s(), places=6)

    def test_it_under_charges_short_frames_which_is_the_known_direction(self):
        """Airtime here is FLAT -- every frame is padded to the code's full
        payload, so a 6 B frame costs the same burst as a 29 B one. Pinned
        because the error's DIRECTION is what matters: the budget believes
        there is more channel left than there is."""
        link = airtime.fm(fm_link.bitrate_bps())
        self.assertLess(link.frame_s(6), fm_link.burst_duration_s())

    def test_the_burst_fits_the_gates_per_frame_cap(self):
        """eligibility.fm_gate refuses a frame that would hold the channel too
        long. The operating point has to clear its own gate."""
        import eligibility
        self.assertLess(fm_link.burst_duration_s(),
                        eligibility.FM_MAX_FRAME_S)

    def test_the_burst_duration_is_the_configs(self):
        self.assertEqual(fm_link.burst_duration_s(),
                         fm_config.OPERATING_POINT.burst_duration_s())


class TestTheAudioPlanKotlinRunsOn(unittest.TestCase):
    """Every number FmAudioBridgeService used to keep its own copy of.

    WHY THIS IS WORTH TESTING AT ALL: each field here is silent when the two
    sides disagree. A sample rate that does not match the profile transmits at
    the wrong pitch and logs nothing at either end; a keying method that does
    not match the lead-in clips the front of every burst and presents as "some
    configurations never sync"; a listen-before-transmit bound sized against
    the wrong burst either cuts into a real transmission or holds a message for
    no reason. None of them raise.
    """

    def tearDown(self):
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)

    def _plan(self, profile=None):
        if profile is not None:
            fm_link.set_profile(profile)
        return json.loads(fm_link.audio_plan())

    def test_it_is_json_with_every_field_kotlin_reads(self):
        """NAMED EXPLICITLY rather than checked for shape: Kotlin reads these
        by string key, so a rename here is a field that silently reads 0 or ""
        on the other side."""
        plan = self._plan()
        for key in ("sample_rate_hz", "keying", "lead_in_s", "burst_s",
                    "ptt_keyup_s", "channel_busy_wait_s", "backoff_max_s",
                    "capacity_bytes",
                    "profile", "summary"):
            self.assertIn(key, plan)

    def test_the_sample_rate_is_the_profiles(self):
        self.assertEqual(self._plan()["sample_rate_hz"],
                         fm_link.config().profile.sample_rate_hz)

    def test_the_capacity_agrees_with_the_mtu(self):
        """Two answers to the same question, and Kotlin sizes nothing off the
        plan's copy that RNS does not size off hw_mtu()."""
        self.assertEqual(self._plan()["capacity_bytes"], fm_link.hw_mtu())

    def test_every_field_follows_a_profile_change(self):
        """The reason the service re-asks per burst rather than capturing once
        -- PythonRnsService applies the profile on a thread that is not ordered
        against the audio service's startup."""
        fast = self._plan("fast")
        narrow = self._plan("narrow")
        self.assertEqual(narrow["profile"], "narrow")
        self.assertNotEqual(narrow["burst_s"], fast["burst_s"])
        self.assertNotEqual(narrow["channel_busy_wait_s"],
                            fast["channel_busy_wait_s"])

    def test_the_burst_is_the_configs_own(self):
        for name in fm_config.profile_names():
            plan = self._plan(name)
            self.assertAlmostEqual(plan["burst_s"],
                                   fm_link.burst_duration_s(), places=9)
            self.assertAlmostEqual(plan["lead_in_s"],
                                   fm_link.config().lead_in_s, places=9)

    def test_the_busy_wait_outlasts_a_burst_plus_the_carrier_release(self):
        """A bound of one burst alone would expire while a burst that has just
        FINISHED still reads busy -- the carrier reading averages over its last
        window, so it goes idle up to fm_carrier.RELEASE_S late -- and
        "transmitted anyway" would be logged for a channel that was in fact
        clear."""
        for name in fm_config.profile_names():
            plan = self._plan(name)
            self.assertGreaterEqual(
                plan["channel_busy_wait_s"],
                plan["burst_s"] + fm_carrier.RELEASE_S,
                "%s: the wait can expire while the channel still reads busy"
                % name)

    def test_the_busy_wait_comes_from_the_live_burst_not_the_ceiling(self):
        """The figure this replaces was MAX_BURST_S + margin: the longest burst
        any configuration is ALLOWED, which at this operating point is nearly
        four times the one actually on the air. Same substitution Phase 4 had
        to make for the beacon slot grid and eligibility.FM_MAX_FRAME_S."""
        for name in fm_config.profile_names():
            plan = self._plan(name)
            self.assertLess(plan["channel_busy_wait_s"],
                            fm_config.MAX_BURST_S,
                            "%s: still sized against the ceiling" % name)

    def test_cm108_charges_the_measured_key_up(self):
        """Charged as a wait BEFORE the audio starts, not absorbed by the
        lead-in: the carrier is not up for this long after PTT is asserted and
        anything played before then is simply gone."""
        plan = self._plan()
        self.assertEqual(plan["keying"], fm_config.KEYING_CM108)
        self.assertEqual(plan["ptt_keyup_s"], airtime.FM_PTT_KEYUP_S)

    def test_vox_charges_nothing_because_the_audio_is_the_keying(self):
        """Waiting before playing under VOX would delay the thing doing the
        keying. Checked against a VOX config directly, since nothing selectable
        produces one today -- which is exactly why it would go unnoticed."""
        vox = fm_config.link_config(keying=fm_config.KEYING_VOX)
        self.assertEqual(fm_link._ptt_keyup_s(vox), 0.0)

    def test_the_keying_is_one_fm_config_knows(self):
        self.assertIn(self._plan()["keying"], fm_config.KEYING_LEAD_IN_S)


class TestKotlinsProfileListMatchesPythons(unittest.TestCase):
    """RadioLink.kt carries its own copy of the profile names, and has to.

    The dialog draws before the bridge is running, so the list cannot be read
    from here. Drift is contained on the Python side rather than prevented --
    fm_config.resolve() substitutes the measured default for a name it does not
    have -- but the two failure directions are not symmetric, and neither is
    visible without this test: a name Kotlin offers and Python does not have is
    an operator selecting a placement that silently becomes a different one,
    and a name Python has and Kotlin does not offer is a measured placement
    nobody can reach.
    """

    def _radio_link_kt(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "java", "io", "github", "stress5094", "customrnsbridge",
            "RadioLink.kt")
        with open(path) as fh:
            return fh.read()

    def _kotlin_profiles(self):
        src = self._radio_link_kt()
        default = re.search(
            r'TONE_PROFILE_DEFAULT\s*=\s*"([^"]+)"', src).group(1)
        listed = re.search(
            r'TONE_PROFILES\s*=\s*listOf\(([^)]*)\)', src).group(1)
        names = []
        for item in listed.split(","):
            item = item.strip()
            if not item:
                continue
            names.append(default if item == "TONE_PROFILE_DEFAULT"
                         else item.strip('"'))
        return default, names

    def test_the_offered_names_are_exactly_the_measured_ones(self):
        _default, names = self._kotlin_profiles()
        self.assertEqual(sorted(names), sorted(fm_config.profile_names()))

    def test_the_two_defaults_agree(self):
        default, _names = self._kotlin_profiles()
        self.assertEqual(default, fm_config.DEFAULT_PROFILE)

    def test_the_custom_name_agrees(self):
        """Kotlin stores PROFILE_CUSTOM and rns_bridge routes on
        fm_custom.CUSTOM_NAME; a mismatch would send the custom settings down
        the preset path, which falls back to the default -- silently a
        different net."""
        import fm_custom
        got = re.search(r'PROFILE_CUSTOM\s*=\s*"([^"]+)"',
                        self._radio_link_kt()).group(1)
        self.assertEqual(got, fm_custom.CUSTOM_NAME)

    def test_the_service_hands_python_the_custom_settings(self):
        """set_fm_profile's third argument. Without it Custom is selectable,
        saved, and never applied."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "java", "io", "github", "stress5094",
                            "customrnsbridge", "PythonRnsService.kt")
        with open(path) as fh:
            src = fh.read()
        call = re.search(r'"set_fm_profile",(.*?)\n\s*\)', src, re.S).group(1)
        self.assertIn("fmCustomSpec(", call)
        self.assertIn("fmVoxForBridge(", call)

    def test_every_offered_name_has_a_label_of_its_own(self):
        """A profile falling through to the default's label would describe the
        wrong frequencies -- the one thing the label exists to give an operator
        to compare out loud."""
        src = self._radio_link_kt()
        _default, names = self._kotlin_profiles()
        labels = re.search(r'fun toneProfileLabel\(name: String\): String = when \(name\) \{(.*?)\n    \}',
                           src, re.S).group(1)
        branches = re.findall(r'"([^"]+)" ->', labels)
        # The default is the `else` branch, so it is the one name that does not
        # need a branch of its own.
        self.assertEqual(sorted(branches),
                         sorted(n for n in names if n != fm_config.DEFAULT_PROFILE))


class TestTheInterfaceTalksToKotlin(unittest.TestCase):

    def test_process_outgoing_writes_a_framed_payload(self):
        iface, theirs, _ = _iface(self)
        self.assertTrue(iface.process_outgoing(b"on the air"))
        self.assertEqual(theirs.recv(4096), fm_link.pack_frame(b"on the air"))

    def test_process_outgoing_counts_the_payload_not_the_framing(self):
        iface, theirs, _ = _iface(self)
        iface.process_outgoing(b"12345")
        theirs.recv(4096)
        self.assertEqual(iface.txb, 5)

    def test_an_offline_interface_refuses_to_send(self):
        iface, theirs, _ = _iface(self)
        iface.online = False
        self.assertFalse(iface.process_outgoing(b"nope"))

    def test_a_write_to_a_dead_socket_reports_failure_not_a_crash(self):
        """process_outgoing mirrors RNS.Packet.send()'s True/False convention,
        because rns_bridge's bypass call sites check it the same way."""
        iface, theirs, _ = _iface(self)
        theirs.close()
        iface.sock.close()
        self.assertFalse(iface.process_outgoing(b"gone"))
        self.assertFalse(iface.online)

    def test_inbound_frames_reach_deliver_frame_not_rns_packet(self):
        got = []
        iface, theirs, owner = _iface(self, deliver=lambda d, i: got.append(d))
        theirs.sendall(fm_link.pack_frame(b"from the radio"))
        _wait(lambda: got)
        self.assertEqual(got, [b"from the radio"])
        self.assertEqual(owner.inbound_calls, [],
                         "the bypass exists so RNS.Packet is never involved")
        self.assertEqual(iface.rxb, len(b"from the radio"))

    def test_without_a_deliver_frame_it_falls_back_to_the_owner(self):
        iface, theirs, owner = _iface(self)
        theirs.sendall(fm_link.pack_frame(b"routed"))
        _wait(lambda: owner.inbound_calls)
        self.assertEqual(owner.inbound_calls, [b"routed"])

    def test_the_read_loop_reassembles_across_recv_boundaries(self):
        got = []
        iface, theirs, _ = _iface(self, deliver=lambda d, i: got.append(d))
        whole = fm_link.pack_frame(b"x" * 300)
        for i in range(0, len(whole), 7):
            theirs.sendall(whole[i:i + 7])
        _wait(lambda: got)
        self.assertEqual(got, [b"x" * 300])

    def test_an_empty_frame_is_not_delivered(self):
        got = []
        iface, theirs, _ = _iface(self, deliver=lambda d, i: got.append(d))
        theirs.sendall(fm_link.pack_frame(b"") + fm_link.pack_frame(b"real"))
        _wait(lambda: got)
        self.assertEqual(got, [b"real"])

    def test_the_far_end_closing_takes_the_link_offline(self):
        iface, theirs, _ = _iface(self)
        theirs.close()
        _wait(lambda: not iface.online)
        self.assertFalse(iface.online)


class TestReconnect(unittest.TestCase):

    def test_a_dropped_socket_is_replaced_and_the_link_comes_back(self):
        fresh = []

        def reconnect():
            ours, theirs = socket.socketpair()
            fresh.append((ours, theirs))
            return ours

        iface, theirs, _ = _iface(self, reconnect=reconnect)
        iface.RECONNECT_WAIT_S = 0.01
        theirs.close()
        self.assertTrue(_wait(lambda: fresh),
                        "reconnect was never asked for a new socket")
        self.assertTrue(_wait(lambda: iface.online))
        self.assertIs(iface.sock, fresh[-1][0])
        self.addCleanup(fresh[-1][1].close)

    def test_the_new_socket_carries_frames(self):
        fresh = []

        def reconnect():
            ours, theirs = socket.socketpair()
            fresh.append((ours, theirs))
            return ours

        iface, theirs, _ = _iface(self, reconnect=reconnect)
        iface.RECONNECT_WAIT_S = 0.01
        theirs.close()
        self.assertTrue(_wait(lambda: fresh and iface.online))
        self.addCleanup(fresh[-1][1].close)
        self.assertTrue(iface.process_outgoing(b"after the reconnect"))
        self.assertEqual(fresh[-1][1].recv(4096),
                         fm_link.pack_frame(b"after the reconnect"))

    def test_no_reconnect_callable_means_no_retry_loop(self):
        """Tests build this against a bare socket pair with no Kotlin bridge
        behind it to reconnect to."""
        iface, theirs, _ = _iface(self, reconnect=None)
        theirs.close()
        _wait(lambda: not iface.online)
        self.assertFalse(iface.online)
        self.assertFalse(iface._reconnecting)

    def test_a_retry_attempt_that_fails_is_tried_again(self):
        calls = []

        def reconnect():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("still down")
            ours, theirs = socket.socketpair()
            self.addCleanup(lambda: _close(ours, theirs))
            return ours

        iface, theirs, _ = _iface(self, reconnect=reconnect)
        iface.RECONNECT_WAIT_S = 0.01
        theirs.close()
        self.assertTrue(_wait(lambda: len(calls) >= 3),
                        "gave up after a failed attempt")
        self.assertTrue(_wait(lambda: iface.online))

    def test_only_one_retry_loop_runs_for_one_drop(self):
        """A write failure and the read loop dying from the same drop BOTH
        reach _start_reconnect, and two loops racing would each swap in a
        socket -- leaving one connected to nothing and the link's own idea of
        which one is live decided by thread scheduling."""
        calls = []

        def reconnect():
            calls.append(1)
            ours, theirs = socket.socketpair()
            self.addCleanup(lambda: _close(ours, theirs))
            return ours

        iface, theirs, _ = _iface(self, reconnect=reconnect)
        # Long enough that both calls below land while the first loop is still
        # in its own sleep, which is the race being tested.
        iface.RECONNECT_WAIT_S = 0.2
        iface._start_reconnect()
        iface._start_reconnect()
        self.assertTrue(_wait(lambda: calls, timeout_s=2.0))
        time.sleep(0.1)
        self.assertEqual(len(calls), 1)

    def test_a_stale_socket_closing_does_not_undo_a_reconnect(self):
        """A reader still on a connection a write failure already replaced must
        not take the FRESH one offline when its own socket finally ends.

        Driven through the real read loop rather than by poking `online`: the
        guard is an identity check in that loop's `finally`, and the only way to
        know it runs is to make the loop exit.
        """
        iface, theirs, _ = _iface(self)
        replacement, spare = socket.socketpair()
        self.addCleanup(lambda: _close(replacement, spare))
        stale = iface.sock
        iface.sock = replacement
        # recv() on the stale socket returns b"" and the read loop exits.
        theirs.shutdown(socket.SHUT_WR)
        time.sleep(0.05)
        self.assertTrue(iface.online)
        self.assertIs(iface.sock, replacement)


class TestTheReceivePathIsPersistent(unittest.TestCase):

    def setUp(self):
        fm_link.reset_rx()
        self.addCleanup(fm_link.reset_rx)

    @requires_codec2
    def test_one_instance_serves_every_chunk(self):
        """A modem rebuilt per chunk throws codec2's sync state away and never
        acquires at all -- a dead link, not a slow one."""
        first = fm_link._rx("")
        self.assertIs(fm_link._rx(""), first)

    @requires_codec2
    def test_channel_busy_reads_that_same_instance(self):
        """channel_busy() answers from the SAME persistent receive path that
        feed_audio() runs, through the carrier detector -- not a second
        detector, and not codec2's sync reading, which is idle mid-burst."""
        rx = fm_link._rx("")
        rx.modem.has_sync = lambda: True        # the old signal, shouting busy
        self.assertFalse(fm_link.channel_busy(""))
        profile = fm_link.config().profile
        fs = rx.modem.sample_rate_hz
        n = fs // profile.symbol_rate_bd
        tones = profile.tone_list()
        burst, phase = [], 0.0
        for k in range(profile.symbol_rate_bd):
            step = 2 * math.pi * tones[k % len(tones)] / fs
            for _ in range(n):
                burst.append(0.5 * math.sin(phase))
                phase += step
        rx.feed(burst)
        self.assertTrue(fm_link.channel_busy(""))

    @requires_codec2
    def test_carrier_detail_reports_what_the_detector_is_using(self):
        """The offline log line must carry the SAME floor and threshold the
        detector decides with, or an analysis of a key-up reads numbers that
        were never the ones in force."""
        rx = fm_link._rx("")
        detail = fm_link.carrier_detail("")
        self.assertIn("level=%.3f" % rx.carrier.level, detail)
        self.assertIn("floor=%.3f" % rx.carrier.floor, detail)
        self.assertIn("thr=%.3f" % rx.carrier.threshold, detail)

    @requires_codec2
    def test_reset_drops_it(self):
        fm_link._rx("")
        fm_link.reset_rx()
        self.assertIsNone(fm_link._RX[0])

    @requires_codec2
    def test_feeding_silence_decodes_nothing(self):
        import fm_burst
        silence = fm_burst.to_pcm16([0.0] * 4000)
        self.assertEqual(fm_link.feed_audio("", silence), b"")

    @requires_codec2
    def test_health_reads_once_audio_has_arrived(self):
        import fm_burst
        fm_link.feed_audio("", fm_burst.to_pcm16([0.0] * 4000))
        snap = fm_link.health_snapshot()
        self.assertIsNotNone(snap)
        self.assertIn("state", snap)


@requires_codec2
class TestTheWholePathAtThePhonesSampleRate(unittest.TestCase):
    """build_tx_burst to feed_audio, against the real library, at the 8000 Hz
    the phone actually runs -- not the bench's 48000.

    This is the one test that catches the two halves of the module having
    drifted into different formats, which is exactly what happened when the
    transmit and receive paths each carried their own copy of the framing.
    """

    def setUp(self):
        fm_link.reset_rx()
        self.addCleanup(fm_link.reset_rx)

    def _round_trip(self, payload):
        pcm = fm_link.build_tx_burst("", payload)
        got = b""
        # Ragged chunks, the way AudioRecord delivers them, plus a tail of
        # silence AS LONG AS THE BURST ITSELF.
        #
        # That length is the documented requirement, not a margin: a frame does
        # not surface until the demodulator has been fed roughly as many samples
        # again as the burst (see fm_modem.demodulate). A HALF-length tail was
        # used here until 2026-10-02 and passed, because it was enough for the
        # 32 B code's short burst -- then the operating point moved to the 128 B
        # code and every round trip in this class returned nothing. Which is
        # precisely the trap fm_modem's docstring names: too short a flush makes
        # short bursts decode while long ones silently do not, and it looks
        # exactly like a bad configuration.
        stream = pcm + bytes(len(pcm))
        for i in range(0, len(stream), 4096):
            got += fm_link.feed_audio("", stream[i:i + 4096])
        return fm_link.FrameParser().feed(got)

    def test_a_payload_comes_back_byte_exact(self):
        payload = b"a full frame, right to the link's own capacity".ljust(
            fm_link.hw_mtu(), b".")[:fm_link.hw_mtu()]
        self.assertEqual(len(payload), fm_link.hw_mtu())
        self.assertIn(payload, self._round_trip(payload))

    def test_both_in_burst_copies_arrive_as_separate_frames(self):
        """The config sends `repeats` copies sharing one preamble. Each decodes
        independently, so duplicate suppression upstream is what stops a chat
        message rendering twice -- not the physical layer.

        RETRIED, because codec2's FSK-LDPC drops the first frame after a
        preamble about 0.4% of the time on clean loopback (measured 2 in 500;
        never the second or third). That is real modem behaviour and exactly
        why the design repeats every frame -- see test_fm_modem's docstring.
        Four attempts makes a false failure about 1e-10.
        """
        repeats = fm_link.config().repeats
        for _ in range(4):
            frames = self._round_trip(b"two copies")
            if len(frames) == repeats:
                self.assertEqual(set(frames), {b"two copies"})
                return
        raise AssertionError(
            "never saw all %d copies in four bursts; last got %r"
            % (repeats, frames))

    def test_an_empty_payload_round_trips(self):
        self.assertIn(b"", self._round_trip(b""))

    def test_a_payload_of_every_byte_value_survives(self):
        """The scrambler is XOR with a positional keystream, so a byte value
        that maps onto a long run is the case to check."""
        payload = bytes(range(fm_link.hw_mtu()))
        self.assertIn(payload, self._round_trip(payload))

    def test_a_bytearray_is_accepted(self):
        """Chaquopy hands Kotlin's argument over as a jarray proxy, not bytes,
        and concatenating one boxes each element with Java's SIGNED byte
        semantics -- every byte >= 0x80 goes negative and only fails far
        downstream. bytes() first is what stops that, and it must accept
        anything buffer-shaped."""
        payload = bytearray(b"\x80\xff\x00 signed bytes")
        self.assertIn(bytes(payload), self._round_trip(payload))

    def test_a_memoryview_is_accepted(self):
        payload = memoryview(b"\x90\xa0\xb0 buffer protocol")
        self.assertIn(bytes(payload), self._round_trip(payload))

    def test_a_payload_over_capacity_is_refused_before_modulation(self):
        with self.assertRaises(fm_framing.FramingError) as cm:
            fm_link.build_tx_burst("", b"x" * (fm_link.hw_mtu() + 1))
        self.assertIn("capacity", str(cm.exception))

    def test_the_burst_runs_for_as_long_as_the_config_says(self):
        pcm = fm_link.build_tx_burst("", b"timing")
        samples = len(pcm) // 2
        seconds = samples / float(fm_link.config().profile.sample_rate_hz)
        self.assertAlmostEqual(seconds, fm_link.burst_duration_s(), places=2)

    def test_the_burst_is_deterministic(self):
        """The scrambler replaced random padding precisely so a bench trial is
        reproducible and a tx/rx comparison is an equality."""
        self.assertEqual(fm_link.build_tx_burst("", b"same bytes"),
                         fm_link.build_tx_burst("", b"same bytes"))


class TestAProfileSwitchNeverFreesTheModemUnderTheAudioThread(unittest.TestCase):
    """2026-10-12: a profile switch during a live receive crashed the Pixel's app
    (SIGSEGV in libcodec2 on fm-audio-rx): reset_rx() closed the native modem
    while feed_audio() was still inside it."""

    def setUp(self):
        self._rx = fm_link._RX[0]
        self._verdicts = fm_link._log_burst_verdicts
        fm_link._log_burst_verdicts = lambda rx: None
        self.addCleanup(self._restore)

    def _restore(self):
        fm_link._RX[0] = self._rx
        fm_link._log_burst_verdicts = self._verdicts

    def test_close_waits_for_a_feed_that_is_still_running(self):
        import threading, time
        events = []
        inside = threading.Event()
        release = threading.Event()

        class Modem(object):
            def close(self_):
                events.append("close")

        class Rx(object):
            modem = Modem()

            def feed_pcm(self_, pcm):
                events.append("feed-start")
                inside.set()
                release.wait(5)
                events.append("feed-end")
                return []

        fm_link._RX[0] = Rx()
        feeder = threading.Thread(target=fm_link.feed_audio, args=("", b"\0\0"))
        feeder.start()
        self.assertTrue(inside.wait(5))
        resetter = threading.Thread(target=fm_link.reset_rx)
        resetter.start()
        time.sleep(0.3)
        self.assertNotIn("close", events, "closed the modem while it was in use")
        release.set()
        feeder.join(5)
        resetter.join(5)
        self.assertEqual(events, ["feed-start", "feed-end", "close"])


@requires_codec2
class TestTheTrialProfileStaysOutOfTheOffer(unittest.TestCase):
    """The two-tone 1000 Bd placement (2026-10-12) is a TRIAL: constructible and
    runnable through the adb hook, but not something the picker may offer or a
    stale preference may fall back to."""

    def setUp(self):
        self.addCleanup(fm_link.set_profile, fm_config.DEFAULT_PROFILE)

    def test_it_is_not_in_the_measured_list_or_reachable_by_name(self):
        self.assertNotIn("twotone", fm_config.profile_names())
        name, substituted = fm_link.set_profile("twotone")
        self.assertTrue(substituted)
        self.assertEqual(name, fm_config.DEFAULT_PROFILE)

    def test_switching_to_it_changes_the_live_config_and_is_shorter(self):
        default_burst = fm_link.burst_duration_s()
        self.assertEqual(fm_link.set_trial_profile("twotone"), "twotone")
        self.assertEqual(fm_link.profile_name(), "twotone")
        self.assertLess(fm_link.burst_duration_s(), default_burst)
        import json
        plan = json.loads(fm_link.audio_plan())
        self.assertEqual(plan["sample_rate_hz"], 8000)

    def test_the_500_baud_trial_differs_from_the_1000_baud_one_only_in_rate(self):
        a, b = fm_config.TRIAL_PROFILES["twotone"], fm_config.TRIAL_PROFILES["twotone500"]
        self.assertEqual((a.first_tone_hz, a.tone_spacing_hz, a.tones),
                         (b.first_tone_hz, b.tone_spacing_hz, b.tones))
        self.assertEqual((a.symbol_rate_bd, b.symbol_rate_bd), (1000, 500))
        self.assertNotIn("twotone500", fm_config.profile_names())
        fm_link.set_trial_profile("twotone500")
        self.assertGreater(fm_link.burst_duration_s(), 3.0)

    def test_an_unknown_trial_raises_and_leaves_the_config_alone(self):
        before = fm_link.profile_name()
        with self.assertRaises(KeyError):
            fm_link.set_trial_profile("nope")
        self.assertEqual(fm_link.profile_name(), before)

    def test_a_frame_round_trips_byte_exact_through_it(self):
        fm_link.set_trial_profile("twotone")
        fm_link.reset_rx()
        payload = b"two tones at 1000 Bd".ljust(fm_link.hw_mtu(), b".")
        pcm = fm_link.build_tx_burst("", payload)
        got = b""
        stream = pcm + bytes(len(pcm))
        for i in range(0, len(stream), 4096):
            got += fm_link.feed_audio("", stream[i:i + 4096])
        self.assertIn(payload, fm_link.FrameParser().feed(got))


class TestBackoffAfterDeferral(unittest.TestCase):
    """Once listen-before-transmit really makes a node wait, every node that
    waited behind the same burst is released at the same instant and keys up
    together -- a collision no listener can hear. A random wait, then listening
    again, is what spreads them."""

    def _plan(self, profile=None):
        if profile is not None:
            fm_link.set_profile(profile)
        return json.loads(fm_link.audio_plan())

    def tearDown(self):
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)

    def test_the_plan_carries_it(self):
        self.assertEqual(self._plan()["backoff_max_s"], fm_link.BACKOFF_MAX_S)

    def test_the_window_is_much_wider_than_the_detectors_latency(self):
        """Two waiters collide only if their draws land within the time the
        detector needs to hear the first (~0.3 s: ~70 ms to cross its threshold
        plus one 0.2 s chunk). A window not several times that is no spread."""
        self.assertGreaterEqual(fm_link.BACKOFF_MAX_S, 5 * 0.3)

    def test_two_waiters_mostly_do_not_collide(self):
        """The arithmetic in BACKOFF_MAX_S's comment, checked rather than
        quoted: two draws within 0.3 s of each other, against ~100% for nodes
        released at the same instant."""
        import random
        rng = random.Random(7)
        n = 20000
        hits = sum(abs(rng.uniform(0, fm_link.BACKOFF_MAX_S)
                       - rng.uniform(0, fm_link.BACKOFF_MAX_S)) < 0.3
                   for _ in range(n))
        self.assertLess(hits / float(n), 0.35)

    def test_the_busy_wait_has_room_for_one_backoff(self):
        """Otherwise a node that deferred gives up and "transmits anyway" in the
        middle of its own politeness."""
        for name in fm_config.profile_names():
            plan = self._plan(name)
            self.assertGreaterEqual(
                plan["channel_busy_wait_s"],
                plan["burst_s"] + fm_carrier.RELEASE_S + plan["backoff_max_s"],
                "%s: no room for the back-off inside the wait" % name)

    def test_kotlin_reads_the_window_from_the_plan_and_listens_again(self):
        """The policy lives in Python and the draw in Kotlin, so a Kotlin that
        stopped reading the key would back off by a stale number or none."""
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "java", "io",
            "github", "stress5094", "customrnsbridge",
            "FmAudioBridgeService.kt")
        with open(path) as fh:
            src = fh.read()
        self.assertIn('json.getDouble("backoff_max_s")', src)
        body = src[src.index("private fun waitForChannelClear"):
                   src.index("private fun transmitOneFrame")]
        self.assertIn("backoffMaxS", body)
        # Backs off, and then LOOPS to check again rather than returning.
        self.assertIn("deferred = false", body)
        self.assertIn("if (!deferred) return", body)


class TestEveryBurstHeardIsLogged(unittest.TestCase):
    """fm_airlog's verdicts reach the log the operator reads, from the same
    feed_audio() Kotlin calls, and a logging problem cannot cost the audio."""

    def setUp(self):
        import RNS
        fm_link.reset_rx()
        self.addCleanup(fm_link.reset_rx)
        self.logged = []
        self._real_log = RNS.log
        RNS.log = lambda msg, level=0, *a, **k: self.logged.append(msg)
        self.addCleanup(setattr, RNS, "log", self._real_log)
        self.clock = [1000.0]
        rx = fm_link._rx("")
        rx.airlog._clock = lambda: self.clock[0]

    def _feed_burst(self, payload=b"logged"):
        pcm = fm_link.build_tx_burst("", payload)
        stream = pcm + bytes(len(pcm))
        got = b""
        fs = fm_link.config().profile.sample_rate_hz
        for i in range(0, len(stream), 4096):
            chunk = stream[i:i + 4096]
            self.clock[0] += (len(chunk) // 2) / float(fs)
            got += fm_link.feed_audio("", chunk)
        return got

    def test_a_decoded_burst_is_logged_as_ok(self):
        got = self._feed_burst()
        self.assertTrue(got)
        verdicts = [m for m in self.logged if m.startswith("[fm-air]")]
        self.assertEqual(len(verdicts), 1, self.logged)
        self.assertIn("burst OK", verdicts[0])

    def test_the_transmit_report_reaches_the_ledger(self):
        fm_link.note_tx_state("", True)
        self.clock[0] += 2.0
        fm_link.note_tx_state("", False)
        self.assertEqual(len(fm_link._rx("").airlog._tx_windows), 1)

    def test_recent_tx_reads_without_building_a_receive_path(self):
        fm_link.reset_rx()
        self.assertIsNone(fm_link.recent_tx(30.0))
        self.assertIsNone(fm_link._RX[0])       # asking must not create one

    def test_recent_tx_reports_a_transmit_the_kotlin_side_announced(self):
        fm_link.note_tx_state("", True)
        self.clock[0] += 3.0
        fm_link.note_tx_state("", False)
        self.clock[0] += 2.0
        n, secs = fm_link.recent_tx(30.0)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(secs, 3.0)

    def test_a_logger_that_raises_still_returns_the_audio(self):
        import RNS

        def boom(*a, **k):
            raise RuntimeError("logger broke")
        RNS.log = boom
        self.assertTrue(self._feed_burst())


class TestOnlyThisModuleCrossesIntoRns(unittest.TestCase):
    """The layering that made Phase 0's measurements possible at all.

    fm_config/fm_framing/fm_modem/fm_burst/fm_health/fm_rx are testable on a
    desktop against the real libcodec2 because none of them reaches for RNS or
    Android. That holds only while exactly one module is allowed to.
    """

    def _source(self, name):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            name + ".py")
        with open(path) as fh:
            return fh.read()

    def test_the_layers_below_do_not_import_rns(self):
        for name in ("fm_config", "fm_framing", "fm_modem", "fm_burst",
                     "fm_health", "fm_rx", "fm_resend", "fm_carrier"):
            src = self._source(name)
            self.assertNotIn("import RNS", src,
                             "%s reaches into RNS -- the desktop bench is how "
                             "every number in this transport was measured"
                             % name)

    def test_this_module_does(self):
        self.assertIn("RNS.Interfaces.Interface", self._source("fm_link"))


if __name__ == "__main__":
    unittest.main()


class TestOnlineNeedsTheAudioSide(unittest.TestCase):
    """2026-10-23: a half-enumerated Digirig (serial chip, no sound card) left
    the socket reconnecting every 5 s, so the interface read online and 22
    beacons were logged "sent" with nothing keyed. Online now also needs audio
    to have arrived recently."""

    def setUp(self):
        self._saved = fm_link._last_audio[0]
        fm_link._last_audio[0] = None
        self.addCleanup(lambda: fm_link._last_audio.__setitem__(0, self._saved))

    def _iface(self, alive):
        ours, theirs = socket.socketpair()
        iface = fm_link.FmInterface(_Owner(), ours, "", audio_alive=alive)
        self.addCleanup(lambda: _close(ours, theirs, iface.sock))
        return iface, theirs

    def test_no_audio_yet_is_offline_and_refuses_to_send(self):
        iface, theirs = self._iface(fm_link.audio_alive)
        self.assertFalse(iface.online)
        self.assertFalse(iface.process_outgoing(b"beacon"))
        self.assertEqual(iface.txb, 0)

    def test_audio_arriving_brings_it_online_and_silence_takes_it_off(self):
        clock = [100.0]
        iface, theirs = self._iface(lambda: fm_link.audio_alive(now=clock[0]))
        fm_link._last_audio[0] = 100.0                 # feed_audio() just ran
        self.assertTrue(iface.online)
        self.assertTrue(iface.process_outgoing(b"beacon"))
        clock[0] = 100.0 + fm_link.AUDIO_ALIVE_S + 0.1  # the card went away
        self.assertFalse(iface.online)
        self.assertFalse(iface.process_outgoing(b"beacon"))

    def test_a_dead_socket_is_offline_whatever_the_audio(self):
        iface, theirs = self._iface(lambda: True)
        iface.online = False
        self.assertFalse(iface.online)

    def test_feed_audio_and_build_tx_burst_both_count_as_alive(self):
        self.assertFalse(fm_link.audio_alive())
        fm_link._note_audio()
        self.assertTrue(fm_link.audio_alive())
        src = open(fm_link.__file__).read()
        for fn in ("def feed_audio(", "def build_tx_burst("):
            body = src.split(fn, 1)[1].split("\ndef ", 1)[0]
            self.assertIn("_note_audio()", body, fn)
