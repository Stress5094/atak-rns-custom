"""settings_link: settings sharing wired to the bridge's real setters
(set_radio_config, set_mesh_passphrase) and to Kotlin's coverage values,
end to end through the JSON entry points the screens call."""

import json
import os
import tempfile
import unittest

import rns_stub

rns_stub.install()

import rns_bridge as rb        # noqa: E402
import settings_link as sl     # noqa: E402
import settings_share as ss    # noqa: E402

PIN = "4821"
CODE500 = "4T-1000/500-500Bd-H1024.2048"
KOTLIN = json.dumps({"coverage": {"fm_mhz": "146.000", "fm_w": "10", "tx_height_m": "1.5"}})


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self._saved = {n: getattr(rb, n) for n in
                       ("_RADIO_CONFIG_PATH", "_MESH_PASSPHRASE_PATH", "_radio_config",
                        "_mesh_passphrase", "_mesh", "_lora_iface", "_node_name")}
        self._saved_sl = {n: getattr(sl, n) for n in
                          ("_RECEIVER_PATH", "_HOST_KEY_PATH", "_HOST_REV_PATH")}
        rb._RADIO_CONFIG_PATH = os.path.join(d, "radio")
        rb._MESH_PASSPHRASE_PATH = os.path.join(d, "pass")
        rb._radio_config = None
        rb._mesh = None
        rb._lora_iface = None
        rb._node_name = "OnePlus KB2007"
        # The receiver/host singleton and its paths are swapped under _lock, the
        # same lock _get_receiver()/_get_host() take. A network thread left
        # over from a previous test can otherwise create a receiver against the
        # real ~/.atakrns_settings_share.json (which may hold a PIN) and hand it
        # to this test's set_pin(), flaking it ("Wrong PIN" / "Set a PIN first").
        with sl._lock:
            sl._RECEIVER_PATH = os.path.join(d, "share.json")
            sl._HOST_KEY_PATH = os.path.join(d, "hostkey.json")
            sl._HOST_REV_PATH = os.path.join(d, "hostrev.json")
            sl._receiver = sl._host = None
        sl._fm.clear()
        sl._fm_out = None
        self._set_radio("Short Slow", "915.000", 22, "old pass")

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(rb, n, v)
        # Restore the shared receiver/host paths AND drop the singletons under
        # the same lock _get_receiver()/_get_host() take, so a leftover network
        # thread cannot create a receiver against the restored (real) path and
        # leak it into the next test (see setUp).
        with sl._lock:
            for n, v in self._saved_sl.items():
                setattr(sl, n, v)
            sl._receiver = sl._host = None
        sl._fm.clear()
        sl._fm_out = None
        import fm_link, fm_config
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)
        self._tmp.cleanup()

    def _set_radio(self, preset, mhz, dbm, passphrase):
        self.assertEqual(rb.set_radio_config(preset, mhz, dbm), "")
        rb._mesh_passphrase = passphrase

    def _j(self, s):
        out = json.loads(s)
        self.assertNotIn("error", out, out)
        return out

    def _share_team_settings(self, **opts):
        """The host phone's settings, as its QR -- then this phone goes back
        to its own, so the scan has something to change."""
        self._set_radio("Medium Slow", "906.875", 20, "team pass")
        team_cov = json.dumps({"coverage": {"fm_mhz": "446.000", "fm_w": "4",
                                            "tx_height_m": "6"}})
        qr = self._j(sl.share_qr(json.dumps(opts or {"lora": True, "coverage": True,
                                                     "passphrase": True}), team_cov))
        self._set_radio("Short Slow", "915.000", 22, "old pass")
        return qr


class SettingsLinkTest(_Fixture):
    def test_scan_approve_apply_and_undo_end_to_end(self):
        qr = self._share_team_settings()
        self.assertEqual(qr["host_name"], "OnePlus KB2007")
        offer = self._j(sl.receive(qr["text"], "qr", KOTLIN))
        self.assertEqual(offer["outcome"], "pending")
        changed = {c["key"]: c["new"] for c in offer["changes"]}
        self.assertEqual(changed["lora_freq_mhz"], "906.875")
        self.assertEqual(changed["passphrase"], "(new passphrase)")
        self.assertEqual(changed["coverage.fm_mhz"], "446.000")

        # No PIN yet: refused, nothing changed.
        self.assertIn("Set a PIN", json.loads(sl.approve(offer["host"], PIN, False, KOTLIN))["error"])
        self.assertEqual(rb._load_radio_config()["preset"], "Short Slow")

        self._j(sl.set_pin(PIN))
        out = self._j(sl.approve(offer["host"], PIN, False, KOTLIN))
        self.assertEqual(out["outcome"], "applied")
        cfg = rb._load_radio_config()
        self.assertEqual((cfg["preset"], cfg["frequency_hz"], cfg["txpower_dbm"]),
                         ("Medium Slow", 906875000, 20))
        self.assertEqual(rb._load_passphrase()[0], "team pass")
        # Kotlin owns the coverage settings: handed back to save.
        self.assertEqual(out["coverage"], {"fm_mhz": "446.000", "fm_w": "4", "tx_height_m": "6"})
        with open(rb._RADIO_CONFIG_PATH) as f:
            self.assertEqual(json.load(f)["preset"], "Medium Slow")   # persisted

        back = self._j(sl.undo(json.dumps({"coverage": out["coverage"]})))
        self.assertEqual(back["problems"], [])
        self.assertEqual(rb._load_radio_config()["preset"], "Short Slow")
        self.assertEqual(rb._load_passphrase()[0], "old pass")
        self.assertEqual(back["coverage"]["fm_mhz"], "146.000")

    def test_trust_on_approval_then_the_next_change_applies_by_itself(self):
        self._j(sl.set_pin(PIN))
        qr = self._share_team_settings(lora=True)
        offer = self._j(sl.receive(qr["text"], "qr", KOTLIN))
        self._j(sl.approve(offer["host"], PIN, True, KOTLIN))
        self.assertEqual([t["name"] for t in self._j(sl.status(KOTLIN))["trusted"]],
                         ["OnePlus KB2007"])
        self._set_radio("Long Fast", "906.875", 20, "team pass")
        qr2 = self._j(sl.share_qr(json.dumps({"lora": True}), KOTLIN))
        self._set_radio("Short Slow", "915.000", 22, "team pass")
        self.assertEqual(self._j(sl.receive(qr2["text"], "qr", KOTLIN))["outcome"], "applied")
        self.assertEqual(rb._load_radio_config()["preset"], "Long Fast")

    def test_a_wrong_pin_on_approval_trusts_nothing_and_applies_nothing(self):
        self._j(sl.set_pin(PIN))
        qr = self._share_team_settings(lora=True)
        offer = self._j(sl.receive(qr["text"], "qr", KOTLIN))
        self.assertIn("Wrong PIN", json.loads(sl.approve(offer["host"], "0000", True, KOTLIN))["error"])
        self.assertEqual(self._j(sl.status(KOTLIN))["trusted"], [])
        self.assertEqual(rb._load_radio_config()["preset"], "Short Slow")

    # -- FM settings (Phase C) ------------------------------------------------

    @staticmethod
    def _kotlin(profile, spec=None, vox=False, clear=None):
        import fm_custom
        if spec is None and profile == "custom":
            spec = fm_custom.parse_net_code(CODE500).to_json()
        fm = {"profile": profile, "custom_spec": spec or "", "vox": vox}
        if clear is not None:
            fm["clear"] = clear
        return json.dumps({"coverage": {"fm_mhz": "146.000", "fm_w": "10",
                                        "tx_height_m": "1.5"},
                           "fm": fm})

    def _fm_qr(self, profile, spec=None, clear=None):
        qr = self._j(sl.share_qr(json.dumps({"fm": True}),
                                 self._kotlin(profile, spec, clear=clear)))
        sl._fm.clear()
        return qr

    # -- "send FM unencrypted" travels with the FM group -----------------------

    def _restore_fm_clear(self):
        saved = rb._fm_clear
        self.addCleanup(rb.set_fm_clear, saved)

    def test_fm_unencrypted_is_shared_and_applied_live(self):
        self._restore_fm_clear()
        rb.set_fm_clear(False)
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("fast", clear=True)
        self.assertIn("fm_clear", [i["key"] for i in qr["items"]])
        here = self._kotlin("fast", clear=False)
        offer = self._j(sl.receive(qr["text"], "qr", here))
        self.assertEqual({c["key"]: c["new"] for c in offer["changes"]},
                         {"fm_clear": True})
        self.assertTrue(all(c["network"] for c in offer["changes"]))
        out = self._j(sl.approve(offer["host"], PIN, False, here))
        self.assertEqual(out["outcome"], "applied")
        self.assertTrue(rb._fm_clear)            # running now, no restart
        self.assertIs(out["fm"]["clear"], True)  # and handed back to save
        back = self._j(sl.undo(self._kotlin("fast", clear=True)))
        self.assertEqual(back["problems"], [])
        self.assertFalse(rb._fm_clear)
        self.assertIs(back["fm"]["clear"], False)

    def test_fm_unencrypted_off_is_shared_too(self):
        self._restore_fm_clear()
        rb.set_fm_clear(True)
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("fast", clear=False)
        here = self._kotlin("fast", clear=True)
        offer = self._j(sl.receive(qr["text"], "qr", here))
        self._j(sl.approve(offer["host"], PIN, False, here))
        self.assertFalse(rb._fm_clear)

    def test_a_host_without_the_setting_shares_no_fm_clear(self):
        qr = self._fm_qr("fast")
        self.assertNotIn("fm_clear", [i["key"] for i in qr["items"]])

    def test_a_phone_too_old_for_fm_clear_says_so(self):
        self._restore_fm_clear()
        rb.set_fm_clear(False)
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("fast", clear=True)
        here = self._kotlin("fast")          # Kotlin sends no "clear"
        offer = self._j(sl.receive(qr["text"], "qr", here))
        out = json.loads(sl.approve(offer["host"], PIN, False, here))
        self.assertIn("too old to send FM unencrypted", json.dumps(out))
        self.assertFalse(rb._fm_clear)

    def test_a_non_boolean_fm_clear_is_refused(self):
        _, b = sl._get_host().bundles({"fm_clear": "yes"}, switch_in_s=None)
        self.assertIn("not understood", json.loads(
            sl.receive(b["qr"], "qr", self._kotlin("fast", clear=False)))["error"])

    def test_a_custom_fm_setting_is_shared_and_applied_live(self):
        import fm_link
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("custom")
        here = self._kotlin("fast")
        offer = self._j(sl.receive(qr["text"], "qr", here))
        self.assertEqual({c["key"]: c["new"] for c in offer["changes"]},
                         {"fm_profile": "custom", "fm_custom": CODE500})
        out = self._j(sl.approve(offer["host"], PIN, False, here))
        self.assertEqual(out["outcome"], "applied")
        # Running now, without a restart...
        self.assertEqual(fm_link.net_code(), CODE500)
        # ...and handed back for Kotlin to save.
        self.assertEqual(out["fm"]["profile"], "custom")
        import fm_custom
        self.assertEqual(fm_custom.net_code(fm_custom.link_config(
            fm_custom.CustomSpec.from_json(out["fm"]["custom_spec"]))), CODE500)

    def test_only_the_net_settings_travel(self):
        """This phone's VOX keying and lead-in are its cable's business and
        must survive a shared net code."""
        import fm_custom
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("custom")
        mine = fm_custom.CustomSpec.from_preset("narrow")
        mine.keying, mine.lead_in_s, mine.repeats = "vox", 1.2, 2
        here = self._kotlin("custom", mine.to_json())
        offer = self._j(sl.receive(qr["text"], "qr", here))
        out = self._j(sl.approve(offer["host"], PIN, False, here))
        got = fm_custom.CustomSpec.from_json(out["fm"]["custom_spec"])
        self.assertEqual((got.keying, got.lead_in_s, got.repeats), ("vox", 1.2, 2))
        self.assertEqual((got.symbol_rate_bd, got.first_tone_hz, got.tone_spacing_hz),
                         (500, 1000, 500))

    def test_a_preset_is_shared_too_and_keeps_this_phones_vox_choice(self):
        import fm_link, fm_config
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("narrow")
        here = self._kotlin("custom", vox=True)
        offer = self._j(sl.receive(qr["text"], "qr", here))
        self._j(sl.approve(offer["host"], PIN, False, here))
        self.assertEqual(fm_link.profile_name(), "narrow")
        self.assertEqual(fm_link.config().keying, fm_config.KEYING_VOX)

    def test_an_unusable_net_code_changes_nothing(self):
        import fm_link
        self._j(sl.set_pin(PIN))
        _, b = sl._get_host().bundles({"fm_profile": "custom",
                                       "fm_custom": "4T-1000/100-400Bd-H1024.2048"},
                                      switch_in_s=None)
        here = self._kotlin("fast")
        before = fm_link.net_code()
        offer = self._j(sl.receive(b["qr"], "qr", here))
        out = json.loads(sl.approve(offer["host"], PIN, False, here))
        self.assertNotIn("fm", out)
        self.assertEqual(fm_link.net_code(), before)
        self.assertIsNone(sl._get_receiver()._s["undo"])

    def test_undo_puts_the_old_fm_setting_back_live(self):
        import fm_link
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("custom")
        here = self._kotlin("fast")
        offer = self._j(sl.receive(qr["text"], "qr", here))
        out = self._j(sl.approve(offer["host"], PIN, False, here))
        self.assertEqual(fm_link.net_code(), CODE500)
        back = self._j(sl.undo(self._kotlin("custom", out["fm"]["custom_spec"])))
        self.assertEqual(back["problems"], [])
        self.assertEqual(fm_link.profile_name(), "fast")
        self.assertEqual(back["fm"]["profile"], "fast")

    def test_a_phone_whose_kotlin_sends_no_fm_cannot_share_or_take_it(self):
        self.assertIn("not known", json.loads(
            sl.share_qr(json.dumps({"fm": True}), KOTLIN))["error"])
        self._j(sl.set_pin(PIN))
        qr = self._fm_qr("custom")
        offer = self._j(sl.receive(qr["text"], "qr", KOTLIN))
        out = json.loads(sl.approve(offer["host"], PIN, False, KOTLIN))
        self.assertIn("too old", json.dumps(out))

    def test_a_power_above_this_radio_is_noted_and_clamped(self):
        self._j(sl.set_pin(PIN))
        host = sl._get_host()
        top = rb._txpower_max_dbm()
        _, b = host.bundles({"lora_tx_dbm": top + 8}, switch_in_s=None)
        offer = self._j(sl.receive(b["qr"], "qr", KOTLIN))
        self.assertTrue(any("at most %d dBm" % top in n for n in offer["notes"]), offer["notes"])
        self.assertEqual(self._j(sl.approve(offer["host"], PIN, False, KOTLIN))["outcome"], "applied")
        self.assertEqual(rb._load_radio_config()["txpower_dbm"], top)

    def test_nothing_the_screens_see_contains_the_passphrase(self):
        qr = self._share_team_settings()
        offer = sl.receive(qr["text"], "qr", KOTLIN)
        status = sl.status(KOTLIN)
        for text in (json.dumps(qr["items"]), offer, status):
            self.assertNotIn("team pass", text)
            self.assertNotIn("old pass", text)

    def test_errors_come_back_as_json_not_exceptions(self):
        self.assertIn("error", json.loads(sl.receive("not a bundle", "qr", KOTLIN)))
        self.assertIn("error", json.loads(sl.share_qr("{}", KOTLIN)))
        self.assertIn("error", json.loads(sl.approve("nobody", PIN, False, KOTLIN)))


class FakeClient(object):
    def __init__(self):
        self.reports = []

    def report(self, host, rev, state, detail=""):
        self.reports.append((host, rev, state))

    def hosts(self):
        return []


class OverWifiTest(_Fixture):
    """A bundle arriving over WiFi, outside any call from Kotlin."""

    def setUp(self):
        super(OverWifiTest, self).setUp()
        self._saved_net = (sl._net_client, sl._events, sl._coverage_out)
        sl._net_client = FakeClient()
        sl._events = []
        sl._coverage_out = None
        sl._coverage.clear()
        sl._coverage.update(json.loads(KOTLIN)["coverage"])

    def tearDown(self):
        sl._net_client, sl._events, sl._coverage_out = self._saved_net
        super(OverWifiTest, self).tearDown()

    def _network_bundle(self, settings):
        host = sl._get_host()
        rev, b = host.bundles(settings, switch_in_s=None)
        return host.keys.host_id, rev, b["qr"]

    def test_an_untrusted_offer_reaches_kotlin_on_the_next_tick(self):
        host, rev, text = self._network_bundle({"lora_tx_dbm": 17})
        self.assertEqual(sl._on_network_bundle(host, text)[:2], (rev, "pending"))
        events = self._j(sl.tick(KOTLIN))["events"]
        self.assertEqual(events[0][0], "offer")
        self.assertEqual(json.loads(events[0][1])["rev"], rev)
        self.assertEqual(self._j(sl.tick(KOTLIN))["events"], [])     # handed over once

    def test_a_trusted_coverage_change_survives_kotlins_stale_copy(self):
        self._j(sl.set_pin(PIN))
        host_id = sl._get_host().keys.host_id
        sl._get_receiver().trust_host(host_id, ss._b64(sl._get_host().keys.ed_pub),
                                      "OnePlus KB2007", PIN)
        _, rev, text = self._network_bundle({"coverage": {"fm_mhz": "446.000"}})
        self.assertEqual(sl._on_network_bundle(host_id, text)[1], "applied")
        # Kotlin's next call still carries the OLD value: it must get the new
        # one back to save, and Python must not take the old one back.
        out = self._j(sl.tick(KOTLIN))
        self.assertEqual(out["coverage"]["fm_mhz"], "446.000")
        self.assertEqual(sl._coverage["fm_mhz"], "446.000")
        self.assertNotIn("coverage", self._j(sl.tick(KOTLIN)))

    def test_approve_and_reject_are_reported_to_the_host(self):
        self._j(sl.set_pin(PIN))
        host, rev, text = self._network_bundle({"lora_tx_dbm": 17})
        sl._on_network_bundle(host, text)
        self._j(sl.approve(host, PIN, False, KOTLIN))
        _, rev2, text2 = self._network_bundle({"lora_tx_dbm": 18})
        sl._on_network_bundle(host, text2)
        self._j(sl.reject(host))
        self.assertEqual(sl._net_client.reports,
                         [(host, rev, "applied"), (host, rev2, "rejected")])

    def test_a_replayed_bundle_is_refused_with_a_reason_for_the_roster(self):
        self._j(sl.set_pin(PIN))
        host, rev, text = self._network_bundle({"lora_tx_dbm": 17})
        sl._on_network_bundle(host, text)
        self._j(sl.reject(host))
        r, state, detail = sl._on_network_bundle(host, text)
        self.assertEqual((r, state), (rev, "refused"))
        self.assertIn("Already decided", detail)


class ServersTest(_Fixture):
    """Servers added by their code, through the real client and a real host
    on this machine (step 4)."""

    def setUp(self):
        super(ServersTest, self).setUp()
        import settings_net as sn
        self.sn = sn
        self._saved_net = (sl._net_client, sl._net_server, sl._events, sl._NET_PATH,
                           sn.RECONNECT_S)
        sn.RECONNECT_S = 0.05
        sl._net_client = sl._net_server = None
        sl._events = []
        sl._NET_PATH = os.path.join(self._tmp.name, "net.json")
        self._j(sl.net_set_listen("false"))         # no WiFi listening on a desktop
        self.team = ss.Host(ss.HostKeys.generate(), "Team server",
                            os.path.join(self._tmp.name, "team_rev.json"))
        self.server = sn.HostServer(self.team, port=0, bind_addr="127.0.0.1", beacon=False)
        self.server.start()
        self.card = sn.make_card(self.team.keys, "Team server", "127.0.0.1", self.server.port)

    def tearDown(self):
        if sl._net_client is not None:
            sl._net_client.stop()
        self.server.stop()
        (sl._net_client, sl._net_server, sl._events, sl._NET_PATH,
         self.sn.RECONNECT_S) = self._saved_net
        super(ServersTest, self).tearDown()

    def _wait(self, cond, timeout=5.0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def _servers(self):
        return self._j(sl.status(KOTLIN))["servers"]

    def test_a_server_from_its_code_is_kept_and_reached_with_wifi_listening_off(self):
        self.assertIsNone(sl._net_client)              # nothing to listen for, nothing listed
        out = self._j(sl.server_add(self.card))
        self.assertEqual(out["server"]["host"], self.team.keys.host_id)
        self.assertTrue(self._wait(lambda: self._servers()[0]["connected"]))
        st = self._j(sl.status(KOTLIN))
        self.assertFalse(st["listening"])
        self.assertEqual((st["servers"][0]["name"], st["servers"][0]["pinned"]),
                         ("Team server", True))
        with open(sl._NET_PATH) as f:
            self.assertEqual(json.load(f)["servers"][0]["host"], self.team.keys.host_id)

    def test_a_push_from_the_server_is_offered_and_the_answer_reaches_its_roster(self):
        self._j(sl.server_add(self.card))
        phone = sl._get_receiver().phone_id
        self.assertTrue(self._wait(lambda: any(e["phone"] == phone
                                               for e in self.server.roster())))
        rev = self.server.push({"lora_tx_dbm": 17}, switch_in_s=None)
        self.assertTrue(self._wait(lambda: sl._get_receiver().pending()))
        events = self._j(sl.tick(KOTLIN))["events"]
        self.assertEqual(events[0][0], "offer")
        self.assertEqual(json.loads(events[0][1])["route"], "network")
        self._j(sl.set_pin(PIN))
        self.assertEqual(self._j(sl.approve(self.team.keys.host_id, PIN, kotlin_json=KOTLIN))
                         ["outcome"], "applied")
        self.assertTrue(self._wait(lambda: self.server.roster()[0]["state"] == "applied"))
        self.assertEqual((self.server.roster()[0]["rev"], rb._load_radio_config()["txpower_dbm"]),
                         (rev, 17))

    def test_removing_the_last_server_with_listening_off_stops_the_client(self):
        self._j(sl.server_add(self.card))
        self.assertIsNotNone(sl._net_client)
        self.assertEqual(self._j(sl.server_remove("127.0.0.1", self.server.port))["servers"], [])
        self.assertIsNone(sl._net_client)

    def test_a_server_typed_by_address_and_a_bad_code(self):
        out = self._j(sl.server_add("127.0.0.1:%d" % self.server.port))
        self.assertIsNone(out["server"]["host"])                 # no key to check against
        self.assertTrue(self._wait(lambda: self._servers()[0]["connected"]))
        self.assertEqual(self._servers()[0]["host"], self.team.keys.host_id)
        # ...until its first answer, which is kept: required from now on.
        self.assertTrue(self._wait(lambda: self._servers()[0]["first_use"]))
        self.assertEqual(sl._net_prefs()["servers"][0]["host"], self.team.keys.host_id)
        self.assertIn("error", json.loads(sl.server_add("RTAKSRV1.garbage")))


class FoundHostsTest(_Fixture):
    """A host heard on WiFi: asked about, connected only when approved, and
    the answer kept."""

    class Client(object):
        def __init__(self):
            self.found_ = [{"host": "aaaa000011112222", "name": "Laptop server",
                            "addr": "192.168.77.106", "port": 47812}]
            self.approved, self.forgotten = [], []

        def found(self):
            return [f for f in self.found_ if f["host"] not in self.approved]

        def hosts(self):
            return [dict(f, manual=False, connected=True, problem="")
                    for f in self.found_ if f["host"] in self.approved]

        def approve_host(self, h):
            self.approved.append(h)
            return True

        def forget_host(self, h):
            self.forgotten.append(h)
            self.approved.remove(h)
            return True

        def report(self, *a):
            pass

    def setUp(self):
        super(FoundHostsTest, self).setUp()
        self._saved_net = (sl._net_client, sl._events, sl._NET_PATH)
        sl._net_client = self.Client()
        sl._events = []
        sl._NET_PATH = os.path.join(self._tmp.name, "net.json")

    def tearDown(self):
        sl._net_client, sl._events, sl._NET_PATH = self._saved_net
        super(FoundHostsTest, self).tearDown()

    def test_a_host_heard_is_asked_about_and_connected_only_when_approved(self):
        sl._found_host("aaaa000011112222", "Laptop server", "192.168.77.106")
        ev = self._j(sl.tick(KOTLIN))["events"]
        self.assertEqual(ev[0][0], "host_found")
        self.assertEqual(json.loads(ev[0][1])["name"], "Laptop server")
        st = self._j(sl.status(KOTLIN))
        self.assertEqual([f["host"] for f in st["found"]], ["aaaa000011112222"])
        self.assertEqual(st["wifi_hosts"], [])
        self._j(sl.host_connect("aaaa000011112222"))
        self.assertEqual(sl._net_client.approved, ["aaaa000011112222"])
        st = self._j(sl.status(KOTLIN))
        self.assertEqual((st["found"], st["wifi_hosts"][0]["name"], st["wifi_hosts"][0]["connected"]),
                         ([], "Laptop server", True))
        with open(sl._NET_PATH) as f:
            self.assertIn("aaaa000011112222", json.load(f)["approved_hosts"])

    def test_an_ignored_host_is_not_asked_about_again_and_forget_disconnects(self):
        self._j(sl.host_ignore("aaaa000011112222"))
        sl._found_host("aaaa000011112222", "Laptop server", "192.168.77.106")
        self.assertEqual(self._j(sl.tick(KOTLIN))["events"], [])
        self.assertEqual(self._j(sl.status(KOTLIN))["found"], [])
        self._j(sl.host_connect("aaaa000011112222"))        # changed their mind
        self.assertEqual(sl._net_prefs()["declined_hosts"], {})
        self._j(sl.host_forget("aaaa000011112222"))
        self.assertEqual((sl._net_client.forgotten, sl._net_prefs()["approved_hosts"]),
                         (["aaaa000011112222"], {}))


    def test_an_ignored_host_can_be_unignored_and_is_asked_about_again(self):
        sl._found_host("aaaa000011112222", "Laptop server", "192.168.77.106")
        sl.tick(KOTLIN)
        self._j(sl.host_ignore("aaaa000011112222"))
        st = self._j(sl.status(KOTLIN))
        self.assertEqual((st["found"], st["ignored"]),
                         ([], [{"host": "aaaa000011112222", "name": "Laptop server"}]))
        r = self._j(sl.host_unignore("aaaa000011112222"))
        self.assertEqual((r["unignored"], r["heard"], r["ignored"]),
                         ("aaaa000011112222", True, []))
        # Heard right now, so it is asked about at once -- and only asked:
        # undoing Ignore must not connect by itself.
        ev = self._j(sl.tick(KOTLIN))["events"]
        self.assertEqual([e[0] for e in ev], ["host_found"])
        st = self._j(sl.status(KOTLIN))
        self.assertEqual(([f["host"] for f in st["found"]], st["wifi_hosts"], st["ignored"]),
                         (["aaaa000011112222"], [], []))
        self.assertEqual(sl._net_client.approved, [])
        self.assertEqual(sl._net_prefs()["approved_hosts"], {})

    def test_unignoring_a_host_not_heard_now_waits_and_unknown_ids_are_refused(self):
        sl._net_client.found_ = []
        self._j(sl.host_ignore("bbbb000011112222"))
        r = self._j(sl.host_unignore("bbbb000011112222"))
        self.assertFalse(r["heard"])
        self.assertEqual(self._j(sl.tick(KOTLIN))["events"], [])
        self.assertIn("not ignored", json.loads(sl.host_unignore("bbbb000011112222"))["error"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# A BUNDLE MUST STILL APPLY WITH NO LORA RADIO CONNECTED (fixed 2026-11-12).
#
# Every group in settings_link._apply is applied whole or not at all, and
# settings_share.Receiver._apply_offer puts the WHOLE bundle back if any group
# reports a problem. So while the radio group counted "there is no radio" as a
# problem, a phone with no radio attached could not be given the group
# passphrase -- which needs no radio whatsoever.
#
# Measured on the Pixel 2026-11-12: two bundles set a new key, the radio group
# raised PortNotOpenError('Attempting to use a port that is not open'), and the
# put-back restored the previous key. The screen said "Not applied -- failed:
# Radio: Could not apply ... Nothing was changed", which was true, and left the
# two bench phones on different keys with no way to converge them.
#
# The tests below use an interface that EXISTS but cannot be written to, which
# is the real state and the one the old `_lora_iface is None` guard missed --
# the rest of this module runs with _lora_iface None, so none of it covered
# this.
# ---------------------------------------------------------------------------


class PortNotOpenError(Exception):
    """Named to match pyserial's, which is what rns_bridge._is_port_gone
    matches on. rns_stub's `serial` has no exception hierarchy to inherit."""


class _FakeRNode(object):
    """Enough of RNS's RNodeInterface for set_radio_config's live path.

    `online` and `serial.is_open` are the two independent things that decide
    whether a write can land -- see rns_bridge._radio_port_live, and the
    2026-11-10 bench note about a radio marked online over a closed port.
    """

    def __init__(self, online=True, port_open=True, fail_on_write=False):
        self.online = online
        self.reconnecting = False
        self.detached = False
        self.serial = type("_Port", (), {"is_open": port_open})()
        self.fail_on_write = fail_on_write
        self.frequency = 915000000
        self.bandwidth = 125000
        self.txpower = 22
        self.sf = 8
        self.cr = 5
        self.bitrate = 1000
        self.r_frequency = None
        self.r_bandwidth = None
        self.r_txpower = None
        self.r_sf = None
        self.r_cr = None
        self.writes = []

    def _write(self, what):
        if self.fail_on_write:
            raise PortNotOpenError("Attempting to use a port that is not open")
        self.writes.append(what)

    # The radio echoes each parameter straight back, so _await_radio_readback
    # is satisfied without a real wait.
    def setFrequency(self):
        self._write("frequency")
        self.r_frequency = self.frequency

    def setBandwidth(self):
        self._write("bandwidth")
        self.r_bandwidth = self.bandwidth

    def setTXPower(self):
        self._write("txpower")
        self.r_txpower = self.txpower

    def setSpreadingFactor(self):
        self._write("sf")
        self.r_sf = self.sf

    def setCodingRate(self):
        self._write("cr")
        self.r_cr = self.cr

    def validateRadioState(self):
        return True

    def updateBitrate(self):
        self.bitrate = 1200


class NoRadioAttachedTest(_Fixture):

    def use_radio(self, **kw):
        iface = _FakeRNode(**kw)
        rb._lora_iface = iface
        return iface

    # -- what set_radio_config itself answers ----------------------------

    def test_a_live_radio_still_gets_the_settings_pushed(self):
        # The control: without this, every assertion below could be passing
        # because nothing is ever pushed to anything.
        iface = self.use_radio()
        self.assertEqual(rb.set_radio_config("Medium Slow", "906.875", 20), "")
        self.assertEqual(iface.writes,
                         ["frequency", "bandwidth", "txpower", "sf", "cr"])
        self.assertEqual(iface.frequency, 906875000)

    def test_an_offline_radio_saves_without_pushing(self):
        iface = self.use_radio(online=False)
        self.assertEqual(rb.set_radio_config("Medium Slow", "906.875", 20),
                         rb.RADIO_SAVED_NO_RADIO)
        self.assertEqual(iface.writes, [])
        self.assertEqual(rb._load_radio_config()["frequency_hz"], 906875000)

    def test_online_over_a_closed_port_saves_without_pushing(self):
        # The state the port watchdog exists for, and the one that produced
        # the Pixel's PortNotOpenError.
        iface = self.use_radio(online=True, port_open=False)
        self.assertEqual(rb.set_radio_config("Medium Slow", "906.875", 20),
                         rb.RADIO_SAVED_NO_RADIO)
        self.assertEqual(iface.writes, [])

    def test_a_port_that_dies_mid_apply_is_saved_not_failed(self):
        # online and open at the check, gone by the write: this link drops
        # constantly over BLE, so the window is real.
        self.use_radio(online=True, port_open=True, fail_on_write=True)
        self.assertEqual(rb.set_radio_config("Medium Slow", "906.875", 20),
                         rb.RADIO_SAVED_NO_RADIO)
        self.assertEqual(rb._load_radio_config()["preset"], "Medium Slow")

    def test_the_settings_are_left_on_the_interface_for_the_reconnect(self):
        # RNS's reconnect_port -> configure_device -> initRadio sends these
        # five attributes, so a radio connecting later comes up on the new
        # settings. This is what makes RADIO_SAVED_NO_RADIO's wording true
        # rather than a hopeful guess.
        iface = self.use_radio(online=False)
        rb.set_radio_config("Medium Slow", "906.875", 20)
        self.assertEqual(iface.frequency, 906875000)
        self.assertEqual(iface.txpower, 20)
        sf, bw, cr = __import__("radio").preset_params("Medium Slow")
        self.assertEqual((iface.sf, iface.bandwidth, iface.cr), (sf, bw, cr))

    def test_a_radio_that_refuses_the_settings_is_still_a_failure(self):
        # The fix must not swallow a real refusal: that is a different fault
        # and the operator has to hear about it.
        iface = self.use_radio()
        iface.validateRadioState = lambda: False
        problem = rb.set_radio_config("Medium Slow", "906.875", 20)
        self.assertNotEqual(problem, rb.RADIO_SAVED_NO_RADIO)
        self.assertIn("did not accept", problem)

    def test_a_radio_that_never_confirms_is_not_saved_no_radio(self):
        # Distinct from "no radio": here the KISS commands DID go out and are
        # not rolled back, so the radio may well be on the new settings. The
        # operator has to be told the confirmation never arrived -- calling it
        # "saved, will be sent later" would describe the opposite situation.
        # Patched rather than waited out: the real readback allows 8 s.
        self.use_radio()
        saved = rb._await_radio_readback
        rb._await_radio_readback = lambda iface, **kw: False
        try:
            problem = rb.set_radio_config("Medium Slow", "906.875", 20)
        finally:
            rb._await_radio_readback = saved
        self.assertNotEqual(problem, rb.RADIO_SAVED_NO_RADIO)
        self.assertIn("did not confirm", problem)

    def test_a_radio_that_never_confirms_still_fails_the_bundle(self):
        self.use_radio()
        saved = rb._await_radio_readback
        rb._await_radio_readback = lambda iface, **kw: False
        try:
            problems = sl._apply({"lora_preset": "Medium Slow",
                                  "lora_freq_mhz": "906.875",
                                  "lora_tx_dbm": 20})
        finally:
            rb._await_radio_readback = saved
        self.assertEqual(len(problems), 1)

    def test_an_unrelated_failure_is_still_a_failure(self):
        iface = self.use_radio()
        def boom():
            raise ValueError("something else entirely")
        iface.setFrequency = boom
        problem = rb.set_radio_config("Medium Slow", "906.875", 20)
        self.assertNotEqual(problem, rb.RADIO_SAVED_NO_RADIO)
        self.assertIn("Could not apply", problem)

    def test_an_invalid_setting_is_refused_before_any_of_this(self):
        self.use_radio(online=False)
        self.assertNotEqual(rb.set_radio_config("Medium Slow", "not a number", 20),
                            rb.RADIO_SAVED_NO_RADIO)

    # -- what the bundle does with it ------------------------------------

    def test_the_passphrase_lands_with_no_radio_connected(self):
        # THE ONE THAT MATTERS: the whole point of the fix.
        #
        # The team QR is built first, with the fixture's _lora_iface still
        # None, because _share_team_settings asserts a clean apply while it
        # poses as the host phone. The radio goes missing only for THIS phone,
        # at the moment the bundle arrives -- which is the situation.
        self._j(sl.set_pin(PIN))
        qr = self._share_team_settings()
        self.use_radio(online=False)
        offer = self._j(sl.receive(qr["text"], "qr", KOTLIN))
        out = self._j(sl.approve(offer["host"], PIN, False, KOTLIN))
        self.assertEqual(out["outcome"], "applied")
        self.assertEqual(rb._load_passphrase()[0], "team pass")
        # And the radio settings are saved for whenever a radio turns up.
        cfg = rb._load_radio_config()
        self.assertEqual((cfg["preset"], cfg["frequency_hz"]),
                         ("Medium Slow", 906875000))
        # Coverage too -- it was rolled back with everything else before.
        self.assertEqual(out["coverage"]["fm_mhz"], "446.000")

    def test_the_apply_reports_no_problem_for_a_missing_radio(self):
        self.use_radio(online=False)
        self.assertEqual(sl._apply({"lora_preset": "Medium Slow",
                                    "lora_freq_mhz": "906.875",
                                    "lora_tx_dbm": 20}), [])

    def test_a_real_radio_problem_still_fails_the_bundle(self):
        iface = self.use_radio()
        iface.validateRadioState = lambda: False
        problems = sl._apply({"lora_preset": "Medium Slow",
                              "lora_freq_mhz": "906.875",
                              "lora_tx_dbm": 20})
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("Radio: "))

    def test_undo_works_with_no_radio_connected_too(self):
        self._j(sl.set_pin(PIN))
        qr = self._share_team_settings()
        self.use_radio(online=False)
        offer = self._j(sl.receive(qr["text"], "qr", KOTLIN))
        out = self._j(sl.approve(offer["host"], PIN, False, KOTLIN))
        self.assertEqual(out["outcome"], "applied")
        back = self._j(sl.undo(json.dumps({"coverage": out["coverage"]})))
        self.assertEqual(back["problems"], [])
        self.assertEqual(rb._load_passphrase()[0], "old pass")
        self.assertEqual(rb._load_radio_config()["preset"], "Short Slow")
