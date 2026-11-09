"""The real port for mt_link: a Meshtastic board, spoken to with the OFFICIAL
`meshtastic` package.

mt_link.MtInterface owns "a port" and nothing about how bytes reach a board.
This is the thing that fills that seam for a real one:

    port.send(payload) -> bool            put one packet on the air
    port.set_receiver(fn)                 fn(payload, rssi, snr, sender)
    port.set_state_listener(fn)           fn(online: bool)

WHY THE OFFICIAL PACKAGE. The wire protocol is a stream of protobuf messages
behind a 4-byte frame header, with a configuration download that the firmware
revises between versions. Hand-writing that is a way to drift out of step with
the board with nothing to say so; the package is maintained against the
firmware. The price is a dependency (see app/build.gradle.kts for why it forced
the app's Python from 3.8 to 3.9).

WHAT IT IS GIVEN: A STREAM, NOT A DEVICE PATH. `open_stream()` returns anything
that behaves like a pyserial port (read/write/flush/close). On a desktop that is
`serial.Serial("/dev/ttyACM0")`; on the phone it is
`serial.serial_for_url("socket://127.0.0.1:PORT")`, the loopback socket the
Kotlin USB bridge already exposes for the RNode -- the same trick rns_bridge's
_make_rnode_serial() uses. The package's own SerialInterface wants a device path
and opens it itself, which Android cannot do, so this subclasses its
StreamInterface and hands it the stream.

THE SEND SETTINGS ARE DELIBERATE.
  * Broadcast, PRIVATE_APP (256): the port number the firmware carries without
    interpreting.
  * want_ack False: the app has its own acknowledgement and resend logic
    (reliable.py, fm_resend). A Meshtastic ack on top of it would be a second
    ARQ stacked on the first, spending airtime to confirm what ours confirms.
  * hop_limit 0: asks other Meshtastic nodes not to rebroadcast. The app's own
    relay policy decides what is forwarded and must not be fighting a second
    flood. Whether every firmware honours 0 is NOT verified here -- see the
    caveat in the module that wires this up.

RECEIVE IS FILTERED TO THIS INTERFACE. The package publishes on a process-wide
pubsub bus, so a listener sees every Meshtastic interface in the process.
Packets are ignored unless they came from the interface this port owns.
"""

import collections
import threading
import time

from meshtastic import config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2
from meshtastic.stream_interface import StreamInterface
from meshtastic.mesh_interface import MeshInterface
from pubsub import pub

import mt_link
import radio

# The pubsub topic the package publishes a PRIVATE_APP payload on. The suffix
# is the port NAME, not the number -- found by reading
# mesh_interface._handlePacketFromRadio, not guessed.
_RECEIVE_TOPIC = "meshtastic.receive.data.PRIVATE_APP"
# Every received packet of every kind, from which the board's refusals of OUR
# packets are picked out (a ROUTING_APP packet naming our packet id).
_ANY_TOPIC = "meshtastic.receive"
_LOST_TOPIC = "meshtastic.connection.lost"

BROADCAST = "^all"


class _StreamBoard(StreamInterface):
    """StreamInterface over a stream we are handed rather than a path we open.

    The base class discards `self.stream` in __init__, so it is constructed
    unconnected, given the stream, and only then connected.
    """

    def __init__(self, stream, timeout=30, on_reject=None):
        super().__init__(noProto=False, connectNow=False, timeout=timeout)
        self._connect_timeout = timeout
        self._on_reject = on_reject
        # One writer at a time. The package writes from whichever thread asks:
        # our transmit thread, its own heartbeat timer (every 300 s) and the
        # liveness probe. DEFENSIVE AND UNPROVEN: no interleaving inside a frame
        # was ever reproduced (each frame is one atomic write at these sizes, and
        # removing this lock does not fail a concurrency test), so this guards
        # against a possibility, not a seen failure. It costs one lock.
        self._write_lock = threading.Lock()
        # Set when this connection is closed, so a thread waiting on it (the
        # liveness probe) wakes at once instead of sleeping out its interval.
        self.closed_event = threading.Event()
        # Set when the package's reader gives up on the stream, so the connect
        # wait can stop at once rather than sit out its timeout (_waitConnected).
        self._lost = threading.Event()
        # Stamped on every message the board sends, so "has it said anything
        # since I asked" can be answered without parsing anything here.
        self.rx_count = 0
        self.last_rx_at = None
        self.last_rx_kind = None
        self.stream = stream
        self.connect()
        self.waitForConfig()

    def _writeBytes(self, b):
        with self._write_lock:
            return super()._writeBytes(b)

    def close(self):
        self.closed_event.set()
        return super().close()

    def _handleFromRadio(self, fromRadioBytes):
        self.rx_count += 1
        self.last_rx_at = time.monotonic()
        try:
            probe = mesh_pb2.FromRadio()
            probe.ParseFromString(fromRadioBytes)
            self.last_rx_kind = probe.WhichOneof("payload_variant")
        except Exception:
            self.last_rx_kind = "unparseable"
        return super()._handleFromRadio(fromRadioBytes)

    def _disconnected(self):
        self._lost.set()
        return super()._disconnected()

    def _waitConnected(self, timeout=30.0):
        """The package's connect() waits for the board with a hard-coded 30 s
        that ignores the `timeout` given to the constructor, so the value passed
        in was not what bounded the wait. Found when a connection dropped by a
        startup race on a phone took 35 s to retry. This makes it the number we
        chose.

        It also gives up the moment the stream is lost. The package's wait
        listens only for success, so a stream closed under it -- the phone's
        bridge dropping a connection made before the Bluetooth link was up --
        still sat out the whole timeout. MEASURED 2026-10-16 (OnePlus, BLE):
        dropped at +0.0 s, noticed at +15.0 s."""
        end = time.monotonic() + self._connect_timeout
        while not self.noProto and not self.isConnected.is_set():
            if self._lost.is_set():
                raise MeshInterface.MeshInterfaceError(
                    "The connection closed before the board answered")
            left = end - time.monotonic()
            if left <= 0:
                break
            self.isConnected.wait(min(left, 0.1))
        return super()._waitConnected(timeout=0)

    def _handleQueueStatusFromRadio(self, queueStatus):
        """Every status the board sends passes through here, so a rejection is
        seen as it arrives. Polling the package's `queueStatus` attribute
        instead was tried and is racy: it holds only the LATEST status, and the
        board follows a rejection with an "all clear" (res=0, id=0) that can
        land after it and erase the evidence."""
        if queueStatus.res and self._on_reject is not None:
            self._on_reject(queueStatus)
        super()._handleQueueStatusFromRadio(queueStatus)


class MeshtasticPort(object):
    RECONNECT_WAIT_S = 5.0

    # How long to wait for the board to finish its configuration download before
    # giving up and retrying. A live board takes well under 2 s (0.4 s measured
    # on a desktop). It used to be 30, which turned a connection dropped by a
    # startup race on a phone (Python connecting a moment before Kotlin had the
    # USB port open) into a 35 s wait before the retry; the retry itself
    # succeeded at once.
    CONNECT_TIMEOUT_S = 10

    # How long send() waits for a free slot in the board's transmit queue
    # (16 slots) before refusing. The package's own sendData() waits for a slot
    # in a loop with NO timeout, so a full queue on a stuck board would freeze
    # whatever thread called it -- here the app's single transmit thread, which
    # also sends acknowledgements and beacons -- until the process is killed. A
    # slot normally frees as a packet goes on the air, and a full-size packet is
    # 2.2 s of airtime, so this is that plus margin: past it the board is not
    # draining, and refusing (like an RNode that is offline) is the honest answer.
    SEND_WAIT_FOR_SPACE_S = 4.0

    # How many recent packet ids to remember so a refusal from the board can be
    # matched to a packet of ours. Far more than can be in flight at once.
    PENDING_IDS = 64

    # LIVENESS WATCHDOG. Every HEARTBEAT_PROBE_S the port sends the board a
    # heartbeat and expects it to say something back. MEASURED on RAK4631s running
    # 2.7.15: 6 of 6 heartbeats answered, in 0.11-0.32 s, each with a queue-status
    # message. A board that is plugged in but says nothing (a dead firmware, or a
    # USB driver that never raised DTR) otherwise looks healthy forever -- that is
    # how a phone sat "connected" to a mute board. After MISSES_BEFORE_RESTART
    # misses in a row the connection is dropped and re-opened. Three, not one: a
    # single miss on a busy board must not tear down a working link, and a miss
    # costs 10 s plus the 30 s interval, so three is about 100 s of silence.
    HEARTBEAT_PROBE_S = 30.0
    HEARTBEAT_ANSWER_WAIT_S = 10.0
    MISSES_BEFORE_RESTART = 3

    # STALE-PACKET GUARD. A board can hand the phone packets it heard earlier, as
    # a burst right after the handshake (seen once: three beacons in one second at
    # connect, minutes old, presented as fresh). Each packet carries the board's
    # own receive time; MEASURED: a live packet is 0.6-1.4 s old. A packet older
    # than STALE_AGE_S is dropped -- but ONLY in the first STALE_GRACE_S after
    # connecting, which is where a replay arrives. Past that, nothing is ever
    # filtered, so a board whose clock is simply wrong can cost at most the first
    # 10 s of a connection and never silences a live link. Times before
    # MIN_VALID_RX_TIME (2020) are a board that has no clock yet, not an old
    # packet, and are not judged. A later retest saw NO replay at all after a
    # 150 s gap, so this is a guard against something intermittent.
    STALE_GRACE_S = 10.0
    STALE_AGE_S = 20.0
    MIN_VALID_RX_TIME = 1577836800

    def __init__(self, open_stream, channel_index=0, reconnect=True,
                 connect_timeout_s=CONNECT_TIMEOUT_S):
        self._open_stream = open_stream
        self._channel_index = channel_index
        self._reconnect = reconnect
        self._connect_timeout_s = connect_timeout_s
        self._receiver = None
        self._listener = None
        self._iface = None
        self._lock = threading.Lock()
        self._stopping = False
        self._connecting = False
        self._thread = None
        self.online = False
        self.node_num = None
        self.sent = 0
        self.received = 0
        self.board_rejected = 0
        self.send_refused_full = 0     # send() gave up: the board's queue stayed full
        self.tx_refused = {}           # reason -> count, from the board's routing errors
        self.last_refusal = None
        self._pending_ids = collections.OrderedDict()
        self.log = None                # optional callable(str): diagnostics go here
        self._probe_threads = []       # this port's liveness probes, for tests and tidy-up
        self.heartbeat_answered = 0
        self.heartbeat_missed = 0
        self.watchdog_restarts = 0
        self.dropped_stale = 0
        self.last_heartbeat_s = None   # seconds the last answered heartbeat took
        self._connected_at = None
        self.last_error = None
        # The board's own telemetry; see _on_telemetry.
        self.local_stats = None
        self.local_stats_at = None
        self.device_metrics = None
        self.device_metrics_at = None
        self._stats_at_connect = None
        # Counts successful connections. A settings change reboots the board,
        # which shows up as this going up; comparing it before and after is how
        # the caller knows the board really came back, rather than assuming the
        # old connection is still the live one.
        self.generation = 0

    # -- the contract mt_link.MtInterface relies on ---------------------------

    def set_receiver(self, fn):
        self._receiver = fn

    def set_state_listener(self, fn):
        self._listener = fn

    def send(self, payload):
        """True once the board has been handed the packet.

        That is a hand-off, not delivery: the board queues it and the answer
        (accepted, or a routing error like TOO_LARGE) arrives asynchronously,
        after this has returned. A refusal is counted in `board_rejected` when
        it arrives -- mt_link refuses oversize frames itself, so the one
        refusal we know of never gets this far.
        """
        iface = self._iface
        if not self.online or iface is None:
            return False
        # The package would wait for a free slot forever; wait a bounded time
        # ourselves and refuse if there is none, so sendData() is only ever
        # called when it will return.
        if not self._wait_for_queue_space(iface):
            self.send_refused_full += 1
            self.last_error = ("send: the board's transmit queue stayed full for "
                               "%.0f s" % self.SEND_WAIT_FOR_SPACE_S)
            return False
        try:
            packet = iface.sendData(bytes(payload), destinationId=BROADCAST,
                                    portNum=portnums_pb2.PortNum.PRIVATE_APP,
                                    wantAck=False, channelIndex=self._channel_index,
                                    hopLimit=0)
        except Exception as e:
            # Any failure here means the link to the board is not trustworthy:
            # the library raises plain Exceptions and OSErrors alike.
            self.last_error = "send: %s" % e
            return False
        self._remember(getattr(packet, "id", 0))
        self.sent += 1
        return True

    def _wait_for_queue_space(self, iface):
        deadline = time.monotonic() + self.SEND_WAIT_FOR_SPACE_S
        while True:
            status = getattr(iface, "queueStatus", None)
            # No status yet means an older firmware or a board that has not
            # reported: the package itself treats that as "go ahead".
            if status is None or status.free > 0:
                return True
            if time.monotonic() >= deadline or not self.online:
                return False
            time.sleep(0.1)

    def _remember(self, packet_id):
        if not packet_id:
            return
        self._pending_ids[packet_id] = time.monotonic()
        while len(self._pending_ids) > self.PENDING_IDS:
            self._pending_ids.popitem(last=False)

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        """Begin connecting, without blocking: the configuration download takes
        seconds, and the caller is bringing up a whole app."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            pub.subscribe(self._on_receive, _RECEIVE_TOPIC)
            pub.subscribe(self._on_any_packet, _ANY_TOPIC)
            pub.subscribe(self._on_lost, _LOST_TOPIC)
            self._thread = threading.Thread(
                target=self._connect_loop, name="mt-port-connect", daemon=True)
            self._thread.start()

    def stop(self):
        self._stopping = True
        for fn, topic in ((self._on_receive, _RECEIVE_TOPIC),
                          (self._on_any_packet, _ANY_TOPIC),
                          (self._on_lost, _LOST_TOPIC)):
            try:
                pub.unsubscribe(fn, topic)
            except Exception:
                pass
        iface, self._iface = self._iface, None
        self._set_online(False)
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    def node(self):
        """The package's Node for the connected board (its `localConfig`,
        `channels` and write calls), or None when not connected. mt_config
        reads and changes settings through this."""
        iface = self._iface
        return None if iface is None else iface.localNode

    def refresh(self):
        """Drop the connection and open a fresh one.

        A new connection makes the board send its whole configuration again,
        which is the only way to READ BACK what it now holds after a change:
        the package's copy of the config is a snapshot from connect time and
        does not update itself. Also what a reboot needs anyway.
        """
        iface, self._iface = self._iface, None
        self._set_online(False)
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
        if self._stopping:
            return
        self._thread = threading.Thread(
            target=self._connect_loop, name="mt-port-refresh", daemon=True)
        self._thread.start()

    def board_preset(self):
        """The modulation preset the board is running, as the app's own name
        for it ("Long Fast"), or None if the board is not using a preset or
        reports one the app has no name for. Read off the configuration the
        board sent at connect, so it is the board's truth and not a setting
        this app made."""
        iface = self._iface
        if iface is None:
            return None
        try:
            lora = iface.localNode.localConfig.lora
            if not lora.use_preset:
                return None
            name = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
        except Exception:
            return None
        pretty = name.replace("_", " ").title()
        return pretty if pretty in radio.PRESETS else None

    def wait_online(self, timeout_s):
        """Block until connected (tests and startup logging); True if it is."""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self.online:
                return True
            time.sleep(0.05)
        return self.online

    # -- internals ------------------------------------------------------------

    def _connect_loop(self):
        # One loop at a time: a refresh arriving while a reconnect is already
        # under way must not start a second connection to the same board.
        with self._lock:
            if self._connecting:
                return
            self._connecting = True
        try:
            self._connect_loop_body()
        finally:
            self._connecting = False

    def _connect_loop_body(self):
        while not self._stopping:
            try:
                stream = self._open_stream()
                iface = _StreamBoard(stream, timeout=self._connect_timeout_s,
                                     on_reject=self._on_board_reject)
            except Exception as e:
                self.last_error = "connect: %s" % e
                if not self._reconnect:
                    return
                time.sleep(self.RECONNECT_WAIT_S)
                continue
            if self._stopping:
                try:
                    iface.close()
                except Exception:
                    pass
                return
            self._iface = iface
            self._connected_at = time.monotonic()
            # Counters are reported since THIS connection: a reconnect (or a
            # board restart) starts the baseline again.
            self._stats_at_connect = None
            self.generation += 1
            probe = threading.Thread(target=self._probe_loop, args=(iface,),
                                     name="mt-port-probe", daemon=True)
            self._probe_threads = [t for t in self._probe_threads if t.is_alive()] + [probe]
            probe.start()
            try:
                self.node_num = iface.myInfo.my_node_num
            except Exception:
                self.node_num = None
            self._set_online(True)
            return

    def _on_lost(self, interface):
        if interface is not self._iface:
            return
        self._set_online(False)
        self._iface = None
        try:
            interface.close()
        except Exception:
            pass
        if self._reconnect and not self._stopping:
            self._thread = threading.Thread(
                target=self._reconnect_after_wait, name="mt-port-reconnect",
                daemon=True)
            self._thread.start()

    def _reconnect_after_wait(self):
        time.sleep(self.RECONNECT_WAIT_S)
        if not self._stopping:
            self._connect_loop()

    def _on_receive(self, packet, interface):
        if interface is not self._iface or interface is None:
            return
        decoded = packet.get("decoded") or {}
        payload = decoded.get("payload")
        if not payload:
            return
        sender = packet.get("from")
        if self.node_num is not None and sender == self.node_num:
            return
        if self._is_stale(packet):
            self.dropped_stale += 1
            self._say("dropped a stale packet from %x (rx_time %s, %.0f s old, "
                      "%.1f s after connecting)"
                      % (sender or 0, packet.get("rxTime"),
                         time.time() - packet["rxTime"],
                         time.monotonic() - self._connected_at))
            return
        self.received += 1
        self._describe(packet, payload)
        fn = self._receiver
        if fn is not None:
            fn(bytes(payload), packet.get("rxRssi"), packet.get("rxSnr"), sender)

    def _say(self, text):
        fn = self.log
        if fn is not None:
            try:
                fn(text)
            except Exception:
                pass

    def _probe_loop(self, iface):
        """Ask the board to say something, log how long it takes, and re-open the
        connection if it stays silent. Stops by itself when this connection is no
        longer the live one."""
        misses_in_a_row = 0
        while not self._stopping and iface is self._iface:
            if iface.closed_event.wait(self.HEARTBEAT_PROBE_S):
                return
            if self._stopping or iface is not self._iface:
                return
            before = iface.rx_count
            t0 = time.monotonic()
            try:
                iface.sendHeartbeat()
            except Exception as e:
                self._say("heartbeat could not be sent: %s" % e)
                return
            while (iface.rx_count == before and iface is self._iface
                   and time.monotonic() - t0 < self.HEARTBEAT_ANSWER_WAIT_S):
                time.sleep(0.05)
            if iface is not self._iface:
                return
            if iface.rx_count > before:
                took = time.monotonic() - t0
                misses_in_a_row = 0
                self.heartbeat_answered += 1
                self.last_heartbeat_s = took
                self._say("heartbeat answered in %.2f s (with a %s message)"
                          % (took, iface.last_rx_kind))
            else:
                misses_in_a_row += 1
                self.heartbeat_missed += 1
                self._say("heartbeat NOT answered within %.0f s (%d of %d before restarting)"
                          % (self.HEARTBEAT_ANSWER_WAIT_S, misses_in_a_row,
                             self.MISSES_BEFORE_RESTART))
                if misses_in_a_row >= self.MISSES_BEFORE_RESTART:
                    self.watchdog_restarts += 1
                    self.last_error = ("watchdog: the board stayed silent through %d "
                                       "heartbeats; reconnecting" % misses_in_a_row)
                    self._say(self.last_error)
                    self.refresh()
                    return

    def _on_telemetry(self, packet, decoded):
        """Keep the board's own reports about itself.

        MEASURED (RAK4631 and Tracker L1, 2.7.15): with a client connected, a
        board hands it a TELEMETRY_APP packet from itself every 60 s, unasked
        and not transmitted (the other board never hears it). That one carries
        DEVICE METRICS: its channel utilisation and its own transmit airtime --
        the channel-load figure an RNode gives in its stats frames, so nothing
        has to be polled for it. LOCAL STATS (counters of packets sent, heard,
        relayed and heard twice) came far less often: once, 20-70 s after
        connecting, and not again in the 3 minutes watched. How often after
        that is NOT measured, so the counters can be old; telemetry() says how
        old. Asking for local stats is answered (0.2 s) but not twice in two
        minutes, so this does not poll.
        """
        if self.node_num is None or packet.get("from") != self.node_num:
            return
        try:
            from google.protobuf import json_format
            t = telemetry_pb2.Telemetry()
            t.ParseFromString(bytes(decoded.get("payload") or b""))
        except Exception:
            return
        now = time.monotonic()
        if t.HasField("local_stats"):
            stats = json_format.MessageToDict(t.local_stats)
            self.local_stats = stats
            self.local_stats_at = now
            if self._stats_at_connect is None:
                self._stats_at_connect = dict(stats)
        if t.HasField("device_metrics"):
            self.device_metrics = json_format.MessageToDict(t.device_metrics)
            self.device_metrics_at = now

    def telemetry(self, max_age_s=180.0):
        """The board's latest view of the channel, or {} if it has said nothing
        recent. Percentages as the board reports them (Meshtastic's own
        windows: channel utilisation is short-term, our transmit airtime is the
        last hour, per its documentation -- not measured here). The counters are
        since THIS connection began, so a board that has been up for days does
        not show its whole history as tonight's relays."""
        now = time.monotonic()
        out = {}
        stats, at = self.local_stats, self.local_stats_at
        metrics, m_at = self.device_metrics, self.device_metrics_at
        fresh_stats = stats is not None and at is not None and now - at <= max_age_s
        fresh_metrics = metrics is not None and m_at is not None and now - m_at <= max_age_s
        # The LOAD comes from whichever report is newest: device metrics arrive
        # every 60 s, local stats rarely (see _on_telemetry).
        if fresh_metrics and (not fresh_stats or m_at >= at):
            source, age = metrics, now - m_at
        elif fresh_stats:
            source, age = stats, now - at
        else:
            source = None
        if source is not None:
            out["age_s"] = round(age, 1)
            # Absent means zero: protobuf leaves zero-valued fields out.
            out["channel_util_pct"] = round(float(source.get("channelUtilization", 0.0)), 1)
            out["air_util_tx_pct"] = round(float(source.get("airUtilTx", 0.0)), 2)
        # The COUNTERS only from local stats, with their own age, kept however
        # old: they are cumulative, so an old reading is out of date, not wrong.
        if stats is not None and at is not None:
            out["counters_age_s"] = round(now - at, 1)
            # Omitted when zero, like every protobuf field here; a board that
            # does not measure a noise floor reports none at all (the RAK4631
            # and Tracker L1 on 2.7.15 do not), which is NOT a 0 dBm floor.
            if fresh_stats and stats.get("noiseFloor"):
                out["noise_floor_dbm"] = int(stats["noiseFloor"])
            base = self._stats_at_connect or {}
            for key, name in (("numTxRelay", "relayed"), ("numRxDupe", "heard_twice"),
                              ("numPacketsTx", "sent"), ("numPacketsRx", "heard"),
                              ("numPacketsRxBad", "heard_bad"),
                              ("numTxDropped", "dropped")):
                out["%s_since_connect" % name] = int(stats.get(key, 0)) - int(base.get(key, 0))
        return out

    def _on_any_packet(self, packet, interface):
        """Pick the board's refusals of OUR packets out of everything it reports.

        When the board will not send a packet it says so with a routing error
        naming the packet (TOO_LARGE, NO_CHANNEL, a duty-cycle limit...). Those
        arrive after send() has already returned True, so without this a packet
        the board threw away is indistinguishable from one that went out.
        """
        if interface is not self._iface or interface is None:
            return
        decoded = packet.get("decoded") or {}
        if decoded.get("portnum") == "TELEMETRY_APP":
            self._on_telemetry(packet, decoded)
            return
        if decoded.get("portnum") != "ROUTING_APP":
            return
        request_id = decoded.get("requestId")
        if request_id not in self._pending_ids:
            return
        reason = (decoded.get("routing") or {}).get("errorReason", "NONE")
        if reason == "NONE":
            return
        self._pending_ids.pop(request_id, None)
        self.tx_refused[reason] = self.tx_refused.get(reason, 0) + 1
        self.last_refusal = "board refused packet %08x: %s" % (request_id, reason)
        self.last_error = self.last_refusal

    def _is_stale(self, packet):
        """True for a packet the board heard long before we connected. See
        STALE_GRACE_S: judged only just after connecting, and only on a plausible
        board clock."""
        if self._connected_at is None:
            return False
        if time.monotonic() - self._connected_at > self.STALE_GRACE_S:
            return False
        rx_time = packet.get("rxTime")
        if not rx_time or rx_time < self.MIN_VALID_RX_TIME:
            return False
        return (time.time() - rx_time) > self.STALE_AGE_S

    def _describe(self, packet, payload):
        """One line per received packet saying what the board told us about it.

        Gathers three facts nobody had: whether the board stamps a receive time
        (and so whether a replayed packet can be told from a live one), how many
        hops a packet took (hop_start minus hop_limit: 0 means it came straight
        from the sender, anything else means a board repeated it), and how long
        after connecting it arrived (a queued backlog comes in a burst right
        after the handshake).
        """
        if self.log is None:
            return
        rx_time = packet.get("rxTime")
        age = None
        if rx_time:
            age = time.time() - rx_time
        # The package leaves out any field that is zero, so an absent hop count
        # means zero (a packet straight from its sender), not "unknown".
        hop_start, hop_limit = packet.get("hopStart", 0), packet.get("hopLimit", 0)
        sender = packet.get("from") or 0
        relay = packet.get("relayNode")
        # relay_node is only the LAST BYTE of the id of the board that last
        # transmitted the packet. Equal to the sender's last byte means it came
        # directly; different means a board repeated it.
        if relay is None:
            path = "unknown"
        elif relay == (sender & 0xFF):
            path = "direct"
        else:
            path = "REPEATED by ..%02x" % relay
        since = None if self._connected_at is None else time.monotonic() - self._connected_at
        self._say("rx from=%x len=%d rx_time=%s age=%s hops=%d (start %d, left %d) path=%s "
                  "since_connect=%s"
                  % (sender, len(payload),
                     rx_time if rx_time else "none",
                     "n/a" if age is None else "%.1fs" % age,
                     hop_start - hop_limit, hop_start, hop_limit, path,
                     "?" if since is None else "%.1fs" % since))

    def _on_board_reject(self, queue_status):
        self.board_rejected += 1
        self.last_error = "board rejected packet %08x (res=%d)" % (
            queue_status.mesh_packet_id, queue_status.res)

    def _set_online(self, online):
        online = bool(online)
        if online == self.online:
            return
        self.online = online
        fn = self._listener
        if fn is not None:
            fn(online)


def serial_stream_opener(path, baud=115200, timeout=0.5):
    """An `open_stream` for a desktop serial device. The phone passes its own
    (a socket:// URL to the Kotlin bridge); this is for benches and tests."""
    def _open():
        import serial
        return serial.Serial(path, baud, timeout=timeout, write_timeout=5)
    return _open
