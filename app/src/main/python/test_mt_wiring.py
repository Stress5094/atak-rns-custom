"""That rns_bridge treats a Meshtastic board as the LoRa radio correctly.

Kept apart from test_rns_bridge_wiring on purpose: that module is large and
shared, and these tests are about one decision -- the board in the LoRa slot is
a Meshtastic board, not an RNode -- whose effects are scattered across the
bridge. What is at risk, and what each group is for:

  * THE SETTING. An unrecognised firmware name must fall back to RNode, the
    only firmware every earlier build knew, never to an unknown state.
  * THE SIZE. A Meshtastic packet carries 231 B, LoRa-over-RNS 464 B. Sizing
    against the wrong one is the mistake that took the FM link offline on its
    first send (see fm_link.hw_mtu), so the budget is asserted for both.
  * THE SEND PATH. A Meshtastic frame must reach the interface directly, not
    through RNS.Packet, whose 19 B header would come out of a 231 B packet.
  * THE RNODE-ONLY FEATURES. Screen mirror, Bluetooth pairing, identify and the
    spectrum survey write KISS commands to `iface.serial`; on a Meshtastic
    board that attribute does not exist. Each must refuse with a reason rather
    than raise on a background thread, and none may change saved settings.
  * THE WHOLE BUILD. `_build_mesh_interface()` run for real against a TCP server
    standing in for Kotlin's byte pipe, with the fake board behind it: the same
    path the phone takes, minus the USB leg.
"""

import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()

import cot_codec
import mesh_view
import mt_link
import peers
import rns_bridge
import test_mt_port as tmp


class RestoresBridgeState(unittest.TestCase):
    """The bridge is a module of globals; leave them as found."""

    NAMES = ("_radio_firmware", "_lora_iface", "_transport",
             "RNODE_BRIDGE_PORT", "_on_rns_packet_received", "_radio_config")

    def setUp(self):
        self._saved = {n: getattr(rns_bridge, n) for n in self.NAMES}
        self._saved_link_ifaces = dict(rns_bridge._link_ifaces)
        self._saved_header = rns_bridge._lora_link.header_bytes

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(rns_bridge, n, v)
        rns_bridge._link_ifaces.clear()
        rns_bridge._link_ifaces.update(self._saved_link_ifaces)
        rns_bridge._lora_link.header_bytes = self._saved_header


class TestFirmwareSetting(RestoresBridgeState):

    def test_default_is_rnode(self):
        self.assertEqual(self._saved["_radio_firmware"], rns_bridge.RADIO_RNODE)

    def test_meshtastic_is_accepted_in_any_case_and_padding(self):
        self.assertEqual(rns_bridge.set_radio_firmware("  Meshtastic "),
                         rns_bridge.RADIO_MESHTASTIC)

    def test_anything_unrecognised_falls_back_to_rnode(self):
        for junk in ("", None, "lora", "rnode2", "MESH"):
            rns_bridge.set_radio_firmware("meshtastic")
            self.assertEqual(rns_bridge.set_radio_firmware(junk),
                             rns_bridge.RADIO_RNODE, junk)


class TestSizing(RestoresBridgeState):

    def test_meshtastic_is_sized_to_its_packet_not_rnses(self):
        rns_bridge._transport = "usb"
        rns_bridge.set_radio_firmware("meshtastic")
        self.assertEqual(rns_bridge._compute_max_packet(), mt_link.hw_mtu())
        self.assertLess(rns_bridge._compute_max_packet(), cot_codec.RNS_MAX_PAYLOAD)

    def test_rnode_sizing_is_unchanged(self):
        rns_bridge._transport = "usb"
        rns_bridge.set_radio_firmware("rnode")
        self.assertEqual(rns_bridge._compute_max_packet(),
                         min(cot_codec.RNS_MAX_PAYLOAD, rns_bridge.RNS.Packet.PLAIN_MDU))

    def test_the_sizing_does_not_need_the_board_to_be_up(self):
        rns_bridge._transport = "usb"
        rns_bridge._lora_iface = None
        rns_bridge.set_radio_firmware("meshtastic")
        self.assertEqual(rns_bridge._compute_max_packet(), 231)


class TestPeerSightingsNameTheBoard(RestoresBridgeState):
    """The mesh view tells an RNode line from a Meshtastic one by the kind the
    bridge stamps on each sighting. Both arrive on the "lora" link, so without
    the stamp the two are indistinguishable."""

    def setUp(self):
        super().setUp()
        self._saved_peers = rns_bridge._peers
        rns_bridge._peers = peers.PeerTable(connected_s=180, forget_s=600)

    def tearDown(self):
        rns_bridge._peers = self._saved_peers
        super().tearDown()

    def kinds(self):
        return [r["radio"] for r in rns_bridge._peers.snapshot()[0]["radios"]]

    def sight(self, link):
        # Relayed, so the range-test recorder stays out of it.
        rns_bridge._note_peer_frame("a1b2c3", None, None, 1, direct=False,
                                    link=link)

    def test_lora_on_an_rnode_is_named_rnode(self):
        rns_bridge.set_radio_firmware("rnode")
        self.sight(rns_bridge.LINK_LORA)
        self.assertEqual(self.kinds(), ["rnode"])

    def test_lora_on_a_meshtastic_board_is_named_meshtastic(self):
        rns_bridge.set_radio_firmware("meshtastic")
        self.sight(rns_bridge.LINK_LORA)
        self.assertEqual(self.kinds(), ["meshtastic"])

    def test_other_links_keep_their_own_name_whatever_the_board(self):
        rns_bridge.set_radio_firmware("meshtastic")
        self.sight(rns_bridge.LINK_FM)
        self.assertEqual(self.kinds(), ["fm"])

    def test_the_mesh_tab_can_draw_every_kind_the_bridge_stamps(self):
        stamped = {rns_bridge.RADIO_RNODE, rns_bridge.RADIO_MESHTASTIC,
                   rns_bridge.LINK_FM, rns_bridge.LINK_WIFI}
        self.assertEqual({k for k, _, _ in mesh_view.KINDS}, stamped)

    def test_get_mesh_view_carries_the_stamped_kind(self):
        rns_bridge.set_radio_firmware("meshtastic")
        self.sight(rns_bridge.LINK_LORA)
        view = json.loads(rns_bridge.get_mesh_view())
        self.assertTrue(view["ok"], view)
        self.assertEqual([l["radio"] for l in view["lines"]], ["meshtastic"])
        self.assertFalse(view["lines"][0]["direct"])

    def test_get_mesh_view_names_this_nodes_own_board(self):
        rns_bridge.set_radio_firmware("meshtastic")
        rns_bridge._link_ifaces[rns_bridge.LINK_LORA] = object()
        view = json.loads(rns_bridge.get_mesh_view())
        self.assertIn("meshtastic", view["me"]["radios"])
        self.assertNotIn("lora", view["me"]["radios"])


class FakeIface(object):
    def __init__(self):
        self.sent = []
        self.online = True

    def process_outgoing(self, data):
        self.sent.append(data)
        return True


class TestSendPath(RestoresBridgeState):

    def test_a_meshtastic_frame_goes_straight_to_the_interface(self):
        rns_bridge.set_radio_firmware("meshtastic")
        rns_bridge._lora_iface = FakeIface()
        self.assertTrue(rns_bridge._transmit_sealed(b"sealed", link=rns_bridge.LINK_LORA))
        self.assertEqual(rns_bridge._lora_iface.sent, [b"sealed"])

    def test_no_interface_reports_a_failure_rather_than_raising(self):
        rns_bridge.set_radio_firmware("meshtastic")
        rns_bridge._lora_iface = None
        self.assertFalse(rns_bridge._transmit_sealed(b"x", link=rns_bridge.LINK_LORA))

    def test_an_rnode_frame_still_goes_through_rns_packet(self):
        rns_bridge.set_radio_firmware("rnode")
        rns_bridge._lora_iface = FakeIface()
        rns_bridge._transmit_sealed(b"x", link=rns_bridge.LINK_LORA)
        self.assertEqual(rns_bridge._lora_iface.sent, [])   # the stub Packet took it


class TestRnodeOnlyFeatures(RestoresBridgeState):

    def setUp(self):
        super().setUp()
        rns_bridge.set_radio_firmware("meshtastic")
        # An interface with NO .serial: any code that reaches for it raises.
        rns_bridge._lora_iface = FakeIface()

    def test_rnode_iface_is_none_for_a_meshtastic_board(self):
        self.assertIsNone(rns_bridge._rnode_iface())

    def test_rnode_iface_is_the_interface_for_an_rnode(self):
        rns_bridge.set_radio_firmware("rnode")
        self.assertIs(rns_bridge._rnode_iface(), rns_bridge._lora_iface)

    def test_radio_settings_are_saved_for_the_profile_and_not_pushed(self):
        # Until 2026-10-14 this was refused, which left a phone's managed board
        # profile stuck on an old RNode preset with no way to change it. Now
        # the settings are saved (they are the profile's source) and nothing
        # is sent to the board from here.
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "radio")
        saved_path = rns_bridge._RADIO_CONFIG_PATH
        rns_bridge._RADIO_CONFIG_PATH = path
        self.addCleanup(lambda: setattr(rns_bridge, "_RADIO_CONFIG_PATH", saved_path))
        self.assertEqual(rns_bridge.set_radio_config("Medium Fast", 915.0, 20), "")
        self.assertEqual(rns_bridge._radio_config["preset"], "Medium Fast")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(rns_bridge._lora_iface.sent, [])

    def test_bad_radio_settings_are_still_refused(self):
        self.assertNotEqual(rns_bridge.set_radio_config("Long Fast", 2400.0, 22), "")

    def test_power_above_the_boards_limit_is_refused_not_capped_quietly(self):
        # 31-37 dBm passes the RNode check, and the managed profile would cap
        # it at 30 without saying so.
        import mt_config
        limit = mt_config.TX_POWER_MAX_DBM
        self.assertEqual(json.loads(rns_bridge.get_radio_config())["txpower_max_dbm"], limit)
        problem = rns_bridge.set_radio_config("Long Fast", 915.0, limit + 1)
        self.assertIn(str(limit), problem)

    def test_an_rnode_keeps_its_own_power_limit(self):
        rns_bridge.set_radio_firmware("rnode")
        self.assertEqual(json.loads(rns_bridge.get_radio_config())["txpower_max_dbm"],
                         rns_bridge.radio.TXPOWER_MAX_DBM)

    def test_bluetooth_pairing_is_refused_with_a_reason(self):
        self.assertIn("Meshtastic", rns_bridge.enable_rnode_bluetooth("on"))

    def test_identify_is_refused_with_a_reason(self):
        self.assertIn("Meshtastic", rns_bridge.identify_radio())

    def test_the_spectrum_survey_is_refused_with_a_reason(self):
        self.assertIn("Meshtastic", rns_bridge.start_sweep())

    def test_none_of_them_touched_the_interface(self):
        import tempfile
        saved_path = rns_bridge._RADIO_CONFIG_PATH
        rns_bridge._RADIO_CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "radio")
        self.addCleanup(lambda: setattr(rns_bridge, "_RADIO_CONFIG_PATH", saved_path))
        rns_bridge.set_radio_config("Long Fast", 915.0, 22)
        rns_bridge.enable_rnode_bluetooth("on")
        rns_bridge.identify_radio()
        rns_bridge.start_sweep()
        self.assertEqual(rns_bridge._lora_iface.sent, [])
        self.assertFalse(hasattr(rns_bridge._lora_iface, "serial"))


class TestKotlinAndPythonAgree(unittest.TestCase):
    """Two copies of the firmware names, one in each language, and a mismatch is
    silent: Kotlin hands Python a string it does not recognise, which falls back
    to RNode, and the operator who ticked "Meshtastic" gets an RNode interface
    waiting on a board that never answers. Same shape as the tone-profile drift
    test in test_fm_link."""

    KOTLIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "java", "io", "github", "stress5094",
                          "customrnsbridge")

    def read(self, name):
        with open(os.path.join(self.KOTLIN, name), encoding="utf8") as f:
            return f.read()

    def test_the_firmware_names_match(self):
        src = self.read("RadioLink.kt")
        self.assertIn('const val RADIO_RNODE = "%s"' % rns_bridge.RADIO_RNODE, src)
        self.assertIn('const val RADIO_MESHTASTIC = "%s"' % rns_bridge.RADIO_MESHTASTIC, src)

    def test_kotlin_actually_hands_the_setting_to_python(self):
        self.assertIn('callAttr("set_radio_firmware", RadioLink.radioFirmware(context))',
                      self.read("PythonRnsService.kt"))

    def test_the_entry_point_kotlin_calls_exists(self):
        self.assertTrue(callable(rns_bridge.set_radio_firmware))

    def test_the_bulk_only_driver_is_limited_to_the_chip_it_was_written_for(self):
        # Measured on a RAK4631: no DTR -> zero bytes back; DTR high -> the
        # whole configuration. The bulk-only driver cannot raise DTR, so it must
        # not be handed boards that need it.
        src = self.read("BulkOnlyCdcDriver.kt")
        self.assertIn("const val ESPRESSIF_VID = 0x303A", src)
        i = src.index("fun probe(device: UsbDevice): Boolean")
        self.assertIn("device.vendorId != ESPRESSIF_VID", src[i:i + 200])

    def test_dtr_is_raised_for_meshtastic_and_only_for_meshtastic(self):
        src = self.read("UsbSerialBridgeService.kt")
        i = src.index("if (RadioLink.isMeshtastic(this)) {")
        block = src[i:i + 200]
        self.assertIn("port.dtr = true", block)
        # RTS is deliberately never touched: some ESP32 auto-reset wiring resets
        # the board on certain DTR/RTS combinations.
        self.assertNotIn("rts = true", src)
        self.assertEqual(src.count("port.dtr = true"), 1)

    def test_every_python_method_the_board_dialog_calls_exists(self):
        # A Kotlin call to a Python function that is not there fails at runtime,
        # on a phone, as a toast at best.
        import re
        src = self.read("MeshBoardDialog.kt")
        # Directly, or through apply(), which takes the function's name.
        called = set(re.findall(r'(?:call|apply)\("([a-z_]+)"', src))
        self.assertEqual(called, {"get_mesh_board_config", "preview_mesh_board_config",
                                  "set_mesh_board_config", "get_mesh_board_url",
                                  "preview_mesh_board_profile", "apply_mesh_board_profile"})
        for name in called:
            self.assertTrue(callable(getattr(rns_bridge, name, None)), name)

    def test_network_settings_reaches_the_board_dialog_for_a_meshtastic_board(self):
        # Since 2026-11-10 the board dialog is opened from Network settings
        # (which replaced the separate Radio / LoRa settings button).
        src = self.read("MainActivity.kt")
        i = src.index("private fun buildNetworkDialog(")
        j = src.index("\n    private fun ", i + 10)
        body = src[i:j]
        self.assertIn("RadioLink.isMeshtastic(this)", body)
        self.assertIn("MeshBoardDialog(", body)

    def test_the_dialog_never_asks_python_for_the_key_except_in_a_share_link(self):
        src = self.read("MeshBoardDialog.kt")
        # The only call that returns a key is the share link, behind a warning.
        self.assertIn("contains the channel key", src)
        self.assertNotIn('"psk"', src.replace('r.put("psk", it)', ''))

    def test_meshtastic_is_never_reported_over_fm(self):
        # The guard that stops a stale "meshtastic" being handed to Python on a
        # link with no LoRa board at all. USB and Bluetooth both carry it.
        src = self.read("RadioLink.kt")
        self.assertIn("if (transport(context) == TRANSPORT_FM) RADIO_RNODE", src)

    def test_the_bluetooth_bridge_knows_the_meshtastic_service(self):
        # The UUIDs are copied from the official package, which is what Python
        # runs; a typo is a board that connects and is "missing a service".
        from meshtastic import ble_interface as ble
        src = self.read("BleSerialBridgeService.kt")
        for uuid in (ble.SERVICE_UUID, ble.TORADIO_UUID, ble.FROMRADIO_UUID,
                     ble.FROMNUM_UUID):
            self.assertIn('UUID.fromString("%s")' % uuid, src)

    def test_the_pairing_dialog_asks_python_for_the_mesh_pin(self):
        # Without it a board with no screen cannot be paired at all.
        # With the board's address and name: the PIN is per board.
        self.assertIn('"get_mesh_bt_pin", device.address, device.name ?: ""',
                      self.read("RadioLinkDialog.kt"))
        self.assertTrue(callable(rns_bridge.get_mesh_bt_pin))

    def test_the_bridge_pin_is_the_derived_one(self):
        import mt_config
        key = bytes(range(32))
        old = rns_bridge._board_mesh_key
        rns_bridge._board_mesh_key = lambda: key
        try:
            self.assertEqual(rns_bridge.get_mesh_bt_pin("02:00:00:0B:AD:02", "1.3_ad02"),
                             "%06d" % mt_config.derive_bt_pin(key, 0x000BAD02))
            self.assertNotEqual(rns_bridge.get_mesh_bt_pin("02:00:00:0B:AD:02"),
                                rns_bridge.get_mesh_bt_pin("02:00:00:0B:AD:03"))
            self.assertEqual(rns_bridge.get_mesh_bt_pin("not an address"), "")
        finally:
            rns_bridge._board_mesh_key = old
        rns_bridge._board_mesh_key, saved = (lambda: None), rns_bridge._board_mesh_key
        try:
            self.assertEqual(rns_bridge.get_mesh_bt_pin("02:00:00:0B:AD:02"), "")
        finally:
            rns_bridge._board_mesh_key = saved

    def test_the_bluetooth_framing_matches_the_packages_stream(self):
        from meshtastic import stream_interface as st
        src = self.read("MeshtasticBleFraming.kt")
        self.assertIn("const val START1 = 0x%X" % st.START1, src)
        self.assertIn("const val START2 = 0x%X" % st.START2, src)
        self.assertIn("const val HEADER_LEN = %d" % st.HEADER_LEN, src)
        self.assertIn("const val MAX_MESSAGE = %d" % st.MAX_TO_FROM_RADIO_SIZE, src)

    def test_a_firmware_change_asks_for_a_restart(self):
        src = self.read("RadioLink.kt")
        self.assertIn("KEY_ACTIVE_RADIO_FIRMWARE, radioFirmware(context)", src)
        self.assertIn(".putString(KEY_ACTIVE_RADIO_FIRMWARE, radioFirmware(context))", src)


class KotlinBridgeStandIn(object):
    """A TCP server that behaves like the Kotlin byte pipe: whatever connects is
    joined, byte for byte, to the other end -- here the fake board's cable.

    ONE CLIENT AT A TIME, like the real RadioBridgeService. That is not a
    detail: Python probes the port with a connect-and-close before opening the
    real connection (rns_bridge._wait_for_kotlin_socket), and a stand-in that
    left the probe's pump running would have it steal the board's replies from
    the real connection -- an intermittent failure that looked like a slow board.
    """

    def __init__(self, board):
        self.board = board
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.connections = 0
        self._stop = False
        self._current = None        # (alive Event, conn) of the live client
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            if self._current is not None:
                old_alive, old_conn = self._current
                old_alive.clear()
                try:
                    old_conn.close()
                except OSError:
                    pass
            alive = threading.Event()
            alive.set()
            self._current = (alive, conn)
            cable = self.board.stream._sock
            threading.Thread(target=self._pump, args=(conn, cable, alive), daemon=True).start()
            threading.Thread(target=self._pump, args=(cable, conn, alive), daemon=True).start()

    def _pump(self, src, dst, alive):
        src.settimeout(0.1)
        while alive.is_set() and not self._stop:
            try:
                chunk = src.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            if not alive.is_set():
                break
            try:
                dst.sendall(chunk)
            except OSError:
                break
        alive.clear()

    def drop_client(self):
        """What a board restart looks like from Python: the byte pipe closes
        under it, then the board is there again for the next connection."""
        if self._current is not None:
            self._current[0].clear()
            try:
                self._current[1].close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        if self._current is not None:
            self._current[0].clear()
            try:
                self._current[1].close()
            except OSError:
                pass
        try:
            self.server.close()
        except OSError:
            pass


class BuiltBoardCase(RestoresBridgeState):

    def setUp(self):
        super().setUp()
        self.board = tmp.FakeBoard(preset="SHORT_FAST")
        self.kotlin = KotlinBridgeStandIn(self.board)
        self.addCleanup(self.kotlin.close)
        self.addCleanup(self.board.close)
        rns_bridge.RNODE_BRIDGE_PORT = self.kotlin.port
        rns_bridge.set_radio_firmware("meshtastic")

        # The stub RNS has no transport or interface constants; give it just
        # enough, and take it away again.
        self.added = []
        RNS = rns_bridge.RNS
        stub_iface = sys.modules["RNS.Interfaces.Interface"].Interface
        patches = [(RNS.Transport, "add_interface", lambda i: self.added.append(i)),
                   (stub_iface, "final_init", lambda self: None),
                   (stub_iface, "MODE_FULL", 1),
                   (stub_iface, "DEFAULT_IFAC_SIZE", 16)]
        for obj, name, value in patches:
            had = name in vars(obj)
            old = vars(obj).get(name)
            setattr(obj, name, value)
            self.addCleanup(lambda o=obj, n=name, h=had, v=old:
                            setattr(o, n, v) if h else delattr(o, n))

        self.delivered = []
        rns_bridge._on_rns_packet_received = lambda data, iface: self.delivered.append((data, iface))
        rns_bridge._lora_iface = None
        self.iface = rns_bridge._build_mesh_interface()
        self.addCleanup(self.iface.port.stop)



class TestBuildMeshInterface(BuiltBoardCase):

    def test_it_registers_as_the_lora_link_and_comes_online(self):
        self.assertIs(rns_bridge._lora_iface, self.iface)
        self.assertIs(rns_bridge._link_ifaces[rns_bridge.LINK_LORA], self.iface)
        self.assertEqual(self.added, [self.iface])
        self.assertTrue(self.iface.online)
        self.assertTrue(self.iface.OUT and self.iface.IN)

    def test_it_connected_through_the_kotlin_port_not_a_device_path(self):
        self.assertGreaterEqual(self.kotlin.connections, 1)

    def test_the_airtime_model_follows_the_boards_own_preset(self):
        self.assertEqual(self.iface.preset, "Short Fast")
        self.assertAlmostEqual(self.iface.bitrate, mt_link.bitrate_bps("Short Fast"))

    def test_the_airtime_link_charges_the_boards_wrapper_not_rns(self):
        self.assertEqual(rns_bridge._lora_link.header_bytes, mt_link.WIRE_OVERHEAD_B)

    def test_a_sealed_frame_reaches_the_board_whole(self):
        data = bytes(range(200))
        self.assertTrue(rns_bridge._transmit_sealed(data, link=rns_bridge.LINK_LORA))
        self.assertTrue(tmp.wait_for(lambda: self.board.received))
        self.assertEqual(self.board.received[0].decoded.payload, data)

    def test_a_received_packet_is_delivered_to_the_bridge_with_this_interface(self):
        self.board.deliver(b"hello", snr=5.5, rssi=-88)
        self.assertTrue(tmp.wait_for(lambda: self.delivered))
        data, iface = self.delivered[0]
        self.assertEqual(data, b"hello")
        self.assertIs(iface, self.iface)
        # ...so the bridge's own identity-based link lookup finds LoRa.
        self.assertEqual(rns_bridge._link_of(iface), rns_bridge.LINK_LORA)
        self.assertEqual((self.iface.r_stat_rssi, self.iface.r_stat_snr), (-88, 5.5))

    def test_the_interface_knows_the_boards_preset_for_the_session_line(self):
        # The session log line prints this, not the app's own saved radio
        # settings (which are never applied to a Meshtastic board).
        self.assertEqual(self.iface.preset, "Short Fast")
        self.assertEqual(self.iface.HW_MTU, 231)

    def test_the_status_block_says_which_firmware_is_running(self):
        import json
        status = json.loads(rns_bridge.get_status())
        self.assertEqual(status.get("radio_firmware"), "meshtastic")


class FastBoardTimings(BuiltBoardCase):
    """A built board with the apply sequence's waits cut to test speed."""

    def setUp(self):
        super().setUp()
        self._saved_timing = (rns_bridge._MESH_SETTLE_S, rns_bridge._MESH_COMEBACK_S,
                              rns_bridge._MESH_RESTART_WATCH_S)
        rns_bridge._MESH_SETTLE_S = 0.05
        rns_bridge._MESH_COMEBACK_S = 20.0
        rns_bridge._MESH_RESTART_WATCH_S = 0.4
        self.iface.port.RECONNECT_WAIT_S = 0.1
        for name, val in zip(("_MESH_SETTLE_S", "_MESH_COMEBACK_S", "_MESH_RESTART_WATCH_S"),
                             self._saved_timing):
            self.addCleanup(lambda n=name, v=val: setattr(rns_bridge, n, v))

class TestBoardSettings(FastBoardTimings):
    """The sequencing around a real board: apply, let it restart, reconnect,
    READ BACK, report. The fake board keeps what it is written across
    connections, like flash, so the read-back is a real read."""

    def call(self, request):
        import json
        return json.loads(rns_bridge.set_mesh_board_config(json.dumps(request)))

    # -- reading ---------------------------------------------------------

    def test_reading_returns_the_boards_settings_and_the_choices(self):
        import json
        r = json.loads(rns_bridge.get_mesh_board_config())
        self.assertTrue(r["ok"])
        self.assertEqual(r["snapshot"]["preset_name"], "Short Fast")
        self.assertEqual(r["snapshot"]["region"], "US")
        self.assertEqual(r["snapshot"]["tx_power_dbm"], 22)
        self.assertIn("EU_868", r["regions"])
        self.assertIn("Long Fast", r["presets"])
        self.assertEqual(len(r["presets"]), len(r["preset_labels"]))
        self.assertIn("NONE", [c["value"] for c in r["rebroadcast_choices"]])

    def test_reading_never_returns_the_channel_key(self):
        import base64
        text = rns_bridge.get_mesh_board_config()
        key = bytes(self.board.channel0.settings.psk)
        self.assertNotIn(base64.b64encode(key).decode(), text)
        self.assertNotIn(base64.urlsafe_b64encode(key).decode(), text)

    def test_reading_says_why_when_this_is_not_a_meshtastic_board(self):
        import json
        rns_bridge.set_radio_firmware("rnode")
        r = json.loads(rns_bridge.get_mesh_board_config())
        self.assertFalse(r["ok"])
        self.assertIn("not a Meshtastic", r["reason"])

    def test_reading_says_why_when_the_board_is_not_connected(self):
        import json
        self.iface.port.stop()
        r = json.loads(rns_bridge.get_mesh_board_config())
        self.assertFalse(r["ok"])
        self.assertIn("not connected", r["reason"])

    def test_the_share_link_is_the_boards_and_carries_the_key_only_on_request(self):
        import mt_config
        url = rns_bridge.get_mesh_board_url()
        chan_set = mt_config.parse_url(url)
        self.assertEqual(chan_set.settings[0].name, "fake")
        self.assertEqual(bytes(chan_set.settings[0].psk), bytes(self.board.channel0.settings.psk))

    # -- writing ---------------------------------------------------------

    def test_a_change_is_applied_confirmed_by_reading_the_board_back(self):
        gen = self.iface.port.generation
        r = self.call({"tx_power_dbm": 17})
        self.assertTrue(r["ok"], r)
        self.assertIn("confirmed by the board", r["message"])
        self.assertEqual(self.board.lora.tx_power, 17)
        self.assertEqual(self.board.admin_log, ["begin", "set_config:lora", "commit"])
        self.assertGreater(self.iface.port.generation, gen)       # it really reconnected
        self.assertEqual(r["changes"][0]["field"], "tx_power_dbm")

    def test_the_status_block_carries_the_boards_counters_and_warnings(self):
        import json
        board = json.loads(rns_bridge.get_status())["board"]
        self.assertTrue(board["online"])
        for key in ("sent", "received", "queue_rejected", "send_refused_full",
                    "tx_refused", "last_error", "connections", "warnings"):
            self.assertIn(key, board)
        self.assertEqual(board["connections"], 1)
        # The fake board repeats everything, like the bench boards did.
        self.assertTrue(any("set to repeat other nodes' packets" in w for w in board["warnings"]))

    def test_the_warning_goes_away_once_the_board_is_told_never_to_repeat(self):
        import json
        r = self.call({"rebroadcast": "NONE"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(json.loads(rns_bridge.get_status())["board"]["warnings"], [])

    def test_sending_is_counted_in_the_status_block(self):
        import json
        before = json.loads(rns_bridge.get_status())["board"]["sent"]
        self.assertTrue(rns_bridge._transmit_sealed(b"abc", link=rns_bridge.LINK_LORA))
        self.assertEqual(json.loads(rns_bridge.get_status())["board"]["sent"], before + 1)

    def test_an_rnode_has_no_board_block(self):
        import json
        rns_bridge.set_radio_firmware("rnode")
        self.assertEqual(json.loads(rns_bridge.get_status())["board"], {})

    def test_the_status_poll_never_raises_even_with_no_interface(self):
        rns_bridge._lora_iface = None
        self.assertEqual(rns_bridge._mesh_board_status(), {})

    def test_the_status_block_never_contains_the_key(self):
        import base64
        text = rns_bridge.get_status()
        key = bytes(self.board.channel0.settings.psk)
        self.assertNotIn(base64.b64encode(key).decode(), text)

    def test_a_change_that_restarts_the_board_waits_for_the_restart_and_reads_again(self):
        # Measured on a real board: a preset change restarts it ~9 s AFTER the
        # write. Here the restart is simulated by the byte pipe dropping while
        # the function is watching for it.
        rns_bridge._MESH_RESTART_WATCH_S = 8.0
        import threading
        import mt_config
        real_plan = mt_config.plan
        calls = []

        def plan_then_restart(node, request):
            result = real_plan(node, request)
            calls.append(1)
            if len(calls) == 2:
                # The 1st call is the apply's own; the 2nd is the first
                # READ-BACK. The board restarts just after it, as the real one
                # did (~9 s after the write).
                threading.Timer(0.2, self.kotlin.drop_client).start()
            return result
        mt_config.plan = plan_then_restart
        self.addCleanup(lambda: setattr(mt_config, "plan", real_plan))
        gen = self.iface.port.generation
        r = self.call({"preset": "Long Slow"})
        self.assertTrue(r["ok"], r)
        self.assertIn("after it restarted", r["message"])
        # one reconnect for the read-back, one for the restart
        self.assertGreaterEqual(self.iface.port.generation - gen, 2)
        self.assertEqual(self.iface.preset, "Long Slow")

    def test_a_restart_class_change_with_no_restart_is_still_confirmed(self):
        r = self.call({"preset": "Long Slow"})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("after it restarted", r["message"])

    def test_a_setting_that_does_not_restart_the_board_does_not_wait_for_one(self):
        import time as _t
        rns_bridge._MESH_RESTART_WATCH_S = 30.0       # would make this test hang if it waited
        t0 = _t.time()
        r = self.call({"tx_power_dbm": 17})
        self.assertTrue(r["ok"], r)
        self.assertLess(_t.time() - t0, 10.0)

    def test_a_preview_reports_the_changes_and_sends_nothing(self):
        import json
        r = json.loads(rns_bridge.preview_mesh_board_config(
            json.dumps({"tx_power_dbm": 17, "rebroadcast": "NONE", "psk": "random"})))
        self.assertTrue(r["ok"], r)
        self.assertEqual([c["field"] for c in r["changes"]],
                         ["tx_power_dbm", "rebroadcast", "psk"])
        self.assertEqual(self.board.admin_log, [])
        self.assertEqual(self.board.lora.tx_power, 22)

    def test_a_preview_of_nothing_new_says_so_by_being_empty(self):
        import json
        r = json.loads(rns_bridge.preview_mesh_board_config(json.dumps({"tx_power_dbm": 22})))
        self.assertTrue(r["ok"])
        self.assertEqual(r["changes"], [])

    def test_a_preview_of_a_bad_request_explains_why(self):
        import json
        r = json.loads(rns_bridge.preview_mesh_board_config(json.dumps({"region": "ATLANTIS"})))
        self.assertFalse(r["ok"])
        self.assertIn("region", r["message"].lower())

    def test_a_preview_never_returns_a_key(self):
        import base64, json
        raw = rns_bridge.preview_mesh_board_config(json.dumps({"psk": "random"}))
        self.assertNotIn(base64.b64encode(bytes(self.board.channel0.settings.psk)).decode(), raw)

    def test_the_airtime_model_follows_a_preset_change(self):
        r = self.call({"preset": "Long Slow"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.iface.preset, "Long Slow")

    def test_asking_for_what_the_board_already_has_writes_nothing(self):
        r = self.call({"tx_power_dbm": 22, "region": "US", "preset": "Short Fast"})
        self.assertTrue(r["ok"])
        self.assertIn("Nothing to change", r["message"])
        self.assertEqual(self.board.admin_log, [])
        self.assertEqual(self.iface.port.generation, 1)           # no reboot, no reconnect

    def test_a_bad_request_is_explained_and_nothing_is_sent(self):
        r = self.call({"tx_power_dbm": 99, "region": "US"})
        self.assertFalse(r["ok"])
        self.assertIn("power", r["message"].lower())
        self.assertEqual(self.board.admin_log, [])

    def test_a_typed_key_that_is_not_valid_is_refused_without_echoing_it(self):
        r = self.call({"psk": "AAAA"})
        self.assertFalse(r["ok"])
        self.assertNotIn("AAAA", r["message"])
        self.assertEqual(self.board.admin_log, [])

    def test_a_random_key_is_made_once_and_confirmed_and_never_returned(self):
        import base64, json
        raw = rns_bridge.set_mesh_board_config(json.dumps({"psk": "random"}))
        r = json.loads(raw)
        self.assertTrue(r["ok"], r)
        new_key = bytes(self.board.channel0.settings.psk)
        self.assertEqual(len(new_key), 32)
        self.assertNotEqual(new_key, bytes(range(32)))
        self.assertNotIn(base64.b64encode(new_key).decode(), raw)
        self.assertNotIn(base64.urlsafe_b64encode(new_key).decode(), raw)
        # A channel-only change: written as a channel, not as a radio config.
        self.assertEqual(self.board.admin_log, ["begin", "set_channel:0", "commit"])

    def test_importing_a_link_from_another_board_sets_this_one_to_match(self):
        import mt_config
        donor = tmp.FakeBoard(preset="LONG_SLOW")
        donor.channel0.settings.name = "squad"
        donor.channel0.settings.psk = bytes(range(50, 82))
        self.addCleanup(donor.close)
        # Build the link the way the Meshtastic app would, from the donor's config.
        from test_mt_config import FakeNode
        node = FakeNode(name="squad", psk=bytes(range(50, 82)))
        node.localConfig.lora.modem_preset = tmp.config_pb2.Config.LoRaConfig.ModemPreset.Value("LONG_SLOW")
        r = self.call({"url": mt_config.make_url(node)})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.board.channel0.settings.name, "squad")
        self.assertEqual(bytes(self.board.channel0.settings.psk), bytes(range(50, 82)))
        self.assertEqual(self.iface.preset, "Long Slow")
        self.assertIn("same region, preset", r["message"])    # the matching reminder

    def test_a_board_that_accepts_and_keeps_nothing_is_reported_not_trusted(self):
        self.board.ignore_writes = True
        r = self.call({"tx_power_dbm": 17})
        self.assertFalse(r["ok"])
        self.assertIn("did not keep", r["message"])
        self.assertIn("Transmit power", r["message"])

    def test_the_matching_reminder_is_left_off_for_a_power_only_change(self):
        r = self.call({"tx_power_dbm": 17})
        self.assertNotIn("same region", r["message"])

    def test_a_board_that_goes_quiet_after_the_write_is_reported_as_not_back(self):
        rns_bridge._MESH_COMEBACK_S = 0.5
        port = self.iface.port
        real_refresh = port.refresh

        def refresh_into_a_dead_bridge():
            self.kotlin.close()          # the "Kotlin" side stops accepting
            real_refresh()
        port.refresh = refresh_into_a_dead_bridge
        r = self.call({"tx_power_dbm": 17})
        self.assertFalse(r["ok"])
        self.assertIn("did not reconnect", r["message"])
        self.assertIn("may or may not have been saved", r["message"])

    def test_a_cable_that_is_already_gone_gives_a_readable_failure_not_a_crash(self):
        self.kotlin.close()
        self.board.pull_cable()
        r = self.call({"tx_power_dbm": 17})
        self.assertFalse(r["ok"])
        self.assertTrue(r["message"])
        self.assertNotIn("Traceback", r["message"])

    def test_two_changes_at_once_are_refused(self):
        self.assertTrue(rns_bridge._mesh_config_lock.acquire(blocking=False))
        try:
            r = self.call({"tx_power_dbm": 17})
        finally:
            rns_bridge._mesh_config_lock.release()
        self.assertFalse(r["ok"])
        self.assertIn("already being applied", r["message"])
        self.assertEqual(self.board.admin_log, [])

    def test_the_log_line_names_what_changed_and_never_the_key(self):
        import base64
        lines = []
        real_log = rns_bridge.RNS.log
        rns_bridge.RNS.log = lambda msg, *a, **k: lines.append(str(msg))
        self.addCleanup(lambda: setattr(rns_bridge.RNS, "log", real_log))
        self.call({"psk": "random", "tx_power_dbm": 17})
        text = "\n".join(lines)
        self.assertIn("Transmit power", text)
        key = bytes(self.board.channel0.settings.psk)
        self.assertNotIn(base64.b64encode(key).decode(), text)
        self.assertNotIn(key.hex(), text)


class TestBoardProfile(FastBoardTimings):
    """The one-tap apply (Phase 3): the board compared with the APP's settings
    on every status poll, and put on them by one call whose key never leaves
    Python."""

    PASSPHRASE = "correct horse battery staple"

    def setUp(self):
        super().setUp()
        saved = (rns_bridge._mesh_passphrase, rns_bridge._board_key_cache)
        self.addCleanup(lambda: (setattr(rns_bridge, "_mesh_passphrase", saved[0]),
                                 setattr(rns_bridge, "_board_key_cache", saved[1])))
        rns_bridge._mesh_passphrase = self.PASSPHRASE
        rns_bridge._radio_config = {"preset": "Long Fast", "frequency_hz": 915000000,
                                    "txpower_dbm": 20}

    def status(self):
        import json
        return json.loads(rns_bridge.get_status())["board"]

    def test_a_board_on_other_settings_is_flagged_with_what_differs(self):
        changes = " | ".join(self.status()["profile_changes"])
        # The fake board: Short Fast, 22 dBm, repeats, channel "fake".
        for label in ("Preset", "Frequency override", "Transmit power",
                      "Repeating", "Channel name", "Channel key"):
            self.assertIn(label, changes)

    def test_one_call_puts_the_board_on_the_apps_settings_and_the_flag_clears(self):
        import json
        import mt_config
        r = json.loads(rns_bridge.apply_mesh_board_profile())
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.status()["profile_changes"], [])
        name, key = mt_config.derive_channel(
            rns_bridge.mesh.derive_key(self.PASSPHRASE))
        self.assertEqual(self.board.channel0.settings.name, name)
        self.assertEqual(bytes(self.board.channel0.settings.psk), key)
        self.assertEqual(self.board.lora.tx_power, 20)
        self.assertAlmostEqual(self.board.lora.override_frequency, 915.0)
        self.assertEqual(self.iface.preset, "Long Fast")
        # THIS board's PIN: derived from the node number the board reported
        # at connect, the same one a phone reads off its Bluetooth address.
        num = rns_bridge._lora_iface.port.node_num
        self.assertIsNotNone(num)
        self.assertEqual(self.board.bluetooth.fixed_pin, mt_config.derive_bt_pin(
            rns_bridge.mesh.derive_key(self.PASSPHRASE), num))

    def test_the_preview_lists_the_same_changes_and_no_key(self):
        import json
        r = json.loads(rns_bridge.preview_mesh_board_profile())
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["changes"]), len(self.status()["profile_changes"]))
        self.assertNotIn("psk_bytes", rns_bridge.preview_mesh_board_profile())

    def test_neither_the_status_nor_the_answer_ever_carries_the_derived_key(self):
        import base64
        import mt_config
        _, key = mt_config.derive_channel(rns_bridge.mesh.derive_key(self.PASSPHRASE))
        text = (rns_bridge.get_status() + rns_bridge.preview_mesh_board_profile()
                + rns_bridge.apply_mesh_board_profile() + rns_bridge.get_status())
        for form in (base64.b64encode(key).decode(), base64.urlsafe_b64encode(key).decode(),
                     key.hex()):
            self.assertNotIn(form, text)

    def test_a_new_passphrase_makes_the_board_differ_again(self):
        import json
        self.assertTrue(json.loads(rns_bridge.apply_mesh_board_profile())["ok"])
        rns_bridge._mesh_passphrase = "a different set of words"
        changes = " | ".join(self.status()["profile_changes"])
        self.assertIn("Channel key", changes)
        self.assertIn("Channel name", changes)

    def test_the_boards_channel_load_reaches_the_status_screen(self):
        # Phase 4: the board's own telemetry, read through the same
        # _channel_conditions() an RNode's stats go through.
        import json
        self.board.deliver(tmp.telemetry_payload(channel_utilization=27.5,
                                                 air_util_tx=3.0, num_tx_relay=2),
                           from_node=tmp.BOARD_NODE,
                           portnum=tmp.portnums_pb2.PortNum.TELEMETRY_APP)
        self.assertTrue(tmp.wait_for(
            lambda: json.loads(rns_bridge.get_status())["board"]["telemetry"]))
        status = json.loads(rns_bridge.get_status())
        self.assertEqual(status["board"]["telemetry"]["channel_util_pct"], 27.5)
        self.assertIn("channel busy 27.5%", status["channel"])
        self.assertEqual(status["channel_load_pct"], 27.5)

    def test_the_apply_is_refused_while_another_change_is_running(self):
        import json
        rns_bridge._mesh_config_lock.acquire()
        try:
            r = json.loads(rns_bridge.apply_mesh_board_profile())
        finally:
            rns_bridge._mesh_config_lock.release()
        self.assertFalse(r["ok"])
        self.assertIn("already being applied", r["message"])


if __name__ == "__main__":
    unittest.main()
