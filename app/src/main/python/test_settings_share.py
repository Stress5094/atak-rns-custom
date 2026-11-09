"""settings_share: the rules a phone enforces on a pushed settings bundle,
against REAL crypto (cryptography) and a fake radio and clock. Plan:
docs/settings-sharing.md."""

import json
import os
import tempfile
import unittest

import settings_share as ss
from settings_share import PinError, Refused

PIN = "4821"
NEW = {"lora_preset": "Medium Slow", "lora_freq_mhz": "906.875", "lora_tx_dbm": 22}


class FakeRadio(object):
    def __init__(self):
        self.settings = {"lora_preset": "Short Slow", "lora_freq_mhz": "915.000",
                         "lora_tx_dbm": 22, "fm_profile": "fast",
                         "coverage": {"fm_mhz": "146.000", "fm_w": "10",
                                      "tx_height_m": "1.5"},
                         "passphrase": "old pass"}
        self.applied = []
        self.refuse = None             # a key whose value makes apply fail

    def current(self):
        return json.loads(json.dumps(self.settings))

    def apply(self, s):
        self.applied.append(s)
        if self.refuse and self.refuse in s:
            # Applies what it can, reports the rest -- like set_radio_config.
            for k, v in s.items():
                if k != self.refuse:
                    self.settings[k] = v
            return ["%s refused by the radio" % self.refuse]
        for k, v in s.items():
            if k == "coverage":
                self.settings["coverage"].update(v)
            else:
                self.settings[k] = v
        return []


class Clock(object):
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.clock = Clock()
        self.radio = FakeRadio()
        self.keys = ss.HostKeys.generate()
        self.host = ss.Host(self.keys, "Host-OnePlus", self._p("rev.json"), clock=self.clock)
        self.rx = self._receiver()

    def tearDown(self):
        self._tmp.cleanup()

    def _p(self, name):
        return os.path.join(self.dir, name)

    def _receiver(self, name="phone.json"):
        return ss.Receiver(self._p(name), self.radio.current, self.radio.apply,
                           clock=self.clock, pin_iterations=1000)

    def _push(self, settings=None, switch_in_s=None, passphrase=None, phones=None):
        phones = [(self.rx.phone_id, self.rx.x_pub)] if phones is None else phones
        _, b = self.host.bundles(settings or NEW, phones=phones,
                                 passphrase=passphrase, switch_in_s=switch_in_s)
        return b

    def _net(self, **kw):
        return self._push(**kw)[self.rx.phone_id]


class BundleTest(Base):
    def test_a_bundle_round_trips_into_an_offer_with_what_changes(self):
        offer = self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        self.assertEqual(offer["outcome"], "pending")
        self.assertEqual(offer["host_name"], "Host-OnePlus")
        self.assertEqual({c["key"]: (c["old"], c["new"]) for c in offer["changes"]},
                         {"lora_preset": ("Short Slow", "Medium Slow"),
                          "lora_freq_mhz": ("915.000", "906.875")})   # power unchanged
        self.assertEqual(self.radio.applied, [])                     # nothing yet

    def test_an_altered_bundle_is_refused(self):
        b = ss.decode(self._net())
        b["body"]["settings"]["lora_freq_mhz"] = "903.000"
        with self.assertRaisesRegex(Refused, "signature"):
            self.rx.receive(ss.encode(b), ss.ROUTE_NETWORK)

    def test_a_bundle_cannot_claim_another_hosts_id(self):
        # Signed properly -- by a different key -- under the real host's id.
        other = ss.HostKeys.generate()
        b = ss.decode(ss.make_bundle(other, "Host-OnePlus", 5, NEW, issued=self.clock()))
        b["body"]["host"] = self.keys.host_id
        b["sig"] = ss._b64(other.sign(ss._canonical(b["body"])))
        with self.assertRaisesRegex(Refused, "does not match its key"):
            self.rx.receive(ss.encode(b), ss.ROUTE_NETWORK)

    def test_garbage_is_refused_with_a_reason(self):
        for text in ("", "hello", "RTAKS1.!!!!", ss.PREFIX + "AAAA"):
            with self.assertRaises(Refused):
                self.rx.receive(text, ss.ROUTE_QR)

    def test_settings_a_phone_does_not_know_refuse_the_bundle(self):
        for bad in ({"pin": "0000"}, {"trusted": {}},
                    {"accept_passphrase_over_network": True},
                    {"coverage": {"fm_mhz": "146", "colour": "red"}}):
            text = ss.make_bundle(self.keys, "H", 99, bad, issued=self.clock())
            with self.assertRaises(Refused, msg=bad):
                self.rx.receive(text, ss.ROUTE_QR)

    def test_an_fm_net_code_must_come_with_custom_selected(self):
        """A net code with nothing selecting it would be applied to nothing,
        and a non-text one is not a net code."""
        for bad in ({"fm_custom": "4T-1000/500-500Bd-H1024.2048"},
                    {"fm_profile": "fast", "fm_custom": "4T-1000/500-500Bd-H1024.2048"},
                    {"fm_profile": "custom", "fm_custom": 500},
                    {"fm_profile": 3}):
            text = ss.make_bundle(self.keys, "H", 99, bad, issued=self.clock())
            with self.assertRaises(Refused, msg=bad):
                self.rx.receive(text, ss.ROUTE_QR)

    def test_an_fm_net_code_is_a_network_setting(self):
        self.assertIn("fm_custom", ss.NETWORK_KEYS)
        changes = ss.diff({"fm_profile": "fast", "fm_custom": ""},
                          {"fm_profile": "custom",
                           "fm_custom": "4T-1000/500-500Bd-H1024.2048"})
        self.assertTrue(all(c["network"] for c in changes))
        self.assertEqual([c["label"] for c in changes],
                         ["FM custom net code", "FM tone profile"])

    def test_a_qr_bundle_with_everything_fits_one_qr_code(self):
        s = dict(NEW, fm_profile="custom", fm_custom="4T-1000/500-500Bd-H1024.2048",
                 coverage={"fm_mhz": "146.520", "fm_w": "5", "tx_height_m": "6"})
        text = self._push(s, switch_in_s=120, passphrase="correct horse battery")["qr"]
        # A version-25 QR at medium error correction holds ~1269 bytes.
        self.assertLess(len(text), 700, len(text))


class RevisionTest(Base):
    def test_the_same_or_an_older_revision_is_refused_once_decided(self):
        old = self._net()
        new = self._net()
        self.rx.set_pin(PIN)
        self.rx.receive(new, ss.ROUTE_NETWORK)
        self.rx.approve(self.keys.host_id, PIN)
        for text in (new, old):
            with self.assertRaisesRegex(Refused, "Already decided"):
                self.rx.receive(text, ss.ROUTE_NETWORK)

    def test_a_rejected_revision_is_not_offered_again(self):
        text = self._net()
        self.rx.receive(text, ss.ROUTE_NETWORK)
        self.rx.reject(self.keys.host_id)
        with self.assertRaises(Refused):
            self.rx.receive(text, ss.ROUTE_NETWORK)
        self.assertEqual(self.radio.applied, [])

    def test_a_newer_revision_replaces_the_one_waiting(self):
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        self.rx.receive(self._net(settings={"lora_tx_dbm": 17}), ss.ROUTE_NETWORK)
        waiting = self.rx.pending()
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["settings"], {"lora_tx_dbm": 17})

    def test_the_host_never_reuses_a_revision_across_a_restart(self):
        self._net()
        self._net()
        again = ss.Host(self.keys, "Host-OnePlus", self._p("rev.json"), clock=self.clock)
        self.assertEqual(again.next_rev(), 3)


class PinTest(Base):
    def test_no_pin_no_approval(self):
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        with self.assertRaisesRegex(PinError, "Set a PIN"):
            self.rx.approve(self.keys.host_id, PIN)
        self.assertEqual(self.radio.applied, [])

    def test_approval_with_the_right_pin_applies(self):
        self.rx.set_pin(PIN)
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        self.assertEqual(self.rx.approve(self.keys.host_id, PIN), "applied")
        self.assertEqual(self.radio.settings["lora_freq_mhz"], "906.875")

    def test_a_wrong_pin_applies_nothing_and_keeps_the_offer(self):
        self.rx.set_pin(PIN)
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        with self.assertRaisesRegex(PinError, "Wrong PIN"):
            self.rx.approve(self.keys.host_id, "0000")
        self.assertEqual(self.radio.applied, [])
        self.assertEqual(len(self.rx.pending()), 1)

    def test_wrong_tries_lock_and_the_lock_doubles(self):
        self.rx.set_pin(PIN)
        for _ in range(4):
            self.assertRaises(PinError, self.rx.check_pin, "0000")
        with self.assertRaisesRegex(PinError, "locked for 60 s"):
            self.rx.check_pin("0000")
        # Locked: even the right PIN is refused until it runs out.
        with self.assertRaisesRegex(PinError, "try again in 60 s"):
            self.rx.check_pin(PIN)
        self.clock.t += 61
        for _ in range(4):
            self.assertRaises(PinError, self.rx.check_pin, "0000")
        with self.assertRaisesRegex(PinError, "locked for 120 s"):
            self.rx.check_pin("0000")

    def test_the_lock_survives_a_restart(self):
        self.rx.set_pin(PIN)
        for _ in range(5):
            self.assertRaises(PinError, self.rx.check_pin, "0000")
        with self.assertRaisesRegex(PinError, "try again"):
            self._receiver().check_pin(PIN)

    def test_the_right_pin_clears_the_count(self):
        self.rx.set_pin(PIN)
        for _ in range(4):
            self.assertRaises(PinError, self.rx.check_pin, "0000")
        self.rx.check_pin(PIN)
        for _ in range(4):
            self.assertRaises(PinError, self.rx.check_pin, "0000")
        self.rx.check_pin(PIN)                 # not locked: the count restarted

    def test_changing_the_pin_needs_the_old_one(self):
        self.rx.set_pin(PIN)
        self.assertRaises(PinError, self.rx.set_pin, "9999", old="1111")
        self.assertRaises(PinError, self.rx.set_pin, "9999")
        self.rx.set_pin("9999", old=PIN)
        self.rx.check_pin("9999")

    def test_pin_shape(self):
        for bad in ("123", "abcd", "12 34", "1234567890123", ""):
            self.assertRaises(PinError, self.rx.set_pin, bad)

    def test_the_pin_is_not_stored(self):
        self.rx.set_pin("73190458")
        with open(self._p("phone.json")) as f:
            self.assertNotIn("73190458", f.read())

    def test_resetting_a_forgotten_pin_forgets_every_trusted_host(self):
        self.rx.set_pin(PIN)
        self.rx.trust_host(self.keys.host_id, ss._b64(self.keys.ed_pub), "H", PIN)
        self.rx.reset_pin()
        self.assertFalse(self.rx.has_pin())
        self.assertEqual(self.rx.trusted_hosts(), {})
        self.assertEqual(self.rx.receive(self._net(), ss.ROUTE_NETWORK)["outcome"], "pending")


class TrustTest(Base):
    def _trust(self):
        self.rx.set_pin(PIN)
        self.rx.trust_host(self.keys.host_id, ss._b64(self.keys.ed_pub), "Host-OnePlus", PIN)

    def test_a_trusted_host_applies_without_asking(self):
        self._trust()
        offer = self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        self.assertEqual(offer["outcome"], "applied")
        self.assertEqual(self.radio.settings["lora_preset"], "Medium Slow")
        self.assertEqual(self.rx.pending(), [])

    def test_trusting_needs_the_pin(self):
        self.rx.set_pin(PIN)
        self.assertRaises(PinError, self.rx.trust_host, self.keys.host_id,
                          ss._b64(self.keys.ed_pub), "H", "0000")
        self.assertEqual(self.rx.trusted_hosts(), {})

    def test_trust_is_bound_to_the_key_not_the_name(self):
        self._trust()
        impostor = ss.HostKeys.generate()
        text = ss.make_bundle(impostor, "Host-OnePlus", 50, NEW, issued=self.clock())
        self.assertEqual(self.rx.receive(text, ss.ROUTE_NETWORK)["outcome"], "pending")
        self.assertEqual(self.radio.applied, [])

    def test_trusting_a_key_under_the_wrong_id_is_refused(self):
        self.rx.set_pin(PIN)
        other = ss.HostKeys.generate()
        with self.assertRaises(Refused):
            self.rx.trust_host(self.keys.host_id, ss._b64(other.ed_pub), "H", PIN)

    def test_the_passphrase_opt_in_needs_the_pin(self):
        self.rx.set_pin(PIN)
        self.assertRaises(PinError, self.rx.set_accept_passphrase_over_network, True, "0000")
        self.assertFalse(self.rx.accepts_passphrase_over_network())


class PassphraseTest(Base):
    def setUp(self):
        super().setUp()
        self.rx.set_pin(PIN)

    def _offer(self, route, **kw):
        b = self._push(settings=NEW, passphrase="new pass", **kw)
        return self.rx.receive(b["qr"] if route == ss.ROUTE_QR else b[self.rx.phone_id], route)

    def test_qr_may_carry_it(self):
        offer = self._offer(ss.ROUTE_QR)
        self.assertEqual(offer["settings"]["passphrase"], "new pass")
        change = [c for c in offer["changes"] if c["key"] == "passphrase"][0]
        self.assertEqual((change["old"], change["new"]), ("(hidden)", "(new passphrase)"))

    def test_over_the_network_it_is_left_out_unless_this_phone_opted_in(self):
        offer = self._offer(ss.ROUTE_NETWORK)
        self.assertNotIn("passphrase", offer["settings"])
        self.assertIn("does not accept it over the network", offer["notes"][0])
        self.assertIn("lora_preset", offer["settings"])      # the rest still offered

    def test_opted_in_it_is_decrypted(self):
        self.rx.set_accept_passphrase_over_network(True, PIN)
        offer = self._offer(ss.ROUTE_NETWORK)
        self.assertEqual(offer["settings"]["passphrase"], "new pass")

    def test_the_network_bundle_does_not_contain_it_readably(self):
        text = self._push(passphrase="new pass")[self.rx.phone_id]
        self.assertNotIn("new pass", json.dumps(ss.decode(text)))

    def test_sealed_for_another_phone_it_is_left_out(self):
        self.rx.set_accept_passphrase_over_network(True, PIN)
        other = self._receiver("other.json")
        b = self._push(passphrase="new pass",
                       phones=[(other.phone_id, other.x_pub)])[other.phone_id]
        offer = self.rx.receive(b, ss.ROUTE_NETWORK)
        self.assertNotIn("passphrase", offer["settings"])
        self.assertIn("another phone", offer["notes"][0])

    def test_a_plain_passphrase_over_the_network_is_never_taken(self):
        self.rx.set_accept_passphrase_over_network(True, PIN)
        qr_text = self._push(passphrase="new pass")["qr"]
        offer = self.rx.receive(qr_text, ss.ROUTE_NETWORK)
        self.assertNotIn("passphrase", offer["settings"])

    def test_a_bundle_left_with_nothing_is_refused(self):
        _, b = self.host.bundles({}, phones=[(self.rx.phone_id, self.rx.x_pub)],
                                 passphrase="new pass", switch_in_s=None)
        with self.assertRaisesRegex(Refused, "Nothing in this bundle"):
            self.rx.receive(b[self.rx.phone_id], ss.ROUTE_NETWORK)


class SwitchTest(Base):
    def setUp(self):
        super().setUp()
        self.rx.set_pin(PIN)

    def test_an_approved_change_waits_for_its_time(self):
        self.rx.receive(self._net(switch_in_s=120), ss.ROUTE_NETWORK)
        self.assertEqual(self.rx.approve(self.keys.host_id, PIN), "scheduled")
        self.clock.t += 119
        self.assertEqual(self.rx.tick(), [])
        self.assertEqual(self.radio.applied, [])
        self.clock.t += 1
        self.assertEqual(self.rx.tick()[0][0], "switched")
        self.assertEqual(self.radio.settings["lora_freq_mhz"], "906.875")

    def test_a_late_approval_switches_at_once(self):
        self.rx.receive(self._net(switch_in_s=120), ss.ROUTE_NETWORK)
        self.clock.t += 600
        self.assertEqual(self.rx.approve(self.keys.host_id, PIN), "applied")

    def test_a_scheduled_change_survives_a_restart(self):
        self.rx.receive(self._net(switch_in_s=120), ss.ROUTE_NETWORK)
        self.rx.approve(self.keys.host_id, PIN)
        self.clock.t += 121
        self.assertEqual(self._receiver().tick()[0][0], "switched")

    def test_a_change_the_radio_refuses_is_rolled_back(self):
        self.radio.refuse = "lora_freq_mhz"
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        outcome = self.rx.approve(self.keys.host_id, PIN)
        self.assertTrue(outcome.startswith("failed: lora_freq_mhz refused"), outcome)
        self.assertEqual(self.radio.settings["lora_preset"], "Short Slow")

    def test_the_undo_button(self):
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        self.rx.approve(self.keys.host_id, PIN)
        self.assertEqual(self.rx.undo(), [])
        self.assertEqual(self.radio.settings["lora_freq_mhz"], "915.000")
        self.assertEqual(self.rx.undo(), ["Nothing to undo"])


class UnsupportedAndUndoScopeTest(Base):
    def test_a_setting_this_build_cannot_apply_is_left_out_with_a_note(self):
        rx = ss.Receiver(self._p("u.json"), self.radio.current, self.radio.apply,
                         clock=self.clock,
                         pin_iterations=1000, unsupported=("fm_profile",))
        _, b = self.host.bundles(dict(NEW, fm_profile="wide"),
                                 phones=[(rx.phone_id, rx.x_pub)], switch_in_s=None)
        offer = rx.receive(b[rx.phone_id], ss.ROUTE_NETWORK)
        self.assertNotIn("fm_profile", offer["settings"])
        self.assertIn("FM tone profile was left out", offer["notes"][0])
        self.assertIn("lora_preset", offer["settings"])

    def test_undo_puts_back_only_what_the_change_touched(self):
        self.rx.set_pin(PIN)
        self.rx.receive(self._net(settings={"coverage": {"fm_mhz": "446.0"}}), ss.ROUTE_NETWORK)
        self.rx.approve(self.keys.host_id, PIN)
        self.radio.settings["lora_tx_dbm"] = 10          # changed by hand since
        self.radio.settings["coverage"]["fm_w"] = "4"
        self.assertEqual(self.rx.undo(), [])
        self.assertEqual(self.radio.applied[-1], {"coverage": {"fm_mhz": "146.000"}})
        self.assertEqual(self.radio.settings["lora_tx_dbm"], 10)
        self.assertEqual(self.radio.settings["coverage"]["fm_w"], "4")


class NoAutomaticUndoTest(Base):
    """An applied change stays until Undo is pressed (operator, 2026-10-23).
    There used to be an automatic undo after 300 s of hearing nobody."""

    def test_a_change_is_never_switched_back_by_itself(self):
        self.rx.set_pin(PIN)
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        self.assertEqual(self.rx.approve(self.keys.host_id, PIN), "applied")
        for _ in range(10):                  # an hour of hearing nobody
            self.clock.t += 360
            self.assertEqual(self.rx.tick(), [])
        self.assertEqual(self.radio.settings["lora_freq_mhz"], "906.875")
        # Undo by hand still works.
        self.assertEqual(self.rx.undo(), [])
        self.assertEqual(self.radio.settings["lora_freq_mhz"], "915.000")

    def test_a_state_file_from_before_still_loads_and_undoes(self):
        self.rx.set_pin(PIN)
        self.rx.receive(self._net(), ss.ROUTE_NETWORK)
        self.rx.approve(self.keys.host_id, PIN)
        self.rx._s["undo"]["armed"] = True   # as an older build saved it
        self.rx._save()
        rx = self._receiver()
        self.clock.t += 3600
        self.assertEqual(rx.tick(), [])
        self.assertEqual(rx.undo(), [])
        self.assertEqual(self.radio.settings["lora_freq_mhz"], "915.000")


if __name__ == "__main__":
    unittest.main()
