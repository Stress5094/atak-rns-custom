"""That changing a Meshtastic board's settings does what it says, and only that.

Run against a fake node built from the REAL protobuf types, so the enum names,
field names and message shapes are the package's, not our guess at them. What
the fake cannot tell us is whether a real board accepts the writes; that was
checked by hand against the bench RAK4631s (see the commit notes).

What is at risk, and what each group is for:

  * NOTHING IS WRITTEN UNLESS IT CHANGES. Every radio write reboots the board
    and drops the link for tens of seconds.
  * ONE TRANSACTION, in order, and only the parts that differ.
  * A BAD REQUEST APPLIES NOTHING. Half a channel change strands a node.
  * THE KEY IS NEVER IN A SNAPSHOT.
  * THE SHARE LINK IS THE MESHTASTIC APP'S FORMAT: checked against the
    package's own getURL, which is the independent oracle.
"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()

from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2, localonly_pb2
from meshtastic.node import Node

import mt_config

LORA = config_pb2.Config.LoRaConfig
DEVICE = config_pb2.Config.DeviceConfig

KEY_A = bytes(range(32))
KEY_B = bytes(range(100, 132))


class FakeNode(object):
    """Quacks like meshtastic.node.Node as far as mt_config uses it."""

    def __init__(self, name="ftx", psk=KEY_A):
        self.localConfig = localonly_pb2.LocalConfig()
        lora = self.localConfig.lora
        lora.region = LORA.RegionCode.Value("US")
        lora.use_preset = True
        lora.modem_preset = LORA.ModemPreset.Value("LONG_FAST")
        lora.channel_num = 20
        lora.override_frequency = 906.875
        lora.tx_power = 30
        lora.tx_enabled = True
        lora.hop_limit = 7
        self.localConfig.device.role = DEVICE.Role.Value("TAK")
        self.localConfig.device.rebroadcast_mode = DEVICE.RebroadcastMode.Value("ALL")
        # A board with no screen out of the box: Bluetooth on, the default PIN.
        bt = self.localConfig.bluetooth
        bt.enabled = True
        bt.mode = config_pb2.Config.BluetoothConfig.PairingMode.Value("FIXED_PIN")
        bt.fixed_pin = 123456
        self.channels = []
        for i in range(8):
            c = channel_pb2.Channel()
            c.index = i
            c.role = (channel_pb2.Channel.Role.PRIMARY if i == 0
                      else channel_pb2.Channel.Role.DISABLED)
            self.channels.append(c)
        self.channels[0].settings.name = name
        self.channels[0].settings.psk = psk
        self.calls = []
        self.fail_with = None

    def beginSettingsTransaction(self):
        self.calls.append("begin")

    def commitSettingsTransaction(self):
        self.calls.append("commit")

    def writeConfig(self, name):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append("config:" + name)

    def writeChannel(self, index):
        self.calls.append("channel:%d" % index)


class TestSnapshot(unittest.TestCase):

    def test_reports_the_boards_settings(self):
        s = mt_config.snapshot(FakeNode())
        self.assertEqual(s["region"], "US")
        self.assertEqual(s["preset"], "LONG_FAST")
        self.assertEqual(s["preset_name"], "Long Fast")
        self.assertEqual(s["channel_num"], 20)
        self.assertEqual(s["override_frequency_mhz"], 906.875)
        self.assertEqual(s["tx_power_dbm"], 30)
        self.assertEqual(s["rebroadcast"], "ALL")
        self.assertEqual(s["role"], "TAK")
        self.assertEqual(s["channel"]["name"], "ftx")

    def test_the_key_is_never_in_a_snapshot(self):
        text = json.dumps(mt_config.snapshot(FakeNode()))
        self.assertNotIn(base64.b64encode(KEY_A).decode(), text)
        self.assertNotIn(base64.urlsafe_b64encode(KEY_A).decode(), text)
        self.assertNotIn(KEY_A.hex(), text)
        self.assertNotIn("psk", text)

    def test_says_what_kind_of_key_it_is(self):
        for psk, kind in ((b"", "none"), (b"\x01", "default"), (KEY_A, "custom"),
                          (KEY_A[:16], "custom")):
            self.assertEqual(mt_config.snapshot(FakeNode(psk=psk))["channel"]["key_kind"], kind)

    def test_describes_key_strength_in_plain_words(self):
        self.assertIn("256-bit", mt_config.key_description(KEY_A))
        self.assertIn("128-bit", mt_config.key_description(KEY_A[:16]))
        self.assertIn("anyone", mt_config.key_description(b"\x01"))
        self.assertIn("No encryption", mt_config.key_description(b""))

    def test_a_preset_the_app_has_no_name_for_still_reads(self):
        node = FakeNode()
        node.localConfig.lora.modem_preset = LORA.ModemPreset.Value("LITE_FAST")
        s = mt_config.snapshot(node)
        self.assertEqual(s["preset"], "LITE_FAST")
        self.assertIsNone(s["preset_name"])


class TestWarnings(unittest.TestCase):
    """The board must behave like the radio the app is built around, and each way
    it does not looks like a dead or noisy radio from the phone."""

    def quiet_board(self):
        node = FakeNode()
        node.localConfig.device.rebroadcast_mode = DEVICE.RebroadcastMode.Value("NONE")
        return node

    def warn(self, node):
        return mt_config.warnings(mt_config.snapshot(node))

    def test_a_board_set_up_as_the_app_expects_has_no_warnings(self):
        self.assertEqual(self.warn(self.quiet_board()), [])

    def test_a_board_that_repeats_everything_is_flagged_with_the_remedy(self):
        w = self.warn(FakeNode())               # the bench boards' state: ALL
        self.assertEqual(len(w), 1)
        self.assertIn("set to repeat other nodes' packets", w[0])
        self.assertIn("Never repeat", w[0])

    def test_every_mode_except_never_is_flagged(self):
        for mode in ("ALL", "ALL_SKIP_DECODING", "LOCAL_ONLY", "KNOWN_ONLY", "CORE_PORTNUMS_ONLY"):
            node = self.quiet_board()
            node.localConfig.device.rebroadcast_mode = DEVICE.RebroadcastMode.Value(mode)
            self.assertTrue(any("repeats" in x for x in self.warn(node)), mode)

    def test_repeater_roles_are_flagged_and_client_roles_are_not(self):
        for role in mt_config.REPEATER_ROLES:
            node = self.quiet_board()
            node.localConfig.device.role = DEVICE.Role.Value(role)
            self.assertTrue(any("repeater" in x for x in self.warn(node)), role)
        for role in ("CLIENT", "CLIENT_MUTE", "TAK", "TAK_TRACKER", "TRACKER"):
            node = self.quiet_board()
            node.localConfig.device.role = DEVICE.Role.Value(role)
            self.assertEqual(self.warn(node), [], role)

    def test_a_board_with_no_region_is_flagged_because_it_will_not_transmit(self):
        node = self.quiet_board()
        node.localConfig.lora.region = LORA.RegionCode.Value("UNSET")
        self.assertTrue(any("No region" in x for x in self.warn(node)))

    def test_a_board_with_transmit_off_is_flagged(self):
        node = self.quiet_board()
        node.localConfig.lora.tx_enabled = False
        self.assertTrue(any("Transmit is turned off" in x for x in self.warn(node)))

    def test_several_problems_are_all_reported(self):
        node = FakeNode()
        node.localConfig.lora.region = LORA.RegionCode.Value("UNSET")
        node.localConfig.lora.tx_enabled = False
        node.localConfig.device.role = DEVICE.Role.Value("REPEATER")
        self.assertEqual(len(self.warn(node)), 4)

    def test_a_warning_never_contains_the_key(self):
        text = " ".join(self.warn(FakeNode()))
        self.assertNotIn(base64.b64encode(KEY_A).decode(), text)


class TestKeys(unittest.TestCase):

    def test_named_keys(self):
        self.assertEqual(mt_config.parse_psk("none"), b"")
        self.assertEqual(mt_config.parse_psk(" DEFAULT "), b"\x01")

    def test_random_keys_have_the_right_size_and_differ(self):
        a, b = mt_config.parse_psk("random"), mt_config.parse_psk("random")
        self.assertEqual((len(a), len(b)), (32, 32))
        self.assertNotEqual(a, b)
        self.assertEqual(len(mt_config.parse_psk("random128")), 16)

    def test_base64_keys_standard_and_urlsafe(self):
        self.assertEqual(mt_config.parse_psk(base64.b64encode(KEY_A).decode()), KEY_A)
        self.assertEqual(mt_config.parse_psk(base64.urlsafe_b64encode(KEY_A).decode()), KEY_A)
        self.assertEqual(mt_config.parse_psk(base64.b64encode(KEY_A[:16]).decode()), KEY_A[:16])

    def test_a_key_of_a_size_the_board_does_not_use_is_refused(self):
        with self.assertRaises(mt_config.MeshConfigError) as cm:
            mt_config.parse_psk(base64.b64encode(b"x" * 20).decode())
        self.assertIn("16 or 32", str(cm.exception))

    def test_garbage_and_empty_are_refused(self):
        for bad in ("", "   ", "not base64 !!", "%%%"):
            with self.assertRaises(mt_config.MeshConfigError, msg=bad):
                mt_config.parse_psk(bad)

    def test_the_error_never_echoes_the_key(self):
        secret = base64.b64encode(b"s" * 20).decode()
        with self.assertRaises(mt_config.MeshConfigError) as cm:
            mt_config.parse_psk(secret)
        self.assertNotIn(secret, str(cm.exception))


class TestShareLinks(unittest.TestCase):

    def test_matches_the_meshtastic_packages_own_getURL(self):
        node = FakeNode()
        self.assertEqual(mt_config.make_url(node), Node.getURL(node))

    def test_round_trips(self):
        node = FakeNode(name="squad", psk=KEY_B)
        chan_set = mt_config.parse_url(mt_config.make_url(node))
        self.assertEqual(chan_set.settings[0].name, "squad")
        self.assertEqual(bytes(chan_set.settings[0].psk), KEY_B)
        self.assertEqual(chan_set.lora_config.channel_num, 20)

    def test_bad_links_raise_our_error_and_do_not_exit(self):
        for bad in ("", "hello", "https://meshtastic.org/e/#", "https://x/#!!!notb64"):
            with self.assertRaises(mt_config.MeshConfigError, msg=bad):
                mt_config.parse_url(bad)

    def test_a_link_with_no_channel_is_refused(self):
        empty = apponly_pb2.ChannelSet()
        empty.lora_config.region = LORA.RegionCode.Value("US")
        url = "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(
            empty.SerializeToString()).decode().rstrip("=")
        with self.assertRaises(mt_config.MeshConfigError):
            mt_config.parse_url(url)


class TestPlan(unittest.TestCase):

    def test_a_request_that_matches_the_board_changes_nothing(self):
        node = FakeNode()
        p = mt_config.plan(node, {"region": "US", "preset": "Long Fast",
                                  "channel_num": 20, "tx_power_dbm": 30,
                                  "override_frequency_mhz": 906.875,
                                  "rebroadcast": "ALL", "channel_name": "ftx",
                                  "psk": base64.b64encode(KEY_A).decode()})
        self.assertEqual((p.changes, p.problems), ([], []))

    def test_only_the_differences_are_listed(self):
        p = mt_config.plan(FakeNode(), {"region": "US", "tx_power_dbm": 22})
        self.assertEqual([c["field"] for c in p.changes], ["tx_power_dbm"])
        self.assertEqual((p.changes[0]["old"], p.changes[0]["new"]), (30, 22))

    def test_presets_by_app_name_or_board_name(self):
        for value in ("Medium Fast", "medium fast", "MEDIUM_FAST"):
            p = mt_config.plan(FakeNode(), {"preset": value})
            self.assertEqual(p.changes[0]["new"], "Medium Fast", value)
            self.assertEqual(p.wants["preset"], "MEDIUM_FAST")

    def test_changes_read_in_plain_words(self):
        p = mt_config.plan(FakeNode(), {"rebroadcast": "NONE"})
        self.assertEqual(p.changes[0]["old"], "Repeat everything it hears")
        self.assertEqual(p.changes[0]["new"], "Never repeat")

    def test_a_changed_key_is_described_not_shown(self):
        p = mt_config.plan(FakeNode(), {"psk": "default"})
        c = p.changes[0]
        self.assertEqual(c["field"], "psk")
        self.assertIn("anyone", c["new"])
        self.assertNotIn(base64.b64encode(KEY_A).decode(), json.dumps(p.changes))

    def test_a_key_replaced_by_one_of_the_same_kind_still_reads_as_a_change(self):
        # Seen on the OnePlus 2026-10-14: "Custom 256-bit key -> Custom
        # 256-bit key", which looks like nothing is changing.
        p = mt_config.plan(FakeNode(psk=KEY_A),
                           {"psk": base64.b64encode(KEY_B).decode()})
        c = p.changes[0]
        self.assertNotEqual(c["old"], c["new"])
        self.assertIn("256-bit", c["new"])
        self.assertIn("different", c["new"])
        self.assertNotIn(base64.b64encode(KEY_B).decode(), json.dumps(p.changes))

    def test_bad_values_are_each_refused_with_a_reason(self):
        bad = [({"region": "ATLANTIS"}, "region"),
               ({"region": "UNSET"}, "region"),
               ({"preset": "Warp"}, "preset"),
               ({"tx_power_dbm": -1}, "power"),
               ({"tx_power_dbm": 31}, "power"),
               ({"tx_power_dbm": "loud"}, "power"),
               ({"channel_num": 256}, "slot"),
               ({"channel_num": -3}, "slot"),
               ({"override_frequency_mhz": 5}, "override"),
               ({"override_frequency_mhz": 5000}, "override"),
               ({"rebroadcast": "SOMETIMES"}, "repeat"),
               ({"channel_name": "twelve chars"}, "name"),
               ({"psk": "nonsense!!"}, "base64")]
        for request, word in bad:
            p = mt_config.plan(FakeNode(), request)
            self.assertTrue(p.problems, request)
            self.assertIn(word, " ".join(p.problems).lower(), request)
            self.assertEqual(p.changes, [], request)

    def test_zero_is_allowed_for_slot_override_and_power(self):
        p = mt_config.plan(FakeNode(), {"channel_num": 0, "override_frequency_mhz": 0,
                                        "tx_power_dbm": 0})
        self.assertEqual(p.problems, [])
        self.assertEqual(len(p.changes), 3)

    def test_a_name_is_limited_in_bytes_not_characters(self):
        # Eleven two-byte characters is 22 bytes and will not fit.
        self.assertTrue(mt_config.plan(FakeNode(), {"channel_name": "é" * 11}).problems)
        self.assertEqual(mt_config.plan(FakeNode(), {"channel_name": "é" * 5}).problems, [])

    def test_one_bad_value_cancels_the_whole_request(self):
        p = mt_config.plan(FakeNode(), {"tx_power_dbm": 22, "region": "ATLANTIS"})
        self.assertEqual(p.changes, [])
        self.assertEqual(p.wants, {})
        self.assertEqual(len(p.problems), 1)

    def test_an_empty_name_is_a_valid_name(self):
        p = mt_config.plan(FakeNode(), {"channel_name": ""})
        self.assertEqual(p.problems, [])
        self.assertEqual(p.changes[0]["new"], "")


class TestApply(unittest.TestCase):

    def test_nothing_to_change_writes_nothing_at_all(self):
        node = FakeNode()
        self.assertEqual(mt_config.apply(node, {"tx_power_dbm": 30, "region": "US"}), [])
        self.assertEqual(node.calls, [])

    def test_a_bad_request_writes_nothing_and_says_why(self):
        node = FakeNode()
        with self.assertRaises(mt_config.MeshConfigError) as cm:
            mt_config.apply(node, {"tx_power_dbm": 99})
        self.assertIn("power", str(cm.exception).lower())
        self.assertEqual(node.calls, [])

    def test_everything_goes_in_one_transaction_in_order(self):
        node = FakeNode()
        changes = mt_config.apply(node, {
            "region": "EU_868", "preset": "Medium Fast", "rebroadcast": "NONE",
            "channel_name": "squad", "psk": base64.b64encode(KEY_B).decode()})
        self.assertEqual(len(changes), 5)
        self.assertEqual(node.calls, ["begin", "config:lora", "config:device",
                                      "channel:0", "commit"])

    def test_only_the_parts_that_changed_are_written(self):
        node = FakeNode()
        mt_config.apply(node, {"tx_power_dbm": 22})
        self.assertEqual(node.calls, ["begin", "config:lora", "commit"])
        node.calls.clear()
        mt_config.apply(node, {"rebroadcast": "NONE"})
        self.assertEqual(node.calls, ["begin", "config:device", "commit"])
        node.calls.clear()
        mt_config.apply(node, {"channel_name": "new"})
        self.assertEqual(node.calls, ["begin", "channel:0", "commit"])

    def test_the_nodes_model_ends_up_holding_what_was_sent(self):
        node = FakeNode()
        mt_config.apply(node, {
            "region": "EU_868", "preset": "Short Fast", "channel_num": 3,
            "override_frequency_mhz": 0, "tx_power_dbm": 14, "rebroadcast": "KNOWN_ONLY",
            "channel_name": "x", "psk": base64.b64encode(KEY_B).decode()})
        lora = node.localConfig.lora
        self.assertEqual(LORA.RegionCode.Name(lora.region), "EU_868")
        self.assertEqual(LORA.ModemPreset.Name(lora.modem_preset), "SHORT_FAST")
        self.assertTrue(lora.use_preset)
        self.assertEqual((lora.channel_num, lora.override_frequency, lora.tx_power), (3, 0, 14))
        self.assertEqual(DEVICE.RebroadcastMode.Name(
            node.localConfig.device.rebroadcast_mode), "KNOWN_ONLY")
        self.assertEqual(node.channels[0].settings.name, "x")
        self.assertEqual(bytes(node.channels[0].settings.psk), KEY_B)

    def test_settings_not_asked_about_are_left_alone(self):
        node = FakeNode()
        mt_config.apply(node, {"tx_power_dbm": 22})
        self.assertEqual(node.localConfig.lora.channel_num, 20)
        self.assertEqual(node.localConfig.lora.hop_limit, 7)
        self.assertEqual(node.channels[0].settings.name, "ftx")
        self.assertEqual(bytes(node.channels[0].settings.psk), KEY_A)

    def test_a_library_exit_becomes_an_error_not_a_dead_thread(self):
        node = FakeNode()
        node.fail_with = SystemExit(1)
        with self.assertRaises(mt_config.MeshConfigError):
            mt_config.apply(node, {"tx_power_dbm": 22})

    def test_importing_a_share_link_sets_channel_and_radio_together(self):
        donor = FakeNode(name="squad", psk=KEY_B)
        donor.localConfig.lora.modem_preset = LORA.ModemPreset.Value("MEDIUM_FAST")
        donor.localConfig.lora.channel_num = 9
        node = FakeNode()
        mt_config.apply(node, {"url": mt_config.make_url(donor)})
        self.assertEqual(node.channels[0].settings.name, "squad")
        self.assertEqual(bytes(node.channels[0].settings.psk), KEY_B)
        self.assertEqual(LORA.ModemPreset.Name(node.localConfig.lora.modem_preset), "MEDIUM_FAST")
        self.assertEqual(node.localConfig.lora.channel_num, 9)
        # The link does not carry a transmit power, a role or a repeat mode.
        self.assertEqual(node.localConfig.lora.tx_power, 30)
        self.assertEqual(DEVICE.Role.Name(node.localConfig.device.role), "TAK")

    def test_a_link_from_a_board_with_no_region_does_not_wipe_ours(self):
        donor = FakeNode(name="squad")
        donor.localConfig.lora.region = LORA.RegionCode.Value("UNSET")
        node = FakeNode()
        mt_config.apply(node, {"url": mt_config.make_url(donor)})
        self.assertEqual(LORA.RegionCode.Name(node.localConfig.lora.region), "US")

    def test_a_damaged_link_changes_nothing(self):
        node = FakeNode()
        with self.assertRaises(mt_config.MeshConfigError):
            mt_config.apply(node, {"url": "https://meshtastic.org/e/#garbage!!"})
        self.assertEqual(node.calls, [])

    def test_two_boards_set_from_one_link_end_up_with_matching_channels(self):
        a, b = FakeNode(name="aaa", psk=KEY_A), FakeNode(name="bbb", psk=KEY_B)
        mt_config.apply(b, {"url": mt_config.make_url(a)})
        self.assertEqual(mt_config.make_url(a), mt_config.make_url(b))


class TestBluetoothPin(unittest.TestCase):
    """The pairing PIN for boards with no screen, derived from the passphrase
    and the board's node number. At risk: two phones deriving different PINs
    (a board one of them can never pair), the phone and the USB side deriving
    different PINs for the same board, boards sharing one PIN, a PIN that
    leaks into a log or a change line, and a board left on the default 123456
    that everyone in range knows."""

    MESH_KEY = bytes(range(32))
    BT = config_pb2.Config.BluetoothConfig.PairingMode
    NODE = 0x000BAD02            # the RAK4631 on the bench

    def request(self, node, node_num=NODE):
        return mt_config.managed_request(self.MESH_KEY, "Long Fast", 915000000, 22,
                                         current_region="US", node_num=node_num)

    def pin(self, node_num=NODE):
        return mt_config.derive_bt_pin(self.MESH_KEY, node_num)

    def test_the_derivation_is_pinned(self):
        # Changing this strands every board already paired. Change the label.
        self.assertEqual(self.pin(), 101632)

    def test_six_digits_and_one_per_passphrase_and_board(self):
        for key in (KEY_A, KEY_B, self.MESH_KEY, bytes(32), b"\xff" * 32):
            for num in (0, 1, self.NODE, 0xFFFFFFFF):
                pin = mt_config.derive_bt_pin(key, num)
                self.assertTrue(100000 <= pin <= 999999, pin)
                self.assertEqual(pin, mt_config.derive_bt_pin(key, num))
        self.assertNotEqual(mt_config.derive_bt_pin(KEY_A, self.NODE),
                            mt_config.derive_bt_pin(KEY_B, self.NODE))

    def test_each_board_gets_its_own_pin(self):
        # The point of v2. The two bench boards, then a run of neighbours.
        self.assertNotEqual(self.pin(0x000BAD02), self.pin(0x000BAD03))
        pins = {self.pin(n) for n in range(0x000BAD00, 0x000BAD00 + 50)}
        self.assertEqual(len(pins), 50)

    def test_no_mesh_key_or_no_node_number_is_refused(self):
        with self.assertRaises(mt_config.MeshConfigError):
            mt_config.derive_bt_pin(b"", self.NODE)
        for bad in (None, -1, 1 << 32):
            with self.assertRaises(mt_config.MeshConfigError):
                mt_config.derive_bt_pin(self.MESH_KEY, bad)

    def test_a_board_that_has_not_said_its_number_is_a_problem_not_a_crash(self):
        req = self.request(FakeNode(), node_num=None)
        self.assertNotIn("bt_pin", req)
        self.assertIn("node number", " ".join(mt_config.profile_problems(req)))

    def test_the_phone_and_the_cable_agree_on_the_bench_boards(self):
        # MEASURED 2026-10-15: address and name as the phone saw them, node
        # number as the board reported it over USB.
        for address, name, num in (("02:00:00:0B:AD:02", "1.3_ad02", 0x000BAD02),
                                   ("02:00:00:0B:AD:03", "1.2_ad03", 0x000BAD03)):
            self.assertEqual(mt_config.node_num_from_ble(address, name), num)
            self.assertEqual(mt_config.node_num_from_ble(address), num)
            self.assertEqual(mt_config.node_num_from_ble(address.lower(), name), num)

    def test_an_address_two_above_the_name_is_read_as_an_esp32(self):
        # NOT measured: the ESP32 convention that Bluetooth is base MAC + 2.
        self.assertEqual(mt_config.node_num_from_ble("24:6F:28:AB:CD:12", "abc_cd10"),
                         0x28ABCD10)
        # Across a byte boundary.
        self.assertEqual(mt_config.node_num_from_ble("24:6F:28:AB:CE:01", "abc_cdff"),
                         0x28ABCDFF)

    def test_a_name_that_matches_no_offset_leaves_the_address_as_it_is(self):
        self.assertEqual(mt_config.node_num_from_ble("02:00:00:0B:AD:02", "Meshtastic_beef"),
                         0x000BAD02)
        self.assertEqual(mt_config.node_num_from_ble("02:00:00:0B:AD:02", "no suffix"),
                         0x000BAD02)

    def test_not_an_address_gives_nothing(self):
        for bad in ("", None, "02:00:00", "not an address at all!"):
            self.assertIsNone(mt_config.node_num_from_ble(bad))

    def test_applying_sets_a_fixed_pin_and_writes_the_bluetooth_section(self):
        node = FakeNode()
        mt_config.apply(node, self.request(node))
        bt = node.localConfig.bluetooth
        self.assertTrue(bt.enabled)
        self.assertEqual(bt.mode, self.BT.Value("FIXED_PIN"))
        self.assertEqual(bt.fixed_pin, self.pin())
        self.assertIn("config:bluetooth", node.calls)
        self.assertEqual(mt_config.plan(node, self.request(node)).changes, [])

    def test_a_board_already_on_the_pin_is_not_rewritten(self):
        node = FakeNode()
        node.localConfig.bluetooth.fixed_pin = self.pin()
        fields = {c["field"] for c in mt_config.plan(node, self.request(node)).changes}
        self.assertNotIn("bt_pin", fields)

    def test_bluetooth_off_or_another_mode_is_a_change(self):
        for enabled, mode in ((False, "FIXED_PIN"), (True, "RANDOM_PIN"), (True, "NO_PIN")):
            node = FakeNode()
            node.localConfig.bluetooth.fixed_pin = self.pin()
            node.localConfig.bluetooth.enabled = enabled
            node.localConfig.bluetooth.mode = self.BT.Value(mode)
            fields = {c["field"] for c in mt_config.plan(node, self.request(node)).changes}
            self.assertIn("bt_pin", fields, (enabled, mode))

    def test_the_pin_is_never_in_a_snapshot_or_a_change_line(self):
        node = FakeNode()
        pin = "%06d" % self.pin()
        changes = mt_config.plan(node, self.request(node)).changes
        self.assertNotIn(pin, repr(changes))
        mt_config.apply(node, self.request(node))
        self.assertNotIn(pin, repr(mt_config.snapshot(node)))

    def test_a_bad_pin_is_refused(self):
        for bad in (12345, 1000000, "abc"):
            p = mt_config.plan(FakeNode(), {"bt_pin": bad})
            self.assertTrue(p.problems, bad)

    def test_no_pin_mode_is_warned_about(self):
        node = FakeNode()
        node.localConfig.bluetooth.mode = self.BT.Value("NO_PIN")
        self.assertTrue(any("read the channel key" in w
                            for w in mt_config.warnings(mt_config.snapshot(node))))
        node.localConfig.bluetooth.enabled = False
        self.assertFalse(any("read the channel key" in w
                             for w in mt_config.warnings(mt_config.snapshot(node))))


class TestManagedProfile(unittest.TestCase):
    """The settings the APP owns on a board (Phase 3 of the parity plan): its
    preset, frequency and power, a channel derived from the passphrase, never
    repeat. What is at risk: two phones with one passphrase deriving DIFFERENT
    channels (a silent split), and the board's key giving anything away about
    the key that seals the app's messages."""

    MESH_KEY = bytes(range(32))

    def request(self, node=None, **kw):
        args = dict(preset="Long Fast", frequency_hz=915000000, txpower_dbm=22,
                    node_num=0x000BAD02)
        args.update(kw)
        region = mt_config.snapshot(node)["region"] if node is not None else "US"
        return mt_config.managed_request(self.MESH_KEY, current_region=region, **args)

    def test_the_derivation_is_pinned(self):
        # A change here splits every deployed network in two: phones on the old
        # scheme and phones on the new one stop hearing each other. Change the
        # version labels in mt_config instead, on purpose.
        name, key = mt_config.derive_channel(self.MESH_KEY)
        self.assertEqual(name, "rt-783af3")
        self.assertEqual(key.hex(), "1ad56bdfca550fa06c33e326a8673975"
                                    "368317228f1dc7f0d6e44b24feb70355")

    def test_same_key_same_channel_and_different_keys_different_channels(self):
        self.assertEqual(mt_config.derive_channel(KEY_A), mt_config.derive_channel(KEY_A))
        a, b = mt_config.derive_channel(KEY_A), mt_config.derive_channel(KEY_B)
        self.assertNotEqual(a[0], b[0])
        self.assertNotEqual(a[1], b[1])

    def test_the_channel_fits_the_board_and_is_not_the_mesh_key(self):
        name, key = mt_config.derive_channel(self.MESH_KEY)
        self.assertLessEqual(len(name.encode()), mt_config.CHANNEL_NAME_MAX)
        self.assertEqual(len(key), 32)
        self.assertNotEqual(key, self.MESH_KEY)
        self.assertNotIn(self.MESH_KEY.hex()[:8], name)

    def test_no_mesh_key_is_refused(self):
        with self.assertRaises(mt_config.MeshConfigError):
            mt_config.derive_channel(b"")

    def test_it_owns_preset_frequency_power_repeat_and_channel(self):
        node = FakeNode()
        labels = {c["field"] for c in mt_config.plan(node, self.request(node)).changes}
        # FakeNode is on Long Fast already, so the preset is NOT a change.
        self.assertEqual(labels, {"override_frequency_mhz", "tx_power_dbm",
                                  "rebroadcast", "channel_name", "psk", "bt_pin"})

    def test_applying_it_leaves_nothing_to_change(self):
        node = FakeNode()
        mt_config.apply(node, self.request(node))
        self.assertEqual(mt_config.plan(node, self.request(node)).changes, [])
        self.assertEqual(DEVICE.RebroadcastMode.Name(
            node.localConfig.device.rebroadcast_mode), "NONE")
        self.assertAlmostEqual(node.localConfig.lora.override_frequency, 915.0)

    def test_two_boards_given_one_passphrase_end_up_identical(self):
        a, b = FakeNode(name="aaa", psk=KEY_A), FakeNode(name="bbb", psk=KEY_B)
        b.localConfig.lora.channel_num = 3
        for n in (a, b):
            mt_config.apply(n, self.request(n))
        # The slot number is left alone (they still differ, 20 and 3) because
        # the frequency override is what the radio tunes to once it is set.
        self.assertEqual(a.channels[0].settings, b.channels[0].settings)
        for field in ("region", "modem_preset", "override_frequency", "tx_power"):
            self.assertEqual(getattr(a.localConfig.lora, field),
                             getattr(b.localConfig.lora, field), field)

    def test_the_region_of_a_configured_board_is_kept(self):
        node = FakeNode()
        node.localConfig.lora.region = LORA.RegionCode.Value("ANZ")
        self.assertNotIn("region", self.request(node))

    def test_a_board_with_no_region_gets_the_one_its_frequency_is_in(self):
        node = FakeNode()
        node.localConfig.lora.region = LORA.RegionCode.Value("UNSET")
        self.assertEqual(self.request(node)["region"], "US")
        self.assertEqual(self.request(node, frequency_hz=868100000)["region"], "EU_868")

    def test_no_region_and_an_unknown_band_is_a_problem_not_a_guess(self):
        node = FakeNode()
        node.localConfig.lora.region = LORA.RegionCode.Value("UNSET")
        req = self.request(node, frequency_hz=433500000)
        problems = mt_config.profile_problems(req)
        self.assertEqual(len(problems), 1)
        self.assertIn("region", problems[0])

    def gps(self, node, mode):
        node.localConfig.position.gps_mode = config_pb2.Config.PositionConfig.GpsMode.Value(mode)
        return node

    def test_a_board_with_its_gps_on_has_it_turned_off(self):
        node = self.gps(FakeNode(), "ENABLED")
        req = mt_config.managed_request(self.MESH_KEY, "Long Fast", 915000000, 22,
                                        current_region="US", current_gps="ENABLED")
        changes = {c["field"]: c for c in mt_config.plan(node, req).changes}
        self.assertEqual(changes["gps_mode"]["new"], "Off")
        mt_config.apply(node, req)
        self.assertIn("config:position", node.calls)
        self.assertEqual(mt_config.snapshot(node)["gps_mode"], "DISABLED")

    def test_a_board_with_no_gps_fitted_is_left_alone(self):
        req = mt_config.managed_request(self.MESH_KEY, "Long Fast", 915000000, 22,
                                        current_region="US", current_gps="NOT_PRESENT")
        self.assertNotIn("gps_mode", req)

    def test_a_gps_that_is_on_is_warned_about_with_the_remedy(self):
        snap = mt_config.snapshot(self.gps(FakeNode(), "ENABLED"))
        text = " ".join(mt_config.warnings(snap))
        self.assertIn("GPS", text)
        self.assertIn("Use the app's settings", text)
        snap = mt_config.snapshot(self.gps(FakeNode(), "DISABLED"))
        self.assertNotIn("GPS", " ".join(mt_config.warnings(snap)))

    def test_power_is_capped_at_what_a_board_takes(self):
        self.assertEqual(self.request(txpower_dbm=37)["tx_power_dbm"],
                         mt_config.TX_POWER_MAX_DBM)

    def test_the_change_list_never_contains_the_key(self):
        node = FakeNode()
        _, key = mt_config.derive_channel(self.MESH_KEY)
        text = json.dumps(mt_config.plan(node, self.request(node)).changes)
        self.assertNotIn(base64.b64encode(key).decode(), text)
        self.assertNotIn(key.hex(), text)


if __name__ == "__main__":
    unittest.main()
