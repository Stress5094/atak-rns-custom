"""That the Meshtastic port speaks the real protocol and honours mt_link's contract.

The board here is a FAKE, but it speaks the REAL wire format: the 0x94 0xC3
frame header and genuine ToRadio/FromRadio protobuf messages, in both
directions, through the REAL `meshtastic` package's own StreamInterface. Only
the radio is missing. That matters because everything this module is for -- the
handshake, the packet it builds, how a received packet is unpacked -- is the
package's behaviour, and a stub of the package would test only our own
assumptions about it.

What is NOT covered, and is not pretended to be: a real board's timing, its
firmware's refusal behaviour, and receiving a packet off the air. The first two
are checked by hand against the bench RAK4631 (see the commit notes); the last
needs a second radio.
"""

import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()

from meshtastic import config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2
from meshtastic.protobuf import admin_pb2, channel_pb2

import mt_link
import mt_port
import radio

START1, START2 = 0x94, 0xC3
BOARD_NODE = 0x000bad01


class SocketStream(object):
    """Just enough of a pyserial port for StreamInterface."""

    def __init__(self, sock):
        self._sock = sock
        self._sock.settimeout(0.05)
        self.closed = False

    def read(self, n=1):
        import serial
        try:
            data = self._sock.recv(n)
        except socket.timeout:
            return b""
        except OSError:
            raise serial.SerialException("device disconnected")
        if not data:
            # EOF is what a pulled cable looks like. pyserial's own socket://
            # handler raises on it rather than returning b"" (which the reader
            # would take for a quiet line and wait on forever).
            raise serial.SerialException("device disconnected")
        return data

    def write(self, data):
        self._sock.sendall(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass


class FakeBoard(object):
    """The other end of the cable."""

    def __init__(self, preset="MEDIUM_FAST", use_preset=True):
        # The board's own settings, kept across connections like a real board's
        # flash: a change written on one connection is what the next one is told.
        self.lora = config_pb2.Config.LoRaConfig()
        self.lora.use_preset = use_preset
        self.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.Value(preset)
        self.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value("US")
        self.lora.tx_power = 22
        self.lora.tx_enabled = True      # a real board ships with transmit on
        self.device = config_pb2.Config.DeviceConfig()
        self.device.rebroadcast_mode = config_pb2.Config.DeviceConfig.RebroadcastMode.Value("ALL")
        # A board with no screen out of the box: Bluetooth on, the default PIN.
        self.bluetooth = config_pb2.Config.BluetoothConfig()
        self.bluetooth.enabled = True
        self.bluetooth.mode = config_pb2.Config.BluetoothConfig.PairingMode.Value("FIXED_PIN")
        self.bluetooth.fixed_pin = 123456
        self.channel0 = channel_pb2.Channel()
        self.channel0.index = 0
        self.channel0.role = channel_pb2.Channel.Role.PRIMARY
        self.channel0.settings.name = "fake"
        self.channel0.settings.psk = bytes(range(32))
        self.mute = False           # a board that is there but says nothing (e.g. DTR low)
        self.answer_heartbeat = True
        self.admin_log = []         # what admin messages arrived, in order
        self.ignore_writes = False  # a board that accepts and then keeps nothing
        self.board_side, client_side = socket.socketpair()
        self.board_side.settimeout(0.05)
        self.stream = SocketStream(client_side)
        self.received = []          # ToRadio messages that carried a packet
        self.handshakes = 0
        self._stop = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _send(self, from_radio):
        b = from_radio.SerializeToString()
        try:
            with self._lock:
                self.board_side.sendall(
                    bytes([START1, START2, (len(b) >> 8) & 0xFF, len(b) & 0xFF]) + b)
        except OSError:
            pass

    def _run(self):
        buf = b""
        while not self._stop:
            try:
                chunk = self.board_side.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                i = buf.find(bytes([START1, START2]))
                if i < 0:
                    buf = buf[-1:] if buf else b""
                    break
                buf = buf[i:]
                if len(buf) < 4:
                    break
                n = (buf[2] << 8) | buf[3]
                if len(buf) < 4 + n:
                    break
                body, buf = buf[4:4 + n], buf[4 + n:]
                msg = mesh_pb2.ToRadio()
                msg.ParseFromString(body)
                self._on_to_radio(msg)

    def _on_to_radio(self, msg):
        if msg.HasField("want_config_id"):
            self.handshakes += 1
            if self.mute:
                return
            my = mesh_pb2.FromRadio()
            my.my_info.my_node_num = BOARD_NODE
            self._send(my)
            # A real board lists itself in its node database; the package's
            # waitForConfig() does not return until it has seen at least one.
            ni = mesh_pb2.FromRadio()
            ni.node_info.num = BOARD_NODE
            ni.node_info.user.id = "!%08x" % BOARD_NODE
            ni.node_info.user.long_name = "fake"
            self._send(ni)
            cfg = mesh_pb2.FromRadio()
            cfg.config.lora.CopyFrom(self.lora)
            self._send(cfg)
            dev = mesh_pb2.FromRadio()
            dev.config.device.CopyFrom(self.device)
            self._send(dev)
            bt = mesh_pb2.FromRadio()
            bt.config.bluetooth.CopyFrom(self.bluetooth)
            self._send(bt)
            ch = mesh_pb2.FromRadio()
            ch.channel.CopyFrom(self.channel0)
            self._send(ch)
            done = mesh_pb2.FromRadio()
            done.config_complete_id = msg.want_config_id
            self._send(done)
        elif msg.HasField("heartbeat"):
            if self.answer_heartbeat:
                self.set_queue_free(16)
        elif msg.HasField("packet"):
            if msg.packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP:
                self._on_admin(msg.packet.decoded.payload)
            else:
                self.received.append(msg.packet)

    def _on_admin(self, payload):
        admin = admin_pb2.AdminMessage()
        admin.ParseFromString(payload)
        if admin.HasField("get_config_request") or admin.HasField("get_owner_request"):
            return
        if admin.begin_edit_settings:
            self.admin_log.append("begin")
        elif admin.commit_edit_settings:
            self.admin_log.append("commit")
        elif admin.HasField("set_config"):
            which = admin.set_config.WhichOneof("payload_variant")
            self.admin_log.append("set_config:" + str(which))
            if not self.ignore_writes:
                if which == "lora":
                    self.lora.CopyFrom(admin.set_config.lora)
                elif which == "device":
                    self.device.CopyFrom(admin.set_config.device)
                elif which == "bluetooth":
                    self.bluetooth.CopyFrom(admin.set_config.bluetooth)
        elif admin.HasField("set_channel"):
            self.admin_log.append("set_channel:%d" % admin.set_channel.index)
            if not self.ignore_writes and admin.set_channel.index == 0:
                self.channel0.CopyFrom(admin.set_channel)

    def deliver(self, payload, from_node=0x1234ABCD, portnum=256, snr=6.5, rssi=-91,
                rx_time=0, hop_start=0, hop_limit=0, relay_node=0):
        fr = mesh_pb2.FromRadio()
        fr.packet.id = 7
        setattr(fr.packet, "from", from_node)
        fr.packet.to = 0xFFFFFFFF
        fr.packet.decoded.portnum = portnum
        fr.packet.decoded.payload = payload
        fr.packet.rx_snr = snr
        fr.packet.rx_rssi = rssi
        if rx_time:
            fr.packet.rx_time = int(rx_time)
        fr.packet.hop_start = hop_start
        fr.packet.hop_limit = hop_limit
        fr.packet.relay_node = relay_node
        self._send(fr)

    def queue_status(self, res, packet_id):
        fr = mesh_pb2.FromRadio()
        fr.queueStatus.res = res
        fr.queueStatus.free = 15
        fr.queueStatus.maxlen = 16
        fr.queueStatus.mesh_packet_id = packet_id
        self._send(fr)

    def set_queue_free(self, free):
        """The board reporting how many slots its transmit queue has left."""
        fr = mesh_pb2.FromRadio()
        fr.queueStatus.res = 0
        fr.queueStatus.free = free
        fr.queueStatus.maxlen = 16
        fr.queueStatus.mesh_packet_id = 0
        self._send(fr)

    def route_error(self, packet_id, reason):
        """The board telling us it could not send one of our packets."""
        fr = mesh_pb2.FromRadio()
        fr.packet.id = 99
        setattr(fr.packet, "from", BOARD_NODE)
        fr.packet.to = 0
        fr.packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
        fr.packet.decoded.request_id = packet_id
        routing = mesh_pb2.Routing()
        routing.error_reason = mesh_pb2.Routing.Error.Value(reason)
        fr.packet.decoded.payload = routing.SerializeToString()
        self._send(fr)

    def pull_cable(self):
        self._stop = True
        try:
            self.board_side.close()
        except OSError:
            pass

    def close(self):
        self.pull_cable()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class PortCase(unittest.TestCase):

    def make(self, reconnect=False):
        if not hasattr(self, "boards"):
            self.boards = []

        def opener():
            b = FakeBoard()
            self.boards.append(b)
            return b.stream
        port = mt_port.MeshtasticPort(opener, reconnect=reconnect)
        port.RECONNECT_WAIT_S = 0.1
        self.addCleanup(port.stop)
        self.addCleanup(lambda: [(b.close(), b.stream.close()) for b in self.boards])
        return port


class TestConnect(PortCase):

    def test_completes_the_real_handshake_and_goes_online(self):
        port = self.make()
        states = []
        port.set_state_listener(states.append)
        port.start()
        self.assertTrue(port.wait_online(10))
        self.assertEqual(states, [True])
        self.assertEqual(port.node_num, BOARD_NODE)
        self.assertEqual(self.boards[0].handshakes, 1)

    def test_start_returns_without_waiting_for_the_board(self):
        port = self.make()
        t0 = time.monotonic()
        port.start()
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_a_failing_opener_leaves_it_offline_and_records_why(self):
        def broken():
            raise OSError("no such device")
        port = mt_port.MeshtasticPort(broken, reconnect=False)
        self.addCleanup(port.stop)
        port.start()
        self.assertTrue(wait_for(lambda: port.last_error is not None))
        self.assertFalse(port.online)
        self.assertIn("no such device", port.last_error)

    def test_send_before_connected_is_refused(self):
        port = self.make()
        self.assertFalse(port.send(b"x"))


class TestBoardPreset(PortCase):

    def setUp(self):
        self.boards = []
        self.addCleanup(lambda: [(b.close(), b.stream.close()) for b in self.boards])

    def connect(self, **board_kwargs):
        def opener():
            b = FakeBoard(**board_kwargs)
            self.boards.append(b)
            return b.stream
        port = mt_port.MeshtasticPort(opener, reconnect=False)
        self.addCleanup(port.stop)
        port.start()
        self.assertTrue(port.wait_online(10))
        return port

    def test_reads_the_boards_own_preset_as_the_apps_name_for_it(self):
        self.assertEqual(self.connect(preset="MEDIUM_FAST").board_preset(), "Medium Fast")

    def test_every_app_preset_maps_from_a_board_preset(self):
        # The app's names ARE Meshtastic's, so each one must survive the
        # round trip through the board's enum.
        for name in radio.PRESETS:
            enum_name = name.upper().replace(" ", "_")
            self.assertEqual(self.connect(preset=enum_name).board_preset(), name)

    def test_a_board_not_using_a_preset_reports_none(self):
        self.assertIsNone(self.connect(use_preset=False).board_preset())

    def test_before_connecting_it_reports_none(self):
        port = mt_port.MeshtasticPort(lambda: None)
        self.assertIsNone(port.board_preset())


class TestSend(PortCase):

    def setUp(self):
        self.port = self.make()
        self.port.start()
        self.assertTrue(self.port.wait_online(10))
        self.board = self.boards[0]

    def sent(self, n=1):
        self.assertTrue(wait_for(lambda: len(self.board.received) >= n),
                        "board never got the packet")
        return self.board.received[n - 1]

    def test_the_packet_is_a_private_app_broadcast_with_our_bytes(self):
        payload = bytes(range(200))
        self.assertTrue(self.port.send(payload))
        p = self.sent()
        self.assertEqual(p.decoded.portnum, portnums_pb2.PortNum.PRIVATE_APP)
        self.assertEqual(p.decoded.payload, payload)
        self.assertEqual(p.to, 0xFFFFFFFF)

    def test_it_asks_for_no_acknowledgement_and_no_rebroadcast(self):
        self.port.send(b"hello")
        p = self.sent()
        self.assertFalse(p.want_ack)
        self.assertEqual(p.hop_limit, 0)

    def test_a_full_size_payload_is_sent_whole(self):
        payload = bytes([i & 0xFF for i in range(mt_link.hw_mtu())])
        self.assertTrue(self.port.send(payload))
        self.assertEqual(self.sent().decoded.payload, payload)

    def test_packets_arrive_in_order(self):
        for i in range(5):
            self.assertTrue(self.port.send(bytes([i]) * 10))
        self.sent(5)
        self.assertEqual([p.decoded.payload[0] for p in self.board.received],
                         [0, 1, 2, 3, 4])

    def test_a_board_rejection_is_counted_when_it_arrives(self):
        self.port.send(b"first")
        first = self.sent()
        self.board.queue_status(res=7, packet_id=first.id)   # TOO_LARGE
        self.assertTrue(wait_for(lambda: self.port.board_rejected == 1))
        self.assertIn("rejected", self.port.last_error)
        self.assertIn("%08x" % first.id, self.port.last_error)

    def test_a_later_all_clear_does_not_erase_a_rejection(self):
        # The real board follows a rejection with res=0, id=0. Reading only the
        # package's latest-status attribute lost the rejection to this.
        self.port.send(b"first")
        first = self.sent()
        self.board.queue_status(res=7, packet_id=first.id)
        self.board.queue_status(res=0, packet_id=0)
        self.assertTrue(wait_for(lambda: self.port.board_rejected == 1))
        time.sleep(0.2)
        self.assertEqual(self.port.board_rejected, 1)

    def test_accepted_packets_are_not_counted_as_rejections(self):
        self.port.send(b"first")
        first = self.sent()
        self.board.queue_status(res=0, packet_id=first.id)
        time.sleep(0.3)
        self.assertEqual(self.port.board_rejected, 0)


class TestDiagnostics(PortCase):
    """The logging build exists to answer questions about real firmware. These
    check that what it reports is right, so the answers can be trusted."""

    def setUp(self):
        self.port = self.make()
        self.lines = []
        self.port.log = self.lines.append
        self.port.HEARTBEAT_PROBE_S = 0.2
        self.port.HEARTBEAT_ANSWER_WAIT_S = 0.6
        self.port.RECONNECT_WAIT_S = 0.1

    def go(self):
        self.port.start()
        self.assertTrue(self.port.wait_online(10))
        self.board = self.boards[0]

    def line_with(self, text):
        return next((l for l in self.lines if text in l), None)

    # -- the liveness probe ----------------------------------------------

    def test_an_answered_heartbeat_is_logged_with_how_long_it_took(self):
        self.go()
        self.assertTrue(wait_for(lambda: self.line_with("heartbeat answered")))
        self.assertGreaterEqual(self.port.heartbeat_answered, 1)
        self.assertIsNotNone(self.port.last_heartbeat_s)
        self.assertLess(self.port.last_heartbeat_s, 0.6)
        self.assertIn("queueStatus", self.line_with("heartbeat answered"))

    def test_a_heartbeat_the_board_ignores_is_logged_as_unanswered(self):
        self.go()
        self.board.answer_heartbeat = False
        self.assertTrue(wait_for(lambda: self.line_with("NOT answered"), 8))
        self.assertGreaterEqual(self.port.heartbeat_missed, 1)

    # -- the watchdog -----------------------------------------------------

    def test_one_or_two_misses_do_not_tear_down_a_working_link(self):
        self.go()
        self.board.answer_heartbeat = False
        self.assertTrue(wait_for(lambda: self.port.heartbeat_missed >= 2, 10))
        self.board.answer_heartbeat = True                # it recovers before the 3rd
        self.assertTrue(wait_for(lambda: self.port.heartbeat_answered >= 1, 10))
        time.sleep(1.5)
        self.assertEqual(self.port.watchdog_restarts, 0)
        self.assertEqual(self.port.generation, 1)
        self.assertTrue(self.port.online)

    def test_three_misses_in_a_row_reconnect(self):
        self.go()
        self.board.answer_heartbeat = False
        self.assertTrue(wait_for(lambda: self.port.watchdog_restarts == 1, 15))
        # ...and the fresh connection (a new fake board, which does answer) is up.
        self.assertTrue(wait_for(lambda: self.port.generation == 2 and self.port.online, 15))
        self.assertIn("watchdog", self.port.last_error)
        self.assertEqual(len(self.boards), 2)
        self.assertIsNotNone(self.line_with("reconnecting"))

    def test_an_answer_resets_the_count_so_misses_must_be_consecutive(self):
        self.go()
        for _ in range(3):                                 # miss, miss, answer -- three times
            self.board.answer_heartbeat = False
            n = self.port.heartbeat_missed
            self.assertTrue(wait_for(lambda: self.port.heartbeat_missed >= n + 2, 10))
            self.board.answer_heartbeat = True
            a = self.port.heartbeat_answered
            self.assertTrue(wait_for(lambda: self.port.heartbeat_answered > a, 10))
        self.assertEqual(self.port.watchdog_restarts, 0)
        self.assertEqual(self.port.generation, 1)

    def test_a_healthy_board_is_never_restarted(self):
        self.go()
        self.assertTrue(wait_for(lambda: self.port.heartbeat_answered >= 5, 10))
        self.assertEqual(self.port.watchdog_restarts, 0)
        self.assertEqual(self.port.heartbeat_missed, 0)
        self.assertEqual(self.port.generation, 1)

    def test_the_watchdog_does_not_fire_after_the_port_is_stopped(self):
        self.go()
        self.board.answer_heartbeat = False
        self.port.stop()
        time.sleep(2.5)
        self.assertEqual(self.port.watchdog_restarts, 0)
        self.assertEqual(len(self.boards), 1)

    # -- the stale-packet guard ---------------------------------------------

    def test_a_replayed_packet_just_after_connecting_is_dropped(self):
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.go()
        self.board.deliver(b"old", rx_time=time.time() - 120)
        self.assertTrue(wait_for(lambda: self.port.dropped_stale == 1))
        self.assertEqual(got, [])
        self.assertEqual(self.port.received, 0)
        self.assertIsNotNone(self.line_with("dropped a stale packet"))

    def test_a_live_packet_just_after_connecting_is_delivered(self):
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.go()
        self.board.deliver(b"live", rx_time=time.time() - 1)
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(self.port.dropped_stale, 0)

    def test_the_age_limit_is_twenty_seconds(self):
        got = []
        self.port.set_receiver(lambda *a: got.append(a[0]))
        self.go()
        self.board.deliver(b"young", rx_time=time.time() - 15)
        self.board.deliver(b"older", rx_time=time.time() - 25)
        self.assertTrue(wait_for(lambda: self.port.dropped_stale == 1))
        self.assertEqual(got, [b"young"])

    def test_after_the_grace_window_nothing_is_ever_filtered(self):
        # A board whose clock is simply wrong must not be able to silence a live link.
        self.port.STALE_GRACE_S = 0.3
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.go()
        time.sleep(0.6)
        self.board.deliver(b"wrong clock", rx_time=time.time() - 3600)
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(self.port.dropped_stale, 0)

    def test_a_packet_with_no_receive_time_is_not_judged(self):
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.go()
        self.board.deliver(b"no clock")
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(self.port.dropped_stale, 0)

    def test_a_board_with_no_real_clock_yet_is_not_judged(self):
        # A board that has not got the time reports seconds since boot or some
        # small number -- that is "no clock", not "a very old packet".
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.go()
        self.board.deliver(b"boot clock", rx_time=4242)
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(self.port.dropped_stale, 0)

    def test_a_packet_from_the_future_is_not_dropped(self):
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.go()
        self.board.deliver(b"ahead", rx_time=time.time() + 600)
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(self.port.dropped_stale, 0)

    def test_the_guard_restarts_with_each_connection(self):
        self.go()
        self.port.refresh()
        self.assertTrue(wait_for(lambda: self.port.generation == 2 and self.port.online, 15))
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.boards[1].deliver(b"old again", rx_time=time.time() - 300)
        self.assertTrue(wait_for(lambda: self.port.dropped_stale == 1))
        self.assertEqual(got, [])

    def test_the_probe_stops_when_the_port_stops(self):
        self.go()
        self.assertTrue(wait_for(lambda: self.line_with("heartbeat answered")))
        self.port.stop()
        time.sleep(0.4)
        n = len(self.lines)
        time.sleep(1.0)
        self.assertEqual(len(self.lines), n)

    def probes(self):
        # THIS port's probes only: other tests' ports wind their own down in their
        # own time, and counting them process-wide made this flaky.
        return [t for t in self.port._probe_threads if t.is_alive()]

    def test_a_refresh_leaves_one_probe_not_two_even_with_the_real_interval(self):
        # The real 30 s interval: a probe that sleeps it out after its connection
        # closed would still be alive here.
        self.port.HEARTBEAT_PROBE_S = 30.0
        self.go()
        self.assertEqual(len(self.probes()), 1)
        self.port.refresh()
        self.assertTrue(wait_for(lambda: self.port.generation == 2 and self.port.online, 15))
        self.assertTrue(wait_for(lambda: len(self.probes()) == 1, 3), len(self.probes()))

    def test_stopping_ends_the_probe_at_once_not_after_its_interval(self):
        self.port.HEARTBEAT_PROBE_S = 30.0
        self.go()
        self.assertEqual(len(self.probes()), 1)
        self.port.stop()
        self.assertTrue(wait_for(lambda: len(self.probes()) == 0, 3), len(self.probes()))

    # -- the per-packet line ---------------------------------------------

    def test_a_packet_with_a_receive_time_reports_its_age(self):
        self.go()
        # 15 s old: old enough to show an age, young enough that the stale guard
        # (20 s) does not drop it before it is described.
        self.board.deliver(b"abc", rx_time=time.time() - 15)
        self.assertTrue(wait_for(lambda: self.line_with("rx from=")))
        line = self.line_with("rx from=")
        self.assertRegex(line, r"age=(14|15|16)\.\ds")
        self.assertIn("len=3", line)

    def test_a_packet_with_no_receive_time_says_so_rather_than_guessing(self):
        self.go()
        self.board.deliver(b"abc")
        self.assertTrue(wait_for(lambda: self.line_with("rx from=")))
        line = self.line_with("rx from=")
        self.assertIn("rx_time=none", line)
        self.assertIn("age=n/a", line)

    def test_a_packet_straight_from_its_sender_is_reported_direct_with_zero_hops(self):
        self.go()
        # Absent hop fields (zero) and a relay byte equal to the sender's last byte.
        self.board.deliver(b"abc", from_node=0x1234ABCD, relay_node=0xCD)
        self.assertTrue(wait_for(lambda: self.line_with("rx from=")))
        line = self.line_with("rx from=")
        self.assertIn("hops=0", line)
        self.assertIn("path=direct", line)

    def test_a_packet_another_board_repeated_is_reported_as_repeated(self):
        self.go()
        self.board.deliver(b"abc", from_node=0x1234ABCD, hop_start=3, hop_limit=2, relay_node=0x77)
        self.assertTrue(wait_for(lambda: self.line_with("rx from=")))
        line = self.line_with("rx from=")
        self.assertIn("hops=1", line)
        self.assertIn("REPEATED by ..77", line)

    def test_a_packet_with_no_relay_byte_is_reported_unknown(self):
        self.go()
        self.board.deliver(b"abc")
        self.assertTrue(wait_for(lambda: self.line_with("rx from=")))
        # relay_node=0 is the proto default, so the package omits it.
        self.assertIn("path=unknown", self.line_with("rx from="))

    def test_the_time_since_connecting_is_reported_so_a_replayed_burst_shows(self):
        self.go()
        self.board.deliver(b"abc")
        self.assertTrue(wait_for(lambda: self.line_with("rx from=")))
        self.assertRegex(self.line_with("rx from="), r"since_connect=\d+\.\ds")

    def test_no_log_callback_means_no_work_and_no_error(self):
        self.port.log = None
        self.go()
        self.board.deliver(b"abc")
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.board.deliver(b"def")
        self.assertTrue(wait_for(lambda: got))

    def test_a_failing_log_callback_cannot_break_receiving(self):
        def boom(text):
            raise RuntimeError("log sink broke")
        self.port.log = boom
        got = []
        self.port.set_receiver(lambda *a: got.append(a))
        self.go()
        self.board.deliver(b"def")
        self.assertTrue(wait_for(lambda: got))

    def test_the_diagnostics_never_contain_payload_bytes(self):
        self.go()
        self.board.deliver(b"SECRET-PAYLOAD-123")
        self.assertTrue(wait_for(lambda: self.line_with("rx from=")))
        self.assertFalse(any("SECRET" in l for l in self.lines))


class TestOneWriterAtATime(PortCase):
    """Integrity under concurrent writers: senders and a heartbeat from several
    threads at once must all arrive whole. NOTE this passes with the write lock
    removed too -- no interleaving was ever reproduced -- so it guards the
    outcome (every frame intact), not the lock itself."""

    def test_concurrent_senders_and_heartbeats_never_corrupt_a_frame(self):
        port = self.make()
        port.start()
        self.assertTrue(port.wait_online(10))
        board = self.boards[0]
        iface = port._iface
        N = 25

        def sender(tag):
            for i in range(N):
                self.assertTrue(port.send(bytes([tag]) * 200))

        def heart():
            for _ in range(N):
                iface.sendHeartbeat()

        threads = [threading.Thread(target=sender, args=(1,)), threading.Thread(target=sender, args=(2,)),
                   threading.Thread(target=heart)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertTrue(wait_for(lambda: len(board.received) >= 2 * N, 30), len(board.received))
        self.assertEqual(len(board.received), 2 * N)
        for p in board.received:
            payload = bytes(p.decoded.payload)
            self.assertEqual(len(payload), 200)
            self.assertEqual(len(set(payload)), 1)          # one sender's bytes, never mixed


class TestBoundedSend(PortCase):
    """The package waits for a free slot in the board's queue with NO timeout. On
    the app's single transmit thread that is a freeze with no way out, taking
    acknowledgements and beacons with it. These tests run send() on a helper
    thread so a regression shows up as a failed assertion, not a hung suite."""

    def setUp(self):
        self.port = self.make()
        self.port.SEND_WAIT_FOR_SPACE_S = 0.6
        self.port.start()
        self.assertTrue(self.port.wait_online(10))
        self.board = self.boards[0]
        # The package sends a heartbeat the moment it connects, and the board
        # answers with a queue status of 16 free. Unawaited, that answer could
        # land AFTER a test's set_queue_free(0) and overwrite it -- the test
        # then waited for a 0 that never came. Flaky in 4 of 6 runs, a
        # different test each time (2026-10-16).
        self.wait_free(16)

    def send_in_thread(self, payload=b"x", timeout=5.0):
        result = []
        t = threading.Thread(target=lambda: result.append(self.port.send(payload)), daemon=True)
        t.start()
        t.join(timeout)
        return result[0] if result else "HUNG"

    def wait_free(self, n):
        iface = self.port._iface
        self.assertTrue(wait_for(lambda: iface.queueStatus is not None
                                 and iface.queueStatus.free == n))

    def test_a_full_queue_is_refused_not_waited_on_forever(self):
        self.board.set_queue_free(0)
        self.wait_free(0)
        self.assertIs(self.send_in_thread(), False)
        self.assertEqual(self.port.send_refused_full, 1)
        self.assertIn("queue stayed full", self.port.last_error)
        time.sleep(0.2)
        self.assertEqual(self.board.received, [])          # nothing reached the board

    def test_a_slot_that_frees_while_waiting_is_used(self):
        self.board.set_queue_free(0)
        self.wait_free(0)
        self.port.SEND_WAIT_FOR_SPACE_S = 4.0
        threading.Timer(0.5, lambda: self.board.set_queue_free(5)).start()
        self.assertIs(self.send_in_thread(timeout=8), True)
        self.assertEqual(self.port.send_refused_full, 0)
        self.assertTrue(wait_for(lambda: self.board.received))

    def test_a_queue_with_room_does_not_wait_at_all(self):
        self.board.set_queue_free(8)
        self.wait_free(8)
        t0 = time.monotonic()
        self.assertIs(self.send_in_thread(), True)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_a_board_that_has_not_reported_a_queue_is_treated_as_having_room(self):
        # Older firmware never sends one; the package itself says "go ahead".
        self.assertIs(self.send_in_thread(), True)

    def test_the_wait_ends_if_the_link_goes_down(self):
        self.board.set_queue_free(0)
        self.wait_free(0)
        self.port.SEND_WAIT_FOR_SPACE_S = 30.0
        threading.Timer(0.3, lambda: setattr(self.port, "online", False)).start()
        t0 = time.monotonic()
        self.assertIs(self.send_in_thread(timeout=10), False)
        self.assertLess(time.monotonic() - t0, 5)


class TestBoardRefusals(PortCase):
    """When the board will not send one of our packets it says so afterwards, in a
    routing error naming the packet. send() has already returned True by then."""

    def setUp(self):
        self.port = self.make()
        self.port.start()
        self.assertTrue(self.port.wait_online(10))
        self.board = self.boards[0]

    def sent_id(self):
        self.assertTrue(self.port.send(b"payload"))
        self.assertTrue(wait_for(lambda: self.board.received))
        return self.board.received[-1].id

    def test_a_refusal_of_our_packet_is_counted_by_reason(self):
        pid = self.sent_id()
        self.board.route_error(pid, "NO_CHANNEL")
        self.assertTrue(wait_for(lambda: self.port.tx_refused.get("NO_CHANNEL") == 1))
        self.assertIn("%08x" % pid, self.port.last_refusal)
        self.assertIn("NO_CHANNEL", self.port.last_refusal)

    def test_different_reasons_are_counted_separately(self):
        a = self.sent_id()
        b = self.sent_id()
        self.board.route_error(a, "NO_CHANNEL")
        self.board.route_error(b, "TOO_LARGE")
        self.assertTrue(wait_for(lambda: len(self.port.tx_refused) == 2))
        self.assertEqual(self.port.tx_refused, {"NO_CHANNEL": 1, "TOO_LARGE": 1})

    def test_a_refusal_naming_a_packet_that_is_not_ours_is_ignored(self):
        self.sent_id()
        self.board.route_error(0x0BADBEEF, "NO_CHANNEL")
        time.sleep(0.4)
        self.assertEqual(self.port.tx_refused, {})

    def test_an_acknowledgement_is_not_a_refusal(self):
        pid = self.sent_id()
        self.board.route_error(pid, "NONE")
        time.sleep(0.4)
        self.assertEqual(self.port.tx_refused, {})

    def test_the_same_refusal_is_not_counted_twice(self):
        pid = self.sent_id()
        self.board.route_error(pid, "NO_CHANNEL")
        self.board.route_error(pid, "NO_CHANNEL")
        self.assertTrue(wait_for(lambda: self.port.tx_refused.get("NO_CHANNEL") == 1))
        time.sleep(0.4)
        self.assertEqual(self.port.tx_refused["NO_CHANNEL"], 1)

    def test_only_recent_ids_are_remembered(self):
        self.port.PENDING_IDS = 3
        ids = [self.sent_id() for _ in range(5)]
        self.assertEqual(len(self.port._pending_ids), 3)
        self.board.route_error(ids[0], "NO_CHANNEL")      # long forgotten
        self.board.route_error(ids[-1], "NO_CHANNEL")     # recent
        self.assertTrue(wait_for(lambda: self.port.tx_refused.get("NO_CHANNEL") == 1))
        time.sleep(0.3)
        self.assertEqual(self.port.tx_refused["NO_CHANNEL"], 1)


class TestReceive(PortCase):

    def setUp(self):
        self.port = self.make()
        self.got = []
        self.port.set_receiver(lambda *a: self.got.append(a))
        self.port.start()
        self.assertTrue(self.port.wait_online(10))
        self.board = self.boards[0]

    def test_a_private_app_packet_is_delivered_with_its_signal_and_sender(self):
        self.board.deliver(b"\x01\x02\x03", from_node=0xCAFEF00D, snr=4.25, rssi=-97)
        self.assertTrue(wait_for(lambda: self.got))
        payload, rssi, snr, sender = self.got[0]
        self.assertEqual(payload, b"\x01\x02\x03")
        self.assertEqual(rssi, -97)
        self.assertEqual(snr, 4.25)
        self.assertEqual(sender, 0xCAFEF00D)

    def test_binary_payloads_survive_byte_for_byte(self):
        data = bytes(range(256))[:231]
        self.board.deliver(data)
        self.assertTrue(wait_for(lambda: self.got))
        self.assertEqual(self.got[0][0], data)

    def test_other_ports_are_not_ours(self):
        self.board.deliver(b"hello", portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP)
        self.board.deliver(b"ours")
        self.assertTrue(wait_for(lambda: self.got))
        self.assertEqual([g[0] for g in self.got], [b"ours"])

    def test_a_packet_that_claims_to_be_from_this_board_is_ignored(self):
        self.board.deliver(b"echo", from_node=BOARD_NODE)
        self.board.deliver(b"real")
        self.assertTrue(wait_for(lambda: self.got))
        self.assertEqual([g[0] for g in self.got], [b"real"])

    def test_only_the_owning_interface_is_listened_to(self):
        # The package publishes on a process-wide bus, so a second board in the
        # same process must not leak into this port.
        other = self.make()
        other.set_receiver(lambda *a: self.got.append(("other",) + a))
        other.start()
        self.assertTrue(other.wait_online(10))
        self.boards[1].deliver(b"for-other")
        self.assertTrue(wait_for(lambda: any(g[0] == "other" for g in self.got)))
        mine = [g for g in self.got if g[0] != "other"]
        self.assertEqual(mine, [])


class TestReconnect(PortCase):

    def test_a_pulled_cable_goes_offline_then_recovers(self):
        port = self.make(reconnect=True)
        states = []
        port.set_state_listener(states.append)
        port.start()
        self.assertTrue(port.wait_online(10))
        self.boards[0].pull_cable()
        self.assertTrue(wait_for(lambda: False in states, 10), states)
        self.assertFalse(port.send(b"x"))
        self.assertTrue(wait_for(lambda: states[-1:] == [True] and len(states) >= 3, 15),
                        states)
        self.assertEqual(len(self.boards), 2)
        self.assertTrue(port.send(b"back"))
        self.assertTrue(wait_for(lambda: self.boards[1].received))

    def test_no_reconnect_when_not_asked_for(self):
        port = self.make(reconnect=False)
        states = []
        port.set_state_listener(states.append)
        port.start()
        self.assertTrue(port.wait_online(10))
        self.boards[0].pull_cable()
        self.assertTrue(wait_for(lambda: False in states, 10))
        time.sleep(0.5)
        self.assertEqual(len(self.boards), 1)

    def test_stop_goes_offline_and_stays_there(self):
        port = self.make(reconnect=True)
        port.start()
        self.assertTrue(port.wait_online(10))
        port.stop()
        self.assertFalse(port.online)
        self.assertFalse(port.send(b"x"))


class TestSilentBoard(PortCase):
    """A board that is plugged in and says nothing: on a phone, a USB driver that
    never raised DTR; at startup, a connection that landed a moment too early."""

    def test_the_default_wait_for_a_board_is_short(self):
        self.assertLessEqual(mt_port.MeshtasticPort.CONNECT_TIMEOUT_S, 10)

    def test_a_silent_board_is_given_up_on_quickly_and_the_reason_is_recorded(self):
        boards = []

        def opener():
            b = FakeBoard()
            b.mute = True
            boards.append(b)
            return b.stream
        port = mt_port.MeshtasticPort(opener, reconnect=False, connect_timeout_s=1)
        self.addCleanup(port.stop)
        self.addCleanup(lambda: [(b.close(), b.stream.close()) for b in boards])
        t0 = time.monotonic()
        port.start()
        self.assertTrue(wait_for(lambda: port.last_error is not None, 8))
        self.assertLess(time.monotonic() - t0, 6)
        self.assertFalse(port.online)
        self.assertIn("connect", port.last_error)

    def test_it_connects_on_the_retry_once_the_board_starts_answering(self):
        boards = []

        def opener():
            b = FakeBoard()
            # The first attempt reaches a silent board; later ones a live one.
            b.mute = len(boards) == 0
            boards.append(b)
            return b.stream
        port = mt_port.MeshtasticPort(opener, reconnect=True, connect_timeout_s=1)
        port.RECONNECT_WAIT_S = 0.1
        self.addCleanup(port.stop)
        self.addCleanup(lambda: [(b.close(), b.stream.close()) for b in boards])
        t0 = time.monotonic()
        port.start()
        self.assertTrue(port.wait_online(15))
        self.assertGreaterEqual(len(boards), 2)
        self.assertLess(time.monotonic() - t0, 8)
        self.assertEqual(port.generation, 1)       # one SUCCESSFUL connection

    def test_a_connection_closed_before_the_board_answers_is_retried_at_once(self):
        # The phone's startup race: the bridge drops a connection made before
        # its Bluetooth link is up. MEASURED 2026-10-16: the wait sat out its
        # whole timeout (15 s) on a stream that had already closed.
        boards = []

        def opener():
            b = FakeBoard()
            if not boards:
                b.mute = True
                threading.Timer(0.2, b.pull_cable).start()
            boards.append(b)
            return b.stream
        port = mt_port.MeshtasticPort(opener, reconnect=True, connect_timeout_s=10)
        port.RECONNECT_WAIT_S = 0.1
        self.addCleanup(port.stop)
        self.addCleanup(lambda: [(b.close(), b.stream.close()) for b in boards])
        t0 = time.monotonic()
        port.start()
        self.assertTrue(port.wait_online(12))
        self.assertLess(time.monotonic() - t0, 4)    # not the 10 s timeout
        self.assertGreaterEqual(len(boards), 2)


class TestNodeAndRefresh(PortCase):

    def test_the_node_is_the_packages_and_carries_the_boards_config(self):
        port = self.make()
        self.assertIsNone(port.node())
        port.start()
        self.assertTrue(port.wait_online(10))
        node = port.node()
        self.assertEqual(node.localConfig.lora.modem_preset,
                         config_pb2.Config.LoRaConfig.ModemPreset.Value("MEDIUM_FAST"))
        self.assertTrue(len(node.channels) >= 1)

    def test_each_connection_bumps_the_generation(self):
        port = self.make()
        self.assertEqual(port.generation, 0)
        port.start()
        self.assertTrue(port.wait_online(10))
        self.assertEqual(port.generation, 1)

    def test_refresh_opens_a_fresh_connection_and_re_reads_the_board(self):
        port = self.make()
        states = []
        port.set_state_listener(states.append)
        port.start()
        self.assertTrue(port.wait_online(10))
        old_node = port.node()
        port.refresh()
        self.assertTrue(wait_for(lambda: port.generation == 2 and port.online, 15))
        self.assertEqual(states, [True, False, True])
        self.assertIsNot(port.node(), old_node)
        self.assertEqual(len(self.boards), 2)
        self.assertEqual(self.boards[1].handshakes, 1)
        self.assertTrue(port.send(b"after"))

    def test_a_second_refresh_while_connecting_does_not_open_a_second_connection(self):
        port = self.make()
        port.start()
        self.assertTrue(port.wait_online(10))
        port.refresh()
        port.refresh()
        self.assertTrue(wait_for(lambda: port.online, 15))
        time.sleep(0.5)
        self.assertEqual(len(self.boards), 2)

    def test_refresh_after_stop_does_nothing(self):
        port = self.make()
        port.start()
        self.assertTrue(port.wait_online(10))
        port.stop()
        port.refresh()
        time.sleep(0.3)
        self.assertFalse(port.online)
        self.assertEqual(len(self.boards), 1)


class TestWithTheInterface(PortCase):
    """The two halves together: MtInterface on a real MeshtasticPort."""

    def test_a_frame_goes_out_and_one_comes_back_through_the_interface(self):
        port = self.make()
        got = []

        class Owner(object):
            def inbound(self, data, iface):
                pass
        iface = mt_link.MtInterface(Owner(), port,
                                    deliver_frame=lambda d, i: got.append(
                                        (d, i.r_stat_rssi, i.r_stat_snr)))
        port.start()
        self.assertTrue(port.wait_online(10))
        self.assertTrue(wait_for(lambda: iface.online))

        self.assertTrue(iface.process_outgoing(b"A" * 100))
        self.assertTrue(wait_for(lambda: self.boards[0].received))
        self.assertEqual(self.boards[0].received[0].decoded.payload, b"A" * 100)

        self.boards[0].deliver(b"B" * 50, snr=3.0, rssi=-102)
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(got[0], (b"B" * 50, -102, 3.0))

    def test_an_oversize_frame_never_reaches_the_board(self):
        port = self.make()

        class Owner(object):
            def inbound(self, data, iface):
                pass
        iface = mt_link.MtInterface(Owner(), port)
        port.start()
        self.assertTrue(port.wait_online(10))
        self.assertTrue(wait_for(lambda: iface.online))
        self.assertFalse(iface.process_outgoing(bytes(mt_link.hw_mtu() + 1)))
        time.sleep(0.3)
        self.assertEqual(self.boards[0].received, [])


def telemetry_payload(**local_stats):
    t = telemetry_pb2.Telemetry()
    if local_stats:
        for k, v in local_stats.items():
            setattr(t.local_stats, k, v)
    else:
        t.device_metrics.channel_utilization = 12.5
        t.device_metrics.air_util_tx = 1.25
    return t.SerializeToString()


class TestBoardTelemetry(PortCase):
    """Phase 4: the board's own channel figures, which it pushes to its client
    every 60 s unasked (measured on two boards), kept and exposed -- including
    the counters that are the only place a repeat ever shows, since a board
    drops duplicates before the client sees them."""

    TELEMETRY = portnums_pb2.PortNum.TELEMETRY_APP

    def online(self):
        port = self.make()
        port.start()
        self.assertTrue(port.wait_online(10))
        return port

    def test_nothing_is_reported_before_the_board_says_anything(self):
        self.assertEqual(self.online().telemetry(), {})

    def test_local_stats_give_load_airtime_and_counters_since_connect(self):
        port = self.online()
        board = self.boards[0]
        board.deliver(telemetry_payload(channel_utilization=31.5, air_util_tx=4.2,
                                        num_tx_relay=500, num_rx_dupe=40,
                                        num_packets_tx=900, num_packets_rx=800),
                      from_node=BOARD_NODE, portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: port.telemetry()))
        board.deliver(telemetry_payload(channel_utilization=33.0, air_util_tx=4.5,
                                        num_tx_relay=503, num_rx_dupe=46,
                                        num_packets_tx=910, num_packets_rx=820),
                      from_node=BOARD_NODE, portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: port.telemetry().get("channel_util_pct") == 33.0))
        t = port.telemetry()
        self.assertAlmostEqual(t["air_util_tx_pct"], 4.5, places=2)
        # Since THIS connection, not since the board last booted.
        self.assertEqual(t["relayed_since_connect"], 3)
        self.assertEqual(t["heard_twice_since_connect"], 6)
        self.assertEqual(t["sent_since_connect"], 10)
        self.assertEqual(t["heard_since_connect"], 20)

    def test_no_noise_floor_is_none_not_zero_dbm(self):
        port = self.online()
        self.boards[0].deliver(telemetry_payload(channel_utilization=5.0),
                               from_node=BOARD_NODE, portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: port.telemetry()))
        self.assertNotIn("noise_floor_dbm", port.telemetry())

    def test_device_metrics_alone_still_give_the_load(self):
        port = self.online()
        self.boards[0].deliver(telemetry_payload(), from_node=BOARD_NODE,
                               portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: port.telemetry()))
        self.assertEqual(port.telemetry()["channel_util_pct"], 12.5)

    def test_the_load_comes_from_the_newest_report(self):
        # As the boards really behave: local stats once after connecting, then
        # device metrics every minute. The load must follow the minute, not
        # stay frozen at the connect-time figure.
        port = self.online()
        board = self.boards[0]
        board.deliver(telemetry_payload(channel_utilization=40.0, num_tx_relay=1),
                      from_node=BOARD_NODE, portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: port.telemetry().get("channel_util_pct") == 40.0))
        board.deliver(telemetry_payload(), from_node=BOARD_NODE, portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: port.telemetry().get("channel_util_pct") == 12.5))
        self.assertIn("relayed_since_connect", port.telemetry())

    def test_another_nodes_telemetry_is_not_taken_for_the_boards(self):
        port = self.online()
        self.boards[0].deliver(telemetry_payload(channel_utilization=99.0),
                               from_node=0x11112222, portnum=self.TELEMETRY)
        time.sleep(0.3)
        self.assertEqual(port.telemetry(), {})

    def test_old_figures_expire(self):
        port = self.online()
        self.boards[0].deliver(telemetry_payload(channel_utilization=5.0),
                               from_node=BOARD_NODE, portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: port.telemetry()))
        port.local_stats_at -= 1000
        t = port.telemetry()
        # The LOAD is a reading of now and expires; the counters are
        # cumulative, so they stay, labelled with their age.
        self.assertNotIn("channel_util_pct", t)
        self.assertGreater(t["counters_age_s"], 900)

    def test_the_interface_reads_them_under_the_rnode_names(self):
        port = self.online()
        iface = mt_link.MtInterface(owner=type("O", (), {"inbound": lambda *a: None})(),
                                    port=port)
        self.assertIsNone(iface.r_channel_load_short)
        self.boards[0].deliver(telemetry_payload(channel_utilization=21.0, air_util_tx=2.0),
                               from_node=BOARD_NODE, portnum=self.TELEMETRY)
        self.assertTrue(wait_for(lambda: iface.r_channel_load_short == 21.0))
        self.assertEqual(iface.r_airtime_long, 2.0)
        self.assertIsNone(iface.r_noise_floor)
        self.assertIsNone(iface.r_channel_load_long)


if __name__ == "__main__":
    unittest.main()
