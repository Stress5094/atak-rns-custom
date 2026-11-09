#!/usr/bin/env python3
"""A third mesh node, on the desktop, so multi-hop relaying can be tested.

Relaying only ever engages when a node can reach somebody the sender cannot
(see the coverage test in relay.py), so a two-phone bench structurally cannot
produce a relay: both nodes hear each other, nobody extends anything, and the
correct behaviour is to forward nothing at all. Testing multi-hop needs a third
node and a LINE -- A hears B, B hears C, A does not hear C -- and the third
node is the RNode on this laptop.

    phone 1  <---->  phone 2  <---->  laptop
      (A)            (relay)           (C)
       |                                |
       +--------- deaf to each other ---+

Neither end of that line can be made deaf by moving furniture: at these radio
settings two RNodes in one building hear each other comfortably. So both ends
ignore each other in software instead -- this tool via --deaf-to, the phones
via ~/.atakrns_deaf_to (see "Testing multi-hop on one desk" in the README).
Both filters drop only what was heard DIRECTLY, keeping what a relay forwarded,
which is the whole measurement.

WHAT THIS IS NOT. It is not the Android bridge with the Android parts removed.
There is no ATAK here, no Kotlin, no USB bridge socket -- this reaches the
RNode through pyserial and the kernel CDC driver, exactly as the other two
bench tools do. What it shares with the app is the part under test: mesh.py,
relay.py and cot_codec.py, imported unmodified. Treat it as an instrument for
watching the phones relay, not as a second implementation to compare against.

Usage (needs read/write on the serial device; on Arch that is the `uucp` group,
and a fresh login or `sg uucp -c ...` if the membership is newer than your
session):

    sg uucp -c "PYTHON tools/mesh_node.py --port /dev/ttyUSB0 \\
        --passphrase 'the same words the phones have' --deaf-to a1b2c3"

The passphrase must match the phones or nothing decrypts and the node sits
silent -- which looks identical to a dead radio, so the key fingerprint is
printed at startup to be checked against the one on the phones' status screen.
"""
import argparse
import collections
import os
import random
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))

import RNS  # noqa: E402

import cot_codec  # noqa: E402
import loadtest  # noqa: E402
import radio  # noqa: E402
import tx_policy  # noqa: E402
import mesh  # noqa: E402
import relay  # noqa: E402
import reliable  # noqa: E402

# MUST match the phones. The destination hash is derived from these, and a
# mismatch means packets transmit fine and are silently dropped -- the failure
# bench_rns_link.py was written to catch.
APP_NAME = "atakrns"
ASPECTS = ("cot", "broadcast")

# Taken from radio.py, the same table the app's Radio dialog offers, rather
# than restated here. It used to be `FREQ, BW, SF, CR = 915000000, 125000, 8,
# 5` -- correct when the app carried those as constants, and silently wrong
# from the moment the preset became something an operator picks. A tool that
# joins the wrong channel looks exactly like a dead radio.
FREQ = radio.DEFAULT_FREQUENCY_HZ

# rns_bridge uses 17. Default to 2 here for the same reason bench_rns_link does:
# the radios are inches apart, where high power desensitizes the receiver.
DEFAULT_TXPOWER = 2

# Matches rns_bridge._REASSEMBLY_STALL_S: short enough that the report
# reaches the sender before its retry timer, so it narrows that retry
# rather than arriving after it.
REASSEMBLY_STALL_S = 1.5

# Match rns_bridge's _ACK_SLOT_S / _ACK_JITTER_MAX_S: the window a receiver
# waits before acking, which is also the window it has to hear somebody else
# answer first.
ACK_SLOT_S = 0.15
ACK_JITTER_MAX_S = 1.0

# Mirrors _BEACON_INTERVAL_S in rns_bridge, and drifted from it twice before
# anyone noticed -- the app went 60 -> 150 -> 90 while this stayed at 60, so a
# bench node was checking in at a different rate from the phones it was meant
# to be measuring alongside. Override with --beacon-every when deliberately
# testing a mismatch.
BEACON_INTERVAL_S = 90
RELIABLE_TICK_S = 1.0           # _RELIABLE_TICK_S in rns_bridge
BEACON_JITTER_S = 6.0
DIGEST_REFRESH_S = 300.0
PEER_CONNECTED_S = BEACON_INTERVAL_S * 3

CONFIG = """[reticulum]
  enable_transport = Yes
  share_instance = No
[logging]
  loglevel = 4
[interfaces]
"""


class Node(object):
    def __init__(self, args):
        self.args = args
        self.sf, self.bandwidth, self.cr = radio.preset_params(args.preset)
        # Acks waiting out their jitter, keyed so they can be cancelled.
        self.ack_pending = {}
        self.ack_lock = threading.Lock()
        self.acks_suppressed = 0
        self.cfgdir = os.path.expanduser("~/.atakrns-node-%s" % args.name)
        os.makedirs(self.cfgdir, exist_ok=True)

        # An explicit id is persisted like a generated one. An override that
        # did not stick would mean the next run without the flag came up under
        # a different id, silently invalidating the deaf-to list on the phone
        # and leaving the bench looking like a relay failure.
        self.node_id = self._load_node_id(args.node_id.strip().lower())
        self.deaf_to = frozenset(d.strip().lower()
                                 for d in (args.deaf_to or "").split(",")
                                 if d.strip())

        epoch, persisted = mesh.load_epoch(os.path.join(self.cfgdir, "epoch"))
        if not persisted:
            print("[warn] epoch could not be persisted; using a random one")
        key = mesh.derive_key(args.passphrase)
        self.fingerprint = mesh.key_fingerprint(key)
        self.mesh = mesh.Mesh(self.node_id, key, epoch=epoch)

        # node_id -> {last_seen, snr, digest}. Same shape as rns_bridge._peers,
        # trimmed to the fields the coverage test actually reads.
        self.peers = {}
        self.peers_lock = threading.Lock()

        # -- offered load -------------------------------------------------
        # The instrument can now GENERATE acknowledged traffic, not merely
        # answer somebody else's. A stress test needs load it controls to the
        # message, and it needs ARQ to be the thing under load: retries are
        # what turn a busy channel into a collapsed one, so a generator that
        # sent fire-and-forget positions would measure the easy half of the
        # problem and call the mesh healthy.
        #
        # reliable.py is driven, never reimplemented. An instrument carrying
        # its own copy of the retry schedule would drift from the app's and
        # then report the disagreement as a mesh failure.
        # The radio's fixed per-leg delay, exactly as the app charges it
        # (rns_bridge._link_latency_s): measured for a Meshtastic board, 0 for
        # an RNode. --link-latency-s overrides it, which is how the timer is
        # A/B'd on real boards.
        if args.link_latency_s is not None:
            latency = args.link_latency_s
        elif args.meshtastic:
            import mt_link
            latency = mt_link.LINK_LATENCY_S
        else:
            latency = 0.0
        self.link_latency_s = latency
        self.reliable_tx = reliable.ReliableTx(airtime_s=self._airtime_s,
                                               link_latency_s=latency)
        self.reliable_lock = threading.Lock()
        self.load_seq = 0
        self.load_sent_at = {}          # msg_id -> monotonic, for latency
        self.load_stats = collections.Counter()
        # Fading-channel fixture state; see _in_fade.
        self._fade_bad = False
        self._fade_until = 0.0

        # Sized to outlive the sender's whole retry schedule, exactly as
        # rns_bridge does. The default 30 s is shorter than a two-hop
        # schedule, so partial reassemblies would be discarded moments
        # before the retry that would have completed them.
        self.reassembler = cot_codec.Reassembler(
            timeout=1.25 * reliable.max_retry_span_s(
                hops=relay.HOPS_ONESHOT + 1,
                frame_len=cot_codec.RNS_MAX_PAYLOAD))
        self.sa_rx = {}
        self.sa_tx = cot_codec.SaTx()
        # origin -> last epoch seen. A change means that node restarted, and
        # both halves of the session state it shares with us are stale. Kept
        # outside self.peers because it must be tracked for RELAYED senders
        # too, and self.peers deliberately holds only direct neighbours.
        self.epochs = {}
        # Bench fixture: fragment indices to drop once each, so the
        # per-fragment ack bitmap has something to report. See
        # _lose_fragment.
        self._lose = frozenset(
            int(i) for i in (args.lose_fragment or "").replace(",", " ").split())
        self._lost = set()

        self.relay_policy = relay.RelayPolicy(airtime_fn=self._airtime_s)
        self.relay_pending = collections.OrderedDict()
        self.relay_lock = threading.Lock()

        self.beacon_seq = 0
        self.digest_sent = None
        self.last_beacon_at = 0.0
        self.counts = collections.Counter()
        self.stop = threading.Event()

        self.iface = None
        self.dest = None
        self.max_payload = cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD

    # -- identity -----------------------------------------------------------

    def _load_node_id(self, override=""):
        """Persisted, for the same reason rns_bridge persists its own: a
        random-per-run id makes every restart look like a brand new node while
        the old one lingers in everybody's peer list as a ghost."""
        path = os.path.join(self.cfgdir, "node_id")
        if not override:
            try:
                with open(path) as f:
                    existing = f.read().strip()
                if existing:
                    return existing
            except OSError:
                pass
        node_id = override or os.urandom(3).hex()
        # Validated BEFORE it is written. Persisting an id the mesh will refuse
        # would poison the config directory: every later run, with or without
        # the flag, would read it back and fail the same way.
        mesh.node_id_bytes(node_id)
        try:
            with open(path, "w") as f:
                f.write(node_id)
        except OSError:
            print("[warn] could not persist the node id to %s; the next run "
                  "will come up under a different one" % path)
        return node_id

    # -- radio --------------------------------------------------------------

    def start_radio(self):
        if not self._claim_port():
            return False
        with open(os.path.join(self.cfgdir, "config"), "w") as f:
            f.write(CONFIG)
        RNS.Reticulum(configdir=self.cfgdir)
        if self.args.meshtastic:
            return self._start_meshtastic()

        from RNS.Interfaces.RNodeInterface import RNodeInterface
        from RNS.Interfaces.Interface import Interface
        iface = RNodeInterface(RNS.Transport, {
            "name": "rnode-%s" % self.args.name, "port": self.args.port,
            "frequency": str(self.args.frequency_hz),
            "bandwidth": str(self.bandwidth),
            "txpower": str(self.args.txpower),
            "spreadingfactor": str(self.sf), "codingrate": str(self.cr),
        })
        # Exactly what rns_bridge._build_rnode_interface does after construction.
        iface.OUT = True
        iface.IN = True
        iface.mode = Interface.MODE_FULL
        iface.ifac_size = RNodeInterface.DEFAULT_IFAC_SIZE
        RNS.Transport.add_interface(iface)
        iface.final_init()

        deadline = time.time() + 20
        while time.time() < deadline and not iface.online:
            time.sleep(0.5)
        if not iface.online:
            print("[fatal] RNode did not come online on %s" % self.args.port)
            return False
        self.iface = iface

        self.dest = RNS.Destination(None, RNS.Destination.IN,
                                    RNS.Destination.PLAIN, APP_NAME, *ASPECTS)
        self.dest.set_packet_callback(self._on_packet)
        self.max_payload = min(cot_codec.RNS_MAX_PAYLOAD,
                               RNS.Packet.PLAIN_MDU) - mesh.OVERHEAD

        print("[node] destination %s bitrate=%s" % (self.dest.hexhash,
                                                    iface.bitrate))
        return True

    def _start_meshtastic(self):
        """A Meshtastic board in place of the RNode, through the app's OWN
        mt_port and mt_link -- the same port, interface and airtime model the
        phone runs, so this node exercises the code under test rather than a
        bench copy of it. Like the app, the board is handed sealed frames
        directly (no RNS.Packet header, which would come out of a 231 B packet).
        """
        import mt_link
        import mt_port
        port = mt_port.MeshtasticPort(mt_port.serial_stream_opener(self.args.port))
        port.log = lambda text: print("[mt-diag] %s" % text) if self.args.mt_diag else None
        iface = mt_link.MtInterface(
            RNS.Transport, port,
            deliver_frame=lambda data, _iface: self._on_packet(data, None))
        port.start()
        if not port.wait_online(30):
            print("[fatal] Meshtastic board did not come online on %s: %s"
                  % (self.args.port, port.last_error))
            return False
        preset = port.board_preset()
        if preset:
            iface.set_preset(preset)
        self.iface = iface
        self.mt_port = port
        self.mt_airtime = mt_link.airtime_link(iface.preset)
        self.max_payload = mt_link.hw_mtu() - mesh.OVERHEAD
        print("[node] Meshtastic board %x online, preset %s, %d B per packet, "
              "bitrate=%.0f" % (port.node_num or 0, preset or "unknown",
                                iface.HW_MTU, iface.bitrate))
        if self.args.manage_board:
            if not self._manage_board(port):
                return False
        return True

    def _manage_board(self, port):
        """Put the board on the managed profile derived from --passphrase and
        the radio arguments -- what the phone's "Use the app's settings" does --
        through the app's own mt_config, then reconnect and read it back."""
        import mt_config
        node = port.node()
        request = mt_config.managed_request(
            mesh.derive_key(self.args.passphrase), self.args.preset,
            self.args.frequency_hz, self.args.txpower,
            current_region=mt_config.snapshot(node)["region"],
            current_gps=mt_config.snapshot(node)["gps_mode"],
            node_num=port.node_num)
        problems = mt_config.profile_problems(request)
        if problems:
            print("[fatal] cannot manage the board: %s" % "; ".join(problems))
            return False
        # The PIN a phone pairs with over Bluetooth, which a board with no
        # screen cannot show; the same one the phone's pairing dialog offers.
        print("[board] Bluetooth PIN for board %x (from the passphrase): %06d"
              % (port.node_num, request["bt_pin"]))
        changes = mt_config.apply(node, request)
        if not changes:
            print("[board] already on the managed profile")
            return True
        print("[board] applied: %s" % "; ".join(
            "%s: %s -> %s" % (c["label"], c["old"], c["new"]) for c in changes))
        gen = port.generation
        time.sleep(3.0)
        port.refresh()
        deadline = time.time() + 60
        while time.time() < deadline:
            if port.online and port.generation > gen:
                left = mt_config.plan(port.node(), request).changes
                if not left:
                    break
            time.sleep(0.5)
        # A preset or frequency change restarts the board some seconds later;
        # wait that out and read again before trusting it.
        restart_deadline = time.time() + 25
        while port.online and time.time() < restart_deadline:
            time.sleep(0.25)
        if not port.online and not port.wait_online(75):
            print("[fatal] the board did not come back after the change")
            return False
        left = mt_config.plan(port.node(), request).changes
        if left:
            print("[fatal] the board did not keep: %s"
                  % ", ".join(c["label"] for c in left))
            return False
        preset = port.board_preset()
        if preset:
            self.iface.set_preset(preset)
        print("[board] confirmed on the managed profile (channel %s)"
              % mt_config.snapshot(port.node())["channel"]["name"])
        return True

    def print_identity(self):
        """Before the radio is touched, because this is what you need in order
        to WIRE the bench -- the phone's deafness list wants this node's id, and
        a run that cannot find its RNode should still tell you what it is."""
        print("[node] id=%s  <-- put this in the phone's .atakrns_deaf_to"
              % self.node_id)
        print("[node] name=%s callsign=%s key=%s"
              % (self.args.name, self.args.callsign, self.fingerprint))
        print("[node] id persisted in %s" % os.path.join(self.cfgdir, "node_id"))
        print("[node] deaf to: %s" % (", ".join(sorted(self.deaf_to)) or "nobody"))
        if self.args.force_relay:
            print("[node] FORCED RELAY: relaying regardless of coverage -- bench "
                  "only, this node is a flood point until it is stopped")

    def _claim_port(self):
        """Refuse to start if another instance already has this radio.

        Nothing in the OS stops two processes opening the same tty, and pyserial
        does not take an exclusive lock by default -- so a second instance does
        not fail, it silently steals bytes from the first. Both then fail to
        detect the RNode, or worse, half-work. That failure looks exactly like
        dead hardware and costs an hour to tell apart from one, which is reason
        enough for the eight lines.
        """
        import fcntl
        safe = self.args.port.strip("/").replace("/", "-")
        self._lock_path = os.path.expanduser("~/.atakrns-port-%s.lock" % safe)
        try:
            self._lock_file = open(self._lock_path, "a+")
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            other = ""
            try:
                with open(self._lock_path) as f:
                    other = f.read().strip()
            except OSError:
                pass
            print("[fatal] %s is already in use by another mesh_node%s.\n"
                  "        Two instances on one radio steal each other's bytes "
                  "and both stop detecting it." % (self.args.port,
                                                   " (pid %s)" % other if other else ""))
            return False
        # Held for the life of the process: kept on self so it is not closed by
        # garbage collection, which would drop the lock while still running.
        self._lock_file.seek(0)
        self._lock_file.truncate()
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()
        return True

    def _airtime_s(self, packet_len):
        """Seconds on the air for a packet of this size, from the radio's own
        reported bitrate -- the same source rns_bridge uses. On a Meshtastic
        board, the same airtime.Link the app builds for it (the board's 24 B
        wrapper rather than an RNS header)."""
        link = getattr(self, "mt_airtime", None)
        if link is not None:
            return link.frame_s(packet_len)
        bitrate = getattr(self.iface, "bitrate", None) or 1000
        return (packet_len * 8.0) / float(bitrate)

    def _send(self, sealed, what):
        if not getattr(self.iface, "online", False):
            print("[tx] %s NOT sent: radio is offline" % what)
            return False
        if getattr(self, "mt_port", None) is not None:
            if not self.iface.process_outgoing(sealed):
                print("[tx] %s REJECTED by the Meshtastic link (%s)"
                      % (what, self.mt_port.last_error))
                return False
            return True
        if RNS.Packet(self.dest, sealed).send() is False:
            print("[tx] %s REJECTED: no interface accepted it" % what)
            return False
        return True

    # -- receive ------------------------------------------------------------

    def _on_packet(self, data, packet):
        # BENCH FIXTURE: pretend this frame was never heard.
        #
        # Dropped on RECEIPT rather than before transmission, which matters for
        # anything measuring congestion: the frame really did go on the air and
        # really did cost that airtime, this node just did not hear it. Dropping
        # at the sender would quietly hand the channel back the time a lost
        # frame actually consumes, and flatter every duty figure in the run.
        #
        # Applies to acks as much as messages, which is the point -- an ack this
        # node never hears is what makes a retry necessary, and retries are the
        # thing whose value on a bad link is being priced.
        # Counted before the fixture decides, so realised loss can be measured
        # rather than assumed. Over a 90 s cell a fading channel goes through
        # only a handful of good/bad cycles, so the loss it actually delivered
        # can sit well away from the target -- and comparing two thresholds
        # that saw different loss would compare nothing.
        self.counts["rx_frames"] += 1
        rate = self._drop_rate()
        if rate > 0:
            fading = self._in_fade(rate)
            lost = random.random() < rate if fading is None else fading
            if lost:
                self.counts["fixture_dropped"] += 1
                if fading:
                    self.counts["fixture_dropped_in_fade"] += 1
                return
        snr = getattr(self.iface, "r_stat_snr", None)
        inbound = self.mesh.unwrap(data)
        if inbound is None:
            self.counts["foreign_or_forged"] += 1
            return
        origin = mesh.node_id_hex(inbound.origin)
        relayed = not self._heard_directly(inbound)

        if origin in self.deaf_to and not relayed:
            # Dropped AND forgotten: unwrap() has already recorded this frame,
            # and leaving it recorded would make the relayed copy -- the entire
            # point of the exercise -- arrive looking like a replay.
            self.mesh.seen.forget((inbound.origin, inbound.epoch, inbound.seq))
            self.counts["ignored_direct"] += 1
            return

        if inbound.origin == self.mesh.origin:
            # Our own frame, come back. Somebody relayed it, which is proof the
            # hop happened without a byte spent asking.
            self._relay_cancel((inbound.origin, inbound.epoch, inbound.seq))
            print("[rx] our own frame %d came back -- somebody relayed it"
                  % inbound.seq)
            self.counts["own_relayed_back"] += 1
            return
        if inbound.duplicate:
            self._relay_cancel((inbound.origin, inbound.epoch, inbound.seq))
            self.counts["duplicate"] += 1
            return

        self._note_epoch(origin, inbound.epoch)

        # DIRECTLY heard frames only. The peer table feeds the neighbour digest,
        # and the digest describes who this node reaches in ONE hop; counting a
        # relayed frame puts a two-hop node in it and tells the relay to stop
        # relaying, which is self-defeating.
        #
        # This started as a deliberate divergence: the app had that bug, and an
        # instrument reproducing it would have been corrupting the very
        # measurement it exists to take. The 2026-08-12 bench caught it in the
        # app -- a marker refused with `no new coverage` seconds after being
        # sent -- and rns_bridge._note_peer_frame now draws the same
        # distinction, so the two agree.
        if relayed:
            self.counts["relayed_in"] += 1
            with self.peers_lock:
                unknown = origin not in self.peers
            if unknown:
                print("[node] %s is TWO hops away -- reachable, but not a "
                      "neighbour, so it stays out of the digest" % origin)
        else:
            self.counts["direct_in"] += 1
            self._note_peer(origin, snr)

        if self.args.relay:
            self._consider_relay(data, inbound, snr)

        if self._lose_fragment(inbound.frame, origin):
            return

        frame = self.reassembler.offer(inbound.frame, sender=origin)
        if frame is None:
            return  # a fragment; nothing whole yet

        via = "RELAYED, %d hop(s) left" % inbound.hops if relayed else "direct"
        try:
            if cot_codec.is_beacon(frame):
                # Since v15 the body carries no node id of its own; the
                # envelope's authenticated origin is the only claim there is.
                beacon = cot_codec.decode_beacon(frame, origin)
                with self.peers_lock:
                    self.peers.setdefault(origin, {})["digest"] = beacon.digest
                print("[rx] beacon from %s (%s) seq=%d snr=%s digest=%s [%s]"
                      % (beacon.name or origin, origin, beacon.seq, snr,
                         (beacon.digest or b"").hex() or "none", via))
                return
            if cot_codec.is_ack(frame):
                print("[rx] ack from %s [%s]" % (origin, via))
                try:
                    heard = cot_codec.decode_ack(frame, origin)
                except cot_codec.CotDecodeError:
                    return
                with self.ack_lock:
                    if self.ack_pending.pop(
                            (heard.origin, heard.msg_id), None) is not None:
                        self.acks_suppressed += 1
                        print("[tx] ack for %s's message %d suppressed -- %s "
                              "answered first"
                              % (heard.origin, heard.msg_id, heard.acker))
                # An ack for load WE offered. on_ack ignores anything that is
                # not ours, so this is safe to call on every ack heard on a
                # destination everybody shares.
                with self.reliable_lock:
                    done = self.reliable_tx.on_ack(heard)
                if done is not None:
                    started = self.load_sent_at.pop(done.msg_id, None)
                    self.load_stats["delivered"] += 1
                    # attempts already counts register()'s own transmission.
                    # Names who answered, as the app's _delivered_line does:
                    # one ack settles it, and proves only that THAT node has it.
                    # Appended after rtt so the scorers' regexes still match.
                    print("[load] DELIVERED msg=%d tx=%d%s acknowledged by %s%s"
                          % (done.msg_id, done.attempts,
                             "" if started is None else
                             " rtt=%.2fs" % (time.monotonic() - started),
                             ",".join(sorted(done.acked)) or "?",
                             "" if not (done.expected - done.acked) else
                             "; NOT confirmed for " + ",".join(sorted(done.expected - done.acked))))
                return
            if cot_codec.is_reliable(frame):
                wrapper = cot_codec.unwrap_reliable(frame, origin)
                frame = wrapper.frame
                self._send_ack(wrapper, relayed)
            session = self.sa_rx.get(origin)
            if session is None:
                session = self.sa_rx[origin] = cot_codec.SaRx()
            xml = cot_codec.decode(frame, session)
        except cot_codec.CotDecodeError as e:
            print("[rx] undecodable frame from %s: %s" % (origin, e))
            return

        summary = None
        try:
            summary = cot_codec.summarize(xml)
        except Exception:
            pass
        label = summary.callsign if summary is not None and summary.callsign else "?"
        print("[rx] %d bytes from %s (%s) -> %d bytes of CoT [%s]"
              % (len(data), origin, label, len(xml), via))
        if self.args.print_cot:
            print("     %s" % xml.decode("utf-8", "replace"))

    def _send_ack(self, wrapper, relayed):
        """Acknowledge a reliable message, exactly as the app does.

        The stamp must MIRROR the app's, whatever it is -- an instrument that
        quietly used a different cap would either hide a real failure or invent
        one. It sent no ack at all until 2026-08-12, which made every multi-hop
        message look undelivered for a reason that was the instrument's fault
        rather than the mesh's.
        """
        try:
            ack = cot_codec.encode_ack(wrapper.origin, wrapper.msg_id,
                                       self.node_id)
        except Exception as e:
            print("[tx] could not build ack: %s" % e)
            return
        hops = _hops_for(ack)
        sealed = self.mesh.wrap(ack, hops)

        # A message whose sender said "any one ack will do" is held for a
        # random moment first, and dropped entirely if somebody else answers
        # in the meantime. Mirrors the app -- an instrument that kept acking
        # unconditionally would hide exactly the saving it is here to measure,
        # and at 16 nodes that saving is most of the ack traffic.
        if getattr(wrapper, "any_ack", False):
            window = tx_policy.jitter_window(len(self.peers), ACK_SLOT_S,
                                             ACK_JITTER_MAX_S)
            with self.ack_lock:
                self.ack_pending[(wrapper.origin, wrapper.msg_id)] = (
                    time.time() + random.random() * window, sealed, hops,
                    relayed)
            return

        if self._send(sealed, "ack"):
            print("[tx] acked message %d to %s (%dB, hops=%d)%s"
                  % (wrapper.msg_id, wrapper.origin, len(sealed), hops,
                     "  <-- sender is TWO hops away; this ack needs a relay to "
                     "get home" if relayed else ""))

    def ack_loop(self):
        """Sends held acks whose moment has come. Anything cancelled in the
        meantime is simply no longer in the table."""
        while not self.stop.wait(0.1):
            now = time.time()
            due = []
            with self.ack_lock:
                for key, entry in list(self.ack_pending.items()):
                    if now >= entry[0]:
                        due.append((key, self.ack_pending.pop(key)))
            for (origin, msg_id), (_, sealed, hops, relayed) in due:
                if self._send(sealed, "ack"):
                    print("[tx] acked message %d to %s (%dB, hops=%d)%s"
                          % (msg_id, origin, len(sealed), hops,
                             "  <-- sender is TWO hops away; this ack needs a "
                             "relay to get home" if relayed else ""))

    def _heard_directly(self, inbound):
        """Did this come from its originator, or did somebody forward it?

        `origin` cannot answer: a relay does not re-wrap, so it names the node
        that ORIGINATED the frame at every hop. The hop count can -- the sender
        stamps a cap fixed by the frame's class and every relay spends one.
        """
        return inbound.hops >= _hops_for(inbound.frame)

    def _lose_fragment(self, frame, origin):
        """Throw a fragment away, ONCE, so the bitmap has something to report.

        A BENCH FIXTURE, like the deafness one, and for the same reason: the
        behaviour under test only happens on loss, and three radios on a desk
        with clear line of sight do not lose anything. Both hardware runs of a
        fragmented route delivered every fragment on the first attempt, so
        T_PARTIAL had never once been emitted on a radio -- the feature was
        built, desktop-tested, installed, and completely unexercised.

        Dropped once per (message, index) rather than every time, because the
        point is to watch the RECOVERY: the sender should be told exactly which
        fragment is missing and resend only that one. Dropping it on every
        attempt would just exhaust the retries and prove nothing.

        Nothing needs un-recording here, unlike simulated deafness: an ARQ
        retry is re-wrapped and carries a fresh mesh sequence, so the resent
        fragment arrives as a new frame rather than a replay.
        """
        if not self._lose:
            return False
        if not frame or frame[0] != cot_codec.CTRL_FRAG or len(frame) < 5:
            return False
        msg_id, index, count = struct.unpack(">HBB", frame[1:5])
        if index not in self._lose:
            return False
        key = (origin, msg_id, index)
        if key in self._lost:
            return False          # already dropped once; let the retry land
        self._lost.add(key)
        self.counts["fragments_dropped"] += 1
        print("[bench] DROPPING fragment %d/%d of message %d from %s "
              "(once, to make the bitmap report something)"
              % (index, count, msg_id, origin))
        return True

    def _note_epoch(self, origin, epoch):
        """Notice a peer restarting, and restate our identity when one does.

        Mirrors rns_bridge._note_peer_restarted, and must. A restarted node
        lost the decode state for everything we have sent it, so every delta we
        send now references a base it no longer holds and every position we
        send is dropped -- for up to a full keyframe interval. The epoch is the
        signal: authenticated, already on every frame, and changed on exactly
        this event.

        Added 2026-08-12, after the app grew the same check and the bench
        immediately showed the gap from this side: the phone that had restarted
        went on dropping THIS node's positions with `last base is None`, because
        the instrument never noticed and never re-keyed. An endpoint that does
        not behave like the app measures the wrong thing.
        """
        previous = self.epochs.get(origin)
        self.epochs[origin] = epoch
        if previous is None or previous == epoch:
            return
        self.sa_tx.force_keyframe()
        self.sa_rx.pop(origin, None)
        print("[node] %s restarted (epoch %s -> %s): identity keyframe queued"
              % (origin, previous, epoch))

    def _note_peer(self, origin, snr):
        with self.peers_lock:
            peer = self.peers.setdefault(origin, {})
            fresh = "last_seen" not in peer
            peer["last_seen"] = time.time()
            if snr is not None:
                peer["snr"] = snr
        if fresh:
            print("[node] new peer %s" % origin)

    # -- relaying (only when this node is the one in the middle) -------------

    def _consider_relay(self, packet, inbound, snr):
        origin = mesh.node_id_hex(inbound.origin)
        covered = False if self.args.force_relay else self._sender_covers_our_neighbours(origin)
        decision = self.relay_policy.decide(
            hops=inbound.hops,
            kind=inbound.frame[0] & 0x0F,
            packet_len=len(packet),
            snr=snr,
            covered=covered,
            msg_id=_message_shape(inbound.frame)[0],
            fragments=_message_shape(inbound.frame)[1],
        )
        if not decision.relay:
            print("[relay] not relaying frame %d from %s: %s"
                  % (inbound.seq, origin, decision.reason))
            return
        forwarded = self.mesh.forward(packet)
        if forwarded is None:
            return
        frame_id = (inbound.origin, inbound.epoch, inbound.seq)
        with self.relay_lock:
            self.relay_pending[frame_id] = (time.monotonic() + decision.delay_s,
                                            forwarded)
        print("[relay] relaying frame %d from %s in %.2fs (%s%s, snr %s)"
              % (inbound.seq, origin, decision.delay_s, decision.reason,
                 ", FORCED" if self.args.force_relay else "", snr))

    def _relay_cancel(self, frame_id):
        with self.relay_lock:
            dropped = self.relay_pending.pop(frame_id, None)
        if dropped is not None:
            self.relay_policy.stats["cancelled"] += 1
            print("[relay] cancelled: heard again from someone else")

    def _sender_covers_our_neighbours(self, sender):
        now = time.time()
        with self.peers_lock:
            digest = (self.peers.get(sender) or {}).get("digest")
            others = [n for n, p in self.peers.items()
                      if n != sender
                      and now - p.get("last_seen", 0) <= PEER_CONNECTED_S]
        return relay.covers(digest, others)

    def partial_ack_loop(self):
        """Ask senders for fragments of theirs that never arrived.

        Mirrors rns_bridge._report_stalled_reassemblies, and must. This node is
        an ENDPOINT for fragmented traffic -- the drawings and long routes the
        phones send -- so without it a phone's retry resends every fragment
        here and the bench shows the bitmap failing when it is the instrument
        that never asked. Third time in one session that an endpoint not
        behaving like the app would have measured the wrong thing; the first
        two were acks it never sent and restarts it never noticed.
        """
        while not self.stop.wait(1.0):
            for origin, msg_id, count, have in self.reassembler.stalled(
                    REASSEMBLY_STALL_S):
                try:
                    frame = cot_codec.encode_partial(origin, msg_id,
                                                     self.node_id, count, have)
                except Exception as e:
                    print("[tx] could not build partial ack: %s" % e)
                    continue
                sealed = self.mesh.wrap(frame, _hops_for(frame))
                if self._send(sealed, "partial ack"):
                    print("[tx] asking %s for fragments %s of message %d "
                          "(%d/%d held)"
                          % (origin, sorted(set(range(count)) - have), msg_id,
                             len(have), count))

    def relay_loop(self):
        while not self.stop.wait(0.05):
            now = time.monotonic()
            due = None
            with self.relay_lock:
                for frame_id, (send_after, sealed) in self.relay_pending.items():
                    if now >= send_after:
                        due = frame_id
                        break
                if due is not None:
                    _, sealed = self.relay_pending.pop(due)
            if due is not None:
                self._send(sealed, "relay")

    # -- transmit -----------------------------------------------------------

    def _neighbour_digest(self):
        now = time.time()
        with self.peers_lock:
            live = [n for n, p in self.peers.items()
                    if now - p.get("last_seen", 0) <= PEER_CONNECTED_S]
        return relay.digest_build(live)

    def beacon_loop(self):
        while not self.stop.is_set():
            digest = self._neighbour_digest()
            now = time.monotonic()
            # Unconditional, unlike rns_bridge, which skips a beacon whenever
            # other traffic has already proved the link this interval. This
            # node is the bench's reference point: a predictable heartbeat is
            # worth more here than the airtime it saves, and its absence in the
            # phones' logs is then a real signal rather than an optimisation.
            changed = digest != self.digest_sent
            self.beacon_seq += 1
            frame = cot_codec.encode_beacon(
                self.node_id, self.beacon_seq, 0,
                callsign=self.args.callsign, name=self.args.name,
                digest=digest)
            # hops=0: a beacon is never relayed. It defines the ONE-HOP
            # neighbour set every node's coverage test runs on, so flooding
            # it would make every node look adjacent to every other.
            sealed = self.mesh.wrap(frame, relay.HOPS_BEACON)
            if self._send(sealed, "beacon"):
                self.digest_sent = digest
                self.last_beacon_at = now
                print("[tx] beacon seq=%d (%dB on air) digest=%s%s"
                      % (self.beacon_seq, len(sealed), digest.hex(),
                         " CHANGED" if changed else ""))
            j = self.args.beacon_jitter
            self.stop.wait(max(0.0, self.args.beacon_every + random.uniform(-j, j)))

    def position_loop(self):
        if self.args.position_every <= 0:
            return
        uid = "ATAKRNS-LAPTOP-%s" % self.node_id
        lat, lon = self.args.lat, self.args.lon
        self.stop.wait(5.0)  # let the first beacons establish the peer table
        while not self.stop.is_set():
            # Wandered a little each time so the marker is visibly alive on the
            # phones' maps rather than a pin that might be a stale duplicate.
            lat += random.uniform(-0.00015, 0.00015)
            lon += random.uniform(-0.00015, 0.00015)
            xml = _pli(uid, self.args.callsign, lat, lon)
            try:
                packets = cot_codec.build_packets(xml, self.max_payload,
                                                  self.sa_tx)
            except cot_codec.CotEncodeError as e:
                print("[tx] position not encodable: %s" % e)
                self.stop.wait(self.args.position_every)
                continue
            for frame in packets:
                sealed = self.mesh.wrap(frame, _hops_for(frame))
                self._send(sealed, "position")
            print("[tx] position %.6f,%.6f as %d packet(s), %dB on air"
                  % (lat, lon, len(packets),
                     sum(len(p) + mesh.OVERHEAD for p in packets)))
            # Re-randomised every period, for the reason the beacon loop
            # already is: a fixed interval is the one thing that can collide
            # FOREVER, because two nodes whose sends drift into alignment have
            # nothing to push them apart again. Measured on the 2026-08-15
            # four-node bench with this line absent -- two identical laptop
            # nodes at the same 2 s interval, and the phones lost 37% of one
            # node's frames against 2% of the other's, the stronger signal
            # capturing the receiver every time they overlapped.
            self.stop.wait(self.args.position_every
                           * random.uniform(0.75, 1.25))

    # -- offered load -------------------------------------------------------

    def _load_interval(self):
        """Seconds between offered messages, re-read every cycle.

        From a file rather than fixed at startup so a ramp can step the load
        without restarting the node. Restarting would clear the peer table,
        and register() tracks nothing at all when handed an empty peer list,
        so the first messages of every step would leave the measurement
        instead of being counted as sent.
        """
        if not self.args.load_file:
            return self.args.load_every
        try:
            with open(self.args.load_file) as f:
                return float(f.read().split()[0])
        except Exception:
            return self.args.load_every

    def _drop_rate(self):
        """Fraction of received frames to discard, re-read every frame.

        From a file for the same reason the load rate is: sweeping it by
        restarting would cost a peer-table rebuild per value.
        """
        if not self.args.drop_file:
            return self.args.drop_rate
        try:
            with open(self.args.drop_file) as f:
                return float(f.read().split()[0])
        except Exception:
            return self.args.drop_rate

    def _in_fade(self, rate):
        """Is the link currently in a bad patch?

        A steady loss rate is the easy kind of bad link. Independent 25% loss
        means a message and all four retries failing is a 1-in-1000 event, so
        retrying nearly always wins eventually -- which flatters ARQ, and is
        not what being at the edge of range does. Real links fade: a few
        seconds where almost nothing gets through, long enough to swallow a
        message AND the retries meant to repair it, which no amount of
        retrying inside the fade survives.

        Two states, each held for an exponentially distributed time, with the
        bad state dropping everything. The mean bad period is set directly;
        the good period is derived so the LONG-RUN loss still equals `rate`.
        That is what makes a fading run comparable with a steady one: same
        average loss, different distribution, so burstiness is the only
        variable that moved.
        """
        bad_mean = self.args.drop_burst_s
        if self.args.burst_file:
            try:
                with open(self.args.burst_file) as f:
                    bad_mean = float(f.read().split()[0])
            except Exception:
                pass
        if bad_mean <= 0 or rate <= 0 or rate >= 1:
            return None                       # steady loss; caller flips a coin
        now = time.monotonic()
        if now < self._fade_until:
            return self._fade_bad
        # p = bad_mean / (bad_mean + good_mean)  ->  good_mean = bad_mean(1-p)/p
        good_mean = bad_mean * (1.0 - rate) / rate
        self._fade_bad = not self._fade_bad
        mean = bad_mean if self._fade_bad else good_mean
        # Exponential holds are the realistic choice and the wrong one for a
        # COMPARISON: a 150 s cell fits only ~9 cycles, so realised loss wanders
        # far enough around its target that two cells can differ by a factor of
        # two -- which is exactly what stopped the 2026-08-16 run separating
        # threshold 4 from 8. Fixed holds give every cell the same loss to three
        # figures, so the threshold is the only thing that differs between them.
        self._fade_until = now + (mean if self.args.drop_burst_fixed
                                  else random.expovariate(1.0 / mean))
        return self._fade_bad

    def _apply_congestion_threshold(self):
        """Re-read the in-flight threshold every tick.

        Sweeping it by restarting would cost a peer-table rebuild per value,
        and register() tracks nothing against an empty peer list -- the same
        reason --load-file exists. ReliableTx reads the attribute on each tick,
        so changing it takes effect at the next one with no other state moved.
        """
        if not self.args.congestion_file:
            return
        try:
            with open(self.args.congestion_file) as f:
                want = int(float(f.read().split()[0]))
        except Exception:
            return
        with self.reliable_lock:
            if want != self.reliable_tx.congestion_inflight:
                print("[load] congestion threshold now %d (was %d)"
                      % (want, self.reliable_tx.congestion_inflight))
                self.reliable_tx.congestion_inflight = want

    def load_loop(self):
        if self.args.load_every <= 0 and not self.args.load_file:
            return
        uid = "ANDROID-BENCH-%s" % self.node_id
        # Let the peer table fill first, for the register() reason above.
        self.stop.wait(max(8.0, self.args.beacon_every * 0.5))
        while not self.stop.is_set():
            interval = self._load_interval()
            if interval <= 0:
                self.stop.wait(1.0)
                continue
            self._offer_one(uid)
            # Jittered for the reason position_loop is: two identical nodes on
            # one fixed interval collide forever, which the 2026-08-15 bench
            # measured directly.
            self.stop.wait(interval * random.uniform(0.75, 1.25))

    def _offer_one(self, uid):
        self.load_seq += 1
        # Shared with the app's own load test, so a bench run here and a bench
        # run on the phones offer byte-identical traffic. See loadtest.py.
        xml = loadtest.geochat(uid, self.args.callsign, self.load_seq,
                               self.args.load_bytes)
        self._send_cot(xml, "load %d" % self.load_seq, "load")

    def _send_cot(self, xml, label, tag):
        """Encode one CoT document and put it on the air, acknowledged.

        Shared by the synthetic load generator and --send-file, so a marker or
        a route injected by hand takes exactly the path a real one would.
        """
        budget = self.max_payload - cot_codec.reliable_overhead()
        try:
            frame = cot_codec.encode(xml, budget)
        except cot_codec.CotEncodeError as e:
            print("[%s] not encodable: %s" % (tag, e))
            return
        now = time.time()
        with self.peers_lock:
            peers = [n for n, p in self.peers.items()
                     if now - p.get("last_seen", 0) <= PEER_CONNECTED_S]
        with self.reliable_lock:
            msg_id, wrapped = self.reliable_tx.register(
                frame, self.node_id, peers, label)
            try:
                packets = cot_codec.fragment_reliable(wrapped, self.max_payload)
                self.reliable_tx.note_fragment_count(msg_id, len(packets))
            except Exception as e:
                self.reliable_tx.forget(msg_id)
                print("[%s] not fragmentable: %s" % (tag, e))
                return
        self.load_sent_at[msg_id] = time.monotonic()
        self.load_stats["sent"] += 1
        on_air = 0
        for p in packets:
            if self._send(self.mesh.wrap(p, _hops_for(p)), tag):
                on_air += len(p) + mesh.OVERHEAD
        print("[%s] sent msg=%d %s frags=%d %dB peers=%d"
              % (tag, msg_id, label, len(packets), on_air, len(peers)))

    def send_loop(self):
        """Transmit whatever CoT is dropped into --send-file, then empty it.

        A bench needs to originate the traffic types the synthetic load cannot:
        markers, drawings and routes come from a human tapping ATAK, and there
        is no ATAK on a laptop. Pointing this at a file of real CoT puts one on
        the air through the ordinary encode-and-acknowledge path, so what
        crosses is exactly what a phone would have sent.

        Same polled-file shape as --load-file and --drop-file, for the same
        reason: no restart, so the peer table survives.
        """
        if not self.args.send_file:
            return
        while not self.stop.wait(0.5):
            try:
                with open(self.args.send_file, "rb") as f:
                    xml = f.read().strip()
            except Exception:
                continue
            if not xml:
                continue
            # Emptied BEFORE sending, so a send that raises cannot loop on the
            # same document forever.
            try:
                open(self.args.send_file, "wb").close()
            except Exception:
                pass
            self._send_cot(xml, "injected %d bytes" % len(xml), "send")

    def reliable_loop(self):
        """Retransmits offered load nobody has acknowledged.

        Mirrors rns_bridge._reliable_loop with one deliberate omission: the
        app defers an entire tick while its TX queue is deep, so that ARQ
        cannot amplify a channel that is already behind. This tool sends
        straight at the interface and has no queue of its own to measure, so
        it retries unconditionally -- which makes it a HARSHER load source
        than the app. Worth remembering when reading a collapse: some of the
        retry pressure here is pressure the app would have withheld.
        """
        while not self.stop.wait(RELIABLE_TICK_S):
            self._apply_congestion_threshold()
            with self.reliable_lock:
                retries, gave_up = self.reliable_tx.tick()
            for msg_id, frame, label, attempts, missing, needed in retries:
                try:
                    packets = cot_codec.fragment_reliable(frame,
                                                          self.max_payload)
                except Exception as e:
                    print("[load] retry not fragmentable: %s" % e)
                    continue
                if needed is not None:
                    packets = [p for i, p in enumerate(packets) if i in needed]
                self.load_stats["retry"] += 1
                for p in packets:
                    self._send(self.mesh.wrap(p, _hops_for(p)), "load retry")
                print("[load] retry msg=%d attempt=%d frags=%d waiting_on=%s"
                      % (msg_id, attempts, len(packets),
                         ",".join(sorted(missing)) or "?"))
            for rec in gave_up:
                self.load_stats["undelivered"] += 1
                self.load_sent_at.pop(rec.msg_id, None)
                print("[load] UNDELIVERED msg=%d after %d tx: %s, owed by %s"
                      % (rec.msg_id, rec.attempts, rec.reason,
                         ",".join(sorted(rec.missing)) or "?"))

    # -- reporting ----------------------------------------------------------

    def status_loop(self):
        while not self.stop.wait(30.0):
            if self.acks_suppressed:
                print("[node] acks suppressed so far: %d" % self.acks_suppressed)
            now = time.time()
            with self.peers_lock:
                lines = ["%s%s snr=%s digest=%s" %
                         (n, "" if now - p.get("last_seen", 0) <= PEER_CONNECTED_S
                          else " (STALE)",
                          p.get("snr"), (p.get("digest") or b"").hex() or "none")
                         for n, p in sorted(self.peers.items())]
            print("[status] %s" % self.mesh.summary_line())
            if self.load_stats or self.args.load_every or self.args.load_file:
                s = self.load_stats
                sent = s.get("sent", 0)
                delivered = s.get("delivered", 0)
                with self.reliable_lock:
                    tx_stats = self.reliable_tx.stats()
                print("[status] load: offered=%d delivered=%d undelivered=%d "
                      "retries=%d suppressed=%d inflight=%d ratio=%s "
                      "interval=%.2fs"
                      % (sent, delivered, s.get("undelivered", 0),
                         s.get("retry", 0),
                         tx_stats.get("retry_suppressed", 0),
                         tx_stats.get("pending", 0),
                         "n/a" if not sent else "%.0f%%"
                         % (100.0 * delivered / sent),
                         self._load_interval()))
            if self.args.relay:
                print("[status] %s" % self.relay_policy.summary_line())
            print("[status] rx direct=%d relayed=%d ignored=%d dup=%d "
                  "own-back=%d foreign=%d fixture-dropped=%d/%d (rate=%.2f)"
                  % (self.counts["direct_in"], self.counts["relayed_in"],
                     self.counts["ignored_direct"], self.counts["duplicate"],
                     self.counts["own_relayed_back"],
                     self.counts["foreign_or_forged"],
                     self.counts["fixture_dropped"], self.counts["rx_frames"],
                     self._drop_rate()))
            for line in lines:
                print("[status]   peer %s" % line)

    def run(self):
        threads = [threading.Thread(target=t, daemon=True) for t in
                   (self.beacon_loop, self.position_loop, self.status_loop,
                    self.relay_loop, self.partial_ack_loop, self.ack_loop,
                    self.load_loop, self.reliable_loop, self.send_loop)]
        for t in threads:
            t.start()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[node] stopping")
            self.stop.set()
            try:
                if getattr(self, "mt_port", None) is not None:
                    self.mt_port.stop()
                else:
                    self.iface.serial.close()
            except Exception:
                pass


def _hops_for(frame):
    """What a SENDER stamps on a frame of this class. Mirrors
    rns_bridge._hops_for; the two must agree or _heard_directly misreads
    every frame."""
    if not frame:
        return 0
    kind = frame[0] & 0x0F
    if kind == cot_codec.T_BEACON:
        return relay.HOPS_BEACON
    if kind in (cot_codec.T_ACK, cot_codec.T_PARTIAL):
        # As far as the message it answers. Was HOPS_BEACON here and in the
        # app, which is what made every multi-hop chat on the 2026-08-12 bench
        # arrive, get acknowledged, and still be reported UNDELIVERED.
        return relay.HOPS_ONESHOT
    if kind in cot_codec.COMPACT_KINDS:
        return relay.HOPS_POSITION
    return relay.HOPS_ONESHOT


def _message_shape(frame):
    """(msg_id, fragment count) for a fragment, (None, 1) for anything else."""
    if len(frame) >= 5 and frame[0] == cot_codec.CTRL_FRAG:
        msg_id, _index, count = struct.unpack(">HBB", frame[1:5])
        return msg_id, max(1, count)
    return None, 1


def _pli(uid, callsign, lat, lon):
    """A position report shaped like the ones ATAK actually emits, so it takes
    the compact path rather than the zlib fallback -- the fixture in
    test_cot_codec.py with this node's identity substituted in."""
    now = time.gmtime()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", now) + ".000Z"
    stale = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() + 135)) + ".000Z"
    return (
        '<event version="2.0" uid="%s" type="a-f-G-U-C" '
        'time="%s" start="%s" stale="%s" how="m-g">'
        '<point lat="%.7f" lon="%.7f" hae="120.0" ce="9.5" le="9999999.0"/>'
        '<detail>'
        '<takv os="29" version="bench" device="LAPTOP RNODE" platform="ATAK-CIV"/>'
        '<contact endpoint="*:-1:stcp" callsign="%s" phone=""/>'
        '<uid Droid="%s"/>'
        '<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
        '<__group role="Team Member" name="Cyan"/>'
        '<status battery="100"/>'
        '</detail></event>'
        % (uid, stamp, stamp, stale, lat, lon, callsign, callsign)
    ).encode("utf-8")


def main():
    # Line-buffered even when stdout is a pipe or a file. Python block-buffers
    # by default off a terminal, so `mesh_node.py | tee bench.log` -- the
    # obvious thing to do with a bench instrument -- shows nothing at all until
    # 8 KB has accumulated or the process exits. A live log that only arrives
    # after the run is over is not a live log.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # pragma: no cover - Python < 3.7
        pass

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="serial device, e.g. /dev/ttyUSB0")
    p.add_argument("--passphrase", default=os.environ.get("ATAKRNS_PASSPHRASE", ""),
                   help="group passphrase; must match the phones exactly")
    p.add_argument("--deaf-to", default="",
                   help="comma-separated node ids to ignore when heard DIRECTLY, "
                        "which is what forces a line topology on one desk")
    p.add_argument("--name", default="laptop", help="device name in beacons")
    p.add_argument("--callsign", default="LAPTOP01", help="ATAK callsign")
    p.add_argument("--node-id", default="",
                   help="six hex characters; persisted, so pass it once and it "
                        "sticks. Omit to have one generated on first run")
    p.add_argument("--id-only", action="store_true",
                   help="print this node's id and key fingerprint and exit, "
                        "without opening the radio -- the id is what goes in "
                        "the phone's .atakrns_deaf_to")
    p.add_argument("--relay", action="store_true",
                   help="also relay for others, i.e. make THIS node the middle "
                        "of the line. Off by default: the point of the bench is "
                        "to watch a phone relay, and a second relay in the cell "
                        "would cancel it")
    p.add_argument("--force-relay", action="store_true",
                   help="BENCH ONLY. With --relay, relay every frame the "
                        "coverage test would refuse, so a desk where every node "
                        "hears every other can still exercise the relay path. "
                        "Hop limit, beacon refusal and the airtime budget still "
                        "apply. Never on a field node: it turns this one into a "
                        "flood point for everything it hears")
    p.add_argument("--position-every", type=float, default=60.0,
                   help="seconds between position reports, 0 to stay quiet")
    p.add_argument("--lat", type=float, default=35.1234567)
    p.add_argument("--lon", type=float, default=-79.7654321)
    p.add_argument("--txpower", type=int, default=DEFAULT_TXPOWER)
    p.add_argument("--preset", default=radio.DEFAULT_PRESET,
                   choices=radio.preset_names(),
                   help="modem preset; must match the phones' Radio setting")
    p.add_argument("--frequency-hz", type=int, default=radio.DEFAULT_FREQUENCY_HZ)
    p.add_argument("--beacon-every", type=float, default=BEACON_INTERVAL_S,
                   help="seconds between beacons; match the phones")
    p.add_argument("--lose-fragment", default="",
                   help="BENCH FIXTURE: fragment indices to drop once each "
                        "(e.g. 1 or 0,2), so a fragmented message stalls and "
                        "the per-fragment ack bitmap has something to report. "
                        "Loss is what the feature reacts to, and a desk with "
                        "clear line of sight never provides any")
    p.add_argument("--beacon-jitter", type=float, default=BEACON_JITTER_S,
                   help="STRESS: +/- seconds of random spread on each beacon "
                        "interval. 0 with a short --beacon-every makes a flood "
                        "of numbered, unacknowledged packets -- a load the "
                        "receiver never answers, so its own transmissions "
                        "cannot cost it receptions")
    p.add_argument("--load-every", type=float, default=0.0,
                   help="STRESS: seconds between offered ACKNOWLEDGED chat "
                        "messages, 0 to offer none. This is the load knob; "
                        "positions and beacons are the background traffic")
    p.add_argument("--send-file", default="",
                   help="poll this file; transmit its contents as a CoT event "
                        "and empty it. For traffic types the synthetic load "
                        "cannot make -- markers, drawings, routes.")
    p.add_argument("--load-file", default="",
                   help="STRESS: read --load-every from this file every cycle, "
                        "so a ramp can change the rate without a restart "
                        "(a restart clears the peer table, and messages "
                        "registered against an empty one are never tracked)")
    p.add_argument("--load-bytes", type=int, default=40,
                   help="STRESS: chat body length in characters")
    p.add_argument("--drop-rate", type=float, default=0.0,
                   help="BENCH FIXTURE: discard this fraction of RECEIVED "
                        "frames, so a desk with clear line of sight can stand "
                        "in for a link at the edge of range. TX power cannot "
                        "do this: at 915 MHz across a desk even 0 dBm leaves "
                        "~85 dB of margin, so nothing is ever lost")
    p.add_argument("--drop-burst-s", type=float, default=0.0,
                   help="BENCH FIXTURE: mean seconds of a BAD patch. 0 spreads "
                        "loss evenly, which is the easy kind of bad link. Above "
                        "0 the loss arrives in fades long enough to take a "
                        "message and its retries together, with the good "
                        "periods sized so the long-run loss still equals "
                        "--drop-rate -- so a fading run and a steady one are "
                        "comparable and burstiness is the only variable")
    p.add_argument("--drop-burst-fixed", action="store_true",
                   help="BENCH FIXTURE: hold each good/bad period for exactly "
                        "its mean instead of drawing it. Less realistic, but "
                        "it hands every cell identical loss, which is what a "
                        "comparison between two settings needs")
    p.add_argument("--burst-file", default="",
                   help="BENCH FIXTURE: re-read --drop-burst-s from this file")
    p.add_argument("--drop-file", default="",
                   help="BENCH FIXTURE: re-read --drop-rate from this file, so "
                        "loss can be swept without restarting")
    p.add_argument("--congestion-file", default="",
                   help="STRESS: re-read reliable.DEFAULT_CONGESTION_INFLIGHT "
                        "from this file every tick, so the threshold can be "
                        "swept without restarting and losing the peer table")
    p.add_argument("--print-cot", action="store_true",
                   help="dump the decoded CoT XML of every event received")
    p.add_argument("--meshtastic", action="store_true",
                   help="the radio on --port is a Meshtastic board, not an RNode")
    p.add_argument("--manage-board", action="store_true",
                   help="with --meshtastic: put the board on the app's managed "
                        "profile (preset/frequency/power from the arguments, "
                        "channel from the passphrase, never repeat) first")
    p.add_argument("--link-latency-s", type=float, default=None,
                   help="per-leg radio delay for the retry timer (default: the "
                        "app's figure for the radio -- mt_link.LINK_LATENCY_S "
                        "with --meshtastic, 0 for an RNode)")
    p.add_argument("--mt-diag", action="store_true",
                   help="with --meshtastic: print the port's per-packet lines")
    args = p.parse_args()
    if args.manage_board and not args.meshtastic:
        p.error("--manage-board needs --meshtastic")
    if args.force_relay and not args.relay:
        p.error("--force-relay only changes what --relay does, so it needs --relay")

    if not args.passphrase.strip():
        p.error("a passphrase is required -- there is deliberately no default, "
                "so an unconfigured node stays isolated rather than joining a "
                "network everyone can read. Use --passphrase or $ATAKRNS_PASSPHRASE.")
    if args.node_id.strip():
        try:
            mesh.node_id_bytes(args.node_id.strip().lower())
        except mesh.MeshError as e:
            p.error("%s. A node id is 3 bytes on the wire, written as exactly 6 "
                    "hex characters (0-9, a-f) -- it is an address, not a name. "
                    "The readable name is --name, which is what beacons carry "
                    "and what the phones' node list shows." % e)

    node = Node(args)
    node.print_identity()
    if args.id_only:
        return 0
    if not node.start_radio():
        return 1
    node.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
