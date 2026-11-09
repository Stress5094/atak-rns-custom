"""
Wire RNS to an RNode reached via the Kotlin bridge socket (USB or Bluetooth,
indistinguishable from here), and relay CoT
events to/from the Kotlin TAK-server bridge socket over a single-hop RNS
broadcast destination.

Milestone 1 relayed raw bytes with no parsing. That could never work for real
traffic: RNS.Packet.PLAIN_MDU is 464 bytes and Packet.pack() raises IOError
above the MTU, while an ATAK position report is 600-900 bytes of XML. As of
milestone 2 every event goes through cot_codec, which compacts a typical PLI
to ~61 bytes and fragments anything still too large.

Milestone 3 added tx_policy, which decides how often an event earns airtime.
Milestone 4 added reliable.py: chat, drawings and routes are acknowledged and
retried, because unlike position reports there is no later event to supersede
a dropped one. Positions stay fire-and-forget -- they self-heal.

Still no multi-hop, and still a single shared PLAIN broadcast destination.

Called from Kotlin via Chaquopy, e.g.:

    val py = Python.getInstance()
    val bridge = py.getModule("rns_bridge")
    bridge.callAttr("start")
"""

import collections
import hashlib
import json
import math
import os
import queue
import random
import signal
import socket
import struct
import threading
import time

import serial  # pyserial, pulled in as an RNS dependency

import airtime
import cot_codec
import display
import eligibility
import fm_config
import fm_custom
import fm_link
import fm_resend
import gateway as gateway_mod
import lora_health
import mesh
import mesh_view
import mt_link
import peers as peers_mod
import radio
import rangetest
import relay
import reliable
import spectrum
import tx_policy
import wifi_health

# RNS.Reticulum() registers SIGINT/SIGTERM handlers internally via
# signal.signal(), which CPython only allows from the main thread of the
# main interpreter. start() runs on a background thread Kotlin spawns via
# Chaquopy (see PythonRnsService.kt), not the thread that first initialized
# the interpreter -- and POSIX signal handling doesn't mean anything in an
# Android app's lifecycle anyway (no SIGINT is ever coming). No-op it out
# before RNS's constructor can reach it.
signal.signal = lambda *args, **kwargs: None

import RNS

# RNS auto-generates a default config file (including an `AutoInterface`
# named "Default Interface") the first time it starts with no existing
# config at this path. That AutoInterface tries to enumerate local network
# interfaces via socket.if_nametoindex(), which Chaquopy's Android Python
# build doesn't implement -- raising an uncaught OSError out of
# RNS.Reticulum() itself before it ever returns. We attach the RNode
# interface manually below, so no configured interfaces are needed at all;
# writing a minimal config ahead of time prevents RNS from ever generating
# the default one.
RNS_CONFIG_DIR = os.path.expanduser("~/.reticulum")
_MINIMAL_RETICULUM_CONFIG = """# Written by rns_bridge.py -- intentionally has no [[interfaces]] entries.
# See the comment above RNS_CONFIG_DIR in rns_bridge.py for why.

[reticulum]
  enable_transport = Yes
  share_instance = No

[logging]
  loglevel = 6

[interfaces]
"""

RNODE_BRIDGE_HOST = "127.0.0.1"
RNODE_BRIDGE_PORT = 7100
COT_BRIDGE_HOST = "127.0.0.1"
COT_BRIDGE_PORT = 7101
# FM's own loopback port, separate from RNODE_BRIDGE_PORT -- for
# LoRa-primary/FM-backup routing, UsbSerialBridgeService/
# BleSerialBridgeService and FmAudioBridgeService can now run concurrently
# (see RadioBridgeService.kt), so they can no longer share one port. Not
# 7101: that's COT_BRIDGE_PORT above, already in use for something else.
FM_BRIDGE_HOST = "127.0.0.1"
FM_BRIDGE_PORT = 7102
RNODE_BAUD = 115200

# Real LoRa radio parameters pushed to the physical RNode during the KISS
# handshake (initRadio()). These MUST match on every node to talk to each
# other, and MUST be legal for your region/license.
#
# SET FROM THE UI, not from here. They were constants until the 2026-08-14
# bench, where the evening was spent editing three of them and rebuilding
# twice -- for a value the operator has to be able to change in the field,
# on both phones, without a laptop. The preset table is Meshtastic's; see
# radio.py for why their names rather than ours.
#
# Persisted in app-private storage the same way the mesh passphrase is, and
# for the same reason: a setting that has to match across devices must survive
# a reinstall, or every `adb install -r` silently splits the network.
_RADIO_CONFIG_PATH = os.path.expanduser("~/.atakrns_radio")
_radio_config = None            # cached; None until first read
_radio_config_lock = threading.Lock()


def _default_radio_config():
    return {
        "preset": radio.DEFAULT_PRESET,
        "frequency_hz": radio.DEFAULT_FREQUENCY_HZ,
        "txpower_dbm": radio.DEFAULT_TXPOWER_DBM,
    }


def _load_radio_config():
    """The saved radio settings, or the defaults.

    Falls back to the defaults on ANY problem rather than raising: a corrupt
    settings file must not be the reason a radio will not come up, because
    that failure is indistinguishable from a dead board and this app is
    already bad at telling those apart.
    """
    global _radio_config
    with _radio_config_lock:
        if _radio_config is not None:
            return dict(_radio_config)
        config = _default_radio_config()
        try:
            with open(_RADIO_CONFIG_PATH) as f:
                saved = json.load(f)
            if radio.validate(saved.get("preset"), saved.get("frequency_hz"),
                              saved.get("txpower_dbm")) is None:
                config = {
                    "preset": saved["preset"],
                    "frequency_hz": int(saved["frequency_hz"]),
                    "txpower_dbm": int(saved["txpower_dbm"]),
                }
            else:
                RNS.log("[cot-bridge] saved radio settings are invalid; "
                        "using defaults", RNS.LOG_WARNING)
        except FileNotFoundError:
            pass
        except Exception as e:
            RNS.log("[cot-bridge] could not read radio settings (%s); "
                    "using defaults" % e, RNS.LOG_WARNING)
        _radio_config = config
        return dict(config)


def _save_radio_config(config):
    try:
        with open(_RADIO_CONFIG_PATH, "w") as f:
            json.dump(config, f)
        return True
    except OSError:
        return False


def _radio_params():
    """The five numbers the RNode actually wants, from the current settings."""
    config = _load_radio_config()
    sf, bandwidth, coding_rate = radio.preset_params(config["preset"])
    return {
        "frequency_hz": config["frequency_hz"],
        "bandwidth_hz": bandwidth,
        "txpower_dbm": config["txpower_dbm"],
        "spreading_factor": sf,
        "coding_rate": coding_rate,
    }

# Fixed broadcast "app namespace" both phones must agree on. Milestone 1
# uses a single shared plaintext broadcast destination -- fine for a bench
# test with two devices in radio range, not meant to be the final design.
APP_NAME = "atakrns"
ASPECTS = ("cot", "broadcast")

# One entry per radio -- spelled out rather than left implicit because
# every frame now has to say which radio it arrived on, and "the interface"
# stops being an answer the moment there are two. Defined here, ahead of
# everything else in the module, because _active_link below needs a value
# at import time.
LINK_LORA = "lora"
LINK_FM = "fm"
LINK_WIFI = "wifi"

# Highest preference first: LoRa is the proven primary long-range link, FM is
# its backup, and WiFi is the link of LAST resort -- deliberately below both,
# not above them. It was originally highest-bandwidth-first, but that made
# merely enabling the WiFi toggle eventually preempt a perfectly healthy
# LoRa/FM link (see _check_auto_switch()'s own failback pass), and toggling it
# back off only takes effect on the next process restart, so a live link could
# not be "rewound" once WiFi had taken over. ATAK already handles multicast
# WiFi for its own SA, so WiFi only earns this link when no radio is working at
# all. _check_auto_switch() walks this tuple, not a hardcoded pair, so a link
# simply not being built (this tuple's entries with no matching _link_ifaces
# entry) degrades gracefully back to LoRa-only behaviour.
#
# FM WAS REMOVED FROM HERE on 2026-10-01 and restored on 2026-10-02, rebuilt
# from the ground up against the bench record in docs/fm-lessons.md and
# docs/open-questions.md. That re-adding it is one entry in this tuple plus a
# _build_fm_interface() is the property that made it worth excising as a
# registry entry rather than unpicking it line by line -- the failover
# machinery is N-way and needed no other change.
_LINK_PRIORITY = (LINK_LORA, LINK_FM, LINK_WIFI)

_reticulum = None
_destination = None
_cot_socket = None
_cot_socket_lock = threading.Lock()
# Any of these may be None: each follows the "None unless actually built"
# convention. _fm_iface is None unless _transport == "fm" or
# _fm_backup_enabled; _wifi_iface unless set_wifi_enabled() said so; and
# _lora_iface when the LoRa hardware is absent.
#
# Three globals rather than one shared _rnode_iface, which is what this was
# before there was a second KIND of interface: LoRa-primary/FM-backup routing
# needs both to be live at once, not to alias each other.
_lora_iface = None
_fm_iface = None
_wifi_iface = None

# The same interfaces as _lora_iface/_wifi_iface above, but keyed by link
# name so the N-way failover machinery below (_iface_for, _set_active_link,
# _check_auto_switch, _link_of, set_link_override) can walk _LINK_PRIORITY
# generically instead of special-casing each link by its own global. The
# named globals stay, because plenty of link-specific code (signal stats,
# say) only makes sense for one particular link and reads its global
# directly. Populated by each _build_*_interface() as it runs; a link with
# no entry here has simply never been built.
_link_ifaces = {}

# Per-link health trackers, keyed the same way. NOT EVERY LINK HAS ONE: FM's
# viability is only ever "is it built and reporting online" (see _state_for()),
# because FM traffic never goes through reliable.py's ack-based ARQ at all and
# so produces no soft signal to track -- see _queue_ack and _escalate_to_fm.
# What FM has instead is fm_health's graded receive-path state, which answers a
# different question (can this node HEAR anything) and is reported separately
# in _fm_status(). LoRa and WiFi both run ARQ, so both get a real tracker.
_link_health = {
    LINK_LORA: lora_health.LoraHealth(),
    LINK_WIFI: wifi_health.WifiHealth(),
}
# Kept as its own name too (rather than only _link_health[LINK_LORA]):
# existing log lines, the status snapshot and _handle_ack/_reliable_loop's
# note_delivered/note_escalated feed points all predate the registry and
# read this name directly for LoRa specifically.
_lora_health = _link_health[LINK_LORA]

# Which link is currently carrying broadcast/periodic traffic (beacons,
# positions, heartbeats -- anything with no per-message ack to hang a
# per-message link choice on). Fixed for the life of the process in
# single-link mode; in multi-link mode, moved by _check_auto_switch()
# (below) or by a person via set_link_override().
_active_link = LINK_LORA

# None (the default) means automatic -- _check_auto_switch() decides.
# Pins the active link regardless of health, until a person clears it
# again. Set only by set_link_override() (Kotlin entry point) -- see that
# function's own docstring.
_link_override = None

# When each link's health last became continuously UP, or None while it is
# not -- one entry per link in _LINK_PRIORITY, all starting unset.
# _check_auto_switch()'s own failback debounce -- see
# _FAILBACK_DEBOUNCE_S's comment for why failing back waits and failing
# over does not.
_link_up_since = {link: None for link in _LINK_PRIORITY}

# Quick to declare trouble (the hard signal is a live boolean and the soft
# one needs only lora_health.DEFAULT_MIN_SAMPLES failed one-shot attempts,
# both far under this), cautious to declare all-clear: a single lucky ack
# right after a bad patch must not cause an immediate failback-then-fail-
# again flap. Mirrors this codebase's existing pattern elsewhere
# (RadioBridgeService.handleLinkFailure()'s eagerness to drop a dead link
# vs. FmAudioBridgeService.CHANNEL_BUSY_MAX_WAIT_MS's deliberately generous
# wait). A starting point for field tuning, not a bench-derived figure --
# unlike fm_resend.py's constants, nothing has measured this one yet. One
# shared value for every tier: failing back from FM to LoRa and failing
# back from LoRa to WiFi are the same kind of decision.
_FAILBACK_DEBOUNCE_S = 30.0

# Long enough to outlive the sender's entire retry schedule, and DERIVED from
# it rather than picked, because the two drifting apart is a silent failure:
# fragments accumulated across attempts are worthless if they are discarded
# just before the attempt that would have completed them, and that is exactly
# the airtime the per-fragment bitmap exists to save. Sized for the worst case
# this link allows -- a full-MTU frame to the furthest peer a one-shot hop cap
# permits -- with a margin on top.
#
# The fixed 30 s this replaces was fine until the multi-hop retry allowance
# landed earlier the same day and took a two-hop schedule to 30.9 s.
_REASSEMBLY_MARGIN = 1.25


def _reassembly_window_s(include_fm=True, airtime_s=None):
    """The longest retry schedule this node may run, plus margin.

    A MAX RATHER THAN A CHOICE, because ONE reassembler serves every link. The
    two schedules are built on completely different principles -- reliable.py's
    ARQ retries on a timer that scales with measured airtime, fm_resend's blind
    resends are spaced in wall-clock MINUTES against a channel whose bad
    patches last minutes -- and the buffer has to survive whichever is longer.

    WHY NOT PER-LINK, which is the obvious alternative: failover is N-way and
    live, so a message's attempt 1 can go out over LoRa and its attempt 4 over
    FM after a DEGRADED switch. Fragments from both have to land in the same
    reassembly or the message cannot complete at all. The pre-teardown code
    guarded FM out of the resize with an early `return`, which was right when
    FM was the exclusive transport and is the wrong shape now.

    THE ASYMMETRY IS WHAT DECIDES THE DEFAULT. Over-sizing costs a stalled
    partial holding one of Reassembler's 16 slots for longer, which is bounded
    and visible -- the cap evicts oldest-first and counts it in `dropped`.
    Under-sizing takes a multi-fragment message from roughly 60% deliverable to
    under 2% (docs/open-questions.md:2291) and is visible from nowhere except a
    delivery rate. So `include_fm` defaults to True: the import-time value is a
    safe floor for any transport, and _resize_reassembly tightens it once
    Kotlin has said which transport this actually is.
    """
    # The Meshtastic board's fixed per-leg delay is charged whatever the radio,
    # by the same asymmetry: it runs at import, before the firmware setting is
    # known, and over-sizing the buffer for an RNode by a few seconds is the
    # cheap direction.
    spans = [reliable.max_retry_span_s(
        hops=relay.HOPS_ONESHOT + 1,
        frame_len=cot_codec.RNS_MAX_PAYLOAD,
        link_latency_s=mt_link.LINK_LATENCY_S,
        **({} if airtime_s is None else {"airtime_s": airtime_s})
    )]
    if include_fm:
        spans.append(fm_resend.span_s())
    return _REASSEMBLY_MARGIN * max(spans)


# Built with the FM figure included, deliberately. It is the SAFE floor: a
# node whose transport is FM may never reach _resize_reassembly at all, since
# both of that function's callers hold an RNode interface. Over-sizing for the
# few milliseconds before startup tightens it costs nothing.
_REASSEMBLY_TIMEOUT_S = _reassembly_window_s(include_fm=True)
_reassembler = cot_codec.Reassembler(timeout=_REASSEMBLY_TIMEOUT_S)

# Decides which captured events are worth airtime. Milestone 2 made events
# small; this decides how often to send them, which is what determines whether
# the link carries two nodes or a squad. See tx_policy.py.
_tx_policy = tx_policy.TxPolicy()

# Bounded so a radio that falls behind sheds load instead of growing without
# limit. Overridden in start() with a cross-check against RNS's own constant.
_tx_queue = queue.Queue(maxsize=32)
# The budget handed to the encoder and the fragmenter: what fits INSIDE a mesh
# envelope. _max_packet is the outer limit RNS will actually accept. Keeping
# both is what stops a one-packet message from silently becoming two once the
# envelope is added.
_max_payload = cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD
_max_packet = cot_codec.RNS_MAX_PAYLOAD

# FM's OWN budget, separate from _max_payload/_max_packet above (which stay
# LoRa's even in dual mode -- LoRa is primary, and ordinary sizing should not
# shrink just because a backup link exists). Only meaningful once _fm_iface is
# built; None until then. Consumed by anything that explicitly targets FM
# regardless of _active_link: FM's own native one-shot path (which in FM-only
# mode equals _max_payload/_max_packet, computed the same way via
# _compute_max_packet), and the escalation path, which needs it in dual mode
# where _max_payload/_max_packet mean something else entirely.
#
# AT THE OPERATING POINT THIS IS 29 B AGAINST LORA'S ~464. The gap is three
# orders of nothing: a beacon fits exactly and a chat message fragments. That
# is why this is its own pair of numbers rather than a clamp applied to the
# primary ones.
_max_payload_fm = None
_max_packet_fm = None

# Authenticated encryption and frame identity for everything on the air. Built
# in start() once the passphrase is known; until then there is nothing to send.
_mesh = None
_MESH_PASSPHRASE_PATH = os.path.expanduser("~/.atakrns_passphrase")

# Whether the radio's own screen shows this app's status instead of the
# firmware's pages. Off until asked for: the screen is the radio's, and taking
# it over is a choice rather than a default.
_RADIO_DISPLAY_PATH = os.path.expanduser("~/.atakrns_display")
_display_enabled = None          # None until first read from disk
_display_lock = threading.Lock()

# What the radio calls itself, which only the phone's Bluetooth stack knows --
# nothing in the KISS protocol RNS speaks carries it. Handed in by Kotlin at
# startup; empty over USB, where the cable answers the question anyway.
_radio_label = ""

# Which RadioLink transport Kotlin has selected -- "usb"/"ble" both build the
# same KISS-speaking RNodeInterface (see _build_rnode_interface(); the two
# transports are transparent to Python, since RadioBridgeService's loopback
# socket carries identical bytes either way). "fm" is different in kind, not
# just in wire: there is no KISS/radio-config handshake to do at all, so it
# gets its own Interface subclass entirely (see _build_fm_interface() and
# fm_link.py). Handed in by Kotlin at startup, before start() -- see
# PythonRnsService.kt's set_transport() call. RadioLink.kt holds the
# Kotlin-side source of truth for the picker and must list the same values.
_transport = "usb"

# WHAT FIRMWARE IS ON THE LoRa BOARD, which is a different question from how it
# is reached. "rnode" speaks KISS and is configured from here (frequency, power,
# the screen, Bluetooth pairing). "meshtastic" is a Meshtastic board used as
# the radio: it speaks Meshtastic's protobuf protocol over the SAME USB byte
# pipe, is configured on the board itself, and carries our sealed frames inside
# private-app packets (mt_link / mt_port). Either one occupies the LoRa slot, so
# failover, health, peers and the status screen see no difference.
#
# Handed in by Kotlin at startup, before start() -- RadioLink.radioFirmware().
# Meshtastic over Bluetooth is NOT supported: its BLE side is a GATT service,
# not a serial stream, so the bridge's byte pipe cannot carry it.
RADIO_RNODE = "rnode"
RADIO_MESHTASTIC = "meshtastic"
_radio_firmware = RADIO_RNODE

# LoRa-primary/FM-backup routing: whether start() should also bring up the FM
# interface alongside USB/BLE, not only when FM is the primary transport
# itself. Handed in by Kotlin at startup, before start() -- see
# PythonRnsService.kt's set_fm_backup_enabled() call. Meaningless (and
# ignored) when _transport is already "fm": there is no LoRa there to back up.
_fm_backup_enabled = False

# Whether start() should also bring up the WiFi interface -- same "handed in by
# Kotlin at startup, before start()" convention as _fm_backup_enabled above,
# via set_wifi_enabled(). Independent of _transport and _fm_backup_enabled
# entirely: WiFi is not a way of reaching the LoRa radio the way USB/BLE are,
# it is its own separate link, same as FM backup is.
_wifi_enabled = False

# The 802.11s mesh network interface name (e.g. "wlan0") WiFi should bind
# to -- handed in by Kotlin via set_wifi_ifname(), read from
# RadioLink.wifiIfname()'s own preference. Deliberately restrictive rather
# than left for AutoInterface to guess: a phone also has cellular and
# possibly an ordinary infrastructure WiFi link up at the same time, and
# joining multicast on those too would be pure waste (and, for the
# infrastructure case, an odd thing to route CoT over at all). Empty
# string means "not configured yet" -- _build_wifi_interface() treats that
# as "let AutoInterface enumerate every non-ignored interface itself",
# same as AutoInterface's own default when no `devices` restriction is
# given at all.
_wifi_ifname = ""

# Where libcodec2.so actually lives on this install
# (Context.applicationInfo.nativeLibraryDir) -- the FM link needs it before it
# can open a modem, because nothing find_library() tries resolves the bundled
# arm64 .so through the app's linker namespace. Passed through to
# fm_link.build_tx_burst()/feed_audio(), which set RTAK_CODEC2_LIB from it.
# Meaningless (and unused) for the LoRa transports, and empty on a desktop
# bench, where the system copy is found normally.
_native_lib_dir = ""

# How often the screen is redrawn. The peer list changes on the beacon
# interval, so anything faster is redrawing the same picture; anything slower
# and a node appearing takes longer to show up here than on the phone.
_DISPLAY_REFRESH_S = 5.0

# A full redraw every so often regardless of what changed, so a radio that
# rebooted (and cleared its framebuffer) repairs itself rather than showing
# half a screen until the peer list happens to move.
_DISPLAY_FULL_REDRAW_EVERY = 12
_mesh_passphrase = ""
# Held while _mesh is replaced. The UI thread can re-key at any moment while
# the TX loop is wrapping and the radio thread is unwrapping; both read _mesh
# into a local first, so a swap under them is safe, but two overlapping
# re-keys would race on the epoch file.
_mesh_lock = threading.Lock()
# (passphrase, fingerprint) -- see _key_fingerprint.
_fingerprint_cache = None

# Acknowledgements jump the queue. An ack that sits behind a multi-fragment
# message arrives after the sender's retry timer has already fired, so the
# sender retransmits, which lengthens the queue, which delays the next ack --
# congestion feeding itself. Separate queues rather than a PriorityQueue: no
# tuple-comparison pitfalls, and the priority reads straight off the code.
# Acks waiting out their jitter, keyed so they can be CANCELLED. A plain dict
# rather than a queue for the same reason _relay_pending is one: a queue cannot
# revoke, and revoking is the entire point -- if somebody else answers first,
# ours is no longer worth the airtime. See _queue_ack.
_ack_pending = collections.OrderedDict()   # (origin, msg_id) -> (send_after, frame)
_ack_pending_lock = threading.Lock()
_MAX_ACK_PENDING = 64
_ack_suppressed = 0
# Acks never queued at all, because the frame named somebody else as the node
# that owes the answer. Counted separately from _ack_suppressed: that one means
# "we would have said the same thing and somebody beat us to it", this one
# means "we were never being asked". Both are savings; only the second scales
# with the size of the mesh.
_ack_not_ours = 0
# Acks that shared a packet with another ack. Surfaced for the same reason
# _ack_suppressed is: the saving leaves no other trace.
_acks_bundled = 0

# The last self-position injected into the local ATAK for each peer, so it can
# be taken off the map when that peer goes quiet. See _withdraw_departed.
_last_marker = {}
_marker_lock = threading.Lock()

# Which peers were connected as of the last tick, so _note_peer_transitions
# can log the instant a peer drops or returns -- independent of whether it
# ever sent a markable position, which is _withdraw_departed's own narrower
# condition (see that function). _ever_connected_peer_ids is the all-time
# half of that: it is what tells a RETURNING peer apart from a peer that is
# simply new (already announced, in real time, by _note_new_peer) -- without
# it, a brand new peer's first appearance would also read as "is back",
# which it is not.
_last_connected_peer_ids = set()
_ever_connected_peer_ids = set()

# How many acks may share one packet. Not a size limit -- at 6 bytes each they
# would fit by the dozen -- but a latency one: an ack bundled behind seven
# others still has to wait for the whole packet to be built and sent, and the
# sender's retry timer is what that eats into.
_MAX_BUNDLED_ACKS = 8

# Reticulum's own per-packet header: 2 bytes of flags and hops, a 16-byte
# destination hash, 1 byte of context. Not ours to change, and quoted here only
# so the bundling log can say what was actually saved.
_RNS_HEADER_B = 19

# Traffic deliberately held back to spread a correlated burst (see
# _ONSET_SLOT_S). Its own lane rather than a stamp on _tx_queue entries,
# because the "not due yet, put it back" step reorders whatever it touches --
# harmless for position reports, which supersede each other, and not something
# to inflict on the reliable path's fragment ordering.
_delayed_queue = queue.Queue(maxsize=32)

# Acknowledged delivery for one-shot traffic (chat, drawings, routes). See
# reliable.py for why this is app-layer ARQ on the broadcast destination
# rather than an RNS Link per peer.
def _link_latency_s():
    """The LoRa-slot radio's fixed per-leg delay on top of airtime: measured
    for a Meshtastic board (mt_link.LINK_LATENCY_S), 0 for an RNode. Read live
    by the retry timer, so a change of firmware setting takes effect at once."""
    if _radio_firmware == RADIO_MESHTASTIC:
        return mt_link.LINK_LATENCY_S
    return 0.0


_reliable_tx = reliable.ReliableTx(airtime_s=lambda n: _airtime_s(n),
                                   hops_away=lambda n: _hops_away(n),
                                   link_latency_s=lambda: _link_latency_s())
_reliable_rx = reliable.ReliableRx()

# The FM link's replacement for the ARQ engine above, used INSTEAD of it rather
# than alongside: acknowledgement traffic is banned on that link (see
# fm_link.py's and fm_resend.py's docstrings), so one-shot traffic there is
# protected by blind, widely-spaced resends instead. Built unconditionally,
# because _transport is not known until set_transport() runs, and idle at zero
# cost on every other link -- nothing registers with it unless FM is built.
#
# SPACED IN MINUTES, NOT SECONDS, and that is the measurement the whole shape
# rests on: this link's dominant failure is a bad patch lasting MINUTES, so two
# attempts seconds apart are two samples of the same bad patch. Redundancy that
# works here is time diversity. See fm_resend.py.
_fm_resend = fm_resend.FmResend()

# A position/heartbeat update gets NO retry at all on LoRa -- tx_policy treats
# a lost one as fine because a fresher one supersedes it 90 s later anyway (see
# _handle_outbound_cot's wants_ack comment). That reasoning holds at LoRa's loss
# rates and stops holding at the per-burst loss measured on FM, where several
# 90 s cycles can pass with nothing landing and a map staying blank for minutes.
# Fixed with ONE quick extra try, not the full one-shot schedule above: a
# position does not need to outlast a multi-minute cluster the way a chat
# message does (it gets a fresh independent sample every 90 s for that), it just
# needs a second roll of the dice against a single missed burst.
#
# attempts=2 means register() schedules exactly one resend via _first_delay()
# and nothing more -- see fm_resend.FmResend.register()'s attempts<=1
# short-circuit for why interval_s is irrelevant here and left at the default.
#
# A SEPARATE INSTANCE from _fm_resend, not a shared one, because the two carry
# structurally different traffic (position: idempotent, superseded, no msg_id on
# the wire; one-shot: exact-bytes-must-match, msg_id-keyed de-dup) and mixing
# their schedules would make _fm_resend.attempts and fm_resend.span_s() lie
# about what a one-shot message actually gets -- which matters, because
# span_s() is what sizes the receive-side reassembly and duplicate windows.
_FM_POSITION_RESEND_FIRST_S = 15.0
_FM_POSITION_RESEND_JITTER_S = 5.0
_fm_position_resend = fm_resend.FmResend(
    attempts=2, first_retry_s=_FM_POSITION_RESEND_FIRST_S,
    first_retry_jitter_s=_FM_POSITION_RESEND_JITTER_S)

# A KEYFRAME losing its one extra try is a much worse outcome than an ordinary
# delta losing its: per cot_codec.py's SaTx docstring, every delta a receiver
# gets afterwards references THIS frame's base, so losing it does not cost one
# stale position, it costs every position from this node for up to
# keyframe_every (5) cycles. That is exactly the "peer never appears on the map"
# symptom found 2026-09-22 with real ATAK traffic: two hardware restarts
# mid-session each forced a keyframe (_note_peer_restarted's force_keyframe()),
# and neither survived loss an ordinary delta shrugs off.
#
# A SEPARATE instance again rather than more attempts on the position one, so an
# ordinary delta's airtime cost is unchanged and only the rarer, higher-value
# keyframe pays. See _handle_outbound_cot for how a send is classified as one or
# the other BEFORE it is built.
_FM_KEYFRAME_RESEND_FIRST_S = 15.0
_FM_KEYFRAME_RESEND_JITTER_S = 5.0
_fm_keyframe_resend = fm_resend.FmResend(
    attempts=4, first_retry_s=_FM_KEYFRAME_RESEND_FIRST_S,
    first_retry_jitter_s=_FM_KEYFRAME_RESEND_JITTER_S)

# The most recent DELTA (never a keyframe -- see encode_beacon's own `position`
# comment) this node built for its own position, held here so _beacon_loop can
# piggyback it onto the very next beacon and give it a second, independent
# chance at delivery. A beacon is a fraction of a position frame's size, and
# smaller frames have consistently measured better survival on this link.
#
# READ-AND-CLEARED rather than read-only, so a delta rides exactly one beacon
# and never a stale one repeated indefinitely: a beacon some time later ought to
# carry whatever is current then, not this.
_fm_last_position_delta_lock = threading.Lock()
_fm_last_position_delta = None

# What this node has received whole and will report on its next beacon, so the
# sender can stop resending blind (fm_resend.note_heard). Bounded by what fits:
# at H_256_768_22's 29 B capacity the beacon has room for exactly _FM_HEARD_MAX
# tags and no more, so this is a ring of the most recent rather than a growing
# list. Re-derived against that code in Phase 2a rather than inherited -- three
# tags fit with ZERO bytes to spare, and only by giving up the 8 B neighbour
# digest (see _beacon_tick). test_fm_resend asserts the arithmetic.
#
# MOST-RECENT-FIRST, because a sender's schedule runs for minutes while the
# beacon repeats every ~90 s: a report that misses one beacon still has later
# ones to ride, and the messages most likely to still be in flight are the
# newest.
_FM_HEARD_MAX = 3
_fm_heard = collections.deque(maxlen=_FM_HEARD_MAX)
_fm_heard_lock = threading.Lock()
# register() runs on the CoT listener thread, on_ack() on the RNode read
# thread, tick() on the retry thread.
_reliable_lock = threading.Lock()
_RELIABLE_TICK_S = 1.0

# Retries pause while the TX queue is at least this deep -- half of its
# capacity. See _reliable_loop.
_TX_BACKPRESSURE_DEPTH = 16

# Receivers wait a random moment before acknowledging, so that N radios do not
# key up in the same instant on hearing the same broadcast.
#
# The window is scaled by how many peers could possibly be answering, because a
# FIXED window was measured doing real harm. On the 2026-07-31 bench a flat 1 s
# of jitter -- longer than the 0.78 s the message itself takes on air -- delayed
# an ack past the sender's retry timer twice in one exchange, costing a full
# retransmission each time. The collision it was guarding against cannot even
# occur with a single peer.
#
# One slot is roughly an ack's airtime plus turnaround, so peers land in
# adjacent slots rather than on top of each other.
_ACK_SLOT_S = 0.15
_ACK_JITTER_MAX_S = 1.0

# The same trick, for the same reason, on movement onsets -- the one class of
# CoT that a whole squad emits at the same instant. Everything else on a
# CSMA-backed link is either already spread (heartbeats are phase-locked to
# when each node was first seen, not to a shared clock) or genuinely rare
# (chat). A patrol stepping off together is one real-world event that makes
# 16 radios key up at once.
#
# Sized larger than the ack window because the frames are larger and there is
# no retry timer to race: a delayed onset costs latency on one marker, whereas
# a delayed ack costs a whole retransmission. Scaled by peer count for the
# reason recorded above -- a fixed window is pure harm on a two-node link.
#
# NOT a complete fix, and shouldn't be mistaken for one: 16 nodes x ~0.2 s is
# 3.2 s of airtime, so a window that fully de-conflicts a simultaneous onset
# would have to be tens of seconds, which is longer than the report is useful
# for. This turns a certain pile-up into roughly a third of the channel busy
# during the burst; the RNode firmware's own CSMA backoff handles the rest, and
# a collided onset is recovered by the next scheduled send anyway.
#
# ALSO applied to one-shot FM traffic (chat/markers/drawings/waypoints), and
# ONLY on FM -- see _stagger_window()'s call site. "Genuinely rare chat" stops
# being a reason not to stagger it once "the firmware's own CSMA backoff handles
# the rest" is no longer true: FM has no CSMA, no firmware and no fallback of
# any kind on one shared half-duplex channel. A one-shot event fired the instant
# tx_policy accepts it, with zero jitter, was found colliding with another
# node's simultaneous send on real hardware 2026-09-20 (both sides transmitting
# roughly a second apart); it survived only because fm_resend retries blind.
# LoRa's chat traffic is left unstaggered, since CSMA and rarity both still hold
# there and this would add latency for no benefit.
_ONSET_SLOT_S = 0.25
_ONSET_JITTER_MAX_S = 5.0

# A floor under FM's one-shot/heartbeat staggering ONLY -- not onset, which has
# its own reasoning (its 0.25 s slot is tuned against LoRa's airtime budget too,
# and FM-only traffic has no reason to inherit that). 1 s, after two real phones
# restarted at the same moment (2026-09-22) collided on every position send
# under the un-floored peer-scaled window: that window sits at _ONSET_SLOT_S
# whenever peer_count is 0, which is exactly the startup case -- and exactly the
# one time a fixed window beats a scaled one, because nobody is heard yet
# specifically BECAUSE both nodes just started, not because the channel is
# empty. Listen-before-transmit cannot save a truly simultaneous key-up either:
# both sides check "clear" before either has started.
_FM_STAGGER_MIN_S = 1.0

# RNS applies no transmit pacing on this interface: RNodeInterface only clears
# interface_ready when flow_control is set, and it defaults to False (and
# can't be set via the config dict, since RNS calls c.as_bool() on it and we
# pass a plain dict). So process_outgoing writes every packet to the USB pipe
# as fast as Python can loop.
#
# The original worry was that a fragment burst would overrun the RNode's
# firmware TX buffer. MEASURED ON HARDWARE (Heltec LoRa32 v3, firmware 1.86,
# SX1262): it does not. 8 full-MTU frames submitted in 0.26s, demanding 9.9s
# of airtime, all arrived byte-exact and in order. The firmware buffers and
# serializes correctly, so pacing is NOT required for correctness.
#
# It is kept for a different and better reason: it decides *where* a backlog
# accumulates. Without it, _rns_tx_loop shovels the whole queue into an opaque
# firmware buffer as fast as it can, and any overflow is dropped by the
# firmware on its terms. With it, a backlog stays in _tx_queue, where the
# drop-oldest policy in _handle_outbound_cot applies -- and dropping the
# stalest position report is a much better loss than an arbitrary one.
# The cost is small: the sleep overlaps the radio's actual transmission, so it
# adds roughly 20% idle time between fragments, not 120%.
_PACING_FACTOR = 1.2
_FALLBACK_BITRATE = 3125  # what RNS computes for SF8 / 125 kHz / CR5

# One link per radio, because the radios do not charge alike. The LoRa model
# is deliberately identical to the open-coded arithmetic it replaced -- same
# header, same live bitrate, no key-up -- so the retry schedule and the TX
# pacing do not shift by a microsecond. What the abstraction buys is somewhere
# for the FM link, whose 55 ms key-up is charged per frame rather than per byte,
# to be described alongside it.
_lora_link = airtime.lora(lambda: getattr(_lora_iface, "bitrate", 0),
                          fallback_bps=_FALLBACK_BITRATE)
# FM's figure is DERIVED from the operating point rather than guessed: see
# fm_link.bitrate_bps(), which solves airtime.Link's own arithmetic so that a
# full-capacity frame charges the real measured burst. The fallback is the same
# number, because unlike LoRa's this is not something a live interface can
# legitimately report as 0 -- airtime.Link just requires one whenever
# bitrate_bps is a callable. It is read at import, so it describes the DEFAULT
# placement; the live figure comes off the interface, which is built after
# set_fm_profile() has run.
#
# NOTE THE TWO SIMILAR NAMES, which are different things: `_fm_link` is this
# airtime model, and `fm_link` is the transport module imported at the top of
# this file. Nothing shadows anything -- but they sit next to each other in a
# few lines below, so read the underscore.
_fm_link = airtime.fm(lambda: getattr(_fm_iface, "bitrate", 0),
                      fallback_bps=fm_link.bitrate_bps())
# A fixed guess, not a live reading: AutoInterface has no per-packet
# bitrate figure the way RNode's live .bitrate is, and airtime.wifi()'s own
# default (AutoInterface.BITRATE_GUESS) already exists for exactly this.
_wifi_link = airtime.wifi()

_STATUS_INTERVAL_S = 30
# For the duty-cycle delta in _status_tick: link -> (interface object id,
# txb, monotonic time), refreshed for EVERY built link on every tick. It was one
# number shared by all links, so a failover compared FM's byte counter with
# LoRa's (duty=-427.5% on 2026-10-23) and a failback charged LoRa's whole
# history to one interval.
_last_txb = {}
_last_duty_pct = 0.0  # most recent measurement, surfaced to the UI

# The figure already named in _status_tick's own duty-cycle comment: above
# this, the channel starts queueing behind itself. Edge-triggered (see
# _duty_over_threshold) so a sustained busy channel logs one WARNING at the
# crossing and one NOTICE when it clears, not a line every 30s tick.
_DUTY_WARN_PCT = 30.0
_duty_over_threshold = False

# Link probe. Small, frequent, and independent of ATAK -- without it the only
# way to know whether the radio link works is to wait for ATAK to emit a CoT
# event and hope, which conflates "no link" with "no traffic".
#
# MILESTONE 5: was 15 s, which was sized on a two-node bench where it costs
# ~1.6% of the channel. It does not scale -- the interval is per node, so 16
# nodes at 15 s is ~13% duty on beacons alone, comparable to all the position
# traffic they exist to protect. At 60 s that falls to ~3%.
#
# URBAN RANGE TEST took this back to 15 s, as measurement resolution rather
# than link tuning: the static node indoors has no GPS fix, so its beacon is
# the ONLY thing it transmits, which makes the interval the sample rate for the
# whole inbound direction, and at 60 s a walking pace samples about every 80 m.
# That override was meant to revert with the range-test radio config and did
# not. REVERTED 2026-08-16, back to the milestone 5 value.
#
# The 2026-08-16 stress bench is why it matters here rather than only at 16
# nodes: beacons are the one traffic this mesh emits that nothing throttles,
# so they are pure subtraction from the budget the ramp showed is far smaller
# than assumed. Range testing loses four times its sample resolution and wants
# this put back to 15 s FOR THAT RUN ONLY -- it is not the shipping value.
# How often a node that has nothing else to say proves it is still there.
#
# Was 60 s, which is where the whole at-rest airtime bill came from: sixteen
# nodes standing still spent 4.7% of the channel doing nothing but resetting
# each other's timers. It was 60 because ATAK drops a marker about two minutes
# after the last position it heard -- so the cadence was set by a countdown in
# somebody else's software rather than by anything about the radio.
#
# That countdown is now the receiver's to set (see _MARKER_STALE_S and
# cot_codec._stale_at), which frees this to be chosen on its own merits: how
# long is it acceptable not to know a node has died? At 90 s, tolerating the
# two missed check-ins _PEER_CONNECTED_S allows, a dead node is noticed in
# 3-4.5 minutes, and the at-rest cost at 16 nodes falls from 6.8% to ~3.1%.
#
# 90 AND NOT THE 150 THIS FIRST SHIPPED WITH (changed 2026-08-17). The stress
# bench put a number on what ATAK's own timer was actually worth: under load
# ~5% of frames between the two phones were lost to collisions, and ATAK gives
# up after roughly ONE missed check-in -- so it would blink a healthy node off
# the map about one time in twenty. 150 s bought 1.9% but cost 5-7.5 minutes to
# notice a death. 90 s keeps most of the saving and lands inside the old
# system's HONEST detection time rather than its optimistic one.
#
# THIS IS STILL THE KNOB THE RANGE TEST SHOULD RETUNE. The tolerance of two
# missed check-ins was set when nothing had ever been lost on the air here.
# That is no longer true -- the 2026-08-17 bench lost ~5% under load and none
# at idle -- but loss at DISTANCE is still unmeasured, which is what actually
# decides whether two missed check-ins is generous or thin.
# TEMPORARILY 60, NOT 90, FOR THE 2026-10-03 TWO-PHONE DIGEST INVESTIGATION.
# Asked for to get heard-digest samples faster: at 90 s each node emits ~40
# beacons an hour, and the digest had not cancelled anything across a 30-minute
# window, so the question needed more cycles per hour to chase.
#
# REVERT TO 90 WHEN THAT INVESTIGATION ENDS. The comment below this one records
# what an edited constant cost last time; this is the same edit, made knowingly.
# What it buys and what it costs, computed before the change:
#
#   interval   slots/cycle   collision chance   2-node duty   collisions/hr
#      90 s         22             4.5%            13.1%           1.8
#      60 s         15             6.7%            16.4%           4.0
#
# Measured channel occupancy on 2026-10-03 was ALREADY 17.1% at 90 s once chat
# resends were flowing -- 91 transmissions in 30 minutes, of which only ~26
# were beacons -- with 14 overlapping transmissions where both nodes keyed at
# once and both went deaf. 60 s makes that worse, so a delivery figure measured
# at this cadence is NOT comparable with one measured at 90.
_BEACON_INTERVAL_S = 60

# A range test wants a beacon every 30 s, not every 90: the beacon sequence IS
# the range test's sample, so at 90 s a twenty-minute walk yields thirteen
# points and no curve. (30 s, not the original 15 s: on a real two-phone FM
# bench 2026-09-27, both nodes sampling every 15 s -- close to FM's own
# ~10-11.5 s burst duration at the shipped CODENAME/REPEATS -- left most
# frames lost and most rows without a usable position; not root-caused to
# on-air collision specifically, but 30 s gives listen-before-transmit a lot
# more idle channel to work with per cycle and is still 3x denser than the
# normal 90 s cadence.)
#
# It is an OVERRIDE WITH A LIFETIME rather than an edited constant, because the
# edited constant is what went wrong last time: the 15 s range-test value was
# left in place through two milestones, carrying a comment claiming it would
# revert, and the 2026-08-16 stress bench measured its cost before anyone
# noticed (see "The beacon was still on its range-test override" in
# benches.md). Tying it to start/stop_range_test() means it cannot outlive the
# run that wanted it.
_RANGE_TEST_BEACON_S = 30
_beacon_interval_override = None


def _beacon_interval():
    """The live beacon cadence: the range-test override, or the normal value."""
    return _beacon_interval_override or _BEACON_INTERVAL_S

# Beacons are the one thing on this link sent on a fixed timer by every node at
# once, which makes them the one thing that can collide FOREVER: two nodes
# whose beacons drift into alignment have nothing to push them apart again, so
# they collide on every interval indefinitely. Re-randomising each period is
# what breaks that. Applied around a fixed schedule rather than added to the
# sleep, so the offsets cancel instead of accumulating into drift.
#
# This alone does not prevent two INDEPENDENT nodes' offsets from drifting
# close together by chance on any given cycle -- a real 2026-09-11 finding on
# real FM hardware: two nodes' beacon-send timestamps, each independently
# jittered, occasionally landed within 0.3s of each other purely by the
# random walk of the difference between two uniform draws, which was enough
# to wreck reception in both directions on a half-duplex link. A deterministic
# per-node slot (below) fixes each node's offset instead of re-rolling it, so
# two DIFFERENT nodes essentially never collide, at the cost of a small,
# fixed chance any given PAIR of nodes hashes to the same slot and would
# collide every cycle with no jitter at all -- _BEACON_SLOT_JITTER_S is kept
# small specifically to break that one remaining case without reopening the
# wide drift the slot was meant to remove.
#
# A SLOT ONLY SEPARATES NODES IF EVERY NODE MEASURES IT FROM THE SAME CLOCK.
# Found 2026-09-30 on the real two-phone bench, and it had silently defeated
# the whole mechanism above: the schedule was anchored to time.monotonic(),
# whose epoch is per-process (time since boot), so adding a fixed per-node
# offset to it shifted that one node's sends uniformly and did nothing
# whatsoever to separate two independent nodes. Whatever phase two phones
# happened to boot into persisted indefinitely -- precisely the collide-
# FOREVER case this mechanism exists to prevent. Measured on the FM link,
# where the burst was long enough to make it obvious: the two bench phones
# sat 3-4s apart on EVERY cycle for a whole session, their slots having been
# 10s apart, and delivery measured 1-2 beacons out of 6. _next_beacon_at()
# now places the slot on the WALL clock, which every node shares, so two
# different slots are genuinely different times on the air. This applies to
# every link and is not FM-specific.
#
# AND THE GRID HAS TO BE WIDER THAN ONE BURST, which is why this is a
# per-link function rather than one constant. 2.5s slots are right for LoRa,
# whose on-air time per beacon is a fraction of a second. A link with a long
# burst needs its grid sized from that burst instead: two nodes in ADJACENT
# slots narrower than one burst still transmit over each other for most of it,
# which is what the 2026-09-30 measurement found (the two bench phones 3-4s
# apart against an ~11s burst, delivering 1-2 beacons out of 6).
#
# A WIDER GRID COSTS SLOT COUNT, and so raises the chance two nodes hash to the
# same slot -- but it is the only honest trade available: no amount of
# scheduling fits more non-overlapping N-second bursts into a 90s cycle than
# 90/N of them.
_BEACON_SLOT_S_DEFAULT = 2.5
_BEACON_SLOT_JITTER_S = 0.5
_beacon_enabled = True


def _beacon_slot_s(link):
    """How far apart two adjacent beacon slots sit, for the link actually
    carrying the beacon -- wide enough that two nodes one slot apart do not
    transmit over each other.

    FM IS SIZED FROM ITS REAL BURST, not from the duration CEILING, and that is
    a deliberate change from the pre-teardown code: that used a flat 15 s, sized
    above fm_config.MAX_BURST_S (11.5 s), which is the longest burst any
    configuration is ALLOWED rather than the length of the one actually on the
    air. At the Phase 0 operating point the real burst is 2.35 s, so a grid
    sized from the ceiling would be 6x wider than it needs to be and would throw
    away two thirds of the available slots -- which is the collision probability
    this whole mechanism exists to lower. Asking the live configuration also
    means a profile change cannot leave the grid silently wrong.

    Both nodes' jitter is included, because the slot has to clear a burst that
    starts up to _BEACON_SLOT_JITTER_S late at one end and up to that early at
    the other. Never narrower than LoRa's default, which no FM burst beats.
    """
    if link == LINK_FM:
        return max(_BEACON_SLOT_S_DEFAULT,
                   fm_link.burst_duration_s() + 2 * _BEACON_SLOT_JITTER_S)
    return _BEACON_SLOT_S_DEFAULT


def _beacon_slot_count(interval, slot_s):
    """How many whole slots fit in one beacon interval.

    At least 1, deliberately: a cadence shorter than a single burst -- a
    range test on FM, say, where 30s hosts two 11s bursts and no more -- has
    no separation to offer anyone, and collapsing to one slot says so plainly
    rather than handing out offsets that overlap by construction.
    """
    return max(1, int(interval // slot_s))


def _beacon_slot_offset(cycle, link=None, interval=None):
    """Where in cycle number `cycle` this node transmits, in seconds past the
    start of that cycle.

    Keyed on the node id -- the same idea as hashing a callsign to a
    TDMA-style slot, using the identity this codebase already has, so it
    needs no coordination handshake and is stable across restarts
    (_NODE_ID is persisted, see _load_node_id).

    RE-DRAWN EACH CYCLE, which a fixed slot was not. Found while fixing the
    2026-09-30 collision bug: once the grid is coarse enough for FM's burst
    only six slots fit a 90s cycle, so a fixed per-node slot gives any pair
    of nodes a 1-in-6 chance of sharing one -- and a shared fixed slot is a
    PERMANENT mutual blackout, the exact collide-forever failure this whole
    mechanism exists to prevent. The two bench phones hit it immediately:
    aaaa02 and aaaa01 both hash to slot 3 of 6. Folding the cycle number
    into the hash keeps every useful property of a fixed slot (deterministic,
    no handshake, shared grid, whole-slot separation when they differ) while
    making a collision a per-cycle coin toss that cannot persist: two nodes
    share a slot on about 1 cycle in `count` and are a full slot width apart
    on all the rest.

    This is NOT a return to the pre-2026-09-11 random jitter, whose problem
    was that two continuously-jittered nodes could land 0.3s apart -- close
    enough to wreck both transmissions while looking like bad luck. Slots
    here are quantised: two nodes either hold the same slot, or are at least
    one full slot (> one burst) apart. There is no near-miss case.

    Depends on the link, because the slot WIDTH does (see _beacon_slot_s):
    a node legitimately re-slots when a failover changes which radio its
    beacons go out on.
    """
    if link is None:
        link = _active_link
    if interval is None:
        interval = _beacon_interval()
    slot_s = _beacon_slot_s(link)
    digest = hashlib.sha256(
        ("%s:%d" % (_NODE_ID, cycle)).encode("ascii")).digest()
    slot = int.from_bytes(digest[:4], "big") % _beacon_slot_count(
        interval, slot_s)
    return slot * slot_s


def _next_beacon_at(now_wall, interval, offset_for_cycle):
    """The next wall-clock time this node transmits.

    Wall clock, NOT time.monotonic(): the entire point of a per-node slot is
    that two nodes land at different points of the SAME cycle, which requires
    a reference both of them share. monotonic() does not have one -- see the
    2026-09-30 finding above, where using it made the slot a no-op across
    nodes. Phones hold their wall clocks within about a second of each other
    over the network, comfortably inside these slot widths; if a clock is
    badly wrong, separation degrades toward the old behaviour rather than to
    anything worse.

    Cycle numbering comes from the same shared clock, so every node agrees
    which cycle it is and therefore draws its slot for the same cycle.

    `offset_for_cycle(cycle)` supplies this node's offset within that cycle;
    it is a callable because the offset is re-drawn per cycle and the cycle
    is what this function is working out.

    Derived from `now_wall` each time rather than advanced one interval at a
    time, so a process that was suspended (doze, the screen-off governor)
    rejoins the grid at the next real slot instead of either firing a
    catch-up burst of stale beacons or holding its old phase.
    """
    cycle = math.floor(now_wall / interval)
    # At most three: this cycle's slot may already have passed, and the next
    # cycle's may be drawn at offset 0.0 exactly on the boundary.
    for c in (cycle, cycle + 1, cycle + 2):
        due = c * interval + offset_for_cycle(c)
        if due > now_wall:
            return due
    raise AssertionError("no beacon slot found within two cycles")

# Beacon frame sizes, cycled one per beacon.
#
# The full ladder below was how the size ceiling question got answered: a link
# proven at 31 bytes is NOT proven at 464, and the run before it delivered
# 31-byte beacons perfectly while 264-byte CoT packets never arrived at all.
# Sweeping the size turned that from a hypothesis into a table readable
# straight off logcat, and on 2026-07-31 both phones reported the complete
# ladder with no gaps.
#
# Now that it has, the ladder is too expensive to leave running: a 483-byte
# packet is ~1.24 s of airtime at SF8, so a full cycle costs ~3.6 s per 90 s
# per device -- roughly 8% of the channel across two phones, competing with
# the CoT it exists to protect. A single small beacon is ~0.5%.
#
# Restore the full ladder when a link is in doubt (new hardware, longer range,
# different radio parameters). Note the top rung is the INNER budget: the mesh
# envelope adds mesh.OVERHEAD on the way out, so padding to the full MDU here
# would produce a packet RNS refuses to pack.
#   _BEACON_SIZES = [0, 64, 128, 256, 384, cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD]
_BEACON_SIZES = [0]
_NODE_ID_PATH = os.path.expanduser("~/.atakrns_node_id")

# A beacon's name+callsign have to fit the TIGHTEST link's per-burst capacity,
# because a beacon that overflows it does not degrade -- it raises, and on FM
# that took the whole interface offline on its first live send (the 2026-09-11
# session: a 37 B beacon against a 30 B code). Neither a device name nor an
# ATAK callsign is bounded by anything upstream, so they are capped here.
#
# CAPPED UNIVERSALLY rather than per link, deliberately: a beacon exists to
# probe the link, not to carry a full device name, and node_id already gives an
# exact identity. One value also means a node's beacon is the same shape on
# every link it sends over, which is what lets _send_one_beacon be the single
# place a beacon is assembled.
#
# RAISED FROM 2+0 on 2026-10-02, and the history is worth keeping because the
# low value was never a judgement about beacons -- it was arithmetic forced by
# one link. FM ran a 30 B code whose beacon budget was 17 B after the mesh
# envelope; encode_beacon's floor (ctrl + seq + 3 empty length-prefixed fields
# + the always-present 8 B neighbour digest) is 16 B, which left exactly 2 B
# for name and callsign COMBINED. It went to `name` rather than `callsign`
# because name is the node's primary identity and available whether or not ATAK
# is running, while callsign is secondary and "never something to depend on"
# (see encode_beacon's docstring). Two bytes of a device name beats zero.
#
# The position path then forced FM onto the 128 B code (see
# fm_config.DEFAULT_CODE), taking the beacon budget from 17 B to 113 B. At
# these caps a beacon carrying the digest AND three heard tags is 51 B, so
# there is better than 2x headroom on the tightest link in the system -- and
# LoRa, which has ~452 B and was paying this cost for no reason at all, gets a
# readable node name back. Pinned by a test against cot_codec and fm_config
# rather than recomputed here, so a code that shrinks again fails loudly
# instead of overflowing on the air.
_BEACON_NAME_MAX_B = 16
_BEACON_CALLSIGN_MAX_B = 12

# NO LONGER THE BINDING CONSTRAINT, 2026-09-20: fm_interface.py's CODENAME
# moved to H_1024_2048_4f (a throughput sweep on the two-Digirig desktop
# bench, see that file's own CODENAME comment), whose hw_mtu() is 125 B --
# a 108 B beacon budget after mesh.OVERHEAD, nowhere near this cap's 2+0.
# These caps are left as-is rather than relaxed: a beacon exists to probe
# the link cheaply, not to carry a full device name, and the tight-fit
# arithmetic below (from when hw_mtu() really was 17-18 B) is kept as
# history, not a live constraint any more. Recheck this comment, not just
# the numbers in it, if CODENAME ever drops back to a 30 B-class code --
# the two are coupled and this file does not enforce that coupling.


def _beacon_safe_str(s, max_bytes):
    """`s` truncated so its UTF-8 encoding fits max_bytes.

    Slicing by character count is not enough on its own: a name or callsign
    with any non-ASCII character could still overflow the byte budget above
    even after being cut to "the right number of characters".
    """
    s = s or ""
    while len(s.encode("utf-8")) > max_bytes:
        s = s[:-1]
    return s


def set_node_name(name):
    """Called from Kotlin before start(), with the device's make and model.

    Kept as an explicit call rather than read from Python because the device
    identity lives on the Android side, and this module is deliberately
    importable and testable without any Android at all.
    """
    global _node_name
    _node_name = (name or "").strip()[:48]
    return _node_name


def set_session_info(build_stamp):
    """Called from Kotlin before start(), same convention as set_node_name --
    only Android knows its own APK build stamp. Purely for the session-start
    log line (see start()): lets an exported field-test log say which build
    produced it without asking separately."""
    global _build_stamp
    _build_stamp = (build_stamp or "").strip()[:32]
    return _build_stamp


def _load_node_id():
    """Identifies this device in beacons.

    Persisted rather than generated per process. A random-per-run id made the
    node list useless: every app restart looked like a brand new node while
    the old one lingered as a ghost -- which is exactly what was observed on
    the bench after a reinstall. Random rather than derived from a device
    serial: it needs to be stable and distinguishable, not identifying.
    """
    try:
        with open(_NODE_ID_PATH) as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    node_id = os.urandom(3).hex()
    try:
        with open(_NODE_ID_PATH, "w") as f:
            f.write(node_id)
    except OSError:
        pass  # non-fatal: falls back to per-process, same as before
    return node_id


_NODE_ID = _load_node_id()


# ---------------------------------------------------------------------------
# Simulated deafness -- a BENCH FIXTURE, not a feature.
#
# Relaying only ever engages when a node can reach somebody the sender cannot,
# so three radios on one desk relay nothing by design and prove nothing. The
# only way to test multi-hop indoors is to make two of them ignore each other,
# which is what this does. The file is normally absent and this whole path
# costs one cached stat per two seconds.
#
# One node id per line, ignoring blanks and `#` comments:
#
#     adb shell run-as io.github.stress5094.customrnsbridge \
#         sh -c 'echo a1b2c3 > files/../.atakrns_deaf_to'
#
# See "Testing multi-hop on one desk" in the README.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CoT dump -- a BENCH FIXTURE, not a feature.
#
# While the file below exists, every CoT document ATAK hands the bridge and
# every one the bridge hands ATAK is appended to it, verbatim, with the time and
# the direction. Added 2026-10-23 to diff a marker as one phone's ATAK made it
# against what the other phone's bridge rebuilt for its ATAK (OnePlus markers
# arrived and were acknowledged but did not appear on the Pixel's map). Absent,
# it costs one stat per CoT document. Capped, so a forgotten file cannot fill
# the phone.
#
#     adb shell run-as io.github.stress5094.customrnsbridge touch files/.atakrns_cot_dump
#     adb shell run-as io.github.stress5094.customrnsbridge cat files/.atakrns_cot_dump
#     adb shell run-as io.github.stress5094.customrnsbridge rm files/.atakrns_cot_dump
# ---------------------------------------------------------------------------

_COT_DUMP_PATH = os.path.expanduser("~/.atakrns_cot_dump")
_COT_DUMP_MAX_B = 2_000_000
_cot_dump_lock = threading.Lock()


def _cot_dump(direction, xml):
    """Append one CoT document to the dump file, if the file exists."""
    try:
        if not os.path.exists(_COT_DUMP_PATH):
            return
        with _cot_dump_lock:
            if os.path.getsize(_COT_DUMP_PATH) > _COT_DUMP_MAX_B:
                return
            with open(_COT_DUMP_PATH, "ab") as f:
                f.write(("\n=== %s %s %d bytes\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), direction, len(xml))).encode())
                f.write(xml if isinstance(xml, bytes) else str(xml).encode())
                f.write(b"\n")
    except Exception:
        pass  # a bench aid must never break delivery


_DEAF_TO_PATH = os.path.expanduser("~/.atakrns_deaf_to")
_DEAF_RECHECK_S = 2.0
# (checked_at, mtime, ids). Rechecked on a timer rather than read per frame so
# that the operator can change the topology without restarting the app, which
# on a bench is the difference between a five-second edit and a re-pair.
_deaf_cache = (0.0, None, frozenset())
_deaf_lock = threading.Lock()


def _deaf_to():
    """Node ids this build is pretending it cannot hear directly."""
    global _deaf_cache
    now = time.monotonic()
    with _deaf_lock:
        checked_at, mtime, ids = _deaf_cache
        if now - checked_at < _DEAF_RECHECK_S:
            return ids
        try:
            current = os.path.getmtime(_DEAF_TO_PATH)
        except OSError:
            current = None
        if current == mtime:
            _deaf_cache = (now, mtime, ids)
            return ids
        fresh = frozenset()
        if current is not None:
            try:
                with open(_DEAF_TO_PATH) as f:
                    fresh = frozenset(
                        entry for entry in
                        (line.split("#", 1)[0].strip().lower() for line in f)
                        if entry)
            except OSError:
                fresh = ids  # unreadable: keep the topology we already had
        _deaf_cache = (now, current, fresh)
    if fresh != ids:
        RNS.log("[cot-bridge] BENCH: pretending not to hear %s directly"
                % (", ".join(sorted(fresh)) or "anybody"), RNS.LOG_NOTICE)
    return fresh


def _out_of_range(active, inbound):
    """Drop this frame as if the radio had never pulled it out of the noise?

    Only ever true on a bench with _DEAF_TO_PATH in place. The distinction that
    makes the fixture useful is that it drops what was heard DIRECTLY and keeps
    what arrived via a relay -- dropping both would simulate the node being
    switched off, which no amount of relaying can fix.
    """
    deaf = _deaf_to()
    if not deaf:
        return False
    origin = mesh.node_id_hex(inbound.origin)
    if origin not in deaf or not _heard_directly(inbound):
        return False
    # Unrecorded as well as undelivered. unwrap() has already put this frame in
    # the replay cache, and leaving it there would make the relayed copy -- the
    # entire point of the exercise -- arrive looking like a replay.
    active.seen.forget((inbound.origin, inbound.epoch, inbound.seq))
    RNS.log("[cot-bridge] BENCH: ignoring frame %d heard directly from %s"
            % (inbound.seq, origin), RNS.LOG_NOTICE)
    return True


def set_mesh_passphrase(passphrase):
    """Set or replace the group passphrase. Called from the Kotlin UI.

    Works before OR after start(). Before, it is simply what start() will use;
    after, the mesh is rebuilt on the spot, because making the operator restart
    the app to fix a typo is how a five-second correction becomes a support
    call. Persisted either way, so it survives a restart.

    Returns the new key fingerprint, or "" if the passphrase was rejected. The
    caller shows that fingerprint next to the one on the other device: matching
    fingerprints are the only quick way to tell a shared key from a typo, since
    both nodes look equally dead when the keys differ.
    """
    global _mesh_passphrase, _mesh
    passphrase = (passphrase or "").strip()
    if not passphrase:
        return ""
    with _mesh_lock:
        _mesh_passphrase = passphrase
        _save_passphrase(passphrase)
        fingerprint = mesh.key_fingerprint(mesh.derive_key(passphrase))
        if _mesh is None:
            # Not started yet. start() builds the mesh under this same lock and
            # reads _mesh_passphrase first, so it cannot miss what was just set
            # -- without that, a passphrase entered during the second or so
            # start() takes would be written to the file and then ignored until
            # the next launch.
            return fingerprint
        _mesh = _build_mesh(passphrase)

    # Peers heard under the old key are on a different network now, and leaving
    # them listed would show nodes this device can no longer talk to as
    # perfectly healthy for the next few minutes.
    _peers.clear()

    # Same reasoning one layer down. What a peer bound under the old key says
    # nothing about who holds the new one, and this node has to restate its own
    # identity because everyone that had it has just been cut off.
    with _sa_lock:
        _sa_rx.clear()
        _sa_tx.force_keyframe()

    # Anything queued for relaying was authenticated under the old key, and
    # forwarding it now would put traffic from the network this node just left
    # onto the one it just joined.
    with _relay_lock:
        _relay_pending.clear()
    RNS.log(f"[cot-bridge] mesh passphrase changed, key {fingerprint}; peer list cleared",
            RNS.LOG_NOTICE)
    return fingerprint


def get_mesh_passphrase():
    """What the UI prefills its field with.

    Deliberately readable rather than write-only: the operator has to get this
    same string onto every other radio, and a secret you cannot read is a
    secret you cannot share. It is already stored in plaintext in app-private
    storage, so hiding it here would buy nothing.
    """
    try:
        passphrase, _ = _load_passphrase()
        return passphrase
    except Exception:
        return ""


def generate_mesh_passphrase():
    """A fresh random passphrase for the UI's Generate button: six everyday
    words (passphrase_gen). Only suggested -- nothing is set until the operator
    presses Set, so a team that already has one is not re-keyed by a stray tap.
    """
    import passphrase_gen
    return passphrase_gen.generate()


def _key_fingerprint():
    """Cached off the passphrase file rather than recomputed: derive_key() is
    deliberately slow (200k PBKDF2 rounds), and get_status() runs every two
    seconds from the UI poller."""
    global _fingerprint_cache
    try:
        passphrase, _ = _load_passphrase()
        cached = _fingerprint_cache
        if cached is not None and cached[0] == passphrase:
            return cached[1]
        fingerprint = mesh.key_fingerprint(mesh.derive_key(passphrase))
        _fingerprint_cache = (passphrase, fingerprint)
        return fingerprint
    except Exception:
        return ""


def _build_mesh(passphrase):
    """A Mesh on a freshly advanced counter generation.

    Always a NEW epoch, never a reused one: re-keying resets the sequence
    counter, and the generation is what keeps (epoch, seq) -- and therefore the
    nonce -- from repeating something this node has already sent.
    """
    epoch, persisted = mesh.load_epoch()
    if not persisted:
        RNS.log(
            "[cot-bridge] could not persist the mesh epoch; using a random one. "
            "Nonce uniqueness across restarts is no longer guaranteed",
            RNS.LOG_WARNING,
        )
    return mesh.Mesh(_NODE_ID, mesh.derive_key(passphrase), epoch=epoch,
                     on_epoch_change=mesh.save_epoch)


def _save_passphrase(passphrase):
    try:
        # 0600 from the moment it exists rather than chmod'ed afterwards, which
        # would leave a window where the group key is readable. Android already
        # keeps app storage private; this costs nothing and stops that from
        # being the only thing protecting it.
        fd = os.open(_MESH_PASSPHRASE_PATH,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(passphrase)
        return True
    except OSError:
        return False


def _load_passphrase():
    """The group passphrase, which is the only thing separating this network
    from anyone else with an RNode on the same frequency.

    Order: whatever Kotlin set, then a file, then a freshly generated one.

    THERE IS DELIBERATELY NO DEFAULT PASSPHRASE. Shipping one would mean every
    install in the world shared a key, which is the mistake that makes the
    default Meshtastic channel readable by anybody -- a network that looks
    encrypted and is not is worse than one that is plainly open, because only
    the second kind gets treated with appropriate suspicion.

    So a node with nothing configured generates its own and says so loudly.
    That node is secure but alone: it will hear nothing and nothing will hear
    it until the same passphrase is put on the other radios. That is the
    correct failure -- isolated and obvious, rather than joined and exposed.

    Returns (passphrase, source).
    """
    if _mesh_passphrase:
        return _mesh_passphrase, "app"
    try:
        with open(_MESH_PASSPHRASE_PATH) as f:
            existing = f.read().strip()
        if existing:
            return existing, "file"
    except OSError:
        pass

    generated = os.urandom(12).hex()
    if _save_passphrase(generated):
        return generated, "generated"
    # Cannot even persist it, so it changes on every launch and this node can
    # never form a network. Still better than falling back to a shared
    # constant; the caller logs it as the fault it is.
    return generated, "ephemeral"


_beacon_seq = 0
# Two threads now number beacons: the beacon loop, and the timer that sends the
# early "I got it" beacon (_fire_confirm_beacon). An unguarded `+= 1` from both
# can hand two beacons the same sequence number, which the far end reads as a
# duplicate (and, worse, as a gap when it skips one).
_beacon_seq_lock = threading.Lock()


def _next_beacon_seq():
    global _beacon_seq
    with _beacon_seq_lock:
        _beacon_seq += 1
        return _beacon_seq


_sizes_heard = set()

# Guards _atak_uid_to_node only. The peer table carries its own lock.
_uid_map_lock = threading.Lock()

# Session compression (milestone 6 step 2). One SaTx for what this node has
# told the mesh about itself, one SaRx per origin for what each peer has told
# us. See the block comment in cot_codec.
#
# The lock covers both. Transmission runs on the ATAK socket reader thread and
# reception on the RNode serial read thread, and a re-key from the UI thread
# clears both at once -- three threads, so the state that decides what a frame
# omits is not somewhere to be casual.
_sa_tx = cot_codec.SaTx()
_sa_rx = {}
_sa_lock = threading.Lock()

# When anything last went on the air, from any path. The beacon uses it to stay
# quiet while other traffic is already proving the link.
_last_tx_at = 0.0

# --- links -----------------------------------------------------------------
#
# One entry per radio -- LINK_LORA/LINK_FM themselves are defined near the
# top of the file (before _active_link, which needs them at import time).
# The point of the registry below is that adding FM is an entry here rather
# than a second copy of every decision below.

# Deliberately uncapped, and eligibility.LORA_MAX_FRAME_S explains why: the
# bridge sends up to 464 bytes sealed, which is 3.60 s at the shipped preset,
# so any of the caps that suit FM would refuse traffic that has worked since
# milestone 1. Capping LoRa is a decision of its own.
_gates = eligibility.Gates([
    eligibility.lora_gate(_lora_link, mtu=cot_codec.RNS_MAX_PAYLOAD),
])

# Relaying (milestone 6 step 3). The token bucket is denominated in airtime, so
# it reads the radio's live bitrate the same way _reliable_tx does. One bucket
# per link: airtime spent on one radio does not occupy the other's channel.
# WiFi (once it exists -- this placeholder only ever has LoRa, see the
# comment on _gates above) is exempted rather than metered: see
# relay.UnmeteredBudget's own docstring for why airtime scarcity is not
# WiFi's real constraint the way it is for a half-duplex radio.
_relay_budgets = relay.LinkBudgets(_gates.names(), unmetered=(LINK_WIFI,),
                                   burst_s=relay.DEFAULT_RELAY_BURST_S)
_relay_policies = {}
for _gate in _gates:
    _relay_policies[_gate.name] = (
        relay.RelayPolicy.for_link(_gate.link,
                                   budget=_relay_budgets[_gate.name])
        if _gate.relays else relay.NoRelay(_gate.name))

# Carrying a frame from the radio it arrived on to the other one.
#
# NOT yet called from the receive path, deliberately. Forwarding onto a NAMED
# link needs per-link transmit, and this bridge has one destination and one
# interface -- so calling it would produce decisions nothing could act on,
# which is worse than not calling it. Constructed and tested here so that the
# second radio is a registry entry plus a transmit path, rather than a new set
# of decisions to get right under time pressure.
#
# Its own budget, at a larger duty than relaying, because a forward across
# links reaches nodes that could not have heard the frame at all. See
# gateway.DEFAULT_GATEWAY_DUTY. WiFi exempted here too, for the same reason
# as _relay_budgets above.
_gateway_budgets = relay.LinkBudgets(_gates.names(),
                                     duty=gateway_mod.DEFAULT_GATEWAY_DUTY,
                                     unmetered=(LINK_WIFI,))
_gateway = gateway_mod.Gateway(_gates, _gateway_budgets)


def _relay_policy_for(link_name):
    """The relay policy for the radio a frame arrived on."""
    return _relay_policies.get(link_name)


# Kept so the status screen and the existing log lines have something to read
# while there is one radio. Both become per-link when there are two.
_relay_policy = _relay_policies[LINK_LORA]

# Frames scheduled to be relayed but not yet sent, keyed by flood identity so
# that hearing the same frame from anyone else can cancel ours. A plain dict
# rather than a queue for exactly that reason: a queue cannot revoke.
_relay_pending = collections.OrderedDict()
_relay_lock = threading.Lock()
_MAX_RELAY_PENDING = 32

# The neighbour digest this node last advertised. Kept so the beacon can fire
# when it CHANGES rather than only on a timer -- a node that just came into
# range has to be announced before anyone will relay for it.
_digest_sent = None

# ...and beaconed at least this often even while traffic is flowing, so the
# digest cannot go stale in a busy mesh. Step 2 quietened the beacon whenever
# anything else was on the air, which was right when the beacon carried only
# liveness; now it carries the input the coverage test runs on.
#
# THIS MUST STAY SHORTER THAN _PEER_CONNECTED_S, and it was not: 300 s against
# a 180 s window, so a node whose only transmission was this refresh dropped
# out of every neighbour's list for two minutes in every five, taking its
# entry out of their digests and churning the coverage test for the whole
# mesh. Set below the definition of _PEER_CONNECTED_S rather than beside it,
# and derived from it, so the two cannot drift apart again.
#
# The case is not exotic. Liveness is refreshed by ANY frame from a node, so
# an ordinary node is proven by its own traffic and this timer rarely fires.
# But a relay forwards frames that carry the ORIGINATOR's id, not its own --
# forwarding, however busy, proves nothing about the forwarder. A node that
# relays constantly while originating nothing is exactly the node this timer
# has to carry, and exactly the one the old value dropped. The 2026-08-12
# bench had one: a middle node with no GPS fix, silent apart from this.
_last_beacon_at = 0.0

# ATAK uid -> node_id, so a direct chat can require an ack from the one node it
# was actually addressed to rather than from every peer in earshot.
#
# That matters for truthfulness as much as for airtime: without it, a chat to
# Alice is reported UNDELIVERED because Bob -- an uninvolved bystander -- did
# not answer, even though Alice received it perfectly.
#
# Learned from traffic rather than advertised, because advertising it would
# cost ~26 bytes in every beacon forever (roughly +0.4% duty per node) to save
# ~0.09 s per direct chat. Wrong trade unless chat is constant. An unknown uid
# simply falls back to expecting every peer, which is the pre-existing
# behaviour, so the map is an optimisation and never a correctness dependency.
_atak_uid_to_node = collections.OrderedDict()
_MAX_UID_MAP = 64

# Shown as connected if heard within this long -- three missed beacons.
#
# This MUST stay a multiple of the beacon interval, and the multiple must be at
# least 2. Pinning it to an absolute value looks tempting when the interval
# grows (milestone 5 took the beacon from 15 s to 60 s, dragging this from 45 s
# to 180 s with it) but is wrong: any window shorter than the interval plus its
# jitter expires before the next beacon is even due, so every peer flaps
# connected/disconnected on every cycle. Three tolerates two consecutive
# losses; two would drop a peer on a single unlucky one.
#
# The cost of the longer window is real and lands on milestone 4: a departed
# node stays in the ack-expectation snapshot for up to 3 minutes rather than
# 45 s, so an UNDELIVERED verdict for a node that walked away takes that much
# longer to arrive. Correct, just slower.
_PEER_CONNECTED_S = _BEACON_INTERVAL_S * 3

# How long the receiving ATAK is told to keep a peer's marker. Deliberately
# LONGER than _PEER_CONNECTED_S, so that a marker is removed because this
# bridge decided the node is gone and not because a timer in ATAK ran out. The
# two must not race: whichever fires first is what the operator sees, and only
# one of them is looking at whether the radio can still hear anything.
_MARKER_STALE_S = _PEER_CONNECTED_S + 60

# Half the window it feeds, so a node stays in its neighbours' lists on the
# strength of this refresh alone -- and, because the beacon thread only wakes
# once per _BEACON_INTERVAL_S, actually fires two intervals in, leaving a full
# interval of margin for one lost beacon. See the note at _last_beacon_at.
_DIGEST_REFRESH_S = _PEER_CONNECTED_S / 2.0
# ...but remembered far longer, because a node list that silently drops
# departed nodes is worse than one showing them greyed out. "Who fell off?" is
# the question you actually have.
_PEER_FORGET_S = 600.0

# Everyone this node has heard from. Lives in peers.py, which is free of RNS
# and Android so it can actually be unit-tested -- two of the four bugs the
# 2026-08-12 bench found were in this table, and neither could have had a test
# while it was a bare dict in this module. Built here rather than in start()
# because a frame can arrive before start() finishes.
_peers = peers_mod.PeerTable(connected_s=_PEER_CONNECTED_S,
                             forget_s=_PEER_FORGET_S)


def _log_seq_gap(node_id, previous, seq, missed, since_s, name):
    """A numbered frame from a peer was skipped: one burst that either never
    arrived or arrived and was not decoded. The "[fm-air] burst ..." lines from
    fm_airlog say which -- an episode near this time is a burst that was heard
    and lost, none is a burst that never reached the detector."""
    who = name or (node_id.hex() if isinstance(node_id, (bytes, bytearray))
                   else str(node_id))
    # Did THIS node's own transmitter cover any of that interval? A frame that
    # never arrived while we were keyed up was most likely hidden by it, and
    # fm_airlog has no burst line to say so (the receive path is not fed audio
    # while we transmit). Only meaningful on an FM node with a receive path.
    tx_note = ""
    if since_s is not None:
        try:
            tx = fm_link.recent_tx(since_s + 3.0)
        except Exception:
            tx = None
        if tx is not None:
            n, secs = tx
            tx_note = ("; this node transmitted %d time(s), %.1f s on air, in "
                       "that interval -- a likely cause" % (n, secs)
                       if n else
                       "; this node did NOT transmit in that interval, so its "
                       "own transmitter does not explain it")
    RNS.log("[cot-bridge] seq gap from %s: missed %d frame(s) (seq %d -> %d%s)%s"
            % (who, missed, previous, seq,
               ", %.0f s after the previous one" % since_s
               if since_s is not None else "", tx_note), RNS.LOG_NOTICE)


_peers.on_gap = _log_seq_gap

# This node's PRIMARY identity: the device's own make/model, supplied by
# Kotlin at startup (see set_node_name). Deliberately not derived from ATAK --
# when ATAK is the thing that's broken, you still need to know which physical
# radio you're talking to in order to confirm the link is fine.
_node_name = ""

# The APK build this phone is running, supplied by Kotlin (see
# set_session_info). Only used in the session-start log line -- not part of
# this node's identity the way _node_name is.
_build_stamp = ""

# Secondary, and strictly a convenience: this device's ATAK callsign, learned
# from outgoing self-SA. Useful for correlating a node with what's on the map,
# absent whenever ATAK isn't running, and never relied on to identify a node.
_local_callsign = None


def _ensure_reticulum_config():
    # Always rewritten (not just written if missing): if RNS ever managed to
    # write its own default config with an AutoInterface before crashing
    # (config creation happens before interface bring-up, so a prior crashed
    # run may have already left one on disk), skipping the write here would
    # leave that broken config in place indefinitely.
    os.makedirs(RNS_CONFIG_DIR, exist_ok=True)
    config_path = os.path.join(RNS_CONFIG_DIR, "config")
    with open(config_path, "w") as f:
        f.write(_MINIMAL_RETICULUM_CONFIG)


def _wait_for_kotlin_socket(host, port, timeout=15):
    """Kotlin's ServerSocket may not be listening yet the instant this
    module loads -- poll briefly instead of failing immediately."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=2)
            # create_connection()'s timeout applies to the connect attempt,
            # but stays set on the returned socket for every subsequent
            # operation too. Callers use this socket for a long-lived
            # blocking read loop (waiting on actual CoT traffic, which may
            # not arrive for a while) -- back to blocking mode so recv()
            # waits instead of raising socket.timeout after 2 idle seconds.
            sock.settimeout(None)
            return sock
        except OSError as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"Could not connect to {host}:{port}: {last_err}")


# How long to wait for the radio to answer the startup handshake, rather than
# assuming it already has.
#
# RNS times both of these with a fixed sleep -- 0.2 s to decide the radio
# answered the detect query, 0.25 s before comparing the settings it echoed
# back -- and both are sized for a local serial port where a round trip is
# free. Our path was already longer than that assumption (Python -> loopback
# TCP -> Kotlin -> USB), and over Bluetooth every round trip additionally waits
# for the next connection interval. Neither timeout being met looks like
# hardware: the first aborts the interface with "Could not detect device", the
# second with "Frequency mismatch" on a radio that is tuned correctly.
#
# Waiting for the condition costs nothing when it is already true, so these are
# ceilings, not delays -- on USB they return as fast as the fixed sleeps did.
_DETECT_WAIT_S = 5.0
_READBACK_WAIT_S = 5.0

# Serialises writes to the radio. RNS has no such lock: it assumes
# process_outgoing is the only writer, which was true until the UI gained a
# button that sends a command of its own (enable_rnode_bluetooth). Two writers
# interleaving inside one KISS frame produces a frame the radio cannot parse,
# and nothing anywhere saying why.
_rnode_tx_lock = threading.Lock()


# The pairing code the radio last reported, as (pin, monotonic time).
_bt_pin = None
_bt_pin_lock = threading.Lock()


class _PinSniffingSerial:
    """The serial connection, plus a watch for the one frame RNS throws away.

    When an RNode is put into pairing mode it reports the six-digit code back
    over the same link, as KISS command 0x62. `rnodeconf` reads it and prints
    it; RNS's RNodeInterface has no case for that command at all, so on our
    path the frame arrives and is silently discarded -- which leaves the code
    visible only on the radio's own screen, and only if you know to hold the
    button for five seconds to get it there. Catching it here is what lets the
    app show the code on the phone that is asking for it.

    A proxy rather than a subclass because the object being wrapped comes out
    of pyserial's URL handler and its real class is an implementation detail.
    Everything except read() goes straight through untouched, and the sniffer
    itself can never raise into the read path: a bug in it must not be able to
    take the radio link down.
    """

    _FEND = 0xC0
    _FESC = 0xDB
    _TFEND = 0xDC
    _TFESC = 0xDD

    def __init__(self, inner):
        self._inner = inner
        self._state = "idle"      # idle -> command -> collecting
        self._buffer = b""
        self._escape = False

    def read(self, size=1):
        data = self._inner.read(size)
        if data:
            try:
                self._scan(data)
            except Exception as e:
                RNS.log("[cot-bridge] pairing-code watcher failed: %r" % (e,),
                        RNS.LOG_DEBUG)
        return data

    def _scan(self, data):
        global _bt_pin
        for byte in data:
            if self._state == "collecting":
                if self._escape:
                    if byte == self._TFEND:
                        byte = self._FEND
                    elif byte == self._TFESC:
                        byte = self._FESC
                    self._escape = False
                elif byte == self._FESC:
                    self._escape = True
                    continue
                elif byte == self._FEND:
                    # Frame ended early; nothing to report.
                    self._state = "command"
                    continue

                self._buffer += bytes([byte])
                if len(self._buffer) == 4:
                    pin = int.from_bytes(self._buffer, "big")
                    with _bt_pin_lock:
                        _bt_pin = (pin, time.monotonic())
                    RNS.log("[cot-bridge] radio reports Bluetooth pairing code "
                            "%06d" % pin, RNS.LOG_NOTICE)
                    self._state = "idle"
                    self._buffer = b""
            elif self._state == "command":
                if byte == _CMD_BT_CTRL_PIN:
                    self._state = "collecting"
                    self._buffer = b""
                    self._escape = False
                elif byte != self._FEND:
                    self._state = "idle"
            elif byte == self._FEND:
                # A FEND is always a frame boundary: inside a frame it would
                # have been escaped, so the next byte is a command.
                self._state = "command"

    def __getattr__(self, name):
        return getattr(self._inner, name)


# The command the firmware answers a pairing request with. RNS knows nothing
# about it; rnodeconf calls it CMD_BT_PIN.
_CMD_BT_CTRL_PIN = 0x62


def _make_rnode_serial():
    """
    Returns a pyserial-compatible object backed by the local TCP socket
    Kotlin exposes for the RNode -- whether it reached it by USB or by
    Bluetooth, which is the whole point of it being a socket -- using pyserial's
    built-in socket:// URL handler so the object quacks like a real
    serial.Serial (.read/.write/.in_waiting/etc).

    serial_for_url() makes its own connection attempt with no retry, so we
    probe first with the same wait/backoff _cot_listen_loop uses for the CoT
    socket -- otherwise this races Kotlin's ServerSocket startup and fails
    the whole bridge on any cold start where Python gets there first.
    """
    _wait_for_kotlin_socket(RNODE_BRIDGE_HOST, RNODE_BRIDGE_PORT).close()
    url = f"socket://{RNODE_BRIDGE_HOST}:{RNODE_BRIDGE_PORT}"
    ser = serial.serial_for_url(url, baudrate=RNODE_BAUD, timeout=1)
    return _PinSniffingSerial(ser)


def _build_rnode_interface():
    """
    Construct RNS's stock RNodeInterface, but with open_port() overridden to
    attach a socket-backed serial connection to Kotlin's USB bridge instead
    of opening a real device path. Everything else -- __init__,
    configure_device(), the KISS detect/radio-config handshake -- runs for
    real against the RNode reached through that socket.

    This matters because RNodeInterface.__init__() sets a lot of state
    (interface_ready, rxb/txb counters, id_callsign, flow_control, etc.)
    that process_outgoing()/process_incoming() depend on, and configure_device()
    is what actually pushes frequency/bandwidth/txpower/sf/cr to the physical
    radio via initRadio(). Hand-building the object field-by-field (skipping
    __init__) leaves that state and the radio config unset -- it looks like
    it's running but never actually configures the hardware and crashes on
    the first packet in either direction.

    RNodeInterface.__init__() also refuses to run at all when
    RNS.vendor.platformutils.is_android() is True (Android has its own
    jnius/usb4a-based interface RNS expects you to use instead). Since we're
    intentionally using the generic interface over our socket bridge, that
    check is patched out for the duration of construction only.
    """
    from RNS.Interfaces.RNodeInterface import RNodeInterface
    from RNS.Interfaces.Interface import Interface
    import RNS.vendor.platformutils as platformutils

    class BridgedRNodeInterface(RNodeInterface):
        def process_outgoing(self, data):
            # See _rnode_tx_lock: the only place RNS writes to the radio once
            # it is running, and therefore the only place that has to agree
            # with anything else that does.
            with _rnode_tx_lock:
                super().process_outgoing(data)

        def detect(self):
            # Ask, then WAIT for the answer. RNS's own caller follows this with
            # a fixed 0.2 s and then treats a missing reply as no radio at all.
            # Safe to block here: RNS only calls detect() from readLoop when
            # the interface is a TCP one, which this is not -- everything else
            # calls it from the single-threaded startup path, and the thread
            # that sets self.detected is a different one.
            super().detect()
            deadline = time.monotonic() + _DETECT_WAIT_S
            while not self.detected and time.monotonic() < deadline:
                time.sleep(0.05)

        def validateRadioState(self):
            # The same problem one step later. RNS compares the parameters the
            # radio echoed back after a fixed 0.25 s; over a slower link some
            # have not arrived, and the comparison then fails on values that
            # are merely still in flight -- reported as a settings mismatch,
            # which sends you looking at the radio config instead of the link.
            _await_radio_readback(self, timeout_s=_READBACK_WAIT_S)
            return super().validateRadioState()

        def open_port(self):
            # Called both for the initial open and by RNS's own
            # reconnect_port() background thread on every retry after a
            # drop. Must establish a fresh connection each time -- reusing
            # one captured object across retries means once the underlying
            # TCP connection dies, every future retry just reattaches the
            # same dead socket and never actually recovers.
            #
            # Must also close the OLD connection first: leaving it open
            # means Kotlin's accept loop (UsbSerialBridgeService) ends up
            # with two live "pump" threads writing to the same real USB
            # serial port concurrently -- the stale one from this old
            # connection, and a new one for the fresh connection below --
            # racing to write to the RNode and corrupting the KISS byte
            # stream, which is exactly the kind of thing that would make
            # the RNode stop responding coherently and never recover.
            old_serial = getattr(self, "serial", None)
            if old_serial is not None:
                try:
                    old_serial.close()
                except Exception:
                    pass

            RNS.log(f"{self} attaching Kotlin bridge socket as serial port", RNS.LOG_VERBOSE)
            self.serial = _make_rnode_serial()

    params = _radio_params()
    config = {
        "name": "rnode-bridge",
        "port": "bridged",  # placeholder string; open_port() above ignores it
        "frequency": str(params["frequency_hz"]),
        "bandwidth": str(params["bandwidth_hz"]),
        "txpower": str(params["txpower_dbm"]),
        "spreadingfactor": str(params["spreading_factor"]),
        "codingrate": str(params["coding_rate"]),
    }

    iface = BridgedRNodeInterface.__new__(BridgedRNodeInterface)

    original_is_android = platformutils.is_android
    platformutils.is_android = lambda: False
    try:
        # owner must be RNS.Transport itself (inbound() is a Transport
        # staticmethod) -- matches what Reticulum's own config-file loader
        # passes when it constructs interfaces.
        iface.__init__(RNS.Transport, config)
    finally:
        platformutils.is_android = original_is_android

    if not iface.online:
        # A failed first detect attempt is common on a cold start (the USB
        # bridge socket isn't always ready in time) and is NOT necessarily
        # fatal: RNodeInterface.__init__() already spawns its own
        # reconnect_port() background thread in this situation, which
        # regularly succeeds a few seconds later on its own. Raising
        # immediately here aborted the whole of start() before that thread
        # got a chance to finish -- the interface would silently come
        # online moments later, but by then _destination and
        # _cot_listen_loop had never been created, breaking the entire
        # bridge while LOOKING like just a transient hiccup. Give the
        # reconnect thread a real window before giving up for good.
        RNS.log("[cot-bridge] RNode not immediately online, waiting up to 15s for the automatic reconnect...", RNS.LOG_WARNING)
        deadline = time.time() + 15
        while time.time() < deadline and not iface.online:
            time.sleep(0.5)

    if not iface.online:
        raise RuntimeError(
            "RNode did not come online -- check the bridge socket is "
            "connected, the radio is plugged in or paired, and the parameters "
            "in rns_bridge.py match your board's supported range."
        )

    # Interface.OUT/IN default to False at the class level and are normally
    # only set to True by Reticulum's config-file interface_post_init(),
    # which we bypass by building this interface manually. Without this,
    # Transport.outbound()'s `for interface in Transport.interfaces: if
    # interface.OUT:` filter silently skips this interface for every
    # outbound packet -- no error, just "No interfaces could process the
    # outbound packet" and nothing ever transmits.
    iface.OUT = True
    iface.IN = True

    # Mirror the handful of other fields Reticulum's config-driven
    # interface loader sets in interface_post_init() after construction,
    # which we bypass by building this interface manually.
    iface.mode = Interface.MODE_FULL
    iface.ifac_size = RNodeInterface.DEFAULT_IFAC_SIZE

    RNS.Transport.add_interface(iface)
    iface.final_init()

    global _lora_iface
    _lora_iface = iface  # _airtime_s() reads .bitrate off this for TX pacing
    _link_ifaces[LINK_LORA] = iface

    # ...and now that it does, re-size the reassembly buffer against the REAL
    # link speed. _REASSEMBLY_TIMEOUT_S below is computed at import, when there
    # is no interface to ask, so it falls back to the SF8 figure baked into
    # reliable._FALLBACK_BITRATE. The retry schedule has no such excuse: it is
    # injected with the live _airtime_s and stretches with the actual bitrate.
    #
    # Left alone, those two disagree the moment the radio is not SF8. At the
    # SF10 range-test config the sender's last retry lands at 109.5 s and the
    # receiver discards the partial message at 104.5 s -- the final attempt,
    # the one carrying the missing fragment, arrives ~5 s after the buffer it
    # was meant to complete is gone. That is the identical failure the
    # 2026-08-12 fix chased, arriving from the other direction: that time the
    # schedule grew, this time the link slows down underneath it.
    #
    # Worth the wiring rather than just correcting the constant, because it
    # only ever bites on fragmented messages at the end of the retry schedule
    # -- which is to say on a marginal link, which is exactly what a range test
    # spends its afternoon in, and exactly when the evidence is least likely to
    # be read as a bug rather than as the range limit being found.
    _resize_reassembly(iface)

    return iface


def _build_mesh_interface():
    """Construct mt_link.MtInterface on a Meshtastic board, reached through the
    same Kotlin byte pipe the RNode uses.

    Registered as the LoRa link (see RADIO_MESHTASTIC). Modelled on
    _build_fm_interface(), and for the same reasons: it is a from-scratch
    Interface, IN/OUT default to False at the class level so Transport silently
    skips an interface that does not set them, and frames bypass RNS.Packet by
    delivering straight to _on_rns_packet_received -- the board is one
    pre-known broadcast channel, so Reticulum's ~19 B header buys nothing.

    NEVER RAISES OVER THE BOARD BEING ABSENT. The port connects in the
    background and keeps retrying, so an unplugged board leaves an offline LoRa
    link that comes up by itself, not a failed start. `mt_port` is imported
    here rather than at the top because it needs the `meshtastic` package: a
    problem with that package must cost the Meshtastic link, not the app.
    """
    import mt_port
    from RNS.Interfaces.Interface import Interface

    def open_stream():
        _wait_for_kotlin_socket(RNODE_BRIDGE_HOST, RNODE_BRIDGE_PORT).close()
        return serial.serial_for_url(
            f"socket://{RNODE_BRIDGE_HOST}:{RNODE_BRIDGE_PORT}",
            baudrate=RNODE_BAUD, timeout=0.5)

    port = mt_port.MeshtasticPort(open_stream, reconnect=True)
    # Diagnostics (per-packet facts, heartbeat answers) go to the same log as the
    # rest of the bridge, under a prefix that is easy to grep.
    port.log = lambda text: RNS.log("[mt-diag] %s" % text, RNS.LOG_NOTICE)
    iface = mt_link.MtInterface(
        RNS.Transport, port, deliver_frame=_on_rns_packet_received)
    iface.OUT = True
    iface.IN = True
    iface.mode = Interface.MODE_FULL
    iface.ifac_size = Interface.DEFAULT_IFAC_SIZE

    RNS.Transport.add_interface(iface)
    iface.final_init()

    global _lora_iface
    _lora_iface = iface
    _link_ifaces[LINK_LORA] = iface
    # The airtime model charges the board's real per-packet wrapper (24 B), not
    # an RNS header this link never sends.
    _lora_link.header_bytes = mt_link.WIRE_OVERHEAD_B

    port.start()
    if port.wait_online(15):
        preset = port.board_preset()
        if preset:
            iface.set_preset(preset)
        RNS.log("[cot-bridge] Meshtastic board online: node %x, preset %s, "
                "%d B per packet" % (port.node_num or 0, preset or "unknown",
                                     iface.HW_MTU), RNS.LOG_NOTICE)
        status = _mesh_board_status()
        for warning in status.get("warnings", []):
            RNS.log("[cot-bridge] BOARD WARNING: %s" % warning, RNS.LOG_WARNING)
        if status.get("profile_changes"):
            RNS.log("[cot-bridge] BOARD PROFILE: the board differs from the app's "
                    "settings (%s); Radio -> Use the app's settings applies them"
                    % "; ".join(status["profile_changes"]), RNS.LOG_WARNING)
        elif status.get("profile_problem"):
            RNS.log("[cot-bridge] BOARD PROFILE: cannot compare with the app's "
                    "settings: %s" % status["profile_problem"], RNS.LOG_WARNING)
    else:
        RNS.log("[cot-bridge] Meshtastic board not online yet (%s); the port "
                "keeps retrying" % (port.last_error or "no answer"),
                RNS.LOG_WARNING)
    _resize_reassembly(iface)
    return iface


def _build_fm_interface():
    """Construct fm_link.FmInterface against Kotlin's FM bridge socket.

    FM_BRIDGE_PORT is separate from RNODE_BRIDGE_PORT on purpose: for
    LoRa-primary/FM-backup routing, FmAudioBridgeService runs CONCURRENTLY with
    UsbSerialBridgeService/BleSerialBridgeService rather than instead of it (see
    RadioBridgeService.kt), so they can no longer share one port. Called either
    when Kotlin called set_transport("fm") before start() (FM as the sole
    transport) or when _fm_backup_enabled is set alongside a LoRa transport --
    see start().

    FmInterface is a plain RNS.Interfaces.Interface.Interface rather than an
    RNodeInterface, because there is no KISS byte stream and no radio-config
    handshake on this link, so subclassing RNodeInterface would inherit
    machinery that cannot apply. That means the fields Reticulum's config-file
    interface_post_init() would normally set have to be set here, exactly as
    _build_rnode_interface() above does and for the identical reason: both
    bypass that loader by constructing the interface directly, and IN/OUT
    default to False at the class level -- Transport.outbound() silently skips
    any interface where OUT is not True.

    deliver_frame=_on_rns_packet_received BYPASSES RNS.Packet for this link.
    Incoming frames go straight to the same mesh-unwrap-and-handle function
    RNS.Transport.inbound() would have called anyway (_on_rns_packet_received's
    `packet` argument is only used to identify the arrival link), which saves
    the ~19 B of RNS framing that is larger than a third of this link's whole
    29 B capacity. See fm_link.py's module docstring. It is still a real
    Transport-registered interface -- only the per-message wire format skips
    RNS.Packet.
    """
    from RNS.Interfaces.Interface import Interface

    sock = _wait_for_kotlin_socket(FM_BRIDGE_HOST, FM_BRIDGE_PORT)
    iface = fm_link.FmInterface(
        RNS.Transport, sock, _native_lib_dir,
        deliver_frame=_on_rns_packet_received,
        reconnect=lambda: _wait_for_kotlin_socket(FM_BRIDGE_HOST,
                                                  FM_BRIDGE_PORT),
        audio_alive=fm_link.audio_alive)

    iface.OUT = True
    iface.IN = True
    iface.mode = Interface.MODE_FULL
    iface.ifac_size = Interface.DEFAULT_IFAC_SIZE

    RNS.Transport.add_interface(iface)
    iface.final_init()

    # ITS OWN GLOBAL, not the one _build_rnode_interface() writes. This used to
    # share that name, because the beacon loop's own-radio-online check, the
    # status screen and TX pacing all read "the current radio interface" through
    # one name that only made sense while there was one kind of radio.
    # LoRa-primary/FM-backup routing is exactly what needs that to stop: the two
    # interfaces now coexist rather than alias each other. A couple of
    # RNode-only stats (r_stat_rssi/snr) degrade to None via getattr()'s default
    # when asked of an FmInterface.
    global _fm_iface
    _fm_iface = iface
    _link_ifaces[LINK_FM] = iface

    # DELIBERATELY NOT _resize_reassembly(iface), unlike the LoRa build above.
    # That function sizes the window from the longest schedule this node could
    # run, FM's included (see _reassembly_window_s), so calling it here would be
    # harmless -- but start() sets both receive-side windows from
    # fm_resend.span_s() once every interface is up, and doing it in one place
    # keeps the duplicate window and the reassembly window derived from the same
    # figure. They have to agree: too short a duplicate window renders a chat
    # message twice, too short a reassembly window takes a multi-fragment
    # message from ~60% deliverable to under 2%.

    RNS.log("[cot-bridge] FM link up: %s" % fm_link.summary_line(),
            RNS.LOG_NOTICE)
    RNS.log("[cot-bridge] FM evidence: %s" % fm_link.evidence_line(),
            RNS.LOG_NOTICE)
    for line in fm_link.budget_lines():
        RNS.log("[cot-bridge] FM airtime: %s" % line, RNS.LOG_DEBUG)

    return iface


# --- WiFi multi-network socket binding --------------------------------------
#
# Fixes a phone with two simultaneous active networks (e.g. the Pixel's
# wlan0+cellular rmnet1) failing AutoInterface's self-multicast-echo check --
# an unbound socket follows Android's own "best network" guess for multicast
# group membership, which is not guaranteed to be WiFi. The surgical
# alternative to ConnectivityManager.bindProcessToNetwork() (tried first:
# fixed the Pixel but broke a WiFi-only phone with no ambiguity to fix, so was
# reverted) -- this binds each of AutoInterface's own sockets individually via
# Network.bindSocket(), leaving every other socket in the process (RNode/FM's
# own loopback sockets included) untouched.
#
# Live-hardware-confirmed (2026-09-24, Pixel 7 + OnePlus 8T) load-bearing
# subtlety this needs to get right: ParcelFileDescriptor.adoptFd() marks the
# adopted fd as Java-owned for Android's fdsan (file-descriptor sanitizer). If
# AutoInterface's own code later closes that socket with a plain Python
# socket.close() -- which every throwaway announce socket in peer_announce()/
# reverse_announce() does, every ANNOUNCE_INTERVAL (~1.6s) -- fdsan sees a raw
# close() on an fd it believes ParcelFileDescriptor still owns and raises
# SIGABRT, killing the whole process outright. Two earlier sessions attempting
# this same per-socket bind both saw the interface build "hang" for 90+
# seconds with no further log output; a session that finally traced every
# socket call (not just the bind itself) instead of just the constructor
# found this was never a hang at all -- logcat showed "Fatal signal 6
# (SIGABRT) ... pid <n> (customrnsbridge)" the instant the first bound socket
# was closed, and what looked like silence was the process being repeatedly
# crash-killed and slowly respawned (ActivityManager backs off "Rescheduling
# restart of crashed service ... in Nms" on repeat crashes) with nothing
# logged under the OLD, since-dead pid a session was still tailing.
#
# _WifiBoundSocket.close() below calls pfd.detachFd() BEFORE closing the raw
# socket, releasing fdsan's ownership tag first -- after that, an ordinary
# close() is safe, same as on a never-bound socket. Its __del__ covers the
# other way a socket can end without an explicit close(): peer_announce()/
# reverse_announce() only close() on their happy path, so a socket whose
# sendto() raises (confirmed live on the OnePlus 8T: a transient failure right
# after WiFi came up) is instead left for CPython's own garbage collector,
# which would otherwise finalize the raw socket directly -- bypassing close()
# entirely and hitting the exact same fdsan crash a second, harder-to-
# reproduce way.
#
# Confirmed NOT needed: WiFi-binding final_init()'s separate
# socketserver.UDPServer listener sockets. Those are bound to a specific
# link-local address, not a multicast group, so Android's multi-network
# ambiguity (which is specifically about multicast group delivery) does not
# apply to them -- self-multicast-echo worked end-to-end on the Pixel with
# only AutoInterface's own sockets bound.
#
# Confirmed (2026-09-24, later the same day, both phones) that binding is
# needed on the RECEIVE-side sockets only, not the send-only ones -- and that
# getting this wrong doesn't crash or hang, it just silently never delivers a
# single real cross-device packet, which took a plain `nc -u` UDP send
# (bypassing this app's code entirely) to prove: a manually-sent packet
# reached the other phone's already-running discovery_socket fine (logged as
# "authentication hash was incorrect", i.e. genuinely received), while
# AutoInterface's own periodic peer_announce()/reverse_announce() packets
# -- sent through a socket this shim was ALSO binding to the WiFi Network,
# on top of AutoInterface's own IPV6_MULTICAST_IF pin -- never arrived,
# with no exception anywhere (`sendto()` on a connectionless UDP socket
# doesn't error just because the packet got dropped in flight). Two
# different mechanisms (Android's Network-based fwmark routing, and the
# POSIX interface-index sockopt) telling the kernel two different things
# about which interface should carry an outgoing multicast send is exactly
# the conflict this file's own history flagged as untested and worth
# trying next. `discovery_socket`/`unicast_discovery_socket` (this app's
# only receive-side sockets, the ones actually reading a multi-network
# phone's ambiguous "best network" guess) call `.bind()`;
# `announce_socket`/`outbound_udp_socket` (send-only) never do. So binding
# is now deferred to the FIRST `.bind()` call instead of happening at
# socket() creation time -- a socket that never binds never gets
# Network.bindSocket() either, and sends exactly like nc's plain socket
# did in the working test above.

_bound_wifi_fds = []


def _wifi_network():
    """
    The Android `Network` object for whichever attached network currently
    has the WIFI transport, found via Chaquopy's direct Java interop
    (ConnectivityManager.getAllNetworks() + getNetworkCapabilities()) --
    the same technique _build_wifi_interface()'s own interface_name_to_index
    override already uses for a different Android API. Returns None if
    there is no WiFi network up right now; callers must treat that as
    "cannot bind, use the socket unbound" rather than raise.
    """
    from com.chaquo.python import Python
    from android.content import Context as AndroidContext
    from android.net import ConnectivityManager, NetworkCapabilities

    context = Python.getPlatform().getApplication()
    cm = context.getSystemService(AndroidContext.CONNECTIVITY_SERVICE)
    for network in cm.getAllNetworks():
        caps = cm.getNetworkCapabilities(network)
        if caps is not None and caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI):
            return network
    return None


def _bind_socket_to_wifi(raw_sock):
    """
    Pin one already-created POSIX socket to the WiFi Network specifically.
    Returns the ParcelFileDescriptor on success (the caller must keep it
    alive -- see _WifiBoundSocket) or None if there is no WiFi Network to
    bind to yet, or the bind itself failed; either way the caller should
    fall back to using the socket unbound rather than fail outright.
    """
    try:
        from android.os import ParcelFileDescriptor

        network = _wifi_network()
        if network is None:
            return None

        pfd = ParcelFileDescriptor.adoptFd(raw_sock.fileno())
        network.bindSocket(pfd.getFileDescriptor())
        _bound_wifi_fds.append(pfd)
        return pfd
    except Exception as e:
        RNS.log(f"[cot-bridge] could not bind a WiFi AutoInterface socket to the WiFi network: {e}", RNS.LOG_WARNING)
        return None


class _WifiBoundSocket(object):
    """
    Wraps one real socket, deferring Network.bindSocket() until this
    socket's first .bind() call -- see this block's own top-of-file
    comment for why send-only sockets (which never call .bind()) must
    stay unbound. Also ensures a bound socket's ParcelFileDescriptor gets
    detached before the socket is ever closed. Every other call passes
    straight through via __getattr__.
    """

    def __init__(self, raw):
        self._raw = raw
        self._pfd = None

    def bind(self, *a, **k):
        self._pfd = _bind_socket_to_wifi(self._raw)
        return self._raw.bind(*a, **k)

    def close(self, *a, **k):
        if self._pfd is not None:
            try:
                self._pfd.detachFd()
                if self._pfd in _bound_wifi_fds:
                    _bound_wifi_fds.remove(self._pfd)
            except Exception as e:
                RNS.log(f"[cot-bridge] could not detach a WiFi-bound socket before closing it: {e}", RNS.LOG_WARNING)
            self._pfd = None
        return self._raw.close(*a, **k)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _WifiBindingSocketModule(object):
    """
    Stands in for the `socket` module inside RNS.Interfaces.AutoInterface's
    OWN module namespace ONLY (rebinding the NAME there, not mutating the
    real, shared `socket` module -- `import socket` everywhere else in the
    process, including RNode/FM's own loopback sockets, is unaffected).
    Every socket.socket(...) call AutoInterface makes goes through .socket()
    below, which creates the real thing and hands back a wrapper that binds
    it to the WiFi Network on its first .bind() call (see _WifiBoundSocket --
    a socket that never binds, i.e. a send-only one, never gets bound to a
    Network either). Everything else (constants, getaddrinfo, inet_pton,
    ...) passes straight through.
    """

    def __init__(self, real_socket_module):
        self._real = real_socket_module

    def socket(self, *args, **kwargs):
        raw = self._real.socket(*args, **kwargs)
        return _WifiBoundSocket(raw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_wifi_socket_binding(autointerface_module):
    autointerface_module.socket = _WifiBindingSocketModule(socket)


# --- end WiFi multi-network socket binding ----------------------------------


def _build_wifi_interface():
    """
    Construct RNS's stock AutoInterface -- unmodified except for one method
    override -- against the 802.11s mesh NIC Kotlin has configured. Unlike
    _build_rnode_interface()/_build_fm_interface(), this interface talks
    directly to the network, not through a Kotlin-owned loopback socket:
    there is no radio hardware to bridge to, WiFi already IS a network
    interface.

    Stock, not a from-scratch interface like fm_interface.py, deliberately:
    AutoInterface is what lets this bridge interoperate with any standard
    Reticulum node on the same WiFi mesh (Sideband, NomadNet, a laptop RNS
    instance), not just other copies of this app -- the whole reason WiFi
    was worth adding at all over a simpler private broadcast scheme.

    ONE thing about it is broken on this platform and needs patching:
    interface_name_to_index() falls back to socket.if_nametoindex()
    (AutoInterface's own implementation, used on every non-Windows platform
    including Android), which Chaquopy's Android Python build does not
    implement -- this is exactly why the Reticulum config this app writes
    (_MINIMAL_RETICULUM_CONFIG) has an empty [interfaces] section instead
    of letting RNS auto-generate one containing AutoInterface.
    AndroidAutoInterface below overrides just that one method.

    Confirmed on a real phone (2026-09-23, OnePlus 8T) that the obvious fix
    -- reading /sys/class/net/<ifname>/ifindex, a plain Linux kernel sysfs
    file -- is ALSO blocked, not merely missing: Android's SELinux policy
    denies a third-party app read access to it (PermissionError, Errno 13),
    caught by start()'s try/except around this function so it degrades to
    "no WiFi link" rather than taking down LoRa/FM, but silently useless on
    its own. What actually works is asking Android's own Java networking
    API for the answer via Chaquopy's Java interop
    (java.net.NetworkInterface.getByName().getIndex()) -- a normal,
    permitted call (it is what ConnectivityManager-based code already uses
    under the hood), not a raw sysfs read.

    _wifi_ifname restricts AutoInterface to the mesh NIC specifically (its
    `devices` config option) -- left unset (empty string), AutoInterface
    enumerates and joins multicast on every non-ignored system interface,
    which on a phone also means cellular and any ordinary infrastructure
    WiFi link, neither of which should be carrying CoT.
    """
    import RNS.Interfaces.AutoInterface as autointerface_module
    from RNS.Interfaces.AutoInterface import AutoInterface
    from RNS.Interfaces.Interface import Interface
    from java.net import NetworkInterface as JNetworkInterface

    # Rebinds AutoInterface's own `socket` name to a shim that WiFi-binds
    # every socket it creates -- see the "WiFi multi-network socket binding"
    # block above this function.
    _install_wifi_socket_binding(autointerface_module)

    class AndroidAutoInterface(AutoInterface):
        def interface_name_to_index(self, ifname):
            # The one patch stock AutoInterface needs on this platform --
            # see this function's own docstring above for why the more
            # obvious sysfs read does not work.
            jiface = JNetworkInterface.getByName(ifname)
            if jiface is None:
                raise OSError("no such interface: %s" % ifname)
            return jiface.getIndex()

        def peer_announce(self, ifname):
            # A full copy of AutoInterface's own peer_announce(), not a call
            # to it -- the one line this changes (the destination address
            # for getaddrinfo(), below) is built inline with no seam to
            # patch from outside. Stock peer_announce() sends its multicast
            # discovery token to a BARE, unscoped destination address,
            # relying entirely on the IPV6_MULTICAST_IF sockopt to pick the
            # egress interface. Every OTHER address AutoInterface itself
            # builds embeds the interface directly in the address string
            # instead: the receive-side discovery_socket bind(),
            # reverse_announce()'s unicast reply, and
            # AutoInterfacePeer.process_outgoing()'s data send. This brings
            # peer_announce() in line with those three, on the theory (from
            # a `nc -u` UDP send to an explicitly-scoped address reliably
            # reaching the other phone, when this method's own announces
            # never did) that IPV6_MULTICAST_IF alone doesn't reliably
            # control real transmission on this platform.
            #
            # Tried and NOT sufficient on its own (2026-09-24, both phones,
            # confirmed via logcat that every send here completes with no
            # exception and the correct scope_id resolved): real
            # cross-device peer discovery ("added peer") still did not
            # occur with this fix alone, nor with IPV6_MULTICAST_IF removed
            # entirely to match nc's exact approach, nor with both phones
            # added to the Doze/App Standby whitelist (a `dumpsys netpolicy`
            # UID entry showed an APP_STANDBY|APP_BACKGROUND-blocked state
            # that looked like a plausible cause; whitelisting made no
            # difference). Kept anyway -- it is still a real correctness
            # fix (three of four address-building call sites already did
            # this; only this one didn't) even though it isn't sufficient
            # by itself. See docs/open-questions.md's "WiFi multicast peer
            # discovery" entry for the full chain of eliminated hypotheses
            # and what's actually left to try (packet capture, or a
            # different AP/router entirely).
            try:
                link_local_address = self.adopted_interfaces[ifname]
                discovery_token = RNS.Identity.full_hash(self.group_id + link_local_address.encode("utf-8"))
                announce_socket = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                addr_info = socket.getaddrinfo(
                    f"{self.mcast_discovery_address}%{ifname}",
                    self.discovery_port, socket.AF_INET6, socket.SOCK_DGRAM,
                )

                ifis = struct.pack("I", self.interface_name_to_index(ifname))
                announce_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, ifis)
                announce_socket.sendto(discovery_token, addr_info[0][4])
                announce_socket.close()

            except Exception as e:
                if (ifname in self.timed_out_interfaces and self.timed_out_interfaces[ifname] == False) or not ifname in self.timed_out_interfaces:
                    RNS.log(str(self) + " Detected possible carrier loss on " + str(ifname) + ": " + str(e), RNS.LOG_WARNING)
                else:
                    pass

    config = {"name": "wifi-mesh"}
    if _wifi_ifname:
        config["devices"] = _wifi_ifname

    # owner must be RNS.Transport itself (inbound() is a Transport
    # staticmethod) -- matches what Reticulum's own config-file loader
    # passes when it constructs interfaces, same as
    # _build_rnode_interface()/_build_fm_interface().
    iface = AndroidAutoInterface(RNS.Transport, config)

    # Interface.OUT defaults to False at the class level (AutoInterface's
    # own __init__ sets it to False explicitly too) and .mode/.ifac_size
    # are never set by __init__ at all -- normally all three are set by
    # Reticulum's config-file interface_post_init(), which building this
    # manually bypasses, same reason the other two builders set them by
    # hand.
    iface.OUT = True
    iface.mode = Interface.MODE_FULL
    iface.ifac_size = AutoInterface.DEFAULT_IFAC_SIZE

    # Same story, and a real bug confirmed live (2026-09-25, Pixel 7):
    # interface_post_init() also sets .ifac_netname/.ifac_netkey (to None
    # when no [interfaces] config section configures them -- not "leave
    # the attribute unset"), and AutoInterface.add_peer() reads both
    # UNCONDITIONALLY off `self` the moment it discovers a genuinely NEW
    # peer (not self-echo, which takes a different branch that never
    # touches these) -- `spawned_interface.ifac_netname = self.ifac_netname`
    # is not optional/guarded. Without this, the very first real peer
    # AutoInterface ever discovers raises
    # `AttributeError: 'AndroidAutoInterface' object has no attribute
    # 'ifac_netname'` inside its own discovery_loop daemon thread --
    # silently killing that thread (a background thread's own uncaught
    # exception doesn't crash the process, just ends the thread), AFTER
    # `self.peers[addr]` was already set but BEFORE
    # `RNS.Transport.add_interface(spawned_interface)`/
    # `self.spawned_interfaces[addr] = spawned_interface` -- so the peer
    # looks half-added (shows up in `self.peers`) but can never actually
    # send or receive anything, and the interface's discovery thread for
    # that adopted NIC is gone for the rest of the process. None matches
    # what an unconfigured `[interfaces]` section would have produced.
    iface.ifac_netname = None
    iface.ifac_netkey = None

    # Same root cause, a second (and, checked directly against a live
    # Interface() instance, LAST) set of attributes it's missing: unlike
    # gravity/ingress_control/ic_*/ec_pr_freq/egress_control -- which
    # Interface.__init__() itself already defaults from the live
    # RNS.Reticulum instance, so those were never actually a problem --
    # announce_rate_target/grace/penalty have NO default anywhere except
    # Reticulum.py's own interface_post_init(), and confirmed missing
    # entirely on a bare Interface() instance. None matches what an
    # unconfigured `[interfaces]` section (and this app's Reticulum
    # instance, which never enables transport_enabled()) would produce.
    iface.announce_rate_target = None
    iface.announce_rate_grace = None
    iface.announce_rate_penalty = None

    RNS.Transport.add_interface(iface)
    # Blocks for a short peering-discovery window (~1.9 s at AutoInterface's
    # own defaults) and then sets iface.online = True unconditionally --
    # unlike RNode's hardware handshake, AutoInterface does not wait for an
    # actual peer to appear before declaring itself online, so there is no
    # separate retry/timeout loop needed here the way
    # _build_rnode_interface() has.
    iface.final_init()

    global _wifi_iface
    _wifi_iface = iface
    _link_ifaces[LINK_WIFI] = iface

    return iface


# --- IPv4 multicast discovery beacon ----------------------------------------
#
# A private, supplementary bootstrap for THIS app's own WiFi peers
# specifically -- NOT a replacement for AutoInterface's own standard IPv6
# multicast discovery, which keeps running completely unmodified alongside
# this, so a generic Reticulum node (Sideband, NomadNet, a laptop instance)
# can still find and be found the normal way on a network where IPv6
# multicast actually works.
#
# Confirmed live (2026-09-24, both phones -- docs/open-questions.md's "WiFi
# multicast peer discovery" entry has the full chain): every attempt at
# getting this app's OWN IPv6 multicast sends to reach another instance of
# itself failed -- Network.bindSocket() (three variants), IPV6_MULTICAST_IF
# (present/absent), scope-embedded destination addresses, Doze whitelisting,
# all eliminated -- while a plain IPv4 multicast send between two copies of
# this exact app (same UID, same code shape) worked flawlessly every time.
# Whatever is special-casing IPv6 multicast on this network/device
# combination doesn't touch IPv4, so this bootstraps peer discovery over
# IPv4 instead, then hands off to AutoInterface's own already-working
# unicast IPv6 data path via the SAME add_peer() call its own discovery
# mechanism uses internally (see AutoInterface.discovery_handler()). Once
# add_peer() succeeds once for a given peer, this beacon stops mattering
# for it -- reverse-peering (reverse_announce(), already scope-correct) and
# real traffic (process_outgoing()/process_incoming(), also already
# scope-correct) both run over the standard, unmodified unicast path from
# then on. This keeps announcing periodically only as a safety net for
# peers not yet discovered, or one that dropped out and needs rediscovery.
#
# Deliberately reuses AutoInterface's own group_id + per-address hash token
# scheme (see AutoInterface.peer_announce()) for the same lightweight
# anti-spoofing property stock discovery already has -- not a new security
# model, just carried over IPv4 instead of IPv6.

_IPV4_BEACON_GROUP = "239.255.42.99"
_IPV4_BEACON_PORT = 42999
_IPV4_BEACON_INTERVAL_S = 2.0


def _ipv4_of(ifname):
    from RNS.Interfaces.util import netinfo

    addrs = netinfo.ifaddresses(ifname)
    if netinfo.AF_INET in addrs:
        for a in addrs[netinfo.AF_INET]:
            if "addr" in a:
                return a["addr"]
    return None


def _ipv4_beacon_sender(ifname, local_ipv4):
    ssock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ssock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ipv4))
    while True:
        try:
            iface = _wifi_iface
            own_addr = iface.adopted_interfaces.get(ifname) if iface is not None else None
            if own_addr is not None:
                token = RNS.Identity.full_hash(iface.group_id + own_addr.encode("utf-8"))
                ssock.sendto(token + own_addr.encode("utf-8"), (_IPV4_BEACON_GROUP, _IPV4_BEACON_PORT))
        except Exception as e:
            RNS.log(f"[cot-bridge] IPv4 discovery beacon send failed: {e}", RNS.LOG_WARNING)
        time.sleep(_IPV4_BEACON_INTERVAL_S)


def _ipv4_beacon_receiver(ifname, local_ipv4):
    rsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        rsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    rsock.bind(("", _IPV4_BEACON_PORT))
    mreq = socket.inet_aton(_IPV4_BEACON_GROUP) + socket.inet_aton(local_ipv4)
    rsock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    hashlen = RNS.Identity.HASHLENGTH // 8
    while True:
        try:
            data, _ = rsock.recvfrom(1024)
            if len(data) <= hashlen:
                continue
            token, addr_bytes = data[:hashlen], data[hashlen:]
            try:
                peer_addr = addr_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue

            iface = _wifi_iface
            if iface is None or peer_addr in iface.link_local_addresses:
                continue  # WiFi not up yet, or this is our own beacon heard back

            expected = RNS.Identity.full_hash(iface.group_id + peer_addr.encode("utf-8"))
            if token == expected:
                iface.add_peer(peer_addr, ifname)
        except Exception as e:
            RNS.log(f"[cot-bridge] IPv4 discovery beacon receive failed: {e}", RNS.LOG_WARNING)


def _start_ipv4_discovery_beacon():
    """Start the IPv4 discovery beacon's sender/receiver threads -- see this
    section's own top-of-file comment for why this exists and what it
    does. A no-op (with a log line, not an exception) if this interface has
    no IPv4 address at all yet."""
    ifname = _wifi_ifname or "wlan0"
    local_ipv4 = _ipv4_of(ifname)
    if local_ipv4 is None:
        RNS.log(f"[cot-bridge] IPv4 discovery beacon: no IPv4 address on {ifname}, not starting", RNS.LOG_WARNING)
        return

    threading.Thread(target=_ipv4_beacon_receiver, args=(ifname, local_ipv4), daemon=True).start()
    threading.Thread(target=_ipv4_beacon_sender, args=(ifname, local_ipv4), daemon=True).start()
    RNS.log(f"[cot-bridge] IPv4 discovery beacon started on {ifname} ({local_ipv4})", RNS.LOG_NOTICE)


# --- end IPv4 multicast discovery beacon ------------------------------------


def _resize_reassembly(iface):
    """Re-size the reassembly buffer against the radio's CURRENT speed.

    Called on startup and again whenever the operator changes the preset --
    which is the case that made it a function rather than inline code. Picking
    "Long Slow" from a menu changes the link from 1074 bps to 183 bps, a factor
    of six, and every retry timer stretches with it. A buffer still sized for
    the old preset would start discarding part-received messages in the middle
    of the sender's schedule, and it would do it silently.

    A LINK WHOSE REDUNDANCY IS BLIND RESENDS RATHER THAN ARQ MUST NOT BE
    SHRUNK HERE. Shrinking FM's window to reliable.py's ARQ retry span
    silently stops fragments accumulating across attempts, which takes a
    multi-fragment message from roughly 60% deliverable to under 2% -- visible
    from nowhere except a delivery rate.

    The pre-teardown code protected that with an early `return` when the
    transport was FM. This now takes the MAX of both schedules instead, which
    is the shape N-way failover needs: FM being the selected transport does not
    stop LoRa carrying some of the same message's attempts, and one reassembler
    serves both. See _reassembly_window_s().

    AND THE TEST IS "IS THERE AN FM SCHEDULE", NOT "IS FM THE TRANSPORT".
    _transport is "usb"/"ble" in LoRa-primary/FM-backup mode, where FM is built
    and _escalate_to_fm registers real blind schedules on it -- so keying on
    _transport alone would have excluded FM's span in exactly the configuration
    that needs it most, on the attempts that reach FM only because LoRa already
    failed. _fm_iface being built is the honest question.
    """
    reassembly_s = _reassembly_window_s(
        include_fm=(_transport == "fm" or _fm_iface is not None),
        airtime_s=_airtime_s)
    _reassembler.set_timeout(reassembly_s)
    RNS.log("[cot-bridge] link is %.0f bps; reassembly window %.0f s"
            % (getattr(iface, "bitrate", 0) or 0, reassembly_s),
            RNS.LOG_NOTICE)


def _make_destination(reticulum_instance):
    # PLAIN destinations are unauthenticated/unencrypted by design and
    # can't hold an identity at all -- RNS.Destination.__init__ raises
    # TypeError if you pass one. Milestone 1 deliberately uses PLAIN
    # (single shared broadcast namespace both phones agree on), so this
    # must be None, not a real RNS.Identity().
    dest = RNS.Destination(
        None,
        RNS.Destination.IN,
        RNS.Destination.PLAIN,
        APP_NAME,
        *ASPECTS,
    )
    dest.set_packet_callback(_on_rns_packet_received)
    return dest


def _link_of(packet):
    """Which of this node's own radios `packet` (the deliver_frame/
    packet_callback argument) arrived on.

    FM IS SPECIAL-CASED FIRST: fm_link's deliver_frame wiring calls
    self._deliver(data, self) directly, bypassing RNS.Packet entirely (see that
    module's docstring for the ~19 B that buys on a 29 B link), so `packet` for
    an FM arrival is the FmInterface instance itself rather than an RNS.Packet.
    Checking identity against _fm_iface keeps this free of an RNS.Packet import.

    Everything else arrives as a real RNS.Packet (LoRa and WiFi both go through
    dest.set_packet_callback -- RNS.Transport.inbound() constructs one from
    the raw bytes and always sets its receiving_interface before calling the
    registered callback), so which link it came in on is read off that
    attribute via the same _link_ifaces registry _iface_for() and
    _set_active_link() use, rather than assumed. Falls back to _active_link
    if the interface is not one this bridge registered at all (should not
    happen, but a wrong-but-live answer here is better than a crash or a
    hardcoded LINK_LORA on a node where LoRa was never even built).
    """
    if packet is _fm_iface:
        return LINK_FM
    receiving = getattr(packet, "receiving_interface", None)
    for name, iface in _link_ifaces.items():
        if iface is not None and iface is receiving:
            return name
    return _active_link


def _on_rns_packet_received(data, packet):
    """RNS -> reassemble -> decode -> local CoT bridge socket -> Kotlin -> ATAK

    Runs on the RNode serial read thread, so anything slow here stalls radio
    reception. Reassembler.offer() is O(1) and never raises; decode() is the
    only thing on this path that does.
    """
    link = _link_of(packet)

    # Signal stats are an RNode-only concept (LoRa's own chip registers) --
    # RNodeInterface.process_incoming() clears them the moment it returns,
    # and FmInterface has no equivalent at all, so asking for them off the
    # wrong interface would silently report LoRa's last reading for a frame
    # that arrived on FM.
    if link == LINK_LORA:
        rssi = getattr(_lora_iface, "r_stat_rssi", None)
        snr = getattr(_lora_iface, "r_stat_snr", None)
    else:
        rssi = None
        snr = None

    # The mesh envelope comes off FIRST, before reassembly: it wraps each
    # fragment individually so that a relay can recognise and forward one
    # without holding the whole message, and so that nothing unauthenticated
    # ever reaches the reassembler's buffers. unwrap() returns None rather than
    # raising for noise, foreign networks and forgeries alike -- all three are
    # the same non-event on a shared frequency.
    # Snapshotted, because the UI thread can swap it mid-packet when the
    # operator changes the passphrase. Finishing this frame under the key it
    # arrived on is right; the next one picks up the new key.
    active = _mesh
    if active is None:
        return
    inbound = active.unwrap(data)
    if inbound is None:
        return
    # Before the peer table, the relay and the reassembler alike: a node the
    # bench has put out of range must leave no trace of having been heard.
    if _out_of_range(active, inbound):
        return
    if inbound.origin == active.origin:
        # Our own transmission, heard back. Nothing to do with it, and
        # delivering it would put a marker for this device on its own map.
        #
        # Since step 3 this is also free passive acknowledgement: hearing our
        # own frame return means a neighbour relayed it, which is proof the
        # hop happened without a single byte spent asking.
        _relay_cancel((inbound.origin, inbound.epoch, inbound.seq),
                      "our own frame came back relayed")
        return
    if inbound.duplicate:
        # Heard twice means it is already circulating -- either somebody
        # relayed it or the sender repeated itself -- so a relay of ours would
        # add nothing. Cancelled before the return below, which is the whole
        # of mechanism 3.
        _relay_cancel((inbound.origin, inbound.epoch, inbound.seq),
                      "heard again from someone else")
        # This is the replay defence, and it does not fight milestone 4's rule
        # that a duplicate must still be acknowledged: an ARQ retry is wrapped
        # afresh on its way out and therefore carries a NEW sequence number, so
        # it arrives here as a new frame and is acked again. Only a byte-identical
        # replay lands in this branch, and answering one would be free
        # amplification for whoever recorded it.
        RNS.log(
            f"[cot-bridge] dropping replayed frame {inbound.seq} from "
            f"{_peer_label(mesh.node_id_hex(inbound.origin))}",
            RNS.LOG_NOTICE,
        )
        return

    # Counted here rather than before the duplicate check: a replay says
    # nothing about whether the sender is still there, and refreshing liveness
    # from one would let a recording keep a departed node looking alive.
    sender = mesh.node_id_hex(inbound.origin)
    # BEFORE _note_peer_frame, which is what writes the range-test row: the
    # fix a beacon carries is the position that row is about.
    if _heard_directly(inbound):
        _book_beacon_fix(sender, inbound.frame)
    sighting = _note_peer_frame(sender, rssi, snr, inbound.seq,
                                direct=_heard_directly(inbound),
                                hops_away=_hops_travelled(inbound),
                                epoch=inbound.epoch, link=link)
    if sighting.fresh:
        _note_new_peer(sender)
    elif sighting.restarted:
        _note_peer_restarted(sender)

    # Before reassembly and before delivery, on the RAW packet: a relay
    # forwards fragments blind, without holding the whole message, and it
    # forwards the bytes exactly as they arrived apart from the hop count.
    _consider_relay(data, inbound, snr, link)

    data = inbound.frame

    try:
        frame = _reassembler.offer(data, sender=sender)
        if frame is None:
            return  # a fragment; nothing whole yet
        if cot_codec.is_bundle(frame):
            # Several frames shared one packet's header and one envelope. They
            # are unpacked here rather than deeper down so that everything
            # after this point handles exactly the frames it always did, with
            # no idea it was ever bundled.
            try:
                inner = cot_codec.unbundle(frame)
            except cot_codec.CotDecodeError as e:
                RNS.log(f"[cot-bridge] dropping malformed bundle: {e}",
                        RNS.LOG_ERROR)
                return
            RNS.log("[cot-bridge] bundle from %s carried %d frames"
                    % (_peer_label(sender), len(inner)), RNS.LOG_DEBUG)
            for one in inner:
                _handle_frame(one, rssi, snr, sender, link)
            return
        _handle_frame(frame, rssi, snr, sender, link)
    except Exception as e:
        RNS.log(f"[cot-bridge] inbound handling failed: {e}", RNS.LOG_ERROR)


def _iface_for(link):
    """The interface object for a link name, or None if it is not built."""
    return _link_ifaces.get(link)


def _set_active_link(link):
    """Which interface currently carries broadcast/periodic traffic, and is
    reachable through RNS.Packet's own implicit broadcast to every
    registered interface.

    Toggles .OUT so RNS.Transport.outbound()'s broadcast branch -- which,
    for this app's PLAIN destination, sends on EVERY registered interface
    where .OUT and .online are both true when packet.attached_interface is
    unset (always true here) -- reaches only the intended link when LoRa or
    WiFi send via RNS.Packet(...).send(), rather than silently duplicating
    onto every built interface at once. .IN stays True on all of them,
    unconditionally, whenever each is built: receiving costs nothing, and
    the whole point of a multi-link node is that every built link stays
    live for receive regardless of which one is currently active for
    transmit. FM's own sends never go through RNS.Packet at all (see
    _transmit_sealed) so this guard only ever matters for LoRa/WiFi's
    path -- but it costs nothing to set every link's .OUT correctly
    regardless.

    Safe to call with any subset of links built: a link with no entry in
    _link_ifaces is simply skipped, same as today's "LoRa-only/FM-only
    mode is a no-op on the other side" behaviour, just generalised to any
    number of links instead of exactly two.

    Also corrects every already-discovered WiFi peer's own .OUT, not
    only the parent AutoInterface's -- a real bug, confirmed live
    (2026-09-25, both phones): AutoInterface.add_peer() spawns a
    SEPARATE AutoInterfacePeer sub-interface per discovered peer and
    copies `.OUT` from the parent exactly ONCE, at spawn time
    (`spawned_interface.OUT = self.OUT`), never again. A peer discovered
    while WiFi was NOT the active link (its parent's `.OUT` already
    False at that moment, e.g. during dual-mode's initial
    non-WiFi-preferred startup pick, or any later failover away from
    WiFi) is born with `.OUT = False` baked in -- and since this
    function's own loop, before this fix, only ever touched the PARENT
    interface objects in `_link_ifaces`, never their nested
    `spawned_interfaces`, that peer stayed permanently unable to
    transmit even after WiFi was later promoted back to active. Confirmed
    exactly this sequence live: a peer was added at T+0s while FM was
    still active, WiFi was not promoted until T+30s, and the peer's own
    `.OUT` never recovered -- both phones showed a real, crash-free,
    fully-registered peer and periodic beacons sending on schedule, yet
    neither ever received anything from the other.
    """
    global _active_link
    _active_link = link
    for name, iface in _link_ifaces.items():
        if iface is not None:
            iface.OUT = (name == link)
            spawned = getattr(iface, "spawned_interfaces", None)
            if spawned:
                for peer_iface in spawned.values():
                    peer_iface.OUT = (name == link)


def _startup_active_link(intended_link, built_links):
    """Which link start() should actually make active, once every radio's
    build has been attempted -- correcting `intended_link` (the ORIGINAL
    transport's own link, chosen before any build was even attempted) for
    whichever ones actually came up.

    `built_links` must already be filtered to `_LINK_PRIORITY` order
    (LoRa, FM, WiFi) -- see start()'s own use of this. Two independent
    reasons this correction has to happen, not just one:

    1. `intended_link` may never have been built at all -- e.g. no RNode
       attached, so LoRa never came up. `_set_active_link(intended_link)`
       on its own tolerates this fine (see its own docstring), but
       `_check_auto_switch()` -- the ONLY thing that would otherwise ever
       revisit this choice -- no-ops entirely with fewer than two links
       built ("nothing to switch between"), so a wrong initial guess on a
       single-link node would never self-correct for the rest of the
       process. Confirmed live: launching with no LoRa/FM hardware at all
       left `_active_link` on "lora" forever even once WiFi came up.
    2. Every `_build_*_interface()` unconditionally sets its OWN
       `iface.OUT = True` right after construction (matches every other
       builder), so a link built AFTER `_set_active_link(intended_link)`
       already ran -- which is exactly what happens when WiFi is enabled,
       since it is always built last -- ends up with `.OUT = True`
       alongside whatever the actual active link already correctly has,
       duplicating every broadcast (a beacon going out on LoRa AND WiFi at
       once) until `_check_auto_switch()` first happens to touch
       `_active_link`, which can take its full `_FAILBACK_DEBOUNCE_S` or
       considerably longer if the active link never needs to fail over in
       that window at all.

    Fixing both means start() must call `_set_active_link()` again,
    unconditionally, once every build has been attempted -- reasserting
    the correct `.OUT` state across the FULL final set of built
    interfaces (item 2) using whichever link this function picks
    (item 1).

    WiFi is avoided as the correction target in item 1 when a non-WiFi
    alternative is ALSO built, so it still only ever becomes the active
    link immediately when it is the sole option built at all -- matching
    the "never WiFi as the instant default" intent the WiFi build
    documents, extended to survive this correction instead of being
    undone by it.

    Returns the link to make active, or None if nothing was built at all
    (nothing to correct to, and nothing for _set_active_link() to do
    either).
    """
    if intended_link in built_links:
        return intended_link
    non_wifi_fallback = next((l for l in built_links if l != LINK_WIFI), None)
    if non_wifi_fallback is not None:
        return non_wifi_fallback
    if built_links:
        return built_links[0]
    return None


def _transmit_sealed(sealed, link=None):
    """Send one already mesh-wrapped frame, returning True/False the same
    way RNS.Packet(...).send() does (False specifically, not just falsy --
    callers check `result is False`).

    `link` forces a specific interface regardless of the current default
    (LINK_FM for the escalation path, which needs to reach FM even while LoRa is
    the active link -- the whole point of trying it is that LoRa already failed
    THIS message). None (the ordinary case for beacons, positions and ordinary
    CoT) follows _active_link, which is fixed for the life of the process in
    single-link mode and only varies once dual mode's auto-switch/manual-
    override logic is in play.

    ON FM, CALLS process_outgoing() DIRECTLY instead of
    RNS.Packet(_destination, sealed).send(), and this is load-bearing rather
    than an optimisation: RNS.Packet's own ~19 B of framing is more than a third
    of this link's entire 29 B capacity, and routing a beacon through it pushed
    the real floor to ~47 B -- larger than the whole capacity of any code small
    enough to be worth using here. The link is a single pre-known broadcast
    destination with no multi-hop routing and no RNS-native encryption to lose.

    LoRa still goes through RNS.Packet, unconditionally and deliberately: it
    keeps the ~19 B Reticulum header this project chose to pay so the link stays
    interoperable with standard Reticulum equipment, and that choice does not
    change just because a second link now exists.

    The interface can be None here (mid-build, or simply not built) the same way
    existing callers tolerate via getattr(..., default) -- mirrored with getattr
    rather than a plain attribute access, so a missing interface reports as
    "could not send" instead of crashing the TX loop.
    """
    target = link or _active_link
    if target == LINK_FM:
        send = getattr(_fm_iface, "process_outgoing", None)
        return bool(send and send(sealed))
    if target == LINK_LORA and _radio_firmware == RADIO_MESHTASTIC:
        # Same bypass as FM, same reason: the board is a single broadcast
        # channel, and RNS.Packet's ~19 B would come out of a 231 B packet for
        # routing this link does not do.
        send = getattr(_lora_iface, "process_outgoing", None)
        return bool(send and send(sealed))
    return RNS.Packet(_destination, sealed).send()


def _compute_max_packet():
    """The outer limit one physical transmission can actually carry, on
    whichever interface is PRIMARY -- what start() hands to _max_packet.

    Deliberately keyed on _transport (fixed for the process, "fm" only in
    FM-only mode), not _active_link (which can vary once dual mode's auto-switch
    is wired up): ordinary CoT sizing budgets against the primary link even
    while temporarily failed over, because failing over is meant to look like a
    delivery problem for THAT traffic, not a standing change to how everything
    is sized. The FM-specific paths budget against _max_payload_fm/
    _max_packet_fm instead (see start()).

    A LINK THAT DOES NOT CROSS RNS.Packet IS ASKED, NOT ASSUMED.
    RNS.Packet.PLAIN_MDU is LoRa's ~464 B budget and describes that interface's
    capacity only; FM traffic never crosses RNS.Packet at all. Sizing FM against
    PLAIN_MDU anyway is what let the very first live FM beacon (42 B) sail past
    a 30 B-capacity code with no complaint from RNS, reach the interface, and
    only fail there -- taking the whole link offline on its first ever send.
    FmInterface exposes its own real per-burst capacity as HW_MTU (pure
    arithmetic, see fm_link.hw_mtu()) specifically so this can ask.
    """
    if _transport == "fm":
        return min(cot_codec.RNS_MAX_PAYLOAD, _fm_iface.HW_MTU)
    if _radio_firmware == RADIO_MESHTASTIC:
        # Arithmetic, not a question to the interface: it is the measured
        # packet limit and must hold even when the board is not up yet.
        return min(cot_codec.RNS_MAX_PAYLOAD, mt_link.hw_mtu())
    return min(cot_codec.RNS_MAX_PAYLOAD, RNS.Packet.PLAIN_MDU)


def _handle_frame(frame, rssi, snr, sender, link=LINK_LORA):
    """One complete, unbundled frame from `sender`, heard on `link`."""
    try:
        if cot_codec.is_beacon(frame):
            _handle_beacon(frame, rssi, snr, sender, link)
            return
        if cot_codec.is_ack(frame):
            _handle_ack(frame, sender)
            return
        if cot_codec.is_partial(frame):
            _handle_partial(frame, sender)
            return
        wrapper = None
        if cot_codec.is_reliable(frame):
            # The envelope's authenticated sender IS the origin since v15 --
            # the wrapper no longer carries its own copy, so there is nothing
            # left for _reject_impostor to disagree with.
            wrapper = cot_codec.unwrap_reliable(frame, sender)
            frame = wrapper.frame
            # Acknowledge FIRST, and unconditionally. Hearing this message
            # twice means our previous ack is what went missing, so staying
            # quiet on a duplicate guarantees another retransmission.
            _queue_ack(wrapper, link)
            if link == LINK_FM:
                # Recorded even for a duplicate, and deliberately BEFORE the
                # dedup check below returns: hearing the same message again
                # means the sender has not learned we got it, so our report is
                # exactly what is missing. Same reasoning the ack path above
                # uses for acking duplicates unconditionally.
                with _fm_heard_lock:
                    tag = cot_codec.heard_tag(wrapper.origin, wrapper.msg_id)
                    if tag not in _fm_heard:
                        _fm_heard.append(tag)
                # Report it now rather than on the next scheduled beacon -- see
                # "THE EARLY 'I GOT IT' BEACON". A duplicate asks too, for the
                # same reason it is tagged above.
                _request_confirm_beacon()
            if not _reliable_rx.should_deliver(wrapper.origin, wrapper.msg_id):
                RNS.log(
                    f"[cot-bridge] duplicate of message {wrapper.msg_id} from "
                    f"{_peer_label(wrapper.origin)}: acked again, not re-injected",
                    RNS.LOG_DEBUG,
                )
                return
    except cot_codec.CotDecodeError as e:
        RNS.log(f"[cot-bridge] dropping {len(frame)}-byte frame: {e}", RNS.LOG_ERROR)
        return
    except Exception as e:
        RNS.log(f"[cot-bridge] codec bug decoding {len(frame)} bytes: {e}", RNS.LOG_ERROR)
        return

    try:
        _decode_and_deliver_compact(frame, sender, wrapper)
    except cot_codec.CotDecodeError as e:
        RNS.log(f"[cot-bridge] dropping {len(frame)}-byte frame: {e}", RNS.LOG_ERROR)
    except Exception as e:
        RNS.log(f"[cot-bridge] codec bug decoding {len(frame)} bytes: {e}", RNS.LOG_ERROR)


def _decode_and_deliver_compact(frame, sender, wrapper=None):
    """Decode a compact/position/marker frame (T_COMPACT, T_ZCOMPACT, ... --
    anything decode() accepts, NOT a beacon/ack/partial) from `sender` and
    deliver the resulting CoT to ATAK.

    Shared by two callers that need the exact same bookkeeping, not just
    the same decode call: the generic inbound path (_handle_frame, above)
    and a position delta piggybacked directly on a beacon (_handle_beacon,
    added 2026-09-22) -- both hand this the identical wire format, the
    second just arrived riding a different frame. Raises CotDecodeError/
    Exception on a malformed frame; each caller decides how loud to be
    about that (a whole frame lost is louder than a piggyback that didn't
    pan out on top of a beacon that otherwise succeeded).
    """
    # The decode session is per origin and the origin is authenticated, so
    # a frame can only ever resolve its identity binding and delta base
    # against the node that actually bound them.
    with _sa_lock:
        session = _sa_rx.get(sender)
        if session is None:
            session = _sa_rx[sender] = cot_codec.SaRx()
        xml = cot_codec.decode(frame, session, _MARKER_STALE_S)
        # The uid this origin's self-SA stream is bound to: only that origin's
        # own position reports carry it (a marker's uid is the marker's, a
        # chat's is the chat's). See the peer-position note below.
        sa_uid = session.uid

    # Remember what we hand to ATAK so that, if ATAK ever reflects it back to
    # us, the policy recognises it instead of putting it back on the air --
    # a loop that would grow with every node on the mesh.
    try:
        summary = cot_codec.summarize(xml)
        _tx_policy.note_injected(summary)
        # Free by-products of traffic we were relaying anyway: which node a
        # given ATAK uid lives on, so a direct chat can be addressed properly.
        if wrapper is not None:
            _learn_from_reliable(wrapper, summary)
        _learn_from_self_sa(summary)
        # Where the OTHER end was. Keyed by `sender` -- the authenticated node
        # id this frame arrived under, and the same key the signal readings are
        # recorded against, so the join cannot mismatch.
        #
        # Decided on the SELF-SA SESSION: an event is the sender's own position
        # when its uid is the uid that sender's self-SA stream is bound to.
        # Only that stream binds a uid (cot_codec encodes an event through the
        # session only when it carries <takv>, i.e. describes the device
        # itself), so a marker's uid -- the marker's -- and a chat's never
        # match.
        #
        # Not summary.is_position: that tests for <takv>, which the compact
        # codec strips, so every decoded position would be judged "not a
        # position". And NOT the frame kind, which is what this was until
        # 2026-10-20: the comment here claimed the kind "excludes markers
        # correctly", but a placed marker and a chat are compact frames too
        # (test_cot_codec.MARKER and a direct chat both encode to kind 7 once
        # the reliable wrapper is off), so every pin a peer dropped was booked
        # as where that peer was -- and kept below as the marker to WITHDRAW
        # when the peer went quiet, which would have deleted the pin from the
        # local map and left the departed peer's own dot behind. _hops_for()
        # also tests the kind but is not affected: it sees the frame as sent,
        # and markers and chat go out inside the reliable wrapper.
        if sa_uid is not None and summary.uid == sa_uid:
            # Which node this ATAK uid lives on, EXACTLY: the origin is
            # authenticated and only its own self-SA binds the session uid.
            # _learn_from_self_sa() above never fires on a received position
            # (it tests <takv>, which the codec strips -- the same trap as
            # is_position), so until a peer had CHATTED us its uid was unknown,
            # a direct chat to it was registered "any peer may ack", and a
            # relay's ack settled it (Stage 3 bench, 2026-10-23).
            _note_uid_mapping(sa_uid, sender, "position")
            _rangetest.note_position(sender, summary.lat, summary.lon,
                                     stamp=summary.time)
            # The Coverage tab's peer footprints.
            _note_peer_position(sender, summary.lat, summary.lon)
            # Kept so it can be WITHDRAWN. ATAK is now told to hold this
            # marker for _MARKER_STALE_S, which is longer than the peer table
            # takes to notice silence -- so something has to take it off the
            # map when a node stops answering, and this is what that something
            # re-sends, expired. A pin or a chat here would be withdrawn in
            # the departed peer's place.
            with _marker_lock:
                _last_marker[sender] = xml
    except Exception:
        pass  # never let bookkeeping block delivery

    RNS.log(f"[cot-bridge] received {len(frame)}-byte frame -> {len(xml)} bytes of CoT for ATAK", RNS.LOG_DEBUG)
    _send_to_atak(xml, "%d bytes of CoT" % len(xml))


def _send_to_atak(xml, what):
    """Hand one CoT document to the local ATAK. Returns True if it went."""
    with _cot_socket_lock:
        if _cot_socket is None:
            RNS.log(f"[cot-bridge] no CoT socket, dropping {what}", RNS.LOG_WARNING)
            return False
        try:
            _cot_socket.sendall(struct.pack(">I", len(xml)) + xml)
            _cot_dump("TO ATAK", xml)
            return True
        except OSError as e:
            RNS.log(f"[cot-bridge] failed writing {what} to CoT socket: {e}",
                    RNS.LOG_ERROR)
            return False


def _withdraw_departed():
    """Take off the map any peer the table has given up on.

    This is the other half of extending the stale time on receipt. ATAK is told
    to hold a marker for _MARKER_STALE_S, which is deliberately longer than the
    peer table waits before calling a node disconnected -- so without this, a
    node that died would linger on the map for the longer of the two timers and
    the extension would have bought airtime at the cost of showing people who
    are not there.

    Runs off the peer table rather than a timer of its own, so "gone" means
    exactly what the node list on the status screen means. One place decides.
    """
    connected = {p["node_id"] for p in _peer_snapshot() if p["connected"]}
    with _marker_lock:
        departed = [n for n in _last_marker if n not in connected]
        stale_xml = [(n, _last_marker.pop(n)) for n in departed]
    for node_id, xml in stale_xml:
        try:
            expired = cot_codec.expire(xml)
        except Exception as e:
            RNS.log(f"[cot-bridge] could not expire marker for {node_id}: {e}",
                    RNS.LOG_WARNING)
            continue
        if _send_to_atak(expired, "marker withdrawal"):
            RNS.log("[cot-bridge] %s has gone quiet -- withdrawing its marker"
                    % _peer_label(node_id), RNS.LOG_NOTICE)


def _note_peer_transitions():
    """Log the instant a peer connects or disconnects, independent of
    whether it ever sent a markable position.

    _withdraw_departed's own condition -- a marker to expire -- misses a
    beacon-only peer (no GPS fix) entirely; without this, that peer's DOWN
    state is only ever visible as a label in the NEXT heartbeat's peer list,
    with nothing to say when it actually happened. A first-time peer is
    already announced in real time by _note_new_peer, so it is deliberately
    excluded here (via _ever_connected_peer_ids) rather than also logged as
    "is back", which it is not.
    """
    global _last_connected_peer_ids, _ever_connected_peer_ids
    snapshot = {p["node_id"]: p for p in _peer_snapshot()}
    current = {node_id for node_id, p in snapshot.items() if p["connected"]}

    for node_id in _last_connected_peer_ids - current:
        last_link = snapshot[node_id]["last_link"] if node_id in snapshot else None
        RNS.log("[cot-bridge] %s (last heard on %s) has gone quiet"
                % (_peer_label(node_id), last_link or "?"), RNS.LOG_NOTICE)

    for node_id in (current - _last_connected_peer_ids) & _ever_connected_peer_ids:
        p = snapshot[node_id]
        rssi = "%ddBm" % p["rssi"] if p["rssi"] is not None else "rssi ?"
        RNS.log("[cot-bridge] %s is back (link %s, %s)"
                % (_peer_label(node_id), p["last_link"] or "?", rssi),
                RNS.LOG_NOTICE)

    _last_connected_peer_ids = current
    _ever_connected_peer_ids |= current


def _learn_local_callsign(summary):
    """Pick up this device's own ATAK callsign from its outgoing position
    reports, so beacons can advertise a name instead of a hex id.

    Only self-SA is used, identified by the presence of <takv>. ATAK also
    emits a-* events for markers the user places, and adopting a callsign from
    one of those would make this node advertise somebody else's name.
    """
    global _local_callsign
    if not (summary.is_position and summary.is_self and summary.callsign):
        return
    # Trim: ATAK happily accepts a callsign with trailing whitespace ("pixel "
    # was observed on the bench), which then renders as "[pixel ]" in the node
    # list and wastes a byte in every beacon. Only the display/beacon copy is
    # trimmed -- CoT relayed through the codec keeps ATAK's value verbatim.
    callsign = summary.callsign.strip()
    if callsign and callsign != _local_callsign:
        _local_callsign = callsign
        RNS.log(f"[cot-bridge] local callsign is {_local_callsign!r}", RNS.LOG_NOTICE)


# _reject_impostor lived here until v15, and its disappearance is the point.
#
# The beacon, the ack, the partial ack and the reliable wrapper each used to
# carry their own copy of the sender's node id, from back when nothing on this
# link was authenticated and the body was the only place to put it. Once the
# mesh envelope started carrying an authenticated sender, those copies could
# only ever disagree with a better copy of themselves -- so this function
# existed to notice when they did, and every frame paid 7 bytes for the
# privilege.
#
# v15 took the copies off the wire and reads the envelope instead. A body can
# no longer contradict its envelope because there is no longer a second claim
# to contradict it with, which is strictly stronger than checking. What is
# unchanged is the limit stated here before: this was never what makes a node
# id unforgeable. Anyone holding the group key can put whatever they like in
# the envelope, and closing THAT needs per-node signatures.


def _note_peer_frame(sender, rssi, snr, seq, direct=True, hops_away=None,
                     epoch=None, link=None):
    """Record a sighting. Returns a peers.Sighting: is this node new, and does
    it look like it restarted since we last heard it?

    A thin pass-through since the table moved to peers.py -- the reasoning
    about what a relayed frame may and may not be trusted to measure lives
    there now, next to the code that enforces it. `link` is which of this
    node's own radios the frame arrived on -- see peers.PeerTable.note_frame
    for why it is stored unconditionally, direct or relayed, unlike rssi/snr.
    """
    # Every DIRECT frame is a range-test data point, which is why the recorder
    # hangs here rather than off the beacon: beacons are the regular heartbeat
    # but position reports and acks are signal readings too, and at the edge of
    # range the extra samples are exactly where the answer is. Relayed frames
    # are excluded because their signal describes the relay, not the sender --
    # the same distinction peers.py draws for the same reason.
    if direct:
        _record_range_sample(sender, rssi, snr, seq, link)

    # Which board is in the LoRa slot, stamped NOW: an RNode and a Meshtastic
    # board cannot hear each other, so this names the radio that carried the
    # frame -- and a later board swap must not relabel it.
    radio = _radio_firmware if link == LINK_LORA else link
    return _peers.note_frame(sender, rssi=rssi, snr=snr, seq=seq,
                             direct=direct, hops_away=hops_away, epoch=epoch,
                             link=link, radio=radio)


def _neighbour_digest():
    """A Bloom filter of the nodes this one currently hears directly.

    Only connected peers: a node that fell off is no longer coverage this node
    can offer, and advertising it would tell everyone else not to relay for it
    -- the failure that leaves a departed node's traffic unrepeated by anybody.
    """
    return _peers.neighbour_digest()


def _sender_covers_our_neighbours(sender):
    """The coverage test: would relaying this sender's frame reach anybody?

    True means no -- everyone we can hear, the sender can hear too, so a relay
    would be pure cost. That is the ordinary case for a squad standing
    together, and it is what keeps flooding free until the group splits.
    """
    return _peers.sender_covers_our_neighbours(sender)


def _ack_destination(frame):
    """The one node a frame has to reach, or None if it is a broadcast.

    Only acknowledgements have one, and they carry it already: the origin of
    the message being answered IS the node the answer must get back to. No path
    on the wire, no new field -- the destination was always there.
    """
    try:
        if cot_codec.is_ack(frame):
            return cot_codec.decode_ack(frame).origin
        if cot_codec.is_partial(frame):
            return cot_codec.decode_partial(frame).origin
    except cot_codec.CotDecodeError:
        pass
    return None


def _coverage_for(frame, sender):
    """The coverage answer that decides whether this frame is worth relaying.

    A broadcast asks "do I reach anyone the sender does not?". An ack asks the
    narrower question -- "do I reach the one node this is addressed to, when
    the sender cannot?" -- which stops a receipt being forwarded down branches
    of the mesh that nobody in the conversation is on. This is the cheap
    version of what MeshCore buys with source-routed paths, and it costs no
    bytes because an ack already names its destination.

    Falls back to the broadcast test when the targeted one has no opinion,
    which is what keeps a three-hop ack moving: the node in the middle has
    never heard of either end.
    """
    targeted = _peers.covers_target(sender, _ack_destination(frame))
    if targeted is not None:
        return targeted
    return _sender_covers_our_neighbours(sender)


def _relay_message_shape(frame):
    """(msg_id, fragment count) for a fragment, (None, 1) for anything else.

    A relay never reassembles -- it forwards fragments blind -- but it does
    need to decide for the whole message at once. Half a relayed drawing
    cannot be reassembled by anyone, so the airtime is spent for nothing;
    charging the token bucket the whole cost up front is what makes the
    refusal happen before the first fragment rather than during the fourth.
    """
    if len(frame) >= 4 and frame[0] == cot_codec.CTRL_FRAG:
        msg_id, _index, count = struct.unpack(">HBB", frame[1:5])
        return msg_id, max(1, count)
    return None, 1


def _consider_relay(packet, inbound, snr, link_name=LINK_LORA):
    """Should this authenticated frame be forwarded, and when?

    `link_name` is the radio it arrived on. A parameter rather than a global
    because the answer is a property of that radio: FM's is a flat no, and
    reading it off the bridge instead of the link would lose that the moment
    there are two. Defaulted while there is one.
    """
    policy = _relay_policy_for(link_name)
    if policy is None or not inbound.frame:
        return
    frame_id = (inbound.origin, inbound.epoch, inbound.seq)
    sender = mesh.node_id_hex(inbound.origin)
    decision = policy.decide(
        hops=inbound.hops,
        kind=inbound.frame[0] & 0x0F,
        packet_len=len(packet),
        snr=snr,
        covered=_coverage_for(inbound.frame, sender),
        msg_id=_relay_message_shape(inbound.frame)[0],
        fragments=_relay_message_shape(inbound.frame)[1],
    )
    if not decision.relay:
        RNS.log("[cot-bridge] not relaying frame %d from %s (%d hops left, %s): %s"
                % (inbound.seq, _peer_label(sender), inbound.hops,
                   link_name, decision.reason), RNS.LOG_NOTICE)
        return
    forwarded = _mesh.forward(packet) if _mesh is not None else None
    if forwarded is None:
        return
    with _relay_lock:
        # link_name travels with the pending relay, not just the packet --
        # see _relay_due()'s own docstring for why a relay must go back out
        # on the link it was heard on, not whichever one is hardcoded or
        # currently _active_link.
        _relay_pending[frame_id] = (time.monotonic() + decision.delay_s, forwarded, link_name)
        while len(_relay_pending) > _MAX_RELAY_PENDING:
            _relay_pending.popitem(last=False)
    RNS.log("[cot-bridge] relaying frame %d from %s in %.2fs (%d hops left, %s, %s, snr %s)"
            % (inbound.seq, _peer_label(sender),
               decision.delay_s, inbound.hops, link_name, decision.reason,
               "?" if snr is None else snr), RNS.LOG_NOTICE)


def _relay_cancel(frame_id, why):
    """Someone else already put this frame back on the air, so ours would be
    pure duplication. This is the passive cancellation that keeps a single
    radio cell from relaying the same frame once per node."""
    with _relay_lock:
        dropped = _relay_pending.pop(frame_id, None)
    if dropped is not None and _relay_policy is not None:
        _relay_policy.stats["cancelled"] += 1
        RNS.log("[cot-bridge] relay of frame %d from %s cancelled: %s"
                % (frame_id[2], _peer_label(mesh.node_id_hex(frame_id[0])), why),
                RNS.LOG_NOTICE)


def _relay_due():
    """The next relay whose moment has come, as (packet, link_name), or None.

    `link_name` is the link the frame was HEARD on, not this node's own
    `_active_link` -- a relay is somebody else's traffic being put back on
    the same channel it arrived on, not a message this node originated, so
    it must not follow wherever this node's own preferred link currently
    points.
    """
    now = time.monotonic()
    with _relay_lock:
        for frame_id, (send_after, packet, link_name) in _relay_pending.items():
            if now >= send_after:
                _relay_pending.pop(frame_id)
                RNS.log("[cot-bridge] relay of frame %s handed to %s"
                        % (frame_id[2], link_name), RNS.LOG_NOTICE)
                return packet, link_name
    return None


def _note_new_peer(sender):
    """A node we have not heard from may never have heard us bind, and a fixed
    keyframe interval serves that case worst -- one joining just after a
    keyframe would see nothing but deltas it cannot resolve until the next one.
    Restating identity costs 32 bytes once."""
    with _sa_lock:
        _sa_tx.force_keyframe()
    RNS.log(
        f"[cot-bridge] first frame from {sender}: identity keyframe queued",
        RNS.LOG_NOTICE,
    )


def _note_peer_restarted(sender):
    """A peer whose epoch changed has rebooted, and its decode state went with
    it -- so every delta this node sends now references a base that peer no
    longer holds, and every position it sends us is dropped.

    Until 2026-08-12 nothing covered this. A keyframe was forced only for a
    node that was NEW, and a node that restarts is one we already know, so the
    recovery was whatever the fixed keyframe interval happened to be: up to
    five minutes of a peer's positions dropped with
    `delta frame references generation N, last base is None`. Reinstalling on
    the bench hit it every time, and a node rebooting in the field is the same
    event.

    The epoch is the right signal and was already on every frame. It is
    authenticated, it changes on exactly this event, and it costs nothing to
    read -- unlike a sequence going backwards, which a reordered frame can fake.

    Also drops what we knew about DECODING that peer: its sequence restarted,
    so the session we hold describes a generation it has forgotten.
    """
    with _sa_lock:
        _sa_tx.force_keyframe()
        _sa_rx.pop(sender, None)
    RNS.log(
        f"[cot-bridge] {_peer_label(sender)} restarted: identity keyframe "
        f"queued and its decode session dropped",
        RNS.LOG_NOTICE,
    )


def _handle_beacon(frame, rssi, snr, sender, link=LINK_LORA):
    """A beacon arrived: the radio link is proven working, in this direction,
    right now. Logged loudly because its absence is the thing that is hard to
    diagnose.

    `sender` is the node id from the mesh envelope, covered by the
    authentication tag. Since v15 the beacon body carries no id of its own, so
    this is the only claim there is."""
    try:
        beacon = cot_codec.decode_beacon(frame, sender)
    except cot_codec.CotDecodeError as e:
        RNS.log(f"[cot-bridge] malformed beacon: {e}", RNS.LOG_WARNING)
        return
    _peers.note_beacon(beacon.node_id, seq=beacon.seq, name=beacon.name,
                       callsign=beacon.callsign, digest=beacon.digest)

    # A peer telling us which of our messages reached it -- the FM link's only
    # delivery signal, and the only thing that can end a blind resend schedule
    # early. Logged at NOTICE because on a link that confirms nothing else, this
    # is the one moment worth seeing.
    #
    # THIS IS THE CALLER fm_resend.note_heard() HAS BEEN WAITING FOR. Phase 2a
    # restored the scheduler with the cancel path working and nothing wired to
    # it; this is the wiring. Whether a 17 B beacon survives often enough to
    # confirm anything is still unmeasured on hardware -- that measurement gates
    # Phase 2b.
    # LOGGED FOR EVERY BEACON, NOT ONLY FM ONES, AND IT NAMES `link`.
    #
    # The first version of this diagnostic sat inside `if link == LINK_FM`,
    # which is the condition it was written to test -- so when the block did
    # not run, it also said nothing about why, and the evening was spent
    # proving by elimination that `link` was the problem. A probe gated on
    # the hypothesis it is testing cannot report the hypothesis failing.
    if beacon.heard or (link == LINK_FM and _fm_resend is not None):
        def _hex0(t):
            return t.hex() if isinstance(t, (bytes, bytearray)) else str(t)
        with _reliable_lock:
            _pend = (_fm_resend.pending_tags()
                     if _fm_resend is not None else {})
        RNS.log(
            "[cot-bridge] beacon from %s on link=%r: heard=[%s] "
            "fm_pending=[%s]"
            % (_peer_label(beacon.node_id), link,
               ", ".join(_hex0(t) for t in (beacon.heard or ())) or "-",
               ", ".join("%s:%s" % (_hex0(t), l) for t, l in _pend.items())
               or "-"),
            RNS.LOG_NOTICE,
        )

    if link == LINK_FM:
        # BOTH SIDES OF THE COMPARISON ARE LOGGED, and that is the point.
        #
        # Measured 2026-10-03, the first hardware run with two phones: beacons
        # went out saying "digest refresh" and carrying real tags, the peer
        # received them, and NOTHING was ever cancelled -- 0 occurrences of
        # the notice below across 30 minutes. The logs could not say whether
        # the digest arrived empty or arrived full of tags that did not match
        # anything pending, because only the SUCCESS path said anything at
        # all. Those two failures want opposite fixes and looked identical.
        #
        # This is the same silent-chain hazard Phase 4 flagged when it wired
        # note_heard up: "a break anywhere in that chain is silent -- the
        # schedule just runs to completion, which is also what a legitimately
        # lost report looks like". It was flagged, and then it happened.
        def _hex(t):
            return t.hex() if isinstance(t, (bytes, bytearray)) else str(t)

        with _reliable_lock:
            pending = _fm_resend.pending_tags()
            confirmed = _fm_resend.note_heard(beacon.heard) if beacon.heard \
                else []
        if beacon.heard or pending:
            RNS.log(
                "[cot-bridge] FM digest from %s: heard=[%s] pending=[%s] "
                "cancelled=%d"
                % (_peer_label(beacon.node_id),
                   ", ".join(_hex(t) for t in (beacon.heard or ())) or "-",
                   ", ".join("%s:%s" % (_hex(t), l)
                             for t, l in pending.items()) or "-",
                   len(confirmed)),
                RNS.LOG_NOTICE,
            )
        for label_done in confirmed:
            RNS.log(
                f"[cot-bridge] {label_done} reached "
                f"{_peer_label(beacon.node_id)} -- no further attempts needed",
                RNS.LOG_NOTICE,
            )

    _sizes_heard.add(len(frame))
    label = beacon.name or beacon.node_id

    # The noise floor rides along with every heard frame, because signal
    # strength on its own does not say whether a link is about to fail. -95 dBm
    # over a -125 floor is a healthy link; the same -95 dBm over a -90 floor is
    # already lost. On the 2026-08-14 walk only the first half of that was
    # recorded, which is why the blackout could not be attributed.
    conditions = _channel_conditions()
    floor = None if conditions is None else conditions["noise_floor"]
    margin = ""
    if floor is not None:
        margin = " floor=%d dBm" % floor
        if rssi is not None:
            # Named "over floor" rather than folded into snr: this is a crude
            # level difference, while snr above is measured on the despread
            # signal and can legitimately be negative on a link that works.
            margin += " (%+d dB over floor)" % (rssi - floor)

    RNS.log(
        "[cot-bridge] LINK OK: heard peer %s%s seq=%d frame=%dB rssi=%s dBm snr=%s dB%s"
        % (label,
           " [%s]" % beacon.callsign if beacon.callsign else "",
           beacon.seq, len(frame),
           "?" if rssi is None else rssi,
           "?" if snr is None else snr,
           margin),
        RNS.LOG_NOTICE,
    )

    # 2026-09-22: a position DELTA piggybacked on this beacon -- see
    # _fm_last_position_delta's own comment in the sender's _beacon_loop
    # for why (this bench has consistently measured the beacon itself far
    # more reliable than a position's own separate transmission). Decoded
    # through the exact same path a normal inbound position frame uses, so
    # it updates the peer table and reaches ATAK identically either way.
    # Quiet on failure, deliberately -- the beacon's own job (proving the
    # link works, reporting identity) already succeeded above regardless
    # of whether this optional extra passenger decoded, so this does not
    # deserve the same log level as a whole frame going missing.
    if beacon.position:
        try:
            _decode_and_deliver_compact(beacon.position, sender)
        except cot_codec.CotDecodeError as e:
            RNS.log(f"[cot-bridge] piggybacked position from {label} "
                    f"did not decode: {e}", RNS.LOG_DEBUG)
        except Exception as e:
            RNS.log(f"[cot-bridge] codec bug decoding piggybacked position "
                    f"from {label}: {e}", RNS.LOG_DEBUG)


def _peer_label(node_id):
    """Name a peer the way LINK OK, the status line and the UI all do: the
    device's own name, falling back to its id. Printing raw hex in one place
    and a name in another makes the same node look like two."""
    return _peers.label(node_id)


def _peer_labels(node_ids):
    return _peers.labels(node_ids)


# Where each peer last SAID it was, for the Coverage tab's peer footprints.
# Fed from the same place as the range-test book (a decoded position frame,
# never a marker or a chat -- see _decode_and_deliver_compact), but kept apart
# from it because starting a range test deliberately wipes that book, and
# the coverage map should not lose every peer when someone presses Record.
_PEER_POSITION_MAX_AGE_S = rangetest.DEFAULT_MAX_FIX_AGE_S   # 15 min
_PEER_POSITION_MAX = 32
_peer_positions = collections.OrderedDict()   # node_id -> (lat, lon, monotonic)
_peer_positions_lock = threading.Lock()


def _note_peer_position(node_id, lat, lon):
    """Never raises: an observer must not break the receive path."""
    try:
        lat, lon = float(lat), float(lon)     # the debug hook passes strings
        if node_id == _NODE_ID or not rangetest._valid(lat, lon):
            return
        with _peer_positions_lock:
            _peer_positions[node_id] = (lat, lon, time.monotonic())
            _peer_positions.move_to_end(node_id)
            while len(_peer_positions) > _PEER_POSITION_MAX:
                _peer_positions.popitem(last=False)
    except Exception:
        pass


def peer_positions():
    """Kotlin entry point: JSON list of peers' last REPORTED positions, newest
    first, each {node_id, name, lat, lon, age_s}. A report older than
    _PEER_POSITION_MAX_AGE_S is left out -- a footprint drawn from where
    somebody was a quarter of an hour ago is a guess, not a map. Never raises.
    """
    try:
        now = time.monotonic()
        with _peer_positions_lock:
            items = list(_peer_positions.items())
        out = []
        for node_id, (lat, lon, at) in reversed(items):
            age = now - at
            if age > _PEER_POSITION_MAX_AGE_S:
                continue
            out.append({"node_id": node_id, "name": _peer_label(node_id),
                        "lat": lat, "lon": lon, "age_s": round(age, 1)})
        return json.dumps(out)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})


def peers_awaiting_position():
    """Kotlin entry point: JSON list of peers heard recently (within
    _PEER_CONNECTED_S) that peer_positions() has nothing current for, each
    {node_id, name}, so the Coverage tab can say whose footprint is missing
    and why. A beacon carries no position: until a node's ATAK has a GPS fix
    it is on the mesh and off the map (seen 2026-10-20, both phones, for the
    first minutes after a restart). Never raises.
    """
    try:
        now = time.monotonic()
        with _peer_positions_lock:
            placed = {n for n, (_, _, at) in _peer_positions.items()
                      if now - at <= _PEER_POSITION_MAX_AGE_S}
        return json.dumps([{"node_id": n, "name": _peer_label(n)}
                           for n in _peers.connected()
                           if n != _NODE_ID and n not in placed])
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})


def _note_uid_mapping(atak_uid, node_id, how):
    """Record that an ATAK uid lives on a given node. Never raises."""
    if not atak_uid or not node_id:
        return
    try:
        with _uid_map_lock:
            if _atak_uid_to_node.get(atak_uid) == node_id:
                return
            _atak_uid_to_node[atak_uid] = node_id
            _atak_uid_to_node.move_to_end(atak_uid)
            while len(_atak_uid_to_node) > _MAX_UID_MAP:
                _atak_uid_to_node.popitem(last=False)
        RNS.log(f"[cot-bridge] {atak_uid} is on node {_peer_label(node_id)} (via {how})",
                RNS.LOG_DEBUG)
    except Exception as e:
        RNS.log(f"[cot-bridge] could not record uid mapping: {e}", RNS.LOG_DEBUG)


def _learn_from_reliable(wrapper, summary):
    """Exact mapping, free: a reliable frame carries its sender's node id, and
    a chat uid carries its sender's ATAK uid."""
    if summary is None:
        return
    _note_uid_mapping(cot_codec.chat_sender(summary.uid), wrapper.origin, "chat")


def _learn_from_self_sa(summary):
    """Fallback mapping, also free: a device's own position report carries its
    ATAK uid and callsign, and beacons carry callsign against node id.

    Only for a document that still carries <takv>: a position decoded from
    the compact codec has lost it, and is mapped exactly from its self-SA
    session in the receive path instead (2026-10-23). This remains for an
    uncompressed self-SA. Ambiguous callsigns are skipped
    rather than guessed: two nodes answering to the same name would otherwise
    silently misroute the ack expectation.
    """
    if not (summary and summary.is_self and summary.callsign and summary.uid):
        return
    callsign = summary.callsign.strip()
    matches = _peers.by_callsign(callsign)
    if len(matches) == 1:
        _note_uid_mapping(summary.uid, matches[0], "callsign")


def _chat_addressee_node(uid):
    """The node a direct chat is addressed to, or None for a room message, an
    unknown peer, or anything that isn't a chat."""
    dest = cot_codec.chat_addressee(uid)
    if not dest:
        return None
    with _uid_map_lock:
        return _atak_uid_to_node.get(dest)


def _hops_away(node_id):
    """How many transmissions it takes to reach this peer. 1 means adjacent.

    A live direct link wins over any remembered distance: hearing a node
    first-hand is proof of adjacency, while `hops_away` is only the last thing
    observed and may describe a relayed copy that arrived while the direct one
    was lost to interference. Falls back to 1 for anyone unknown, which keeps
    the timing exactly as it was before relaying existed.
    """
    return _peers.hops_away(node_id)


def _connected_peer_count():
    """How many peers could plausibly answer right now. Sizes the ack jitter
    window; runs on the RNode read thread, so it stays O(number of nodes)."""
    return _peers.connected_count()


def _queue_ack(wrapper, link=LINK_LORA):
    """Acknowledge a reliable message heard on `link`. Runs on the RNode
    read thread, so it only enqueues -- the jitter is applied by the TX
    loop, not slept here."""
    global _ack_not_ours
    # NOTHING IS ACKNOWLEDGED ON FM, and this is the enforcement point for
    # the constraint fm_interface.py's docstring states. A sender on that
    # link never registers with reliable.ReliableTx (see _build_fm_oneshot),
    # so an ack would have nothing to settle -- it would be pure airtime on
    # a half-duplex ~500 bps channel, sent once per received frame, against
    # a sender that is not listening for it. Duplicate suppression does NOT
    # depend on this: ReliableRx.should_deliver() keys on (origin, msg_id)
    # independently of whether anything was ever acked, which is what makes
    # blind resending safe. Gated on the FRAME's own link, not the node's
    # current default (_active_link): a frame that arrived on FM never earns an
    # ack regardless of which link this node currently prefers to transmit on.
    if link == LINK_FM:
        return
    # A frame that names its addressee is owed an answer by that node alone.
    # Everyone else stays silent: those acks were never counted by the sender
    # and, at 16 nodes, cost about twice the airtime of the message that
    # provoked them. Only a NAMED other node silences us -- an unaddressed
    # frame still gets the unconditional ack it always did, because absence of
    # a name means the sender wanted anyone who heard it to answer.
    if not reliable.owes_ack(wrapper.addressee, _NODE_ID):
        _ack_not_ours += 1
        RNS.log("[cot-bridge] message %d from %s is addressed to %s, not acking"
                % (wrapper.msg_id, _peer_label(wrapper.origin),
                   _peer_label(wrapper.addressee)),
                RNS.LOG_DEBUG)
        return
    try:
        frame = cot_codec.encode_ack(wrapper.origin, wrapper.msg_id, _NODE_ID)
    except Exception as e:
        RNS.log(f"[cot-bridge] could not build ack: {e}", RNS.LOG_ERROR)
        return
    window = tx_policy.jitter_window(
        _connected_peer_count(), _ACK_SLOT_S, _ACK_JITTER_MAX_S)
    send_after = time.monotonic() + random.random() * window

    # Only a message whose sender said "any one ack will do" may be cancelled
    # by somebody else's. A direct chat is owed an answer by one named node,
    # and a bystander speaking first must not silence it.
    key = (wrapper.origin, wrapper.msg_id) if wrapper.any_ack else None

    with _ack_pending_lock:
        _ack_pending[key or (wrapper.origin, wrapper.msg_id, _NODE_ID)] = (
            send_after, frame, key is not None)
        while len(_ack_pending) > _MAX_ACK_PENDING:
            # Shedding the oldest is survivable: the sender retransmits and we
            # ack the copy. It means the ack lane is saturated, which it should
            # never be at 17 bytes a message.
            _ack_pending.popitem(last=False)
            RNS.log("[cot-bridge] ack table full, dropped an ack", RNS.LOG_WARNING)


# How long a reassembly must be quiet before we ask for the missing pieces.
# Short enough to arrive before the sender's retry timer, so the report can
# still narrow that retry rather than turn up after it -- reliable.py's floor
# is 2 s plus the relay allowance, and a stall is only declared once nothing
# has arrived, which on a paced multi-fragment burst already means the burst
# is over.
_REASSEMBLY_STALL_S = 1.5


def _report_stalled_reassemblies():
    """Tell senders which fragments of theirs never arrived.

    Speculative, and cheap on purpose: ~20 bytes against the ~0.4 s a
    redundant fragment costs to resend, so it pays for itself the first time it
    prevents one. Rate-limited inside the reassembler, which reports a given
    message at most once per stall and forgets it said anything the moment
    another fragment arrives.
    """
    if _mesh is None:
        return
    try:
        stalled = _reassembler.stalled(_REASSEMBLY_STALL_S)
    except Exception as e:
        RNS.log(f"[cot-bridge] could not scan for stalled reassemblies: {e}",
                RNS.LOG_ERROR)
        return
    for origin, msg_id, count, have in stalled:
        try:
            frame = cot_codec.encode_partial(origin, msg_id, _NODE_ID,
                                             count, have)
        except Exception as e:
            RNS.log(f"[cot-bridge] could not build partial ack: {e}", RNS.LOG_ERROR)
            continue
        _enqueue([frame])
        RNS.log(
            f"[cot-bridge] asking {_peer_label(origin)} for fragments "
            f"{sorted(set(range(count)) - have)} of message {msg_id} "
            f"({len(have)}/{count} held)",
            RNS.LOG_NOTICE,
        )


def _handle_partial(frame, sender):
    """A peer holds some of one of our fragmented messages and wants the rest.

    Not an acknowledgement: it never completes a message and never refreshes
    the retry timer. It only narrows WHAT the next retry sends.
    """
    try:
        partial = cot_codec.decode_partial(frame, sender)
    except cot_codec.CotDecodeError as e:
        RNS.log(f"[cot-bridge] malformed partial ack: {e}", RNS.LOG_WARNING)
        return
    if partial.origin != _NODE_ID:
        return  # broadcast, so we hear reports meant for other senders

    with _reliable_lock:
        _reliable_tx.on_partial(partial)
    RNS.log(
        f"[cot-bridge] {_peer_label(partial.acker)} has {len(partial.have)}/"
        f"{partial.count} fragments of message {partial.msg_id}; "
        f"missing {sorted(set(range(partial.count)) - partial.have)}",
        RNS.LOG_NOTICE,
    )


def _cancel_pending_ack(origin, msg_id, acker):
    """Drop our own queued ack because somebody else already sent one.

    Only ever cancels an ack the SENDER marked as suppressible -- see the
    any_ack flag in cot_codec. Entries that are not suppressible are stored
    under a different key and cannot be reached from here at all, which is
    what keeps a direct chat's addressee from being silenced by a bystander.
    """
    global _ack_suppressed
    with _ack_pending_lock:
        if _ack_pending.pop((origin, msg_id), None) is not None:
            _ack_suppressed += 1
            RNS.log("[cot-bridge] ack for %s's message %d suppressed -- %s "
                    "answered first" % (_peer_label(origin), msg_id,
                                        _peer_label(acker)),
                    RNS.LOG_DEBUG)


def _handle_ack(frame, sender):
    try:
        ack = cot_codec.decode_ack(frame, sender)
    except cot_codec.CotDecodeError as e:
        RNS.log(f"[cot-bridge] malformed ack: {e}", RNS.LOG_WARNING)
        return

    if ack.origin != _NODE_ID:
        # Somebody else has answered this message. If we were about to say the
        # same thing, do not: this is the whole saving, and it is the same
        # cancellation the relay does when it hears another node forward a
        # frame it was about to forward itself.
        _cancel_pending_ack(ack.origin, ack.msg_id, ack.acker)

        # Every frame here is broadcast, so we hear acks addressed to other
        # senders. Not ours, and counting them would make ack_unknown useless.
        RNS.log(
            f"[cot-bridge] ack from {_peer_label(ack.acker)} for "
            f"{_peer_label(ack.origin)}'s message {ack.msg_id}, not ours",
            RNS.LOG_DEBUG,
        )
        return

    with _reliable_lock:
        done = _reliable_tx.on_ack(ack)
    if done is not None:
        # ARQ-tracked (ack-based) one-shot traffic only ever rides whichever
        # link is _active_link at the time -- FM never reaches this path at
        # all, it has its own blind-resend schedules instead (see
        # _escalate_to_fm) -- so crediting the delivery to _active_link's
        # own tracker attributes it to the link that actually earned the
        # ack. _link_health.get() rather than an assumed LoRa/WiFi split:
        # a link with no tracker (only FM today) just has nothing to feed.
        health = _link_health.get(_active_link)
        if health is not None:
            health.note_delivered()
        RNS.log(f"[cot-bridge] {_delivered_line(done)}", RNS.LOG_NOTICE)
    else:
        RNS.log(
            f"[cot-bridge] ack for message {ack.msg_id} from {_peer_label(ack.acker)}",
            RNS.LOG_NOTICE,
        )


def _delivered_line(done):
    """Say what a completed message's acknowledgement actually PROVES.

    One ack settles an unaddressed message (reliable.DEFAULT_REQUIRED_ACKS),
    and that ack proves only that the node which sent it holds the message. A
    flat "DELIVERED" read as "everyone has it", and on 2026-10-14 it said so
    about a room chat the only relay had refused: the relay node, also a
    recipient, acked it, and the node beyond it never got it
    (results/2026-10-14_meshtastic_phase2, section 13). So the line names who
    answered, whether that node heard us directly or through a relay, and who
    else was expected and is NOT confirmed.
    """
    ackers = sorted(done.acked & done.expected) or sorted(done.acked)

    def how(node):
        try:
            hops = _peers.hops_away(node)
        except Exception:
            hops = 1
        return "direct" if hops <= 1 else "through a relay"

    who = ", ".join(f"{_peer_label(n)} ({how(n)})" for n in ackers) or "?"
    unconfirmed = sorted(done.expected - done.acked)
    if len(done.expected) <= 1:
        tail = ""
    elif unconfirmed:
        tail = (f" -- confirms only that {'they' if len(ackers) > 1 else 'it'} "
                f"{'have' if len(ackers) > 1 else 'has'} it; NOT confirmed for "
                f"{_peer_labels(unconfirmed)}")
    else:
        tail = " -- every expected node answered"
    return (f"DELIVERED {done.label}: acknowledged by {who} after "
            f"{done.attempts} transmission(s){tail}")


def _state_for(link, iface):
    """UP/DEGRADED/DOWN for one link, honouring whichever health tracker
    (if any) is registered for it in _link_health.

    Not every link has a tracker: FM never gets one (see _link_health's own
    comment) and simply falls back to a hard-signal-only reading here --
    exactly its old role, where FM was treated as viable purely on being
    built and reporting online, with no soft signal feeding it at all.
    """
    online = getattr(iface, "online", False)
    health = _link_health.get(link)
    if health is not None:
        return health.state(online)
    return lora_health.UP if online else lora_health.DOWN


def _summary_for(link, iface):
    health = _link_health.get(link)
    if health is not None:
        return health.summary(getattr(iface, "online", False))
    return _state_for(link, iface)


def _check_auto_switch():
    """Automatic failover/failback across every built link, in priority
    order (_LINK_PRIORITY: LoRa, then FM, then WiFi).

    Called from _reliable_loop's own tick, since that is where the health
    signal's inputs (acks, give-ups) are already being processed -- no
    separate loop needed. No-ops entirely with fewer than two links built
    (nothing to switch between) and entirely under a manual override
    (set_link_override has the final say; every link's health keeps
    computing underneath regardless, so clearing the override later
    resumes from a current reading, not a stale one -- see that
    function's own docstring).

    Two passes, the same shape as the LoRa/FM version this replaces:
    failing DOWN away from an unhealthy active link happens instantly, to
    whichever next-best link is healthy right now; failing UP to a
    higher-priority link only happens once that link has been
    continuously healthy for _FAILBACK_DEBOUNCE_S, so one lucky ack right
    after a bad patch cannot start a switch-back-then-fail-again flap.
    Both are exactly today's asymmetric-speed behaviour, just walked over
    however many links are actually built instead of hardcoded to two.
    """
    built = [link for link in _LINK_PRIORITY if _link_ifaces.get(link) is not None]
    if len(built) < 2:
        return
    if _link_override is not None:
        return

    now = time.monotonic()
    for link in built:
        state = _state_for(link, _link_ifaces[link])
        if state == lora_health.UP:
            if _link_up_since.get(link) is None:
                _link_up_since[link] = now
        else:
            _link_up_since[link] = None

    active_iface = _link_ifaces.get(_active_link)
    active_state = (_state_for(_active_link, active_iface)
                     if active_iface is not None else None)

    if active_state != lora_health.UP:
        for link in built:
            if link == _active_link:
                continue
            if _state_for(link, _link_ifaces[link]) == lora_health.UP:
                summary = (_summary_for(_active_link, active_iface)
                           if active_iface is not None else "not built")
                RNS.log(
                    f"[cot-bridge] {_active_link} {active_state} "
                    f"({summary}) -- failing over to {link}",
                    RNS.LOG_WARNING,
                )
                _set_active_link(link)
                return
        return  # nothing healthy at all; stay put rather than go dark

    for link in built:
        if link == _active_link:
            return  # nothing higher-priority than the active link remains
        up_since = _link_up_since.get(link)
        if up_since is not None and now - up_since >= _FAILBACK_DEBOUNCE_S:
            RNS.log(
                f"[cot-bridge] {link} healthy for "
                f"{_FAILBACK_DEBOUNCE_S:.0f}s -- failing back from "
                f"{_active_link}",
                RNS.LOG_NOTICE,
            )
            _set_active_link(link)
            return


def _on_gave_up(record):
    """One message the active link's ARQ gave up on: hand it to FM if this
    node has FM and is not already on it, and say which happened."""
    # Only a genuine NO_ACK exhaustion is evidence about the
    # active link's own health -- DISPLACED/CONGESTED mean the
    # channel is already busy, which is a different problem and
    # must not read as "the link is unhealthy" (see
    # lora_health.py's own module docstring).
    escalating = False
    if record.reason == reliable.NO_ACK:
        # Same attribution as _handle_ack's note_delivered():
        # ARQ-tracked traffic rides _active_link, so that link's
        # tracker is the one that just lost a message.
        health = _link_health.get(_active_link)
        if health is not None:
            health.note_escalated()
        if _fm_iface is not None and _active_link != LINK_FM:
            # A last resort, tried alongside -- not instead of --
            # the report below. reliable.py's own
            # bookkeeping stays exactly what it has always been, an
            # honest record of what the active link's ARQ achieved;
            # whether FM's own blind schedule eventually gets it
            # there is never knowable either way (no ack exists
            # there -- see fm_resend.py), so there is no honest way
            # to defer this report until every link has finished
            # trying.
            escalating = True
            _escalate_to_fm(record)
    # Handed to FM, it is NOT undelivered yet: the 2026-10-23
    # failover logged "[Error] UNDELIVERED" for a chat that arrived
    # over FM 5 s later. The ARQ record is the same either way; only
    # the verdict word and the level follow what happens next.
    if escalating:
        RNS.log(
            f"[cot-bridge] no ack on {_active_link} for {record.label} "
            f"after {record.attempts} transmission(s) ({record.reason}), "
            f"still owed by {_peer_labels(record.missing)} -- continuing "
            f"on FM; delivery there is confirmed only by a heard report",
            RNS.LOG_WARNING,
        )
    else:
        RNS.log(
            f"[cot-bridge] UNDELIVERED {record.label} after "
            f"{record.attempts} transmission(s) on {_active_link}: "
            f"{record.reason}, still owed by "
            f"{_peer_labels(record.missing)}",
            RNS.LOG_ERROR,
        )


def _reliable_loop():
    """Retransmits one-shot messages nobody has acknowledged, and gives up on
    the ones that stay unacknowledged.

    The give-up path is the point of the whole mechanism: a chat message that
    silently vanishes is the failure this milestone exists to eliminate, so it
    is logged at ERROR and surfaced in get_status() rather than merely counted.
    """
    while True:
        time.sleep(_RELIABLE_TICK_S)
        try:
            # Backpressure. ARQ must not amplify congestion: if the TX queue is
            # already deep, the channel is demonstrably not keeping up, and
            # adding retransmissions to it makes every message later, including
            # the acks that would end the retries. Skipping the whole tick
            # rather than skipping individual sends matters -- tick() is what
            # consumes an attempt, so a message must never burn one of its
            # tries (_fm_resend.attempts of them) on a transmission that
            # never happened.
            if _tx_queue.qsize() > _TX_BACKPRESSURE_DEPTH:
                RNS.log(
                    f"[cot-bridge] deferring retries, TX queue at {_tx_queue.qsize()}",
                    RNS.LOG_DEBUG,
                )
                continue
            # FM's blind schedules tick in the same loop, under the same
            # backpressure check above: a resend is worth no more than a retry
            # when the radio is already behind, and skipping the whole TICK
            # (rather than the individual send) is what stops a message burning
            # one of its limited attempts on a transmission that never happened.
            #
            # Gated on _fm_iface being built at all, not on _transport == "fm":
            # in LoRa-primary/FM-backup mode these schedules also carry escalated
            # one-shot messages (see _escalate_to_fm), so they need ticking
            # whenever FM exists. Every _enqueue() below forces link=LINK_FM
            # explicitly, because these packets are built at FM's own MTU and
            # must reach FM whatever _active_link happens to be by the time they
            # are dequeued.
            with _reliable_lock:
                fm_due = _fm_resend.tick() if _fm_iface is not None else []
            for msg_id, packets, label, attempt in fm_due:
                RNS.log(
                    f"[cot-bridge] resending {label} (msg {msg_id}, attempt "
                    f"{attempt} of {_fm_resend.attempts}), blind -- this link "
                    f"reports no delivery either way",
                    RNS.LOG_NOTICE,
                )
                _enqueue(packets, link=LINK_FM)

            # Same tick, same backpressure check, separate schedule -- see
            # _fm_position_resend's own comment for why this one is a different
            # instance with a much shorter shape.
            with _reliable_lock:
                fm_pos_due = (_fm_position_resend.tick()
                              if _fm_iface is not None else [])
            for msg_id, packets, label, attempt in fm_pos_due:
                RNS.log(
                    f"[cot-bridge] resending position for {label} "
                    f"(msg {msg_id}, attempt {attempt} of "
                    f"{_fm_position_resend.attempts}), blind -- this link "
                    f"reports no delivery either way",
                    RNS.LOG_DEBUG,
                )
                _enqueue(packets, link=LINK_FM)

            # Same tick, same backpressure check, a THIRD schedule -- see
            # _fm_keyframe_resend's own comment for why a keyframe gets more
            # tries than an ordinary position delta.
            with _reliable_lock:
                fm_kf_due = (_fm_keyframe_resend.tick()
                             if _fm_iface is not None else [])
            for msg_id, packets, label, attempt in fm_kf_due:
                RNS.log(
                    f"[cot-bridge] resending KEYFRAME for {label} "
                    f"(msg {msg_id}, attempt {attempt} of "
                    f"{_fm_keyframe_resend.attempts}), blind -- this link "
                    f"reports no delivery either way",
                    RNS.LOG_NOTICE,
                )
                _enqueue(packets, link=LINK_FM)

            with _reliable_lock:
                retries, gave_up = _reliable_tx.tick()
            # Logging and enqueueing happen OUTSIDE the lock: _peer_label takes
            # the peer table's lock, and nesting the two in opposite orders in
            # different threads is how deadlocks get built.
            for msg_id, frame, label, attempts, missing, needed in retries:
                only = "" if needed is None else f", fragments {sorted(needed)} only"
                RNS.log(
                    f"[cot-bridge] retransmitting {label} (msg {msg_id}, "
                    f"attempt {attempts}{only}), still waiting on "
                    f"{_peer_labels(missing)}",
                    RNS.LOG_WARNING,
                )
                _enqueue_frame(frame, needed)
            _report_stalled_reassemblies()
            for record in gave_up:
                _on_gave_up(record)
            _check_auto_switch()
        except Exception as e:
            RNS.log(f"[cot-bridge] reliable loop error: {e}", RNS.LOG_ERROR)


def _fm_position_airtime_warning(summary, packets):
    """Say, once per send, what a multi-burst position on FM actually costs.

    FOUND WIRING PHASE 4, AND IT IS A CAPACITY CONSEQUENCE OF THE PHASE 0 CODE
    CHOICE, not a bug in any one module. The chain is:

      * `H_256_768_22` carries 29 B per burst, which is 17 B after the mesh
        envelope.
      * A compact self-SA KEYFRAME is 34 B, so it does not fit one burst.
      * cot_codec.encode() deliberately refuses to fragment a frame carrying
        session state -- fragmenting a fire-and-forget position multiplies the
        chance the receiver never sees it while the SENDER has already advanced
        its base, stranding every later delta against a base that never arrived.
        So it aborts the session and falls back to zlib'd XML.
      * The fallback is ~180 B, which is ELEVEN bursts at this capacity, about
        26 s of airtime -- and because the session was aborted, `forced` stays
        True, so the next position is another keyframe. The compact delta path,
        whose whole purpose is a 16 B follow-up, never engages on this link at
        all.

    Measured at the operating point: 11 packets / 25.9 s per position on FM,
    against 1 packet / 2.4 s at the 113 B budget the pre-teardown transport had
    on the 126 B code. cot_codec's guard is right on its own terms; what is
    wrong is asking a 29 B link to carry a 34 B keyframe.

    NOT SILENTLY DROPPED, because that is a decision rather than wiring: it
    would leave an FM node present in the peer list and absent from the map, and
    the alternatives (a larger code at a longer burst, a smaller keyframe, or
    beacon-only presence on FM) are an operating-point question that wants the
    weak-link hardware measurement behind it. Logged at WARNING so the cost is
    visible on the first real run rather than inferred from a saturated channel.
    """
    seconds = len(packets) * fm_link.burst_duration_s()
    RNS.log(
        "[cot-bridge] FM position %s is %d packets / %.1f s of airtime -- a "
        "self-SA keyframe (34 B) does not fit this link's %d B budget, so it "
        "falls back to zlib'd XML and the compact delta path never engages. "
        "Sent once, NOT resent: resending it would cost that again per attempt. "
        "See _fm_position_airtime_warning()."
        % (summary.uid, len(packets), seconds, _max_payload),
        RNS.LOG_WARNING,
    )


def _take_fm_beacon_extras():
    """The heard-report and piggybacked position delta an FM-formatted beacon
    carries -- see _fm_heard's and _fm_last_position_delta's own module-level
    comments.

    Shared by the active-link-is-FM beacon and the FM reciprocal beacon below,
    which can never both fire in the same tick (one requires _active_link ==
    LINK_FM, the other requires it does not), so the read-and-clear of
    _fm_last_position_delta is still exactly once per tick either way.
    """
    with _fm_heard_lock:
        heard = list(_fm_heard)
    with _fm_last_position_delta_lock:
        global _fm_last_position_delta
        position_delta = _fm_last_position_delta
        _fm_last_position_delta = None
    return heard, position_delta


def _send_one_beacon(link, seq, digest, note, fm_extras=False):
    """Build one beacon frame targeted explicitly at `link` and transmit it,
    bypassing the TX queue exactly like _beacon_tick's caller always has --
    a link probe that waits behind the traffic it exists to diagnose is not
    much of a probe. The one place a beacon frame is actually assembled, so the
    active-link beacon and the FM reciprocal beacon stay identical in shape
    apart from what each one carries.

    `digest` is the neighbour digest to carry, or None to omit it (see
    _beacon_tick: FM omits it because relaying is LoRa-only, so it buys nothing
    there).

    `fm_extras` asks for the heard-report and the piggybacked position delta.

    THESE TWO USED TO BE ONE ARGUMENT -- `digest=None` both omitted the digest
    and selected the extras -- because at the 32 B code's 17 B beacon budget
    they could not coexist, so "no digest" and "heard report instead" were the
    same decision. At 113 B they can (25 B carries both), and leaving them
    coupled would be a quiet trap: passing a digest on FM would silently drop
    the heard report, and the only symptom would be resend schedules that never
    cancel -- which is indistinguishable from a report that never arrived.

    `note` is only for logging. Returns the (frame, sealed) bytes on
    success, None otherwise.
    """
    size = _BEACON_SIZES[(seq - 1) % len(_BEACON_SIZES)]
    if fm_extras:
        heard, position_delta = _take_fm_beacon_extras()
    else:
        heard, position_delta = None, None
    frame = cot_codec.encode_beacon(
        _NODE_ID, seq, size,
        callsign=_beacon_safe_str(_local_callsign, _BEACON_CALLSIGN_MAX_B),
        name=_beacon_safe_str(_node_name, _BEACON_NAME_MAX_B),
        digest=digest, heard=heard, position=position_delta,
        fix=_range_test_beacon_fix())
    # Don't claim to have sent anything while the radio is down. RNS
    # accepts the packet regardless -- the interface stays registered
    # with Transport even when offline -- so `send()` returns truthy
    # and the old wording read "beacon sent" all the way through an
    # outage. That is a smaller version of exactly the lie the USB
    # failure handling was added to stop telling.
    if not getattr(_iface_for(link), "online", False):
        RNS.log(
            f"[cot-bridge] beacon NOT sent (seq={seq}, {note}): "
            f"{link} interface is offline",
            RNS.LOG_WARNING,
        )
        return None
    if _mesh is None:
        return None
    # hops=0: a beacon is never relayed. It defines the ONE-HOP neighbour
    # set that every node's coverage test runs on, so flooding it would make
    # every node look adjacent to every other and destroy the test the
    # whole relay budget depends on.
    sealed = _mesh.wrap(frame, relay.HOPS_BEACON)
    result = _transmit_sealed(sealed, link=link)
    if result is False:
        RNS.log(
            "[cot-bridge] beacon REJECTED: no interface accepted it "
            f"({link} online={getattr(_iface_for(link), 'online', None)}, {note})",
            RNS.LOG_ERROR,
        )
        return None
    RNS.log(
        f"[cot-bridge] beacon sent (node={_NODE_ID} seq={seq} link={link} "
        f"frame={len(frame)}B, {len(sealed)}B on air, {note})",
        RNS.LOG_NOTICE,
    )
    return frame, sealed


# ---------------------------------------------------------------------------
# THE EARLY "I GOT IT" BEACON
#
# A sender on FM learns that a one-shot message arrived only from the receiver's
# NEXT BEACON, which lists a tag for it (_fm_heard). Measured 2026-10-12 with
# four markers sent Pixel -> OnePlus, every one of which the OnePlus received
# and decoded within 9-50 s:
#
#   * the spot map was confirmed 64 s after it arrived (the wait for the next
#     beacon), so its second attempt went out for nothing;
#   * the other three were confirmed only by beacons that reached the sender at
#     ~5-6 min, after all five attempts had been spent -- about 18 bursts of
#     resends (three attempts x two packets x three messages) for messages the
#     far end already had.
#
# Two things made it slow. The beacon is the ONLY carrier and goes out on its
# own schedule (60 s now). And its list of tags is fixed when it is BUILT: the
# OnePlus built one 1.2 s before the waypoint arrived, so even that beacon could
# not name it, and it then sat ~13 s on the air-side queue behind two
# listen-before-transmit deferrals.
#
# So a node that hears a reliable message on FM does not wait for its schedule:
# it sends a beacon of its own 1-3 s later. No new frame type and no wire
# change -- it is the ordinary beacon, built at the moment the tag exists, so an
# old build simply hears one more of them.
#
#   * COALESCED. Arrivals while one is pending share it; a two-packet message
#     and its duplicates cost one beacon, not one each.
#   * SPACED. At least _CONFIRM_BEACON_MIN_GAP_S between them, so a run of
#     arrivals cannot turn into a stream of beacons. A request inside the gap
#     is deferred to the end of it, not dropped -- the report is what the sender
#     is waiting for.
#   * DUPLICATES COUNT. Hearing a message again means the sender has not yet
#     learned we have it, which is exactly when a report is wanted (same
#     reasoning as tagging a duplicate in _handle_frame).
#
# WHAT IT COSTS: one beacon burst (~3.4 s) per received reliable message, or per
# batch. The sender is deaf while it transmits, and a retry can land on top of
# this beacon; the receiver's own listen-before-transmit defers when it can
# hear one. If the early beacon is lost, the regular schedule is exactly as
# before. Whether the saving beats the cost on a loaded channel is not measured.
# ---------------------------------------------------------------------------
_CONFIRM_BEACON_DELAY_S = (1.0, 3.0)
_CONFIRM_BEACON_MIN_GAP_S = 20.0
_confirm_lock = threading.Lock()
_confirm_timer = None
_last_confirm_at = None
# Injectable so a test can drive it without sleeping, as fm_resend's clock is.
_confirm_clock = time.monotonic
_confirm_timer_factory = threading.Timer


def _request_confirm_beacon():
    """Ask for an early beacon carrying the heard report. Safe to call from the
    receive thread on every reliable FM arrival; returns True if it scheduled
    one, False if one is already pending."""
    global _confirm_timer
    if not _beacon_enabled:
        return False
    with _confirm_lock:
        if _confirm_timer is not None:
            return False
        now = _confirm_clock()
        delay = random.uniform(*_CONFIRM_BEACON_DELAY_S)
        if _last_confirm_at is not None:
            delay += max(0.0, _last_confirm_at + _CONFIRM_BEACON_MIN_GAP_S - now)
        timer = _confirm_timer_factory(delay, _fire_confirm_beacon)
        timer.daemon = True
        _confirm_timer = timer
    timer.start()
    return True


def _fire_confirm_beacon():
    global _confirm_timer, _last_confirm_at
    with _confirm_lock:
        _confirm_timer = None
        _last_confirm_at = _confirm_clock()
    if not _beacon_enabled or _destination is None or _sweep_active:
        return
    try:
        # Built NOW, so the report names everything heard up to this moment --
        # the regular beacon's list is frozen whenever that one was built.
        _send_one_beacon(LINK_FM, _next_beacon_seq(), digest=None,
                         fm_extras=True, note="confirm: reliable message heard")
    except Exception as e:
        RNS.log(f"[cot-bridge] confirm beacon send failed: {e}", RNS.LOG_ERROR)


def _beacon_tick():
    """One _beacon_loop iteration's worth of work -- pulled out so a test can
    call it directly instead of driving the scheduling loop, the same reason
    _check_auto_switch() is its own function.

    Two separable jobs live here:

    The PRIMARY beacon proves _active_link's own health and carries the
    neighbour digest that link's coverage test depends on (or, when
    _active_link is FM, the heard-report/position-delta payload instead --
    unchanged from before this function existed).

    The FM RECIPROCAL beacon closes a real gap found live 2026-09-26 (see
    the project memory / docs/open-questions.md): a node that fails over to
    FM stays visible to its peer, because ITS OWN beacon now arrives over
    FM -- but nothing ever made the HEALTHY peer transmit over FM too, so
    that peer could go invisible on the failed-over node's map for as long
    as the outage lasts, even though the failed-over node is genuinely
    receiving it fine. Fired only when there is real, current evidence a
    peer needs it -- the most recent sighting of some connected peer arrived
    over FM, meaning THAT peer is currently reaching us only that way -- not
    unconditionally on every tick, which would mean duplicating every
    beacon onto FM and giving up the RX-always-on/TX-only-when-needed
    airtime tradeoff this design deliberately made (see the project memory
    for the original design conversation).
    """
    global _beacon_seq, _digest_sent, _last_beacon_at
    if not _beacon_enabled or _destination is None or _sweep_active:
        return
    now = time.monotonic()
    digest = _neighbour_digest()
    # Three reasons to beacon, and step 3 added the last two.
    #
    # QUIET: nothing else has proved the link this interval, which is what
    # the probe was built for -- ATAK down, or no CoT flowing.
    #
    # CHANGED: the set of nodes this one can hear is different, and until
    # that is advertised nobody will relay for whoever just arrived. This
    # is the case a fixed timer serves worst.
    #
    # STALE: the digest is the input to every other node's coverage test,
    # so it cannot be allowed to age out in a busy mesh just because
    # something else keeps happening to be on the air.
    quiet = now - _last_tx_at >= _beacon_interval()
    changed = digest != _digest_sent
    stale = now - _last_beacon_at >= _DIGEST_REFRESH_S
    if _beacon_seq and not (quiet or changed or stale):
        RNS.log(
            f"[cot-bridge] beacon skipped: transmitted "
            f"{now - _last_tx_at:.0f}s ago, digest unchanged",
            RNS.LOG_DEBUG,
        )
    else:
        try:
            seq = _next_beacon_seq()
            # ON FM THE NEIGHBOUR DIGEST IS DEAD WEIGHT. It exists for the
            # relay coverage test, and relaying is LoRa-only -- _gates carries
            # one link and _relay_policy_for("fm") is None -- so those 9 bytes
            # (8 digest + 1 length) buy nothing there.
            #
            # NO LONGER A BUDGET DECISION, and that distinction is the point.
            # Through Phase 2a and the 32 B code this was forced: 10 B of beacon
            # floor plus three 2-byte tags consumed the 17 B budget exactly, and
            # the digest alone would have overflowed it. At the 128 B code both
            # fit comfortably (25 B of 113). So the digest is omitted here
            # because it is USELESS on this link, not because there is no room
            # -- which means `fm_extras` is now a separate argument from
            # `digest`, and a future reason to carry the digest on FM can simply
            # pass one without silently costing the heard report.
            fm = _active_link == LINK_FM
            note = 'quiet' if quiet else 'digest ' + ('changed' if changed else 'refresh')
            sent = _send_one_beacon(_active_link, seq,
                                     digest=None if fm else digest, note=note,
                                     fm_extras=fm)
            if sent is not None:
                _digest_sent = digest
                _last_beacon_at = now
        except Exception as e:
            RNS.log(f"[cot-bridge] beacon send failed: {e}", RNS.LOG_ERROR)

    if (_active_link != LINK_FM and _fm_iface is not None
            and getattr(_fm_iface, "online", False)):
        # Without this, a peer reachable on FM only never hears from a node
        # beaconing on LoRa -- and so never learns it has a neighbour at all.
        #
        # last_link() (via snapshot()'s "last_link") is who most recently
        # carried a frame FROM this peer TO us -- see peers.py's note_frame
        # docstring -- so a connected peer last heard on FM is live,
        # present-tense evidence that peer cannot currently reach us any other
        # way, not just a peer this node once happened to hear there.
        peer_needs_fm = any(
            p["last_link"] == LINK_FM and p["connected"]
            for p in _peers.snapshot())
        if peer_needs_fm:
            try:
                _send_one_beacon(
                    LINK_FM, _next_beacon_seq(), digest=None, fm_extras=True,
                    note=f"fm reciprocal: a peer reaches us only via fm "
                         f"while we're active on {_active_link}")
            except Exception as e:
                RNS.log(f"[cot-bridge] fm reciprocal beacon send failed: {e}",
                        RNS.LOG_ERROR)


def _beacon_loop():
    # The slot this loop last transmitted on. Without it, a wake-up that
    # lands even microseconds before `due` would re-select the SAME slot and
    # fire again, and again, until the wall clock caught up -- measured on
    # the bench 2026-09-30 as three beacons 150ms apart (seq 16/17/18), i.e.
    # ~33s of airtime spent announcing the same thing three times, on the one
    # link that can least afford it. The jitter below is non-negative for the
    # same reason; this is the belt to its braces, since time.sleep() is only
    # promised not to return LATE.
    last_due = 0.0
    while True:
        # The slot is placed on the wall clock, which every node shares, so
        # two nodes in different slots really do key up at different times --
        # see _next_beacon_at() for why monotonic() could never do this, and
        # what it cost on real hardware. The schedule is derived fresh from
        # the current time each cycle rather than accumulated, so it cannot
        # drift and needs no catch-up branch for a suspended process.
        #
        # The jitter is still re-rolled every period. It no longer has to
        # rescue a shared slot -- _beacon_slot_offset() re-draws per cycle
        # for that -- but it stays because on the cycles where two nodes DO
        # draw the same slot, keying at the exact same instant is the one
        # case listen-before-transmit cannot break: whoever starts a
        # half-second earlier is detectable, and the other defers.
        #
        # NON-NEGATIVE, unlike the jitter this replaced: a negative draw
        # would wake the loop before its own slot, which re-selects that
        # same slot and fires again (see last_due above). It only ever
        # delays within the slot's own slack -- the FM slot is 15s against
        # an 11.5s burst ceiling -- never advances past it.
        interval = _beacon_interval()
        now = max(time.time(), last_due)
        due = _next_beacon_at(
            now, interval,
            lambda c: _beacon_slot_offset(c, interval=interval))
        delay = due - time.time() + random.uniform(
            0.0, _BEACON_SLOT_JITTER_S)
        if delay > 0:
            time.sleep(delay)
        last_due = due
        # _sweep_active as well as the TX gate: the beacon builds a frame and
        # enqueues it, so gating only the drain would pile up a beacon per
        # interval for the whole survey and then emit the lot at once.
        _beacon_tick()


def _handle_outbound_cot(payload):
    """One captured CoT event -> zero or more queued RNS packets.

    MUST NOT raise: this runs inline in the listener loop, and a single
    malformed event taking the whole bridge down is exactly the failure shape
    milestone 1 kept hitting.
    """
    # Captured once, rather than read fresh at each decision below: this
    # whole function's shaping/staggering/enqueue choices have to agree
    # with EACH OTHER, even if _active_link changes mid-call (once
    # LoRa-primary/FM-backup routing's auto-switch is wired up to actually
    # move it) -- a message shaped for FM must still be explicitly sent on
    # FM, not silently retargeted at LoRa because the default flipped in
    # the few milliseconds since encode_packets() ran. Fixed for the life
    # of the process (always LINK_LORA/LINK_FM matching _transport) in
    # LoRa-only/FM-only mode, same as today.
    link = _active_link

    # Decide before encoding: an event that isn't going out shouldn't cost the
    # work, and more importantly shouldn't cost the airtime.
    try:
        summary = cot_codec.summarize(payload)
    except cot_codec.CotEncodeError as e:
        RNS.log(f"[cot-bridge] dropping unparseable {len(payload)}-byte event: {e}",
                RNS.LOG_ERROR)
        return
    except Exception as e:
        RNS.log(f"[cot-bridge] codec bug summarizing {len(payload)} bytes: {e}",
                RNS.LOG_ERROR)
        summary = None

    is_onset = False
    if summary is not None:
        _learn_local_callsign(summary)
        # Where WE are. Recorded before the policy decides, and deliberately:
        # a suppressed position is still a position, and a range test that
        # only learned our location when the throttle happened to let one
        # through would carry a fix up to a minute stale for no reason.
        if summary.is_position and summary.is_self:
            _rangetest.note_position(_NODE_ID, summary.lat, summary.lon,
                                     stamp=summary.time)
        decision = _tx_policy.decide(summary)
        is_onset = decision.kind == tx_policy.SEND_ONSET
        if not decision.send:
            RNS.log(
                f"[cot-bridge] suppressed {summary.uid} ({summary.type}): {decision.reason}",
                RNS.LOG_DEBUG,
            )
            return
        RNS.log(
            f"[cot-bridge] sending {summary.uid} ({summary.type}): {decision.reason}",
            RNS.LOG_DEBUG,
        )

    # Everything that is not a periodic position report gets acknowledged
    # delivery. That is the same test tx_policy uses to exempt traffic from
    # throttling, and deliberately so: one property of an event -- "does a
    # later event supersede this one?" -- drives both decisions, rather than
    # two lists that can quietly drift apart. A position that goes missing is
    # replaced seconds later; a chat message is gone for good.
    wants_ack = summary is not None and not summary.is_position
    # Set only on the position path below, before build_packets() advances
    # _sa_tx's own state -- see that branch's comment for why it has to be read
    # BEFORE encoding rather than inferred from the result.
    will_be_keyframe = False

    # LoRa-primary/FM-backup routing: LoRa's own ~16 s ARQ schedule is otherwise
    # ALWAYS tried first for one-shot traffic, even for a peer most recently
    # heard on FM (see peers.heard_on()'s docstring for why last_link() alone is
    # not enough to decide this) -- except for a peer this node has never once
    # heard on LoRa but HAS heard on FM. Retrying four times on a link that
    # specific peer is not known to be capable of hearing at all is just 16
    # wasted seconds; a peer with any LoRa history keeps the normal, unchanged
    # treatment. An addressee never heard on EITHER link also gets no special
    # treatment: there is no positive evidence FM would do better, so the honest
    # default (try the primary) stands.
    fm_only_addressee = False
    if wants_ack and link == LINK_LORA and _fm_iface is not None:
        addressee = _chat_addressee_node(summary.uid)
        fm_only_addressee = (
            addressee is not None
            and _peers.heard_on(addressee, LINK_FM)
            and not _peers.heard_on(addressee, LINK_LORA)
        )
        if fm_only_addressee:
            RNS.log(
                f"[cot-bridge] {_peer_label(addressee)} has never been "
                f"heard on LoRa but has on FM -- sending {summary.uid} "
                f"there directly instead of trying LoRa's ARQ first",
                RNS.LOG_DEBUG,
            )
    # The link these packets are actually built for and must be sent on -- NOT
    # necessarily _active_link (`link`, captured above): an fm_only_addressee
    # message is built by _build_fm_oneshot() below even though this node's own
    # default is still LoRa, and staggering/enqueueing must agree with what was
    # actually built, not with the node's default.
    send_link = LINK_FM if (link == LINK_FM or fm_only_addressee) else link

    try:
        if wants_ack and send_link == LINK_FM:
            # Same question ("does a later event supersede this?"), different
            # answer to it. On FM there is no ack to wait for, so one-shot
            # traffic gets blind spaced resends instead of ARQ -- see
            # _build_fm_oneshot(). Without this branch reliable.py's engine would
            # run on a link its own interface forbids it on, putting ack frames
            # and retry storms onto a half-duplex channel delivering ~99 bps.
            packets = _build_fm_oneshot(payload, summary)
        elif wants_ack:
            packets = _build_reliable(payload, summary)
        else:
            # The session goes only to this path. Everything reaching
            # _build_reliable is one-shot traffic -- chat, markers, drawings --
            # whose uid belongs to the message rather than to this node, so
            # there is nothing constant in it to bind.
            with _sa_lock:
                # Mirrors cot_codec._sa_plan()'s own keyframe test exactly
                # (missing only the uid/callsign-changed cases, which this
                # node's own identity practically never hits mid-session -- see
                # _fm_keyframe_resend's comment for why this distinction
                # matters). HAS TO BE READ BEFORE build_packets() below, which
                # commits _sa_tx's pending state change and leaves
                # since_keyframe already advanced for the NEXT call.
                will_be_keyframe = (_sa_tx.forced or _sa_tx.since_keyframe
                                    >= _sa_tx.keyframe_every - 1)
                packets = cot_codec.build_packets(payload, _max_payload, _sa_tx)
    except cot_codec.CotEncodeError as e:
        RNS.log(f"[cot-bridge] dropping {len(payload)}-byte event: {e}", RNS.LOG_ERROR)
        return
    except Exception as e:
        # Structural, not contingent on the codec being bug-free.
        RNS.log(f"[cot-bridge] codec bug encoding {len(payload)}-byte event: {e}", RNS.LOG_ERROR)
        return

    total = sum(len(p) for p in packets)
    RNS.log(
        f"[cot-bridge] {len(payload)} bytes CoT -> {len(packets)} packet(s), {total} bytes on air",
        RNS.LOG_DEBUG,
    )

    if summary is not None and summary.is_position and link == LINK_FM:
        # See _fm_position_resend's/_fm_keyframe_resend's own comments for why a
        # keyframe gets more tries than an ordinary delta, and why this is one
        # quick extra try either way rather than the one-shot schedule.
        #
        # REGISTERED WITH THE SAME BYTES this call is about to transmit. A replay
        # must be byte-identical, not re-encoded: re-encoding silently advances
        # _sa_tx's delta/keyframe generation a second time (see cot_codec.py's
        # SaTx docstring) and hands the receiver a base it never asked for.
        #
        # ONLY A SINGLE-BURST POSITION IS WORTH RESENDING, and at the Phase 0
        # operating point that is NOTHING -- see _fm_position_airtime_warning()
        # below for the measurement and why. The resend exists to give one quick
        # second roll of the dice against a single missed burst; a position that
        # is already eleven bursts long has rolled eleven dice, and resending it
        # four times (the keyframe schedule) is 104 s of airtime on a 90 s
        # heartbeat cycle -- a channel saturated by one node's own position
        # reports. eligibility.fm_gate already refuses to FORWARD a fragmented
        # frame on this link for the same reason; this is the same judgement
        # applied to originating one.
        if len(packets) == 1:
            resend = (_fm_keyframe_resend if will_be_keyframe
                      else _fm_position_resend)
            resend.register(resend.next_msg_id(), packets, summary.uid)
        else:
            _fm_position_airtime_warning(summary, packets)
        if not will_be_keyframe and len(packets) == 1:
            # Stashed for _beacon_loop to piggyback onto its very next beacon --
            # see _fm_last_position_delta's own comment. A keyframe never rides
            # here (too big, see encode_beacon's `position` comment); a delta
            # that somehow needed fragmenting is skipped too, since a beacon
            # carries one opaque blob rather than a fragment sequence.
            with _fm_last_position_delta_lock:
                global _fm_last_position_delta
                _fm_last_position_delta = packets[0]

    # A movement onset is the one event a whole squad produces simultaneously,
    # so it is worth staggering on every transport. A one-shot event (chat,
    # marker, drawing, waypoint) fires the instant a human triggers it, with
    # no schedule to spread it from whatever any other node happens to be
    # doing at that exact moment -- on FM specifically, where nothing else in the
    # stack catches a collision (see _ONSET_SLOT_S's own comment), that is worth
    # the same small delay. A routine FM heartbeat position gets the same
    # treatment for the same reason -- see stagger_reason's "heartbeat" branch
    # below for why it is not optional the way it looks. Everything else goes
    # straight out.
    stagger_reason = None
    if is_onset:
        stagger_reason = "onset"
    elif wants_ack and send_link == LINK_FM:
        stagger_reason = "one-shot event"
    elif summary is not None and summary.is_position and link == LINK_FM:
        # Found 2026-09-22, real hardware: two nodes restarted within a second
        # of each other collided on their very first position send, every time.
        # tx_policy.decide()'s heartbeat check is a pure elapsed-time threshold
        # with NO jitter of its own (unlike the beacon's _beacon_slot_offset()/
        # _BEACON_SLOT_JITTER_S), so two nodes whose clocks started together
        # stay in lockstep on EVERY later heartbeat too, not just the first --
        # and listen-before-transmit cannot save a truly simultaneous key-up,
        # because both sides check "clear" before either has started.
        stagger_reason = "heartbeat"

    if stagger_reason:
        window = _stagger_window()
        if stagger_reason != "onset":
            # _ONSET_SLOT_S's peer-scaled formula collapses to one slot whenever
            # peer_count is 0 -- which is always true for the very first send
            # after a restart, precisely because the node has not had time to
            # hear anyone yet, not because collision risk is low there. Two
            # nodes restarting together are exactly the case that floor was
            # reasoned NOT to need protecting, and exactly where the reasoning
            # breaks. Not given to onset: its 0.25 s slot is tuned against
            # LoRa's airtime budget too, which this FM-only traffic has no
            # reason to inherit.
            window = max(window, _FM_STAGGER_MIN_S)
        RNS.log(
            f"[cot-bridge] staggering {stagger_reason} for {summary.uid} "
            f"(window {window:.1f}s)",
            RNS.LOG_DEBUG,
        )
        _enqueue_delayed(packets, window, link=send_link)
        return

    _enqueue(packets, link=send_link)


def _build_fm_oneshot(payload, summary):
    """Encode a one-shot event for the FM link and schedule blind resends.

    The FM counterpart to _build_reliable() below, and deliberately NOT a
    variation of it: acknowledged delivery is unavailable on this link by design
    (see fm_link.py), so everything _build_reliable does to manage acks --
    snapshotting who owes one, narrowing to an addressee, naming that addressee
    on the wire -- would be bytes and bookkeeping spent on a mechanism that never
    runs. At 29 B of capacity, naming an addressee alone is ~7 B.

    WHAT IS KEPT, AND WHY THIS IS NOT JUST A BARE SEND: the reliable wrapper
    stays, purely for its message id. Two things the RECEIVER does are keyed on
    it, and both are load-bearing here precisely BECAUSE the resends are blind:

      * duplicate suppression (reliable.ReliableRx.should_deliver), which is what
        stops several blind attempts rendering a chat message that many times;
      * fragment reassembly across attempts (cot_codec.fragment_reliable gives
        every attempt the same id), which at this budget is the difference
        between a multi-fragment message being deliverable and not -- roughly
        60% against under 2%.

    Both windows must outlast the resend schedule, which start() sizes from
    fm_resend.span_s() rather than leaving to their defaults.
    """
    # any_ack=False and addressee=None: nobody acks on this link, so the "one ack
    # from anybody will do" flag has nothing to modify, and naming an addressee
    # would spend a quarter of the budget silencing bystanders who were never
    # going to speak.
    msg_id = _fm_resend.next_msg_id()
    budget = _max_payload - cot_codec.reliable_overhead(_NODE_ID, None)
    frame = cot_codec.encode(payload, budget)
    _, wrapped = cot_codec.wrap_reliable(frame, _NODE_ID, msg_id,
                                         any_ack=False, addressee=None)

    label = "%s %s" % (summary.type, summary.uid)
    if len(label) > 60:
        label = label[:57] + "..."

    packets = cot_codec.fragment_reliable(wrapped, _max_payload)
    # The tag a peer echoes on its beacon if this arrives, which is what lets the
    # schedule stop early. Built from OUR node id and this msg_id so another
    # node's identical msg_id cannot cancel our message -- see
    # cot_codec.heard_tag(), and note the asymmetry it exists for: a false
    # positive abandons a message that never arrived, which is the failure the
    # resends exist to prevent.
    #
    # Under _reliable_lock, exactly as _build_reliable wraps its own register():
    # this runs on the CoT listener thread while _reliable_loop ticks the same
    # structure on the retry thread.
    with _reliable_lock:
        _fm_resend.register(msg_id, packets, label,
                            tag=cot_codec.heard_tag(_NODE_ID, msg_id))
    RNS.log(
        f"[cot-bridge] {label} (msg {msg_id}) queued for up to "
        f"{_fm_resend.attempts} blind attempts over "
        f"{fm_resend.span_s() / 60.0:.0f} min, stopping early only if a peer "
        f"reports hearing it",
        RNS.LOG_NOTICE,
    )
    return packets


def _escalate_to_fm(record):
    """The active link's own ARQ schedule (LoRa's or, once it carries one-shot
    traffic, WiFi's) fully exhausted `record` with no ack at all
    (reliable.NO_ACK) -- last resort: try it once more on FM, forced explicitly
    to LINK_FM regardless of _active_link.

    ALWAYS FM SPECIFICALLY, not "the next-best link in priority order": FM is the
    only link with its own blind-repeat scheduler that needs no ack at all, which
    is exactly what makes it a meaningful LAST resort after an ack-based link has
    already exhausted real acknowledged retries. WiFi has no such scheme -- when
    WiFi is active its one-shot traffic already gets the same acked ARQ LoRa
    gets, so there is nothing WiFi-specific to add here. A message reaching this
    function already means whichever ack-based link was carrying it has failed
    THIS message, and _active_link's current value does not change that.

    RE-FRAGMENTS record.frame -- the reliable-wrapped envelope the active link's
    own reliable.register() already built, NOT a fresh encode from the raw
    payload -- at FM's own budget, reusing the SAME msg_id already embedded in it
    (fragment_reliable does not renumber it), so a receiver's reassembly and
    duplicate suppression treat a late FM arrival as the same message rather than
    a new one. Registered with fm_resend the same as FM's own native one-shot
    path: no ack exists on this link either, so one fire-and-forget transmission
    would be strictly worse odds for no reason.

    Caller's responsibility: only ever for reliable.NO_ACK, never
    DISPLACED/CONGESTED (see _reliable_loop), and only when _fm_iface is not None
    and _active_link is not already LINK_FM.
    """
    if _max_payload_fm is None:
        return  # defensive; _fm_iface is not None is expected to imply this
    try:
        packets = cot_codec.fragment_reliable(record.frame, _max_payload_fm)
    except Exception as e:
        # Overwhelmingly "message needs N fragments (max 16)": at FM's 17 B
        # budget cot_codec.MAX_FRAGMENTS caps a whole message at 192 B, so a
        # long chat message or a drawing simply cannot cross this link. That is
        # a real limit of the small code rather than an error to chase, and it
        # is the right place to stop: the alternative is a message that could
        # never complete occupying the channel for a minute while it fails.
        RNS.log(
            f"[cot-bridge] {record.label} (msg {record.msg_id}) will not fit "
            f"FM's {_max_payload_fm} B budget, so the escalation is declined: "
            f"{e}",
            RNS.LOG_WARNING,
        )
        return
    with _reliable_lock:
        _fm_resend.register(record.msg_id, packets, record.label,
                            tag=cot_codec.heard_tag(_NODE_ID, record.msg_id))
    RNS.log(
        f"[cot-bridge] {record.label} (msg {record.msg_id}) got no ack on "
        f"{_active_link} after {record.attempts} attempt(s) -- trying FM, up to "
        f"{_fm_resend.attempts} blind attempts, stopping early only if a "
        f"peer reports hearing it",
        RNS.LOG_WARNING,
    )
    _enqueue(packets, link=LINK_FM)


def _build_reliable(payload, summary):
    """Encode a one-shot event and register it for acknowledged delivery."""
    # Snapshotted now, and not updated afterwards: a message is delivered to
    # the group that existed when it was sent. Adding peers that appear
    # mid-flight would mean a message can never complete on a network anyone
    # is joining.
    peers = [p["node_id"] for p in _peer_snapshot() if p["connected"]]

    # A direct chat is owed an ack by the node it was addressed to, and by
    # nobody else. Bystanders acking it costs airtime that scales with the
    # mesh, and -- worse -- makes an UNDELIVERED report mean "somebody in
    # earshot stayed quiet" rather than "the recipient never got it".
    #
    # Only narrows when the addressee is actually known. A room message, an
    # unknown peer, or anything that isn't a chat falls through to expecting
    # every peer, which is the behaviour this replaced.
    addressee = _chat_addressee_node(summary.uid)

    # Naming the addressee on the wire is what actually keeps the bystanders
    # quiet, and it costs ~7 bytes. With one peer there ARE no bystanders, so
    # those bytes would buy nothing -- which is the two-node bench, i.e. every
    # run this has ever had. Narrow the expectation either way; only pay for
    # the name when somebody could otherwise answer who was not asked.
    named = addressee if (addressee is not None and len(peers) > 1) else None
    if addressee is not None:
        peers = [addressee]

    # Shrink the budget by what the wrapper will add, so a message that fits in
    # one packet still fits in one packet once wrapped. Sized against `named`,
    # not `addressee`: the two differ exactly when the name is not going on the
    # wire, and sizing against the longer header there would cost a fragment.
    budget = _max_payload - cot_codec.reliable_overhead(_NODE_ID, named)
    frame = cot_codec.encode(payload, budget)

    label = "%s %s" % (summary.type, summary.uid)
    if len(label) > 60:
        label = label[:57] + "..."

    with _reliable_lock:
        msg_id, frame = _reliable_tx.register(frame, _NODE_ID, peers, label,
                                              addressee=named)

    if peers:
        # Worded for what the sender actually waits for. "needs an ack from
        # A, B" read as "from both"; for an unaddressed message the FIRST
        # answer from any of them settles it (reliable.DEFAULT_REQUIRED_ACKS).
        if addressee is not None:
            wants = f"an ack from its addressee, {_peer_label(addressee)}"
        elif len(peers) > 1 and _reliable_tx.required_acks <= 1:
            wants = (f"one ack from any of {_peer_labels(peers)} "
                     f"(the first answer settles it)")
        else:
            wants = f"an ack from {_peer_labels(peers)}"
        RNS.log(
            f"[cot-bridge] {label} needs {wants} (msg {msg_id})",
            RNS.LOG_NOTICE,
        )
    else:
        # Not an error, and not worth retrying into: with nobody heard from
        # recently there is no ack to wait for. Say so plainly, because the
        # alternative reading -- "the message failed" -- is wrong.
        RNS.log(
            f"[cot-bridge] {label} sent unacknowledged (msg {msg_id}): no peers heard from",
            RNS.LOG_NOTICE,
        )

    try:
        # Fragmented under the reliable msg_id, so a retry lands in the same
        # reassembly the first attempt started rather than beginning again.
        packets = cot_codec.fragment_reliable(frame, _max_payload)
        with _reliable_lock:
            _reliable_tx.note_fragment_count(msg_id, len(packets))
        return packets
    except Exception:
        # Registered but never actually transmitted -- e.g. a message needing
        # more than MAX_FRAGMENTS. Retrying it would fail identically every
        # time and then report a delivery failure for something that never
        # went out, so drop the registration and let the caller log the real
        # reason.
        with _reliable_lock:
            _reliable_tx.forget(msg_id)
        raise


def _enqueue(packets, link=None):
    """`link` forces which interface these packets go out on regardless of
    _active_link at send time -- None (the ordinary case) follows whatever
    is active when _next_outbound() dequeues them. Anything already shaped
    for a specific link (FM's own one-shot/position/keyframe resends, built
    at FM's own MTU/format) must pass its link explicitly: _active_link can
    move between being queued and being sent, and packets built for one
    link are not valid on the other.
    """
    item = (packets, link)
    try:
        _tx_queue.put_nowait(item)
    except queue.Full:
        # Position updates supersede each other, so when the radio is behind,
        # shedding the OLDEST queued message loses the least information. A
        # reliable message shed this way is not lost: its retry timer puts it
        # back, so congestion delays it instead of destroying it.
        try:
            _tx_queue.get_nowait()
            _tx_queue.put_nowait(item)
            RNS.log("[cot-bridge] TX queue full, dropped oldest message", RNS.LOG_WARNING)
        except Exception:
            pass


Outbound = collections.namedtuple("Outbound", "packets sealed link",
                                  defaults=(None,))


def _hops_for(frame, _depth=0):
    """How far a frame this node is originating may travel.

    Read off the frame itself rather than passed down from the caller, because
    the wrap happens at the single choke point every transmission passes
    through and there is no caller context left there -- the same property that
    makes that spot the right place to encrypt.
    """
    if not frame:
        return 0
    kind = frame[0] & 0x0F
    if kind == cot_codec.T_BUNDLE:
        # One envelope, one hop count, so a bundle travels as far as the frames
        # inside it -- which bundle() guarantees all want the same reach. The
        # first one therefore answers for all of them.
        #
        # _depth exists because this also runs on RECEIVED frames, via
        # _heard_directly. bundle() refuses to nest, but nothing stops a
        # crafted frame from doing so, and "recurse until the bytes run out" is
        # not a thing to offer anyone holding the group key. One level is all a
        # legitimate bundle ever has.
        if _depth:
            return 0
        try:
            return _hops_for(cot_codec.unbundle(frame)[0], _depth + 1)
        except cot_codec.CotDecodeError:
            return 0
    if kind == cot_codec.T_BEACON:
        return relay.HOPS_BEACON
    if kind in (cot_codec.T_ACK, cot_codec.T_PARTIAL):
        # As far as the message it answers, and for the same reason. This was
        # HOPS_BEACON -- zero -- on the reasoning that an ack goes to "a node
        # that just transmitted to us, so it is by definition one hop away".
        # Relaying made that false, and the 2026-08-12 bench measured the
        # result: 3 multi-hop GeoChats, 3 delivered, 3 reported UNDELIVERED,
        # 9 retries spent re-sending messages that had already arrived. An ack
        # that cannot retrace the path turns milestone 4 from a guarantee into
        # a lie the moment a frame takes two hops.
        #
        # Only one-shot traffic is ever acknowledged -- positions are
        # fire-and-forget because they self-heal -- so HOPS_ONESHOT is exactly
        # enough to get back along any path the message itself could have
        # taken, and never more.
        #
        # The precise distance would be cheaper still: the message arrived with
        # `inbound.hops` left of this same cap, so the sender is exactly
        # `HOPS_ONESHOT - inbound.hops` hops away. It is deliberately NOT used.
        # Every receiver infers "did somebody forward this?" by comparing the
        # hop count against the fixed cap the frame's class always gets (see
        # _heard_directly), and that inference is what keeps a two-hop node out
        # of the neighbour digest. A per-frame cap would make it unknowable and
        # reintroduce exactly the bug the same bench found. The cost of the
        # fixed cap is bounded by the coverage test and the token bucket like
        # everything else: in a single cell no ack is relayed at all.
        return relay.HOPS_ONESHOT
    if kind in cot_codec.COMPACT_KINDS:
        # Positions self-heal within the minute, so they get the small cap and
        # are what the token bucket sacrifices first. Every spelling of a
        # compact frame counts: whether it was squeezed against the dictionary,
        # or carries generic elements, says nothing about what it carries.
        return relay.HOPS_POSITION
    # Chat, markers, drawings, routes, and every fragment of them.
    return relay.HOPS_ONESHOT


def _heard_directly(inbound):
    """Did this frame come from its originator, or did somebody forward it?

    `origin` cannot answer: a relay does not re-wrap, so it names the node that
    ORIGINATED the frame at every hop -- which is deliberate, and is what keeps
    a frame's flood identity stable. The hop count can. The sender stamps the
    cap _hops_for() chose and every relay spends exactly one, so a frame still
    carrying its full cap has been through nobody.

    The pair of them is load-bearing: this is what separates "I can reach that
    radio" from "somebody is reaching it for me", which the neighbour digest
    depends on being told apart. See _note_peer_frame.
    """
    return inbound.hops >= _hops_for(inbound.frame)


def _hops_travelled(inbound):
    """How many transmissions this frame took to arrive: 1 if it came straight
    from its sender, 2 if one relay carried it, and so on.

    The same arithmetic as _heard_directly, read for its magnitude rather than
    just its sign. Every relay spends exactly one hop, so the difference
    between the cap the sender stamped and what is left is the number of relays
    that touched it.

    This is what tells the ARQ how long to wait: a relay does not forward on
    arrival, it waits out its cancellation window first, so a peer two hops off
    is not merely further away but slower by a fixed amount per relayed leg.
    See reliable.ReliableTx._relay_allowance.
    """
    spent = _hops_for(inbound.frame) - inbound.hops
    return max(1, spent + 1)


def _enqueue_delayed(packets, window, link=None):
    """Queue packets to go out at a random moment within `window` seconds.
    `link` forces which interface -- see _enqueue's own comment.

    Falls back to sending immediately if the lane is full: the point of this
    is to spread a burst, and refusing to send at all would turn a congestion
    problem into a lost position report.
    """
    send_after = time.monotonic() + random.random() * window
    try:
        _delayed_queue.put_nowait((send_after, packets, link))
    except queue.Full:
        RNS.log("[cot-bridge] delay lane full, sending immediately", RNS.LOG_WARNING)
        _enqueue(packets, link=link)


def _stagger_window():
    """Shared by movement onsets (every transport) and one-shot FM traffic
    (FM only) -- see _ONSET_SLOT_S/_ONSET_JITTER_MAX_S's own comment for why
    the same window serves both."""
    return tx_policy.jitter_window(
        _connected_peer_count(), _ONSET_SLOT_S, _ONSET_JITTER_MAX_S)


def _enqueue_frame(frame, needed=None):
    """Re-fragment and queue an already-wrapped frame, for retransmission.

    Under the SAME fragment id as the original, which is the whole point: the
    receiver adds these to the partial message it already holds instead of
    starting a second one and abandoning the first.

    `needed` narrows it to the fragments somebody actually still wants, from
    the partial acks they sent. None means send everything, which is what
    happens whenever nobody has reported -- the first retry of any message, and
    every retry of an unfragmented one.

    Re-fragmented rather than stored: fragment_reliable is deterministic for a
    given frame and MTU, so index N here is byte-identical to index N on the
    first attempt. Keeping a copy would only be a second thing to get out of
    step with the first.
    """
    try:
        packets = cot_codec.fragment_reliable(frame, _max_payload)
        if needed is not None and len(packets) > 1:
            keep = [p for i, p in enumerate(packets) if i in needed]
            if keep:
                packets = keep
        _enqueue(packets)
    except Exception as e:
        RNS.log(f"[cot-bridge] could not requeue frame for retry: {e}", RNS.LOG_ERROR)


def _airtime_s(payload_len):
    return _lora_link.frame_s(payload_len)


# An interference reading this far above the noise floor is treated as real.
# Below it, it is most likely the radio's own LNA recalibration -- upstream RNS
# carries the same 10 dB threshold in commented-out code next to the reading,
# which is to say the false positives are known but the filter never shipped.
# Without it the interference number is untrustworthy, and an untrustworthy
# number on a status screen is worse than no number.
_INTERFERENCE_MARGIN_DB = 10


# Range test recording. The CSV sits beside the log files so Export and the
# documented `run-as` pull both already reach it, and so a test comes home as
# one directory rather than as two things to remember.
_RANGETEST_DIR = os.path.join(os.path.expanduser("~"), "logs")
_rangetest = rangetest.RangeTest(distance_fn=tx_policy.haversine_m,
                                 clock=time.monotonic, wall_clock=time.time)
_rangetest_file = None
_rangetest_path = None
_rangetest_lock = threading.Lock()


def _record_range_sample(sender, rssi, snr, seq, link):
    """One heard frame -> one CSV row. Never raises: a range test is an
    observer, and an observer that can break the thing it is watching is worse
    than no observer.

    `link` is which of this node's own radios the frame arrived on -- stored
    in its own column (see rangetest.COLUMNS) so a run doesn't have to be
    re-read later to guess it from rssi_dbm being blank. _channel_conditions()
    is read off the RNode specifically and means nothing for a frame that
    arrived on a different link, so it is only attached for LoRa -- the same
    LINK_LORA gate the caller already applies to rssi/snr, kept consistent
    here rather than writing a LoRa noise floor into an FM row.
    """
    try:
        conditions = _channel_conditions() if link == LINK_LORA else None
        row = _rangetest.note_frame(
            local_id=_NODE_ID,
            peer_id=sender,
            peer_name=_peer_label(sender),
            seq=seq,
            rssi=rssi,
            snr=snr,
            noise_floor=None if conditions is None else conditions["noise_floor"],
            link=link,
        )
        line = rangetest.format_row(
            row, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with _rangetest_lock:
            handle = _rangetest_file
            if handle is None:
                return
            handle.write(line + "\n")
            handle.flush()   # a range test that loses its tail to a buffer
                             # loses precisely the far end of the walk
    except Exception as e:
        RNS.log("[cot-bridge] range test row failed: %s" % e, RNS.LOG_DEBUG)


def _range_test_beacon_fix():
    """Our own latest fix as a beacon `fix` field, while a range test runs.

    None when no test is running -- the 13 bytes are paid only for the length
    of a test -- and None when we have no fix, or none recent enough to be
    worth sending (rangetest's own max_fix_age_s). See encode_beacon's `fix`
    for why the beacon carries it at all.

    The time sent is when the fix was TAKEN, rebuilt from the book's monotonic
    `at`, so the far end ages it correctly however long it sat here first.
    """
    try:
        with _rangetest_lock:
            if _rangetest_file is None:
                return None
            book = _rangetest
        own = book.fix_for(_NODE_ID)
        if own is None:
            return None
        age = time.monotonic() - own.at
        if age > book.max_fix_age_s:
            return None
        return cot_codec.pack_beacon_fix(own.lat, own.lon, time.time() - age)
    except Exception as e:
        RNS.log("[cot-bridge] range test fix not attached: %s" % e,
                RNS.LOG_DEBUG)
        return None


def _book_beacon_fix(sender, frame):
    """A directly heard beacon's range-test fix -> the position book.

    Only while WE run a range test (nothing else reads the book's peer
    entries), and only a fix no older than one already held: the same fix
    rides every beacon until the sender's ATAK reports a new one, and booking
    a repeat would count it as a stale report it is not. Never raises.

    THE CLOCK CHECK IS ALSO THE PADDING CHECK. A padded diagnostic beacon has
    random bytes where the fix would be, and about one in a thousand reads as
    a well-formed 12-byte field; requiring its time within max_fix_age_s of
    our clock takes that to about one in a billion. Two phones with clocks
    15 minutes apart would lose their beacon fixes to it, and say so here.
    """
    try:
        if _rangetest_file is None or not cot_codec.is_beacon(frame):
            return
        fix = cot_codec.decode_beacon(frame, sender).fix
        if fix is None:
            return
        book = _rangetest
        if abs(time.time() - fix.time) > book.max_fix_age_s:
            RNS.log("[cot-bridge] range test: ignoring %s's beacon fix, its "
                    "time is %+ds from ours" % (_peer_label(sender),
                                                fix.time - time.time()),
                    RNS.LOG_DEBUG)
            return
        held = book.fix_for(sender)
        if held is not None and held.stamp is not None and fix.time <= held.stamp:
            return
        book.note_position(sender, fix.lat, fix.lon, stamp=fix.time)
    except Exception as e:
        RNS.log("[cot-bridge] range test beacon fix failed: %s" % e,
                RNS.LOG_DEBUG)


def start_range_test():
    """Begin a new CSV. Returns the filename, or "" if it could not start."""
    global _rangetest_file, _rangetest_path, _rangetest, _beacon_interval_override
    try:
        os.makedirs(_RANGETEST_DIR, exist_ok=True)
        name = "rangetest-%s.csv" % time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(_RANGETEST_DIR, name)
        handle = open(path, "w")
        handle.write(rangetest.header_line() + "\n")
        handle.flush()
        with _rangetest_lock:
            if _rangetest_file is not None:
                _rangetest_file.close()
            _rangetest_file = handle
            _rangetest_path = path
            # A fresh book as well as a fresh file: positions carried over
            # from a previous run would be joined against new readings and
            # quietly produce distances from wherever the phone used to be.
            _rangetest = rangetest.RangeTest(distance_fn=tx_policy.haversine_m,
                                             clock=time.monotonic,
                                             wall_clock=time.time)
        # See _RANGE_TEST_BEACON_S: the beacon is the sample, so speed it up
        # for exactly as long as the test is running and not one second more.
        _beacon_interval_override = _RANGE_TEST_BEACON_S
        RNS.log("[cot-bridge] range test recording to %s -- beacon now every "
                "%ds (reverts when the test stops)" % (name, _RANGE_TEST_BEACON_S),
                RNS.LOG_NOTICE)
        return name
    except Exception as e:
        RNS.log("[cot-bridge] could not start range test: %s" % e, RNS.LOG_ERROR)
        return ""


def stop_range_test():
    """Close the CSV. Returns a summary line for the UI."""
    global _rangetest_file, _rangetest_path, _beacon_interval_override
    # Before anything that can fail: the override must not be able to survive
    # the run, which is the whole reason it is an override. See
    # _RANGE_TEST_BEACON_S.
    _beacon_interval_override = None
    with _rangetest_lock:
        if _rangetest_file is not None:
            try:
                _rangetest_file.close()
            except Exception:
                pass
        _rangetest_file = None
        _rangetest_path = None
    line = _rangetest.summary_line()
    RNS.log("[cot-bridge] range test stopped -- %s -- beacon back to %ds"
            % (line, _BEACON_INTERVAL_S), RNS.LOG_NOTICE)
    return line


def range_test_status():
    """Recording state and row count, as JSON, for the status screen."""
    with _rangetest_lock:
        running = _rangetest_file is not None
        name = os.path.basename(_rangetest_path) if _rangetest_path else ""
    return json.dumps({
        "running": running,
        "file": name,
        "rows": _rangetest.rows,
        "no_fix": _rangetest.skipped_no_fix,
        "stale_refused": _rangetest.stale_refused,
    })


def fm_test_rangetest(on):
    """adb hook (debug builds): start ("1") or stop ("0") the range test, so a
    two-phone run can be started on both ends together without the UI."""
    if str(on).strip() in ("0", "off", "false", "stop"):
        line = stop_range_test()
    else:
        line = start_range_test() or "could not start"
    print("[fm-test] rangetest: %s" % line)
    return line


def _channel_conditions():
    """What the radio hears when nobody is talking.

    Every one of these arrives from the RNode already, in the same CHTM stats
    frame that produces RNS's "Radio detected interference" line -- which logs
    ONE of the five values and silently drops the rest. The noise floor and the
    channel load are the two that answer "how usable is this frequency here",
    and until now they were being parsed and thrown away on every update.

    That gap had a cost. The 2026-08-14 range test ended with the link dying at
    -95 dBm while roughly 35 dB of receiver sensitivity was still unspent, and
    two explanations fit equally well: the path was blocked, or the noise floor
    where the phone was standing was far above the noise floor at home. Nothing
    in the logs could tell those apart, because the one measurement that
    separates them was the one being discarded.

    Read live off the interface, the same way _airtime_s() reads .bitrate, so
    there is no state to keep in sync and nothing to go stale.

    Units are percent, and the two windows are 15 s and 1 hour -- taken from
    RNS's own rnstatus, which prints these fields directly as percentages. The
    long window is the honest measure of a location and needs an hour to mean
    anything; the short one is what moves while you walk.
    """
    iface = _lora_iface
    if iface is None:
        return None

    noise = getattr(iface, "r_noise_floor", None)
    interference = getattr(iface, "r_interference", None)

    # Reported only when it clears the floor by enough to be a real signal
    # rather than the receiver recalibrating against itself.
    if (interference is not None and noise is not None
            and interference - noise < _INTERFERENCE_MARGIN_DB):
        interference = None

    return {
        "noise_floor": noise,
        "rssi": getattr(iface, "r_current_rssi", None),
        "load_short": getattr(iface, "r_channel_load_short", None),
        "load_long": getattr(iface, "r_channel_load_long", None),
        # The radio's own measurement of how much of the channel WE occupied,
        # which is worth having next to the duty cycle _status_loop computes
        # from bytes and bitrate. Two independent routes to the same number:
        # if they agree, the airtime model milestones 3, 5 and 6 are sized on
        # is confirmed against the hardware rather than against itself. If they
        # ever disagree, one of them is wrong and it matters which.
        "airtime_short": getattr(iface, "r_airtime_short", None),
        "airtime_long": getattr(iface, "r_airtime_long", None),
        "interference": interference,
    }


def _channel_line():
    """The channel conditions as one status line, or None before the radio has
    reported any. Kept separate from _channel_conditions() so the UI and the
    log format the same numbers without either owning the wording."""
    conditions = _channel_conditions()
    if conditions is None:
        return None
    if _radio_firmware == RADIO_MESHTASTIC:
        # The board's own figures, in its own windows (see mt_link): no noise
        # floor on the boards measured, so the line leads with the load.
        if conditions["load_short"] is None:
            return None
        line = "board: channel busy %.1f%%  ·  our airtime %.1f%% (1h)" % (
            conditions["load_short"], conditions["airtime_long"] or 0.0)
        if conditions["noise_floor"] is not None:
            line += "  ·  noise floor %d dBm" % conditions["noise_floor"]
        return line
    if conditions["noise_floor"] is None:
        return None

    line = ("noise floor %d dBm  ·  load %.1f%% (15s) / %.1f%% (1h)"
            "  ·  our airtime %.1f%% (15s)") % (
        conditions["noise_floor"],
        conditions["load_short"] or 0.0,
        conditions["load_long"] or 0.0,
        conditions["airtime_short"] or 0.0,
    )
    if conditions["interference"] is not None:
        line += "  ·  interference %d dBm (%+d dB over floor)" % (
            conditions["interference"],
            conditions["interference"] - conditions["noise_floor"],
        )
    return line


# True while the radio is off the operating frequency for a survey. Read by
# the TX gate below and by _beacon_loop. Transmitting during a sweep would be
# wrong twice over: it would put our own carrier into the measurement we are
# taking, and it would key up on whatever channel the sweep happens to be
# parked on, which is not a thing to do to a band you are only supposed to be
# listening to.
_sweep_active = False
_sweep_lock = threading.Lock()
_sweep_state = None       # progress dict for the UI, or None when idle
_sweep_result_lines = []  # the last report, kept so the UI can show it


def _txpower_max_dbm():
    """The highest power the radio in use takes: RNS's limit for an RNode, the
    board's for Meshtastic -- whose managed profile would otherwise cap a saved
    37 dBm at 30 without anyone being told."""
    if _radio_firmware == RADIO_MESHTASTIC:
        import mt_config
        return mt_config.TX_POWER_MAX_DBM
    return radio.TXPOWER_MAX_DBM


def get_radio_config():
    """Current settings plus the preset list, as JSON, for the UI."""
    config = _load_radio_config()
    return json.dumps({
        "preset": config["preset"],
        "frequency_hz": config["frequency_hz"],
        "frequency_mhz": "%.3f" % (config["frequency_hz"] / 1e6),
        "txpower_dbm": config["txpower_dbm"],
        "txpower_max_dbm": _txpower_max_dbm(),
        "presets": radio.preset_names(),
        # Parallel to "presets" and indexed together: the UI shows these and
        # sends back the plain name, so the label can say whatever an operator
        # needs without becoming part of the stored value.
        "preset_labels": radio.preset_labels(),
        "band": radio.band_name(config["frequency_hz"]) or "",
        "summary": radio.describe(config["preset"], config["frequency_hz"],
                                  config["txpower_dbm"]),
        "bitrate": int(radio.bitrate_bps(config["preset"]) or 0),
    })


# A Meshtastic board set to transmit power 0 uses "the default", which it
# does not report as a number: the firmware takes the region's limit (30 dBm
# in the US) capped at what the board's radio can do -- 22 dBm for the SX1262
# on every board this project has used. The coverage label says "assumed".
_MT_DEFAULT_TX_DBM_ASSUMED = 22


def coverage_radio():
    """Kotlin entry point: the LoRa settings the Coverage tab should model --
    those of the radio IN USE where it can be asked, as JSON {preset,
    frequency_mhz, txpower_dbm, source}. `source` says where the numbers
    came from, for the coverage label. Never raises.

      RNode         the power and frequency the RNode REPORTS (r_txpower,
                    r_frequency -- what it is actually doing; the saved
                    setting can differ when the radio clamped it), with the
                    app's preset, which is what it was configured with.
      Meshtastic    the board's own LoRa config: its power (0 -> the board
                    default, assumed _MT_DEFAULT_TX_DBM_ASSUMED), its preset
                    when it uses one the app has a name for, its frequency
                    override when set. A board need not run the app's
                    settings (the managed profile is optional), so the
                    app's would be the wrong numbers.
      otherwise     the app's Radio settings, saying the radio is not
                    connected.
    """
    try:
        config = _load_radio_config()
        out = {"preset": config["preset"],
               "frequency_mhz": "%.3f" % (config["frequency_hz"] / 1e6),
               "txpower_dbm": int(config["txpower_dbm"]),
               "source": "app Radio settings, radio not connected"}
        iface = _lora_iface
        if iface is None:
            return json.dumps(out)
        if _radio_firmware == RADIO_MESHTASTIC:
            port = getattr(iface, "port", None)
            if port is None or not port.online or port.node() is None:
                return json.dumps(out)
            import mt_config
            snap = mt_config.snapshot(port.node())
            notes = []
            power = int(snap.get("tx_power_dbm") or 0)
            if power <= 0:
                power = _MT_DEFAULT_TX_DBM_ASSUMED
                notes.append("board default power, assumed %d dBm" % power)
            out["txpower_dbm"] = power
            if snap.get("preset_name"):
                out["preset"] = snap["preset_name"]
            else:
                notes.append("board preset %s has no app name, using %s"
                             % (snap.get("preset"), out["preset"]))
            if snap.get("override_frequency_mhz"):
                out["frequency_mhz"] = "%.3f" % snap["override_frequency_mhz"]
            else:
                notes.append("board frequency from its slot, using %s MHz"
                             % out["frequency_mhz"])
            out["source"] = "; ".join(["Meshtastic board"] + notes)
            return json.dumps(out)
        reported_power = getattr(iface, "r_txpower", None)
        if not getattr(iface, "online", False) or reported_power is None:
            return json.dumps(out)
        out["txpower_dbm"] = int(reported_power)
        reported_freq = getattr(iface, "r_frequency", None)
        if reported_freq:
            out["frequency_mhz"] = "%.3f" % (reported_freq / 1e6)
        out["source"] = "reported by the RNode"
        return json.dumps(out)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})


def set_radio_config(preset, frequency_mhz, txpower_dbm):
    """Change the radio settings. Returns "" on success, else why not.

    Applied to the live radio rather than only on the next launch. The setting
    that matters most here is the one you are most likely to get wrong, and
    making someone restart the app to try 903 MHz instead of 915 turns a
    ten-second experiment into a minute -- which, in a field test, means it
    does not get tried.

    Every parameter is pushed and then READ BACK. RNS's own validate_config()
    is what catches a firmware that silently clamped something, and skipping it
    here would let a radio come up on settings nobody chose.
    """
    global _radio_config

    try:
        frequency_hz = int(round(float(frequency_mhz) * 1e6))
    except (TypeError, ValueError):
        return "Frequency must be a number in MHz"

    problem = radio.validate(preset, frequency_hz, txpower_dbm)
    if problem is not None:
        return problem
    if int(txpower_dbm) > _txpower_max_dbm():
        return ("TX power must be %d dBm or less on this radio"
                % _txpower_max_dbm())

    config = {"preset": preset, "frequency_hz": frequency_hz,
              "txpower_dbm": int(txpower_dbm)}

    with _radio_config_lock:
        _radio_config = dict(config)
    if not _save_radio_config(config):
        # Not fatal, but it will silently revert on restart, and a setting that
        # quietly un-does itself is worse than one that refuses.
        RNS.log("[cot-bridge] could not persist radio settings", RNS.LOG_ERROR)

    # A Meshtastic board is not pushed anything from here: these settings are
    # the source of its MANAGED PROFILE, and the board takes them only through
    # "Use the app's settings", which shows the change and confirms it. Saving
    # them is the whole job. Refusing (as this did until 2026-10-14) left the
    # profile stuck on whatever an RNode last used -- found on a phone whose
    # saved Short Slow would have moved its board off the Long Fast network.
    if _radio_firmware == RADIO_MESHTASTIC:
        RNS.log("[cot-bridge] radio settings saved for the Meshtastic board's "
                "profile: %s" % radio.describe(preset, frequency_hz, int(txpower_dbm)),
                RNS.LOG_NOTICE)
        return ""

    iface = _lora_iface
    if iface is None:
        return ""       # not started yet; start() will pick this up

    if _sweep_active:
        return "A survey is running -- stop it first"

    sf, bandwidth, coding_rate = radio.preset_params(preset)
    try:
        iface.frequency = frequency_hz
        iface.bandwidth = bandwidth
        iface.txpower = int(txpower_dbm)
        iface.sf = sf
        iface.cr = coding_rate

        # Blanked so the wait below cannot be satisfied by the values the radio
        # reported for the PREVIOUS settings, which on a no-op change would
        # match immediately and prove nothing.
        iface.r_frequency = None
        iface.r_bandwidth = None
        iface.r_txpower = None
        iface.r_sf = None
        iface.r_cr = None

        iface.setFrequency()
        iface.setBandwidth()
        iface.setTXPower()
        iface.setSpreadingFactor()
        iface.setCodingRate()

        if not _await_radio_readback(iface):
            # The radio may well have taken the settings anyway -- the KISS
            # commands went out and are not rolled back -- so this says what is
            # actually known rather than claiming the change failed. A fixed
            # 1 s sleep here reported failure for a change that had in fact
            # applied, which is worse than not checking: it sends the operator
            # looking for a hardware fault that is not there.
            RNS.log("[cot-bridge] radio settings sent but not confirmed within "
                    "the timeout", RNS.LOG_ERROR)
            return ("Sent, but the radio did not confirm in time -- check the "
                    "settings line before relying on it")

        # If the board clamped the power, take ITS number as the truth and
        # persist that.
        #
        # This is not cosmetic. RNS aborts RNode startup outright when the
        # reported parameters do not match the configured ones -- "Aborting
        # RNode startup", serial closed, interface never online. So a power the
        # hardware cannot deliver would apply fine now and then present as a
        # completely dead radio on the next launch, recoverable only by
        # deleting the settings file over adb. Storing what the radio actually
        # did means the saved config is always one the radio will accept again.
        clamped = None
        reported_power = getattr(iface, "r_txpower", None)
        if reported_power is not None and reported_power != iface.txpower:
            clamped = (iface.txpower, reported_power)
            iface.txpower = reported_power
            config["txpower_dbm"] = reported_power
            with _radio_config_lock:
                _radio_config = dict(config)
            _save_radio_config(config)

        # validateRadioState(), not validate_config(): RNS's own name, and
        # getting it wrong cost a bench cycle here. It re-reads every echoed
        # parameter and logs which one disagrees, which is the difference
        # between "the radio refused that" and a silent wrong setting.
        if not iface.validateRadioState():
            return ("The radio did not accept those settings -- see the log "
                    "for which one")

        iface.updateBitrate()
        _resize_reassembly(iface)
    except Exception as e:
        # Logged as well as returned. Returning it only to the caller puts the
        # reason in a toast that is gone in three seconds and in no file at
        # all -- which is how this branch swallowed a real failure during the
        # 2026-08-15 bench and left the radio changed, the settings saved, and
        # nothing anywhere saying why the confirmation never appeared.
        RNS.log("[cot-bridge] could not apply radio settings: %r" % (e,),
                RNS.LOG_ERROR)
        return "Could not apply: %s" % e

    RNS.log("[cot-bridge] radio set to %s"
            % radio.describe(preset, frequency_hz, config["txpower_dbm"]),
            RNS.LOG_NOTICE)

    if clamped is not None:
        RNS.log("[cot-bridge] radio clamped TX power %d -> %d dBm; saved the "
                "radio's figure" % clamped, RNS.LOG_WARNING)
        # Boards clamp at BOTH ends, which this message used to deny: asking a
        # Heltec v3 for 2 dBm gets 13 back, and reporting that as "tops out at
        # 13" describes a radio that also does 28. Measured on the bench
        # 2026-08-18, where the floor is what mattered -- a stress run wants
        # the radios quiet, not loud.
        asked, got = clamped
        edge = "will not go below" if got > asked else "tops out at"
        return ("Set, but this radio %s %d dBm — saved %d, not %d"
                % (edge, got, got, asked))
    return ""


# KISS command the RNode firmware uses for its own Bluetooth: 0x00 off,
# 0x01 on, 0x02 on and discoverable for the next minute or so. The same
# command rnodeconf's -b / -p flags send down a cable.
_CMD_BT_CTRL = 0x46
_BT_STATES = {"off": 0x00, "on": 0x01, "pair": 0x02}

# How long a reported pairing code is worth showing. The radio's own pairing
# window is about a minute; past that the code is stale and offering it is
# worse than offering nothing.
_BT_PIN_VALID_S = 120.0


def get_bt_pairing_pin():
    """The last pairing code the radio reported, or "" if there isn't a fresh one.

    Kotlin entry point.
    """
    with _bt_pin_lock:
        if _bt_pin is None:
            return ""
        pin, when = _bt_pin
        if time.monotonic() - when > _BT_PIN_VALID_S:
            return ""
        return "%06d" % pin


def enable_rnode_bluetooth(mode):
    """Switch the radio's own Bluetooth on, or put it into pairing mode.

    Kotlin entry point. Returns a message for the UI.

    Exists because of a chicken and egg: a radio ships with Bluetooth off, and
    the only way to turn it on is to send it a command over a link you already
    have -- which normally means a laptop and rnodeconf. This app is already
    holding that link, so it can do it in one tap over the cable that is
    plugged in right now. The setting lives in the radio's own storage, so it
    is once per radio, not once per session.

    Deliberately reports only that the command was SENT. The firmware does not
    acknowledge this one, so claiming Bluetooth is now on would be a guess --
    and a wrong guess here sends someone hunting through phone settings for a
    radio that never started advertising.
    """
    if _radio_firmware == RADIO_MESHTASTIC:
        return _NOT_AN_RNODE % "Bluetooth pairing from here"
    iface = _lora_iface
    if iface is None or not getattr(iface, "online", False):
        return ("No link to the radio right now -- plug in the USB cable and "
                "wait for the radio to come up, then try again")

    state = _BT_STATES.get(str(mode).lower())
    if state is None:
        return "Unknown Bluetooth mode %r" % (mode,)

    # Discard any earlier code before asking for a new one, so a stale one from
    # a previous attempt cannot be mistaken for this one's answer -- typing
    # last minute's code into Android's prompt fails in a way that looks like
    # the radio refusing to pair.
    global _bt_pin
    with _bt_pin_lock:
        _bt_pin = None

    command = bytes([0xC0, _CMD_BT_CTRL, state, 0xC0])
    try:
        with _rnode_tx_lock:
            written = iface.serial.write(command)
    except Exception as e:
        RNS.log("[cot-bridge] could not send Bluetooth command to the radio: %r"
                % (e,), RNS.LOG_ERROR)
        return "Could not reach the radio: %s" % e

    if written != len(command):
        return "The radio only took %d of %d bytes" % (written, len(command))

    RNS.log("[cot-bridge] sent Bluetooth %s command to the radio" % mode,
            RNS.LOG_NOTICE)
    if state == 0x02:
        # The radio answers a pairing request with the code Android will ask
        # for. Wait for it rather than returning immediately: without the code
        # this button leaves someone staring at a PIN prompt with nowhere to
        # read the PIN, which is exactly the dead end it exists to prevent.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            pin = get_bt_pairing_pin()
            if pin:
                return ("Pairing code: %s\n\nThe radio is discoverable for "
                        "about a minute. Tap it in the list and enter this "
                        "code when Android asks." % pin)
            time.sleep(0.05)
        return ("Radio is discoverable for about a minute -- scan for it now, "
                "and pair when it appears.\n\nIt did not report a pairing "
                "code. If Android asks for one, hold the radio's button for "
                "five seconds and read the code off its screen.")
    if state == 0x01:
        return ("Bluetooth switched on at the radio. Scan for it, pair, then "
                "switch this phone's link to Bluetooth")
    return "Bluetooth switched off at the radio"


# The firmware's own "flash the light" command: one payload byte, the number of
# blinks, passed straight to led_indicate_info(). Nothing in RNS or rnodeconf
# sends it -- they only name it -- so this is the one command here that is not
# a copy of something upstream does.
_CMD_BLINK = 0x30

# Long enough to find by looking rather than by staring, and short enough that
# two radios asked in quick succession do not overlap into one blur.
_IDENTIFY_BLINKS = 12


def identify_radio():
    """Flash the light on the radio THIS phone is using.

    Kotlin entry point. Returns a message for the UI.

    Answers the question a cable used to answer by existing: with two radios in
    a bag and two phones in two pockets, nothing on either screen tells you
    which is paired to which. A name does not help -- "RNode 3f2a" is not
    written on the box.

    Reports what it asked for, not what happened: the firmware does not
    acknowledge this, and boards differ in what they light up.
    """
    if _radio_firmware == RADIO_MESHTASTIC:
        return _NOT_AN_RNODE % "Identify-by-blinking"
    iface = _lora_iface
    if iface is None or not getattr(iface, "online", False):
        return "No link to the radio right now, so there is nothing to flash"

    command = bytes([0xC0, _CMD_BLINK, _IDENTIFY_BLINKS, 0xC0])
    try:
        with _rnode_tx_lock:
            written = iface.serial.write(command)
    except Exception as e:
        RNS.log("[cot-bridge] could not send identify command: %r" % (e,),
                RNS.LOG_ERROR)
        return "Could not reach the radio: %s" % e

    if written != len(command):
        return "The radio only took %d of %d bytes" % (written, len(command))

    RNS.log("[cot-bridge] asked the radio to flash %d times" % _IDENTIFY_BLINKS,
            RNS.LOG_NOTICE)
    return ("The radio this phone is using should be flashing now. If nothing "
            "lights up, this board may have no indicator the firmware can use.")


def _load_display_enabled():
    """Whether the radio's screen is ours, remembered across restarts."""
    global _display_enabled
    with _display_lock:
        if _display_enabled is not None:
            return _display_enabled
        enabled = False
        try:
            with open(_RADIO_DISPLAY_PATH) as f:
                enabled = json.load(f).get("enabled") is True
        except FileNotFoundError:
            pass
        except Exception as e:
            RNS.log("[cot-bridge] could not read the screen setting (%s); "
                    "leaving the radio's own display alone" % e, RNS.LOG_WARNING)
        _display_enabled = enabled
        return enabled


def get_radio_display():
    """Kotlin entry point. "on" when the radio is showing our status."""
    return "on" if _load_display_enabled() else "off"


def set_radio_display(mode):
    """Take over the radio's screen, or give it back. Returns a UI message."""
    global _display_enabled
    want = str(mode).lower() in ("on", "true", "1", "yes")

    _load_display_enabled()
    with _display_lock:
        _display_enabled = want
    try:
        with open(_RADIO_DISPLAY_PATH, "w") as f:
            json.dump({"enabled": want}, f)
    except OSError as e:
        # Not fatal, but it will silently revert on restart, and a setting that
        # quietly un-does itself is worse than one that refuses.
        RNS.log("[cot-bridge] could not save the screen setting: %s" % e,
                RNS.LOG_ERROR)

    iface = _rnode_iface()
    if not want:
        try:
            with _rnode_tx_lock:
                if iface is not None:
                    iface.disable_external_framebuffer()
        except Exception as e:
            RNS.log("[cot-bridge] could not hand the screen back: %r" % (e,),
                    RNS.LOG_ERROR)
        return "The radio is showing its own screens again"

    if iface is None or not getattr(iface, "online", False):
        return "Saved. The screen will fill in once the radio is up"
    if getattr(iface, "display", None) is None:
        # RNS only sets this for boards it knows have a screen, so this is the
        # honest answer rather than leaving someone waiting for a picture that
        # is never coming.
        return "This board reports no screen the firmware can draw on"
    return "The radio's screen is now showing this node"


def set_radio_label(name):
    """What the radio calls itself, which only the phone's Bluetooth stack knows.

    Kotlin entry point, called at startup. Empty over USB.
    """
    global _radio_label
    _radio_label = (name or "").strip()[:32]
    return _radio_label


def set_transport(transport):
    """Which RadioLink transport Kotlin has selected -- "usb", "ble", or
    "fm". Kotlin entry point, called at startup, before start().
    """
    global _transport
    _transport = (transport or "usb").strip().lower()
    return _transport


def set_radio_firmware(name):
    """Which firmware the LoRa board runs (RadioLink.radioFirmware()). Kotlin
    entry point, called at startup, before start(). Anything unrecognised is
    treated as an RNode, the only firmware every earlier build knew."""
    global _radio_firmware
    name = (name or RADIO_RNODE).strip().lower()
    _radio_firmware = name if name in (RADIO_RNODE, RADIO_MESHTASTIC) else RADIO_RNODE
    return _radio_firmware


def _rnode_iface():
    """The LoRa interface, but only when it is an RNode. Everything that sends
    KISS commands (the screen mirror, Bluetooth pairing, the identify blink, a
    spectrum survey) asks for this rather than `_lora_iface`, because on a
    Meshtastic board those commands do not exist and attributes like
    `.serial` are not there to be written to."""
    return _lora_iface if _radio_firmware == RADIO_RNODE else None


_NOT_AN_RNODE = ("%s is an RNode feature; this radio is a Meshtastic board, "
                 "which is set up in the Meshtastic app")


# ---------------------------------------------------------------------------
# A Meshtastic board's own settings, changed from here so the Meshtastic app is
# not needed to set one up. The logic lives in mt_config (pure, tested against
# fakes); this is the sequencing around a real board: apply, let it restart,
# reconnect, READ BACK, and report what actually took.
#
# Imported lazily, like mt_port, so a problem with the meshtastic package costs
# these screens and not the app.
# ---------------------------------------------------------------------------

_mesh_config_lock = threading.Lock()
# Let the board finish saving before the link is dropped to re-read it.
_MESH_SETTLE_S = 3.0
# How long to wait for the board to be back. A change that does not restart the
# board is back in about a second (measured, RAK4631 on 2.7.15); this ceiling
# is for one that does.
_MESH_COMEBACK_S = 75.0
# Settings that make the board restart, and WHEN. MEASURED for the preset: the
# board's USB dropped 9.2-9.4 s after the write, twice, and was back on USB
# 0.5-0.7 s later. A first version returned "confirmed" from a read-back taken
# BEFORE that restart, so it reported success a few seconds ahead of the link
# dropping, and had not shown the setting survives a restart. MEASURED for the
# frequency override (2026-10-14, Tracker L1 and RAK4631): a managed-profile
# apply whose only radio-group change was the override restarted both boards,
# and the read-back after the restart confirmed it. Region and frequency slot
# sit in the same radio group and are still ASSUMED to behave the same.
# Transmit power, repeat mode and channel name were measured NOT to restart
# the board.
# The board's GPS setting is in this set UNMEASURED: whether a position-config
# write restarts the board was not cleanly separable from the other writes on
# the bench, and watching for a restart that never comes costs only the watch.
_MESH_RESTART_FIELDS = frozenset(("region", "preset", "channel_num",
                                  "override_frequency_mhz", "gps_mode"))
# How long to watch for the restart after the first read-back: the measured
# delay, plus margin.
_MESH_RESTART_WATCH_S = 25.0


def _mesh_board():
    """(port, reason). The connected Meshtastic port, or why there isn't one."""
    if _radio_firmware != RADIO_MESHTASTIC:
        return None, "This radio is not a Meshtastic board"
    port = getattr(_lora_iface, "port", None)
    if port is None:
        return None, "The Meshtastic link is not running"
    if not port.online or port.node() is None:
        return None, ("The board is not connected right now -- check the "
                      "cable and wait for it to come up")
    return port, None


def get_mesh_board_config():
    """Kotlin entry point. The board's current settings and the choices the
    settings screen offers, as JSON. Never raises. Contains no key."""
    try:
        import mt_config
        port, reason = _mesh_board()
        if port is None:
            return json.dumps({"ok": False, "reason": reason})
        names = radio.preset_names()
        return json.dumps({
            "ok": True,
            "snapshot": mt_config.snapshot(port.node()),
            "regions": mt_config.REGIONS,
            "presets": names,
            "preset_labels": [radio.preset_label(n) for n in names],
            "rebroadcast_choices": [
                {"value": v, "label": l}
                for v, l in mt_config.REBROADCAST_CHOICES.items()],
            "tx_power_max": mt_config.TX_POWER_MAX_DBM,
            "name_max": mt_config.CHANNEL_NAME_MAX,
        })
    except Exception as e:
        RNS.log("[cot-bridge] reading the board's settings failed: %r" % (e,),
                RNS.LOG_ERROR)
        return json.dumps({"ok": False, "reason": "Could not read the board: %s" % e})


# (passphrase, mesh key). derive_key() is 200k PBKDF2 rounds and the profile
# check runs on every status poll, so the key is derived once per passphrase.
_board_key_cache = None


def _board_mesh_key():
    """The mesh key the board's channel is derived from, or None."""
    global _board_key_cache
    try:
        passphrase, _ = _load_passphrase()
    except Exception:
        return None
    if not passphrase:
        return None
    cached = _board_key_cache
    if cached is not None and cached[0] == passphrase:
        return cached[1]
    key = mesh.derive_key(passphrase)
    _board_key_cache = (passphrase, key)
    return key


def _managed_board_request(port):
    """(request, problems): the settings the APP wants on this board -- its
    preset, frequency and power, a channel derived from the passphrase, never
    repeat (Phase 3 of the parity plan; mt_config.managed_request says what is
    owned and why). The request holds the channel key as bytes: it must not be
    logged or turned into JSON. Takes the PORT because the Bluetooth PIN needs
    the board's node number, which the port read at connect."""
    import mt_config
    node = port.node()
    key = _board_mesh_key()
    if key is None:
        return None, ["There is no mesh passphrase yet"]
    config = _load_radio_config()
    request = mt_config.managed_request(
        key, config["preset"], config["frequency_hz"], config["txpower_dbm"],
        current_region=mt_config.snapshot(node)["region"],
        current_gps=mt_config.snapshot(node)["gps_mode"],
        node_num=port.node_num)
    return request, mt_config.profile_problems(request)


def _board_profile_changes(port):
    """(changes, problem) between the board and the managed profile. `changes`
    are mt_config.plan's dicts, labels and descriptions only (no key)."""
    import mt_config
    request, problems = _managed_board_request(port)
    if problems:
        return [], "; ".join(problems)
    the_plan = mt_config.plan(port.node(), request)
    if the_plan.problems:
        return [], "; ".join(the_plan.problems)
    return the_plan.changes, ""


def _mesh_board_status():
    """Counters and warnings for the status screen: {} unless this is a
    Meshtastic board. Never raises -- it is called on every status poll."""
    if _radio_firmware != RADIO_MESHTASTIC:
        return {}
    try:
        import mt_config
        port = getattr(_lora_iface, "port", None)
        if port is None:
            return {}
        warns = []
        profile_changes, profile_problem = [], ""
        if port.online and port.node() is not None:
            warns = mt_config.warnings(mt_config.snapshot(port.node()))
            try:
                profile_changes, profile_problem = _board_profile_changes(port)
            except Exception as e:
                profile_problem = "Could not compare the board with the app: %s" % e
        return {
            "online": bool(port.online),
            "sent": port.sent,
            "received": port.received,
            "queue_rejected": port.board_rejected,
            "send_refused_full": port.send_refused_full,
            "tx_refused": dict(port.tx_refused),
            "last_error": port.last_error,
            "connections": port.generation,
            "heartbeat_answered": port.heartbeat_answered,
            "heartbeat_missed": port.heartbeat_missed,
            "watchdog_restarts": port.watchdog_restarts,
            "dropped_stale": port.dropped_stale,
            # The board's own channel figures and counters since this
            # connection ({} until it has reported): relayed and heard_twice
            # are how repeating shows up, since duplicates never reach us.
            "telemetry": port.telemetry() if hasattr(port, "telemetry") else {},
            "warnings": warns,
            # Where the board differs from the app's own settings, as labels.
            # Non-empty means the status screen offers the one-tap apply.
            "profile_changes": ["%s: %s -> %s" % (c["label"], c["old"], c["new"])
                                for c in profile_changes],
            "profile_problem": profile_problem,
        }
    except Exception as e:
        return {"error": str(e)}


def get_mesh_bt_pin(address="", name=""):
    """Kotlin entry point: the Bluetooth PIN the board at this address carries
    once the app's settings are applied (mt_config.derive_bt_pin), as six
    digits, or "" when there is no passphrase yet or the address is not one.
    For the pairing dialog -- a board with no screen has nowhere else to read
    it from. Each board has its own; the node number it is derived from comes
    from the address and advertised name (mt_config.node_num_from_ble).

    Works with no board connected: the point is pairing one that is not
    reachable yet. Never logged."""
    try:
        import mt_config
        key = _board_mesh_key()
        node_num = mt_config.node_num_from_ble(address, name)
        if key is None or node_num is None:
            return ""
        return "%06d" % mt_config.derive_bt_pin(key, node_num)
    except Exception:
        return ""


def get_mesh_board_url():
    """Kotlin entry point. The share link for the board's channel and radio
    settings -- which CONTAINS THE CHANNEL KEY, so it is only produced when the
    operator asks. "" if there is no board to ask."""
    try:
        import mt_config
        port, _ = _mesh_board()
        return "" if port is None else mt_config.make_url(port.node())
    except Exception as e:
        RNS.log("[cot-bridge] building the share link failed: %r" % (e,),
                RNS.LOG_ERROR)
        return ""


def preview_mesh_board_config(request_json):
    """Kotlin entry point. What `set_mesh_board_config` WOULD change for this
    request, without sending anything: JSON {"ok", "changes", "message"}. The
    settings screen shows this and asks before applying, because these are
    changes to a radio's key and frequency."""
    try:
        import mt_config
        port, reason = _mesh_board()
        if port is None:
            return json.dumps({"ok": False, "changes": [], "message": reason})
        try:
            request = json.loads(request_json or "{}")
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except ValueError:
            return json.dumps({"ok": False, "changes": [],
                               "message": "The settings request was not understood"})
        typed = request.pop("psk", None)
        if typed:
            try:
                request["psk_bytes"] = mt_config.parse_psk(typed)
            except mt_config.MeshConfigError as e:
                return json.dumps({"ok": False, "changes": [], "message": str(e)})
        the_plan = mt_config.plan(port.node(), request)
        if the_plan.problems:
            return json.dumps({"ok": False, "changes": [],
                               "message": "; ".join(the_plan.problems)})
        return json.dumps({"ok": True, "changes": the_plan.changes, "message": ""})
    except Exception as e:
        return json.dumps({"ok": False, "changes": [],
                           "message": "Could not check the settings: %s" % e})


def _apply_mesh_request(mt_config, port, request):
    """Apply a validated request to the board and CONFIRM it: write, let it
    settle, reconnect, read back, and -- for a change that restarts the board --
    wait out the restart and read back again. Returns the JSON string the
    Kotlin entry points return. The caller holds _mesh_config_lock."""
    try:
        changes = mt_config.apply(port.node(), request)
    except mt_config.MeshConfigError as e:
        return json.dumps({"ok": False, "changes": [], "message": str(e)})
    if not changes:
        return json.dumps({"ok": True, "changes": [], "message":
                           "Nothing to change -- the board already has these "
                           "settings"})

    # Labels and descriptions only; never the key.
    summary = "; ".join("%s: %s -> %s" % (c["label"], c["old"], c["new"])
                        for c in changes)
    RNS.log("[cot-bridge] Meshtastic board settings changed: %s" % summary,
            RNS.LOG_NOTICE)

    time.sleep(_MESH_SETTLE_S)
    port.refresh()
    if not port.wait_online(_MESH_COMEBACK_S):
        return json.dumps({"ok": False, "changes": changes, "message":
                           "The settings were sent, but the board did not "
                           "reconnect within %d s. Check the cable; they may "
                           "or may not have been saved. (%s)"
                           % (_MESH_COMEBACK_S, summary)})

    # READ BACK: whatever the same request would still change is something
    # the board did not take.
    left = mt_config.plan(port.node(), request).changes
    preset = port.board_preset()
    if preset and hasattr(_lora_iface, "set_preset"):
        _lora_iface.set_preset(preset)
    if left:
        return json.dumps({"ok": False, "changes": changes, "message":
                           "The board is back but did not keep: %s"
                           % ", ".join(c["label"] for c in left)})

    # THE FIRST READ-BACK PROVES THE BOARD TOOK THE VALUE, NOT THAT IT
    # KEEPS IT. A change that restarts the board does so seconds later, so
    # wait for the link to drop and come back and read AGAIN: that is the
    # setting surviving a restart, and "done" then means the link is steady.
    restarted = False
    if any(c["field"] in _MESH_RESTART_FIELDS for c in changes):
        deadline = time.time() + _MESH_RESTART_WATCH_S
        while port.online and time.time() < deadline:
            time.sleep(0.25)
        if not port.online:
            restarted = True
            if not port.wait_online(_MESH_COMEBACK_S):
                # NOT a failure to apply: the read-back above already showed
                # the board holding every value. What is unconfirmed is only
                # that they survived the restart. Measured on the OnePlus
                # 2026-10-14: the restart re-enumerates the board on USB,
                # Android drops this app's permission to use it and asks
                # again, and this wait ran out with the prompt on screen --
                # the screen then said "Not applied" about settings the board
                # had taken.
                return json.dumps({
                    "ok": False, "unconfirmed": True, "changes": changes,
                    "message":
                        "The board took the new settings and restarted, but has "
                        "not reconnected after %d s, so it is not yet confirmed "
                        "that it kept them. If Android is asking whether this "
                        "app may use the board, tap OK. Then open this screen "
                        "again: it will show any setting the board did not keep. "
                        "(%s)" % (_MESH_COMEBACK_S, summary)})
            left = mt_config.plan(port.node(), request).changes
            preset = port.board_preset()
            if preset and hasattr(_lora_iface, "set_preset"):
                _lora_iface.set_preset(preset)
            if left:
                return json.dumps({"ok": False, "changes": changes, "message":
                                   "The board restarted and did not keep: %s"
                                   % ", ".join(c["label"] for c in left)})
    note = ""
    if any(c["field"] != "tx_power_dbm" and c["field"] != "rebroadcast"
           for c in changes):
        note = (" Every board on this network must have the same region, "
                "preset, frequency slot and channel key, or they will not "
                "hear each other.")
    return json.dumps({"ok": True, "changes": changes, "message":
                       "Applied and confirmed by the board%s: %s.%s"
                       % (" after it restarted" if restarted else "", summary, note)})


def set_mesh_board_config(request_json):
    """Kotlin entry point. Apply the settings in `request_json` to the board.

    Returns JSON {"ok", "message", "changes"}; `message` is written for the
    operator and is shown as-is. The call does not return until the board has
    been reconnected and READ, so "applied" means the board reports it; a few
    seconds normally, longer if the change makes the board restart.
    """
    try:
        import mt_config
    except Exception as e:
        return json.dumps({"ok": False, "changes": [],
                           "message": "Meshtastic support is unavailable: %s" % e})
    if not _mesh_config_lock.acquire(blocking=False):
        return json.dumps({"ok": False, "changes": [],
                           "message": "A change is already being applied"})
    try:
        port, reason = _mesh_board()
        if port is None:
            return json.dumps({"ok": False, "changes": [], "message": reason})
        try:
            request = json.loads(request_json or "{}")
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except ValueError:
            return json.dumps({"ok": False, "changes": [],
                               "message": "The settings request was not understood"})

        # Keys are turned into bytes ONCE, here. "random" would otherwise make a
        # different key each time it is read, and the read-back below compares
        # against the same request.
        try:
            typed = request.pop("psk", None)
            if typed:
                request["psk_bytes"] = mt_config.parse_psk(typed)
        except mt_config.MeshConfigError as e:
            return json.dumps({"ok": False, "changes": [], "message": str(e)})

        return _apply_mesh_request(mt_config, port, request)
    except Exception as e:
        RNS.log("[cot-bridge] applying board settings failed: %r" % (e,),
                RNS.LOG_ERROR)
        return json.dumps({"ok": False, "changes": [],
                           "message": "Could not change the board's settings: %s" % e})
    finally:
        _mesh_config_lock.release()


def preview_mesh_board_profile():
    """Kotlin entry point. How the board differs from the APP's settings (its
    preset, frequency and power, the channel derived from the passphrase, never
    repeat): JSON {"ok", "changes", "message"}, no key. The same comparison the
    status screen's warning comes from, offered before the one-tap apply."""
    try:
        port, reason = _mesh_board()
        if port is None:
            return json.dumps({"ok": False, "changes": [], "message": reason})
        changes, problem = _board_profile_changes(port)
        if problem:
            return json.dumps({"ok": False, "changes": [], "message": problem})
        return json.dumps({"ok": True, "changes": changes, "message": ""})
    except Exception as e:
        return json.dumps({"ok": False, "changes": [],
                           "message": "Could not compare the board with the app: %s" % e})


def apply_mesh_board_profile():
    """Kotlin entry point: the ONE-TAP APPLY. Puts the app's settings on the
    board and confirms them, exactly as set_mesh_board_config does for a typed
    request -- the key never leaves Python, which is why this is its own call
    and not a request the screen builds. Returns the same JSON."""
    try:
        import mt_config
    except Exception as e:
        return json.dumps({"ok": False, "changes": [],
                           "message": "Meshtastic support is unavailable: %s" % e})
    if not _mesh_config_lock.acquire(blocking=False):
        return json.dumps({"ok": False, "changes": [],
                           "message": "A change is already being applied"})
    try:
        port, reason = _mesh_board()
        if port is None:
            return json.dumps({"ok": False, "changes": [], "message": reason})
        request, problems = _managed_board_request(port)
        if problems:
            return json.dumps({"ok": False, "changes": [],
                               "message": "; ".join(problems)})
        return _apply_mesh_request(mt_config, port, request)
    except Exception as e:
        RNS.log("[cot-bridge] applying the app's settings to the board failed: %r"
                % (e,), RNS.LOG_ERROR)
        return json.dumps({"ok": False, "changes": [],
                           "message": "Could not change the board's settings: %s" % e})
    finally:
        _mesh_config_lock.release()


def set_fm_backup_enabled(enabled):
    """Whether Kotlin has FM backup turned on (RadioLink.fmBackupEnabled()).
    Kotlin entry point, called at startup, before start(). See
    _fm_backup_enabled's own comment.
    """
    global _fm_backup_enabled
    _fm_backup_enabled = bool(enabled)
    return _fm_backup_enabled


def set_fm_profile(name, vox=False, custom_spec=""):
    """Which FSK tone placement the FM link uses (RadioLink.fmToneProfile()).
    Kotlin entry point, called at startup, before start(). Returns the profile
    actually in effect, which is the measured default when the stored name is not
    one this build has -- see fm_config.resolve() for why that is a fallback and
    not an error.

    APPLIED HERE rather than left to fm_link's own default because the placement
    is a per-NET decision: every node on an FM net must be on the same one, and
    nothing on the link itself can detect or report a mismatch (a disagreeing
    receiver recovers no frames at all, which presents as a dead link rather than
    as a configuration error). For now the only check is an operator comparing
    the line the status screen shows (see _fm_status()). The one place a mismatch
    COULD be detected automatically is a peer reachable on LoRa or WiFi, where
    the profile name would still be readable -- not built, and deliberately not
    faked: a warning that only fires when FM already works would be worse than
    none.

    `vox` is RadioLink.fmVoxCable(): the cable has no PTT pin, so build bursts
    with the long VOX lead-in. Default False keeps the Digirig Lite path as it
    was, and an older Kotlin caller that passes one argument still works.

    `custom_spec` is RadioLink's stored custom settings (fm_custom JSON), used
    only when `name` is fm_custom.CUSTOM_NAME. A spec that cannot be built
    falls back to the default preset and says why at ERROR -- the operator
    chose a net and this node is no longer on it.

    MUST NOT RAISE: a bad preference should not stop the bridge starting.
    """
    try:
        if (name or "").strip().lower() == fm_custom.CUSTOM_NAME:
            profile, error = fm_link.set_custom(custom_spec, vox=bool(vox))
            if error:
                RNS.log(
                    "[cot-bridge] custom FM settings cannot be used (%s) -- "
                    "falling back to %r (%s). Every node on the net must "
                    "agree, and nothing on this link can report a mismatch."
                    % (error, profile, fm_link.net_code()), RNS.LOG_ERROR)
            else:
                RNS.log("[cot-bridge] FM on custom settings %s"
                        % fm_link.net_code(), RNS.LOG_NOTICE)
            return profile
        profile, substituted = fm_link.set_profile(name, vox=bool(vox))
        if substituted:
            RNS.log(
                "[cot-bridge] FM tone placement %r is not in this build -- "
                "falling back to %r. Every node on the net must agree, and "
                "nothing on this link can report a mismatch."
                % (name, profile), RNS.LOG_ERROR)
        return profile
    except Exception as e:
        RNS.log("[cot-bridge] could not set FM tone profile %r: %s"
                % (name, e), RNS.LOG_ERROR)
        return ""



# ---------------------------------------------------------------------------
# TEST HOOKS for the over-the-air profile comparison (2026-10-12), driven from
# adb by a debug-only receiver in PythonRnsService. They change nothing
# persistent: the profile switch lasts until the process restarts, and the
# stored preference is untouched.
# ---------------------------------------------------------------------------

def fm_test_profile(name):
    """Switch the FM tone profile in this running process and reset the
    receive path (fm_link.set_profile does that). Returns the profile name in
    effect, or "" on failure. Both phones must be switched or nothing decodes."""
    try:
        if name in fm_config.TRIAL_PROFILES:
            profile, substituted = fm_link.set_trial_profile(name), False
        else:
            profile, substituted = fm_link.set_profile(name, vox=False)
        RNS.log(f"[fm-test] profile now {profile!r} ({fm_link.summary_line()})"
                + (" (REQUESTED NAME NOT FOUND, substituted)" if substituted else ""),
                RNS.LOG_NOTICE)
        return profile
    except Exception as e:
        RNS.log(f"[fm-test] profile switch failed: {e}", RNS.LOG_ERROR)
        return ""


def fm_test_custom(spec):
    """As fm_test_profile, for operator-entered settings: `spec` is fm_custom
    JSON or a net code (e.g. "4T-1000/400-400Bd-H1024.2048"). RAISES nothing
    and does NOT fall back -- a test that quietly ran the default would be
    scored as the thing it was not -- so a bad spec leaves the current config
    in place and returns "". Not persistent."""
    try:
        text = (spec or "").strip()
        parsed = (fm_custom.CustomSpec.from_json(text) if text.startswith("{")
                  else fm_custom.parse_net_code(text))
        fm_custom.link_config(parsed)          # refuse before switching
        profile, error = fm_link.set_custom(parsed.to_json())
        if error:
            raise fm_config.ProfileError(error)
        RNS.log(f"[fm-test] custom settings now {fm_link.net_code()} "
                f"({fm_link.summary_line()})", RNS.LOG_NOTICE)
        return fm_link.net_code()
    except Exception as e:
        RNS.log(f"[fm-test] custom settings refused: {e}", RNS.LOG_ERROR)
        return ""


def fm_tune_send():
    """Kotlin/adb entry point: transmit the tone sweep (fm_tune) on FM.
    Returns its length in seconds, or 0 when FM is not running."""
    if _fm_iface is None:
        RNS.log("[fm-tune] sweep NOT sent: the FM link is not up (yet)", RNS.LOG_WARNING)
        return 0
    try:
        s = fm_link.tune_send()
        RNS.log("[fm-tune] sweep queued: %.1f s on air" % s, RNS.LOG_NOTICE)
        return s
    except Exception as e:
        RNS.log("[fm-tune] sweep could not be queued: %s" % e, RNS.LOG_ERROR)
        return 0


def fm_tune_listen(seconds=30):
    """Kotlin/adb entry point: record `seconds` of FM audio and measure the
    sweep in it. Scheduled beacons pause while it listens -- this phone hears
    nothing while it transmits -- and come back when the result is in.
    Poll fm_tune_status(). Returns the seconds it will listen, 0 if FM is
    not running."""
    global _beacon_enabled
    if _fm_iface is None:
        RNS.log("[fm-tune] cannot listen: the FM link is not up (yet)", RNS.LOG_WARNING)
        return 0
    if isinstance(seconds, str):
        seconds = float(seconds or 30)
    was = _beacon_enabled
    _beacon_enabled = False
    got = fm_link.tune_listen(seconds)
    RNS.log("[fm-tune] listening %.0f s for a sweep (beacons paused)" % got,
            RNS.LOG_NOTICE)

    def restore():
        global _beacon_enabled
        # Generous: the analysis is pure Python on a phone that is also
        # running the receiver. A timeout is reported as one, below.
        deadline = time.time() + got + 300
        while time.time() < deadline:
            st = json.loads(fm_link.tune_status())
            if st["state"] in ("done", "idle"):
                break
            time.sleep(1)
        _beacon_enabled = was
        st = json.loads(fm_link.tune_status())
        r = st.get("result") or {}
        if st["state"] not in ("done", "idle"):
            RNS.log("[fm-tune] gave up waiting for the measurement (still %s "
                    "after %.0f s); beacons resumed" % (st["state"], got + 300),
                    RNS.LOG_WARNING)
        elif st["state"] == "idle":
            RNS.log("[fm-tune] listening cancelled; beacons resumed", RNS.LOG_NOTICE)
        elif r.get("found"):
            RNS.log("[fm-tune] sweep measured: band %s Hz, tilt %s dB/kHz, "
                    "best match %s" % (r.get("band_hz"), r.get("tilt_db_per_khz"),
                                       r.get("score")), RNS.LOG_NOTICE)
            for c in (r.get("recommendation") or {}).get("candidates", []):
                RNS.log("[fm-tune]   candidate %s: tones %s, spread %s dB, "
                        "burst %s s %s" % (c["net_code"], c["tones_hz"],
                                           c["spread_db"], c["burst_s"],
                                           "; ".join(c["notes"])), RNS.LOG_NOTICE)
            ctl = (r.get("recommendation") or {}).get("control")
            if ctl:
                RNS.log("[fm-tune]   control preset %s: spread %s dB"
                        % (ctl["preset"], ctl["spread_db"]), RNS.LOG_NOTICE)
        else:
            RNS.log("[fm-tune] no sweep measured: %s" % r.get("error"),
                    RNS.LOG_NOTICE)

    threading.Thread(target=restore, daemon=True, name="fm-tune-wait").start()
    return got


def fm_tune_status():
    return fm_link.tune_status()


def fm_tune_cancel():
    fm_link.tune_cancel()
    return True


def fm_test_beacons(enabled):
    """Pause or resume the scheduled beacons so they do not share the channel
    with a test train. The early confirm beacon is a beacon too, so it stops."""
    global _beacon_enabled
    # From adb the value arrives as a string, and bool("0") is True.
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("1", "true", "on", "yes")
    _beacon_enabled = bool(enabled)
    RNS.log(f"[fm-test] scheduled beacons {'on' if enabled else 'OFF'}",
            RNS.LOG_NOTICE)
    return _beacon_enabled


def fm_test_train(n, gap_s, label=""):
    """Send `n` numbered beacons on FM, `gap_s` apart, from a background thread.
    Each is a full burst (airtime is flat), the receiver logs every one it
    decodes, and the sender logs its own sequence numbers, so delivery is
    received / sent over the same window. Returns immediately."""
    n = int(n)
    gap_s = float(gap_s)

    def run():
        sent = 0
        for k in range(1, n + 1):
            try:
                if _send_one_beacon(LINK_FM, _next_beacon_seq(), digest=None,
                                    fm_extras=False,
                                    note=f"fm-test {label} {k}/{n}"):
                    sent += 1
            except Exception as e:
                RNS.log(f"[fm-test] send {k}/{n} failed: {e}", RNS.LOG_ERROR)
            time.sleep(gap_s)
        RNS.log(f"[fm-test] train {label!r} done: {sent}/{n} handed to the radio",
                RNS.LOG_NOTICE)

    threading.Thread(target=run, name="fm-test-train", daemon=True).start()
    RNS.log(f"[fm-test] train {label!r} started: {n} beacons, {gap_s:.1f} s apart",
            RNS.LOG_NOTICE)
    return n


def set_wifi_enabled(enabled):
    """Whether Kotlin has the WiFi link turned on (RadioLink.wifiEnabled()).
    Kotlin entry point, called at startup, before start() -- see
    _wifi_enabled's own comment.
    """
    global _wifi_enabled
    _wifi_enabled = bool(enabled)
    return _wifi_enabled


def set_wifi_ifname(ifname):
    """The 802.11s mesh interface name Kotlin has configured
    (RadioLink.wifiIfname()). Kotlin entry point, called at startup, before
    start() -- see _wifi_ifname's own comment.
    """
    global _wifi_ifname
    _wifi_ifname = (ifname or "").strip()
    return _wifi_ifname


def set_link_override(link):
    """Force the active link regardless of _check_auto_switch()'s health
    signal, or clear the override ("auto") to resume automatic switching.

    Kotlin entry point -- but unlike set_transport()/set_fm_backup_enabled()
    above, called LIVE from the status screen's override control, not only
    at startup: this is a runtime knob on an already-running bridge, not a
    link-selection change that needs a restart. Absolute while set: bypasses
    the health/hysteresis check in _check_auto_switch() entirely, which
    keeps computing underneath regardless, so clearing the override later
    resumes from a current reading rather than a stale one. Returns a short
    status string for the UI, matching enable_rnode_bluetooth's convention.

    Logs every real change at NOTICE -- a 2026-09-26 field test spent ~7
    minutes with LoRa unambiguously down and FM healthy, yet never failed
    over, and this function's total silence was the reason it was
    impossible to say afterward whether an override (e.g. an accidental tap
    on the status screen's tiny Force-LoRa/Force-FM/Force-WiFi buttons,
    which sit right where the operator was watching) was the cause. A
    no-op call (already "auto", or already forced to the same link) does
    not log, so repeated redraws/re-applies of an unchanged preference
    don't spam the log the way every other call site here already avoids.
    """
    global _link_override
    link = (link or "auto").strip().lower()
    previous = _link_override or "auto"
    if link == "auto":
        if _link_override is not None:
            RNS.log(
                f"[cot-bridge] link override cleared ({previous} -> auto), "
                "resuming automatic failover",
                RNS.LOG_NOTICE,
            )
        _link_override = None
        return "Automatic"
    if link not in _LINK_PRIORITY:
        return "Unknown link %r" % link
    if _link_ifaces.get(link) is None:
        return "%s is not attached" % link
    if link != previous:
        RNS.log(
            f"[cot-bridge] link override set ({previous} -> {link}), "
            "automatic failover suspended until cleared",
            RNS.LOG_NOTICE,
        )
    _link_override = link
    _set_active_link(link)
    return "Forced to %s" % link


def set_native_lib_dir(path):
    """Where libcodec2.so lives on this install
    (Context.applicationInfo.nativeLibraryDir) -- needed by the FM
    interface only. Kotlin entry point, called at startup, before start().
    """
    global _native_lib_dir
    _native_lib_dir = (path or "").strip()
    return _native_lib_dir


def _push_frame(iface, frame, previous):
    """Send the rows that changed, one KISS frame each.

    The transmit lock is taken PER ROW rather than around the whole frame.
    Held across all 64, a full redraw would block CoT for as long as the push
    takes -- which over Bluetooth is a second or two, since every write waits
    for the radio to acknowledge it. Per row, a position report only ever waits
    for the row in flight. Each row is a complete KISS frame, so nothing is
    corrupted by another frame landing between two of them.
    """
    for row_no, row in enumerate(frame):
        if previous is not None and previous[row_no] == row:
            continue
        with _rnode_tx_lock:
            iface.write_framebuffer(row_no, bytes(row))


def _display_loop():
    """Keep the radio's screen showing who this node is and who it can hear."""
    last_frame = None
    ticks = 0
    while True:
        time.sleep(_DISPLAY_REFRESH_S)
        try:
            if not _load_display_enabled():
                last_frame = None
                continue

            iface = _rnode_iface()
            if iface is None or not getattr(iface, "online", False):
                # The radio is gone, and whatever it shows when it returns is
                # not what we last drew. Forget the comparison rather than
                # sending a diff against a screen that no longer exists.
                last_frame = None
                continue

            frame = display.render(display.compose(
                node_id=_NODE_ID,
                radio_name=_radio_label,
                phone_name=_node_name,
                peers=_peer_snapshot(),
            ))

            full = last_frame is None or ticks % _DISPLAY_FULL_REDRAW_EVERY == 0
            with _rnode_tx_lock:
                # Re-asserted on every pass, not once at startup: a radio that
                # browned out and came back has forgotten, and the symptom
                # would be a screen that silently stops updating.
                iface.enable_external_framebuffer()
            _push_frame(iface, frame, None if full else last_frame)

            last_frame = frame
            ticks += 1
        except Exception as e:
            # Never fatal. A screen is the least important thing this process
            # does, and it must not be able to take the radio down with it.
            RNS.log("[cot-bridge] radio screen update failed: %r" % (e,),
                    RNS.LOG_ERROR)
            last_frame = None


def _await_radio_readback(iface, timeout_s=8.0):
    """Wait until the radio has echoed back every parameter we just set.

    Polled rather than slept, because the five echoes arrive on the firmware's
    schedule and not ours -- a fixed wait is either too short (and reports a
    working change as broken) or too long (and makes the UI feel dead). Polling
    the values directly rather than calling validate_config() in a loop, since
    that logs a mismatch error on every failed attempt and would fill the log
    with alarming lines describing a change that is merely still in flight.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        reported = getattr(iface, "r_frequency", None)
        # TX power is waited for but NOT required to match, because the board
        # is allowed to disagree: an SX1262 clamps anything above ~22 dBm and
        # reports the clamped figure. Demanding equality here would time out on
        # a change that worked perfectly. The caller reconciles it instead.
        if (reported is not None
                and abs(int(reported) - iface.frequency) <= 100
                and getattr(iface, "r_bandwidth", None) == iface.bandwidth
                and getattr(iface, "r_txpower", None) is not None
                and getattr(iface, "r_sf", None) == iface.sf
                and getattr(iface, "r_cr", None) == iface.cr):
            return True
        time.sleep(0.1)
    return False


def _tune(iface, freq_hz, timeout_s=2.0):
    """Retune, and wait for the radio to say it retuned.

    The RNode echoes CMD_FREQUENCY back and RNS stores it, so this is a real
    confirmation rather than a hopeful sleep. It matters here more than
    anywhere else in this file: a retune that silently failed would have the
    survey attribute the OLD channel's noise to the new frequency, and every
    reading after it would be quietly wrong in a way nothing downstream could
    detect.
    """
    iface.frequency = freq_hz
    iface.r_frequency = None
    iface.setFrequency()

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        reported = getattr(iface, "r_frequency", None)
        if reported is not None and abs(int(reported) - freq_hz) <= 100:
            return True
        time.sleep(0.05)
    return False


def _sample_floor(iface, timeout_s):
    """The next noise floor reading to arrive AFTER this call, or None.

    Blanking the cached value first is what makes it "after". The radio pushes
    its channel stats unsolicited and at its own cadence, so simply sleeping
    and reading would, on a fast sweep, record the previous channel's noise
    against this one -- the exact off-by-one-channel error that would make a
    survey confidently wrong. Waiting for the field to refill turns an
    assumption about timing into a fact about arrival.
    """
    iface.r_noise_floor = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if getattr(iface, "r_noise_floor", None) is not None:
            return _channel_conditions()
        time.sleep(0.05)
    return None


def _run_sweep(low_hz, high_hz, passes, dwell_s):
    """Walks the band, measuring each channel. Runs on its own thread."""
    global _sweep_active, _sweep_state, _sweep_result_lines

    iface = _lora_iface
    channels = spectrum.plan_channels(low_hz, high_hz, _radio_params()["bandwidth_hz"])
    sweep = spectrum.Sweep(channels, _radio_params()["bandwidth_hz"])
    total = len(channels) * passes
    done = 0
    retune_failures = 0
    no_reading = 0

    RNS.log("[cot-bridge] survey: %d channels %s, %d pass(es) -- OFF AIR until done"
            % (len(channels), "%.1f-%.1f MHz" % (low_hz / 1e6, high_hz / 1e6), passes),
            RNS.LOG_NOTICE)
    try:
        for _ in range(passes):
            for freq in channels:
                if not _sweep_active:      # cancelled
                    return
                if not _tune(iface, freq, timeout_s=dwell_s):
                    retune_failures += 1
                    done += 1
                    continue
                conditions = _sample_floor(iface, dwell_s)
                if conditions is None:
                    no_reading += 1
                else:
                    sweep.observe(freq, conditions["noise_floor"],
                                  conditions["load_short"])
                done += 1
                with _sweep_lock:
                    _sweep_state = {"done": done, "total": total,
                                    "freq_hz": freq}
            sweep.note_pass_complete()
    except Exception as e:
        RNS.log("[cot-bridge] survey failed: %s" % e, RNS.LOG_ERROR)
    finally:
        # The radio comes home whatever happened, including a cancel or an
        # exception mid-band. A bridge left parked on 907 MHz looks exactly
        # like a dead radio, and would be diagnosed as one.
        _sweep_active = False
        home_hz = _radio_params()["frequency_hz"]
        try:
            if not _tune(iface, home_hz, timeout_s=5.0):
                RNS.log("[cot-bridge] survey: COULD NOT RETUNE to %.3f MHz -- "
                        "restart the app before testing anything"
                        % (home_hz / 1e6), RNS.LOG_ERROR)
            else:
                RNS.log("[cot-bridge] survey: back on %.3f MHz"
                        % (home_hz / 1e6), RNS.LOG_NOTICE)
        except Exception as e:
            RNS.log("[cot-bridge] survey: retune home failed: %s" % e, RNS.LOG_ERROR)

        lines = spectrum.format_recommendation(
            sweep.recommendation(current_hz=home_hz),
            top=sweep.ranked()[:5])
        if retune_failures or no_reading:
            lines.append("survey: %d retune failure(s), %d channel(s) with no "
                         "reading -- treat the ranking with that in mind"
                         % (retune_failures, no_reading))
        for line in lines:
            RNS.log("[cot-bridge] %s" % line, RNS.LOG_NOTICE)

        with _sweep_lock:
            _sweep_state = None
            _sweep_result_lines = lines


def start_sweep(band="us915", passes=2, dwell_s=3.0):
    """Kotlin entry point. Returns a message for the UI."""
    global _sweep_active
    if _radio_firmware == RADIO_MESHTASTIC:
        return _NOT_AN_RNODE % "The spectrum survey"
    if _lora_iface is None:
        return "No radio"
    with _sweep_lock:
        if _sweep_active:
            return "Survey already running"
        limits = spectrum.BANDS.get(band)
        if limits is None:
            return "Unknown band %s" % band
        _sweep_active = True

    threading.Thread(
        target=_run_sweep, args=(limits[0], limits[1], int(passes), float(dwell_s)),
        daemon=True, name="sweep",
    ).start()
    return "Survey started"


def stop_sweep():
    """Cancels a running survey. The retune home happens in _run_sweep's
    finally, so cancelling is safe at any point."""
    global _sweep_active
    _sweep_active = False
    return "Survey cancelling"


def sweep_status():
    """Progress and last result, as JSON, for the status screen."""
    with _sweep_lock:
        state = dict(_sweep_state) if _sweep_state else None
        lines = list(_sweep_result_lines)
    return json.dumps({"running": _sweep_active, "progress": state,
                       "result": lines})


def _rns_tx_loop():
    """Drains the TX queue, pacing multi-fragment messages to the radio's
    actual bitrate. On its own thread on purpose: sleeping inside
    _cot_listen_loop would stall reads from ATAK for the duration of a
    multi-fragment send."""
    global _last_tx_at
    while True:
        try:
            # Nothing is dequeued during a survey, rather than dequeued and
            # dropped: a sweep lasts minutes, and anything worth sending is
            # still worth sending when the radio comes home. The queues are
            # bounded, so a long sweep sheds load the same way a slow radio
            # already does.
            if _sweep_active:
                time.sleep(0.2)
                continue

            out = _next_outbound()
            if out is None:
                continue
            packets = out.packets
            # Resolved once per batch, matching _transmit_sealed's own
            # resolution (out.link or _active_link) -- everything below that
            # needs to know "which interface" asks this, not _active_link
            # directly, so a batch explicitly targeted at a non-default link
            # is never accidentally sized or logged against the primary one.
            #
            # A LINK WITH ITS OWN SMALLER MTU NEEDS ITS OWN LIMIT HERE, which
            # on FM is 29 B against LoRa's ~464: a batch explicitly targeted at
            # FM (an escalated one-shot message, say) must not be sized or
            # logged against LoRa just because _active_link is LoRa.
            target_link = out.link or _active_link
            packet_limit = (_max_packet_fm if target_link == LINK_FM
                            else _max_packet)
            for i, frame in enumerate(packets):
                # Wrapped HERE rather than at the point each frame was built,
                # because this is the single choke point every transmission
                # passes through -- CoT, beacons, acks and ARQ retries alike --
                # so nothing can be added later that accidentally goes out in
                # clear. It also gives ARQ what it needs for free: a retry is
                # re-wrapped, so it carries a fresh sequence number and is
                # relayed rather than suppressed as a duplicate, while its
                # msg_id (inside the envelope) stays the same message.
                if not out.sealed:
                    try:
                        frame = _mesh.wrap(frame, _hops_for(frame))
                    except Exception as e:
                        RNS.log(f"[cot-bridge] could not wrap frame for sending: {e}",
                                RNS.LOG_ERROR)
                        continue
                if packet_limit is not None and len(frame) > packet_limit:
                    # Belt and braces -- Packet.pack() raises IOError above the
                    # MDU, and this makes that structurally unreachable.
                    RNS.log(
                        f"[cot-bridge] dropping oversize frame ({len(frame)} > {packet_limit})",
                        RNS.LOG_ERROR,
                    )
                    continue
                try:
                    txb_before = getattr(_iface_for(target_link), "txb", 0)
                    result = _transmit_sealed(frame, link=out.link)
                    if result is False:
                        # Packet.send() returns False when Transport.outbound()
                        # found no interface willing to carry it -- RNS logs
                        # "No interfaces could process the outbound packet" and
                        # otherwise says nothing. Silently losing every packet
                        # while the bridge looks healthy is exactly the failure
                        # this needs to be loud about.
                        RNS.log(
                            "[cot-bridge] SEND REJECTED: no interface accepted the packet "
                            f"({target_link} online="
                            f"{getattr(_iface_for(target_link), 'online', None)}, "
                            f"OUT={getattr(_iface_for(target_link), 'OUT', None)})",
                            RNS.LOG_ERROR,
                        )
                    else:
                        _last_tx_at = time.monotonic()
                        txb_after = getattr(_iface_for(target_link), "txb", 0)
                        RNS.log(
                            f"[cot-bridge] transmitted {len(frame)} bytes "
                            f"(interface txb {txb_before} -> {txb_after})",
                            RNS.LOG_DEBUG,
                        )
                except Exception as e:
                    RNS.log(f"[cot-bridge] packet send failed: {e}", RNS.LOG_ERROR)
                if i + 1 < len(packets):
                    time.sleep(_airtime_s(len(frame)) * _PACING_FACTOR)
        except Exception as e:
            RNS.log(f"[cot-bridge] TX loop error: {e}", RNS.LOG_ERROR)
            time.sleep(0.5)


def _next_outbound():
    """Acks first, then anything whose stagger has elapsed, then ordinary CoT.
    Returns None when there is nothing due.

    Both jittered lanes carry the earliest time an entry may go out (see
    _ACK_JITTER_MAX_S and _ONSET_JITTER_MAX_S). One that isn't due yet goes
    back on its queue and ordinary traffic gets a turn, which at worst delays
    it behind a single message -- still far inside the sender's retry timer.
    """
    now = time.monotonic()
    with _ack_pending_lock:
        # EVERY due ack, not the first one. Acks are the smallest frames this
        # link sends and the only ones that routinely come in groups, so they
        # are what bundling was built for: four of them separately cost four
        # packet headers and four envelopes, and together they cost one of
        # each. They all carry the same hop cap by construction, which is the
        # condition bundle() requires.
        due = [key for key, (send_after, _, _) in _ack_pending.items()
               if now >= send_after][:_MAX_BUNDLED_ACKS]
        if due:
            frames = [_ack_pending.pop(key)[1] for key in due]
            if len(frames) > 1:
                try:
                    packed = cot_codec.bundle(frames)
                except cot_codec.CotEncodeError as e:
                    # Never worth losing acks over: send them the old way.
                    RNS.log(f"[cot-bridge] could not bundle {len(frames)} acks: {e}",
                            RNS.LOG_WARNING)
                else:
                    global _acks_bundled
                    _acks_bundled += len(frames)
                    RNS.log("[cot-bridge] bundled %d acks into one packet "
                            "(%d bytes instead of %d)"
                            % (len(frames), len(packed),
                               sum(len(f) for f in frames)
                               + (len(frames) - 1) * (mesh.OVERHEAD + _RNS_HEADER_B)),
                            RNS.LOG_DEBUG)
                    return Outbound([packed], False)
            return Outbound(frames[:1], False)

    try:
        send_after, packets, delayed_link = _delayed_queue.get_nowait()
    except queue.Empty:
        pass
    else:
        if time.monotonic() >= send_after:
            return Outbound(packets, False, delayed_link)
        try:
            _delayed_queue.put_nowait((send_after, packets, delayed_link))
        except queue.Full:
            # Only reachable if the lane filled between the get and the put.
            # Send now rather than drop: late is better than never for the
            # report that says a unit started moving.
            return Outbound(packets, False, delayed_link)

    # Relays are already sealed -- forward() passed the original ciphertext and
    # tag through untouched -- so they must NOT go back through wrap(). Placed
    # after the lanes above and before the main queue because a relay is
    # somebody else's traffic: this node's own events come first.
    relayed = _relay_due()
    if relayed is not None:
        # The link it goes back out on is whichever one it was HEARD on
        # (see _relay_due()'s own docstring), not this node's own
        # _active_link -- FM never reaches here at all (its gate has
        # relays=False, see eligibility.fm_gate), but WiFi's does, so this
        # can no longer be hardcoded to LINK_LORA the way it could when
        # LoRa was the only link capable of relaying.
        packet, origin_link = relayed
        return Outbound([packet], True, origin_link)

    try:
        # A timeout rather than a blocking get, so a jittered ack sitting in
        # the lane above is picked up promptly even on an otherwise idle link.
        packets, queued_link = _tx_queue.get(timeout=0.2)
        return Outbound(packets, False, queued_link)
    except queue.Empty:
        return None


def _peer_snapshot():
    """Peers, freshest first. See peers.PeerTable.snapshot."""
    return _peers.snapshot()


def _fm_status():
    """The FM link's own status block: what the receive path has observed, what
    one-shot traffic is still trying to get out, and the operating point it is
    all running at.

    MUST NOT RAISE -- get_status() is called from the UI thread's poller, and a
    status screen that crashes because a diagnostic block could not be built is a
    worse outcome than a status screen without one.
    """
    block = {
        # Delivery is confirmable here ONLY when a peer happens to report hearing
        # a message on its own beacon (fm_resend.note_heard) -- never on demand,
        # never for every message, and never promptly. False rather than True
        # because the UI must not imply delivery from the absence of an error,
        # which is the case that matters: silence on this link means nothing at
        # all.
        "delivery_confirmable": False,
        "confirmed_total": _fm_resend.stats().get("confirmed", 0),
        "resend": _fm_resend.snapshot(),
        "resend_summary": _fm_resend.summary_line(),
        "position_resend_summary": _fm_position_resend.summary_line(),
        "keyframe_resend_summary": _fm_keyframe_resend.summary_line(),
    }
    # The operating point this link is RUNNING on, not the one Kotlin has stored
    # -- the same distinction KEY_ACTIVE_TRANSPORT exists for on the Kotlin side.
    # Between saving a profile change and the bridge restart that lands it the
    # two differ, and the screen has to show the one carrying traffic. Outside
    # the try below so a broken health reading does not also cost the screen
    # these lines; it is the only setting on this link whose mismatch is
    # undetectable from the air.
    try:
        block["profile"] = fm_link.profile_name()
        block["profile_is_default"] = fm_link.profile_is_default()
        # What two operators read to each other: equal codes hear each other,
        # whatever each one's profile is called.
        block["net_code"] = fm_link.net_code()
        block["operating_point"] = fm_link.summary_line()
        block["evidence"] = fm_link.evidence_line()
        block["burst_s"] = round(fm_link.burst_duration_s(), 2)
        block["capacity_bytes"] = fm_link.hw_mtu()
    except Exception as e:
        block["profile"] = ""
        block["profile_is_default"] = True
        block["net_code"] = ""
        block["operating_point"] = "FM operating point: unknown (%s)" % e
        block["evidence"] = ""
    try:
        health = fm_link.health_snapshot()
        # None means no audio has ever been fed, which is NOT the same as a
        # silent link -- see fm_link.health_snapshot().
        block["health"] = health if health is not None else {
            "state": "down",
            "summary": "no audio has reached the receive path yet",
        }
        block["lines"] = fm_link.status_lines()
    except Exception as e:
        block["health"] = {"state": "unknown", "summary": "no reading: %s" % e}
        block["lines"] = []
    return block


def self_position_pending():
    """Kotlin entry point: CotTakServerBridgeService polls this on its own
    schedule, independent of get_status()'s UI-driven polling, so the
    cold-GPS-race recovery it drives keeps working while MainActivity is
    paused -- which is most of this app's real operating life, since ATAK
    is normally what's in the foreground. See
    tx_policy.TxPolicy.self_position_pending()'s own docstring for what
    this actually detects. MUST NOT raise: called from a background
    service loop with nothing to show an error in.
    """
    try:
        return _tx_policy.self_position_pending()
    except Exception:
        return False


def get_mesh_view():
    """JSON for the Mesh tab: nodes, one line per radio each is heard on, and the
    probable lines between other nodes. All the judgement is in mesh_view.py;
    this only gathers its inputs.

    A separate call from get_status() so it costs nothing while the tab is not
    open. MUST NOT raise, for the same reason get_status() must not.
    """
    try:
        me_radios = []
        for link in _LINK_PRIORITY:
            if _link_ifaces.get(link) is not None:
                me_radios.append(_radio_firmware if link == LINK_LORA else link)
        view = mesh_view.build(
            {"node_id": _NODE_ID, "name": _node_name,
             "callsign": _local_callsign or "", "radios": me_radios},
            _peers.snapshot(), connected_s=_PEER_CONNECTED_S,
            beacon_interval_s=_BEACON_INTERVAL_S,
            probable=_peers.probable_pairs())
        view["ok"] = True
        return json.dumps(view)
    except Exception as e:
        return json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


def get_status():
    """JSON status for the Kotlin UI, called via Chaquopy from MainActivity.

    Returns a JSON string rather than a Python object because Chaquopy's type
    conversion for nested dict/list structures is fiddly, and a single string
    crossing the boundary is trivially debuggable from either side.

    MUST NOT raise: this is called from the UI thread's poller, and an
    exception here would surface as a crash in an app whose whole job is to
    keep running in the background.
    """
    try:
        iface = _lora_iface
        # Under the lock: the retry thread mutates the same structures, and a
        # dict changing size mid-iteration is exactly the sort of transient
        # failure that would make the UI flicker for no visible reason.
        with _reliable_lock:
            reliable_snapshot = _reliable_tx.snapshot()
        # The protocol carries node ids; the UI should say what every other
        # surface says -- the device's own name. Resolved here rather than in
        # reliable.py, which deliberately knows nothing about peers.
        for entry in reliable_snapshot.get("inflight", []):
            entry["waiting_on"] = [_peer_label(n) for n in entry["waiting_on"]]
        for entry in reliable_snapshot.get("undelivered", []):
            entry["missing"] = [_peer_label(n) for n in entry["missing"]]
        # Present whenever FM is built at all -- FM-only mode, or LoRa-primary
        # with FM backup, since the operator needs to see backup's health even
        # while LoRa is carrying traffic. ABSENT (not present-and-empty) when
        # there is no FM interface, so a UI showing it cannot render "no audio"
        # for a LoRa-only node that has no audio path to begin with.
        fm_block = None
        if _fm_iface is not None:
            # Under the same lock reliable_snapshot used above: _fm_status()
            # reads fm_resend's pending dict (via snapshot()/stats()), which
            # register()/tick() mutate under _reliable_lock elsewhere in this
            # file. This call site previously did not hold it, which was a real
            # (if rare) "dictionary changed size during iteration" race with the
            # retry thread.
            with _reliable_lock:
                fm_block = _fm_status()

        # Only meaningful once a second link could exist at all -- absent
        # (not a value nobody asked for) in single-link mode, the same
        # present-only-when-relevant convention fm_block just above uses.
        # "At least two of the three links built", so WiFi alongside just one of
        # LoRa/FM already counts.
        active_link_block = None
        _built_count = sum(1 for l in _LINK_PRIORITY if _link_ifaces.get(l) is not None)
        if _built_count >= 2:
            active_link_block = {
                "active_link": _active_link,
                "link_override": _link_override or "auto",
                "lora_health": _lora_health.snapshot(
                    getattr(_lora_iface, "online", False)),
            }
            if _wifi_iface is not None:
                active_link_block["wifi_health"] = _link_health[LINK_WIFI].snapshot(
                    getattr(_wifi_iface, "online", False))

        return json.dumps({
            "ok": True,
            "fm": fm_block,
            "link": active_link_block,
            "running": _reticulum is not None,
            "node_id": _NODE_ID,
            "name": _node_name,
            "callsign": _local_callsign or "",
            "rnode_online": bool(getattr(iface, "online", False)),
            "radio_firmware": _radio_firmware,
            "board": _mesh_board_status(),
            "txb": getattr(iface, "txb", 0) or 0,
            "rxb": getattr(iface, "rxb", 0) or 0,
            "bitrate": getattr(iface, "bitrate", 0) or 0,
            "duty_pct": round(_last_duty_pct, 1),
            # Ambient conditions on the operating channel. Empty until the
            # radio has reported its first CHTM frame. See _channel_conditions.
            "channel": _channel_line() or "",
            # The same short-window figure as the line above, as a number, so
            # the status screen can colour it against a threshold instead of
            # printing a sentence nobody reads under load. -1 when the radio
            # has not reported a CHTM frame yet, which is distinguishable from
            # a genuine 0% and must stay that way.
            "channel_load_pct": (
                lambda c: -1.0 if not c or c.get("load_short") is None
                else round(float(c["load_short"]), 1))(_channel_conditions()),
            # Shown on both phones and compared, exactly like the key
            # fingerprint: mismatched radio settings and a dead radio look
            # identical from one device.
            "radio": radio.describe(
                _load_radio_config()["preset"],
                _load_radio_config()["frequency_hz"],
                _load_radio_config()["txpower_dbm"]),
            "tx_queue": _tx_queue.qsize(),
            "destination": _destination.hexhash if _destination is not None else "",
            "atak_connected": _cot_socket is not None,
            "policy": _tx_policy.summary_line(),
            # Drives MainActivity's cold-GPS-race recovery: true once ATAK's
            # self-marker has been seen at null island and nothing real has
            # gone out since. See TxPolicy.self_position_pending()'s own
            # comment for why a stuck node needs a nudge rather than a wait.
            "self_position_pending": _tx_policy.self_position_pending(),
            # Encrypted is a property the operator cannot see on a map, so it
            # gets said explicitly. `unauthenticated` counts frames that failed
            # the tag: some is another network on the same frequency, some is
            # corruption, and a rising count with peers present is worth
            # noticing.
            "mesh": _mesh.stats() if _mesh is not None else {},
            "encrypted": _mesh is not None,
            # Four characters the operator can compare across devices. Matching
            # is the only quick proof two nodes share a key, because a wrong
            # passphrase and a dead radio look identical from the outside.
            "key": _key_fingerprint(),
            # The other thing two nodes must agree on. A version gap is the
            # single most likely way to break a rollout, and it fails silently
            # by design -- both radios transmit perfectly and refuse each
            # other. Shown next to the key fingerprint because they are the
            # same kind of check: read it off both phones and compare.
            "protocol": cot_codec.PROTOCOL_VERSION,
            "relay": _relay_policy.summary_line(),
            # Per-link, so a second radio shows up here without a schema
            # change. One entry today.
            "links": _gates.summary_line(),
            "relay_budgets": _relay_budgets.summary_line(),
            "peers": _peer_snapshot(),
            # In-flight and given-up one-shot messages. The undelivered list is
            # the whole point of milestone 4: before it, a chat message that
            # never arrived looked exactly like one that did.
            "reliable": reliable_snapshot,
        })
    except Exception as e:
        return json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


_status_clock = time.monotonic   # replaced by tests


def _duty_pct(active, active_iface, now):
    """Percent of the active link's airtime used since the previous tick, or
    None when there is no fair baseline: the link has just become active for
    the first time, or its interface was rebuilt and the counter restarted.

    Every built link's counter is refreshed on every tick, active or not, so a
    failback compares LoRa with LoRa thirty seconds ago -- not with LoRa before
    the outage, nor with FM. The elapsed time is measured, not assumed."""
    duty = None
    for link, iface in list(_link_ifaces.items()):
        if iface is None:
            continue
        txb = getattr(iface, "txb", 0) or 0
        prev = _last_txb.get(link)
        _last_txb[link] = (id(iface), txb, now)
        if link != active or iface is not active_iface:
            continue
        if prev is None or prev[0] != id(iface) or txb < prev[1] or now <= prev[2]:
            continue
        bitrate = getattr(iface, "bitrate", 0) or _FALLBACK_BITRATE
        duty = 100.0 * ((txb - prev[1]) * 8.0 / bitrate) / (now - prev[2])
    return duty


def _status_tick():
    """One pass of the periodic health line. Split out from _status_loop as
    its own function -- same "small pure function called from a loop" shape
    _check_auto_switch() already uses -- so a test can call one tick directly
    instead of driving the infinite loop.

    Exists because the first two-phone bench run produced logs where both ends
    transmitted and neither received, with nothing in between to say why. The
    two counters below split that question cleanly:

      rxb == 0 on both phones  -> no RF is being heard at all (radio config,
                                  antenna, distance) -- nothing above the radio
                                  layer is worth debugging yet.
      rxb climbing, no packets -> bytes arrive but RNS discards them (framing,
                                  destination hash mismatch, IFAC).

    The destination hash is logged at startup for the same reason: if the two
    phones disagree, every packet transmits perfectly and is silently dropped
    on arrival, which looks identical to a dead radio from the logs.
    """
    try:
        # Before the health line and outside the interface check: a peer
        # that went quiet because the RADIO died still has to come off the
        # map, and that is exactly the case where `iface` is None.
        _withdraw_departed()
        _note_peer_transitions()
    except Exception as e:
        RNS.log(f"[cot-bridge] peer/marker maintenance failed: {e}", RNS.LOG_ERROR)
    try:
        # Whichever link is actually carrying traffic right now, not always
        # LoRa -- during an FM/WiFi failover LoRa's own counters go stale,
        # and reporting them here would describe the link that ISN'T moving
        # anything, exactly when knowing what is matters most.
        iface = _link_ifaces.get(_active_link)
        if iface is None:
            RNS.log("[cot-bridge] status(%s): no interface built" % _active_link,
                     RNS.LOG_WARNING)
            return
        snapshot = _peer_snapshot()
        # Label peers the same way LINK OK and the UI do -- device name
        # first, falling back to the id only when a peer isn't advertising
        # one. Printing raw hex here while the other lines print names
        # makes the same node look like two.
        peers = ", ".join(
            "%s%s %s (%.0fs ago%s%s)"
            % (p["name"] or p["node_id"],
               " [%s]" % p["callsign"] if p["callsign"] else "",
               # A node reached only through somebody else is up, but it is
               # not a neighbour, and the difference is worth seeing: it is
               # what decides whether this node relays for it. Reading
               # "UP" for a node the radio cannot actually hear is what
               # made the 2026-08-12 bench confusing to follow.
               ("UP" if p["direct"] else "UP via relay")
               if p["connected"] else "DOWN",
               p["age_s"],
               # Only when there is loss to report, so a clean link stays
               # readable. The 2026-08-04 bench could not check this at all
               # because it existed only in the UI, and the logs are what
               # actually get read afterwards.
               ", %d/%d lost" % (p["lost"], p["lost"] + p["heard"])
               if p["lost"] else "",
               # Only when a link has actually reported a reading for this
               # peer -- a WiFi-only peer, or one never heard directly on a
               # link that carries rssi/snr at all, has neither.
               ", %s %ddBm/%.1fdB" % (p["last_link"], p["rssi"], p["snr"])
               if p["rssi"] is not None and p["snr"] is not None else "")
            for p in snapshot
        ) or "NONE HEARD"
        # Duty cycle is the number milestone 3 exists to move: bytes
        # actually transmitted since the last status, as a fraction of the
        # airtime available in that window. Above _DUTY_WARN_PCT the channel
        # starts queueing behind itself.
        global _last_duty_pct, _duty_over_threshold
        txb = getattr(iface, "txb", 0) or 0
        duty = _duty_pct(_active_link, iface, _status_clock())
        if duty is not None:
            _last_duty_pct = duty

        # Edge-triggered: one WARNING at the crossing, one NOTICE when it
        # clears, not a line every tick a busy channel stays busy.
        if duty is None:
            pass   # no baseline this interval: say nothing either way
        elif duty >= _DUTY_WARN_PCT and not _duty_over_threshold:
            RNS.log(
                "[cot-bridge] duty cycle %.1f%% on %s exceeds %.0f%% -- "
                "channel likely queueing" % (duty, _active_link, _DUTY_WARN_PCT),
                RNS.LOG_WARNING)
            _duty_over_threshold = True
        elif duty < _DUTY_WARN_PCT and _duty_over_threshold:
            RNS.log("[cot-bridge] duty cycle back under %.0f%% on %s"
                    % (_DUTY_WARN_PCT, _active_link), RNS.LOG_NOTICE)
            _duty_over_threshold = False

        RNS.log(
            "[cot-bridge] status(%s): online=%s txb=%s rxb=%s duty=%s | "
            "tx_queue=%d | peers: %s | beacon sizes heard: %s"
            % (_active_link, getattr(iface, "online", None), txb,
               getattr(iface, "rxb", "?"),
               "n/a" if duty is None else "%.1f%%" % duty,
               _tx_queue.qsize(), peers,
               sorted(_sizes_heard) or "none"),
            RNS.LOG_NOTICE,
        )
        # Its own line rather than appended to the status one, because it
        # describes the LoRa RF ENVIRONMENT specifically -- it stays true
        # when every other number here is zero, and whether or not LoRa is
        # the active link right now.
        channel = _channel_line()
        if channel is not None:
            RNS.log("[cot-bridge] channel (lora): %s" % channel, RNS.LOG_NOTICE)

        RNS.log("[cot-bridge] tx policy: %s" % _tx_policy.summary_line(),
                RNS.LOG_NOTICE)
        RNS.log("[cot-bridge] %s" % _relay_policy.summary_line(),
                RNS.LOG_NOTICE)
        with _reliable_lock:
            reliable_line = _reliable_tx.summary_line()
        RNS.log("[cot-bridge] %s%s" % (
            reliable_line,
            # Surfaced because it is the only visible sign the saving is
            # happening: the acks that are NOT sent leave no other trace.
            "" if not (_ack_suppressed or _ack_not_ours or _acks_bundled)
            else " | acks suppressed=%d not-ours=%d bundled=%d" % (
                _ack_suppressed, _ack_not_ours, _acks_bundled)),
            RNS.LOG_NOTICE)
        if _mesh is not None:
            RNS.log("[cot-bridge] %s" % _mesh.summary_line(), RNS.LOG_NOTICE)
    except Exception as e:
        RNS.log(f"[cot-bridge] status loop error: {e}", RNS.LOG_ERROR)


def _status_loop():
    """Calls _status_tick() every _STATUS_INTERVAL_S -- see that function's
    own docstring for what the line means and why it exists."""
    while True:
        time.sleep(_STATUS_INTERVAL_S)
        _status_tick()


def _cot_listen_loop():
    """local CoT bridge socket (from ATAK) -> encode -> RNS broadcast send"""
    global _cot_socket
    while True:
        try:
            sock = _wait_for_kotlin_socket(COT_BRIDGE_HOST, COT_BRIDGE_PORT)
        except Exception as e:
            RNS.log(f"[cot-bridge] cannot reach CoT socket: {e}", RNS.LOG_ERROR)
            time.sleep(2.0)
            continue

        with _cot_socket_lock:
            # Close the old connection before adopting the new one -- same
            # lesson as the RNode socket: a leaked socket leaves Kotlin with a
            # stale pump thread racing the live one.
            if _cot_socket is not None:
                try:
                    _cot_socket.close()
                except Exception:
                    pass
            _cot_socket = sock

        try:
            while True:
                header = _recv_exact(sock, 4)
                if header is None:
                    break
                (length,) = struct.unpack(">I", header)
                if length <= 0 or length > 1_000_000:
                    RNS.log(f"[cot-bridge] bogus frame length {length}, resyncing", RNS.LOG_ERROR)
                    break
                payload = _recv_exact(sock, length)
                if payload is None:
                    break
                _cot_dump("FROM ATAK", payload)
                _handle_outbound_cot(payload)
        except OSError as e:
            # _recv_exact raises ECONNRESET when Kotlin's service restarts
            # (which ColorOS-style background killing makes routine). Before,
            # that killed this thread silently and the bridge went quiet with
            # no log and no recovery.
            RNS.log(f"[cot-bridge] CoT socket read failed: {e}", RNS.LOG_WARNING)

        RNS.log("[cot-bridge] CoT socket closed, reconnecting", RNS.LOG_WARNING)
        time.sleep(1.0)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def start():
    global _reticulum, _destination, _max_payload, _max_packet, _mesh
    global _max_payload_fm, _max_packet_fm
    global _gates, _relay_budgets, _relay_policies
    global _gateway_budgets, _gateway, _relay_policy

    if _reticulum is not None:
        # start() runs again if MainActivity.onCreate() fires again while
        # this process (and its one persistent Python interpreter) is still
        # alive -- e.g. Android recreating the Activity after backgrounding
        # rather than a fresh process launch. RNS.Reticulum() is a
        # process-wide singleton and raises OSError on a second call, so
        # treat re-entry as a no-op rather than letting that propagate.
        RNS.log("[cot-bridge] start() called again; already running, ignoring", RNS.LOG_WARNING)
        return

    RNS.log("[cot-bridge] starting Reticulum", RNS.LOG_NOTICE)
    _ensure_reticulum_config()
    RNS.log("[cot-bridge] config written, constructing RNS.Reticulum()", RNS.LOG_NOTICE)
    _reticulum = RNS.Reticulum(configdir=RNS_CONFIG_DIR)
    RNS.log("[cot-bridge] RNS.Reticulum() constructed, connecting to RNode bridge socket", RNS.LOG_NOTICE)

    # EVERY BUILD IS WRAPPED, because one link failing must not take the others
    # down. This was once missing on both branches, which meant a phone with no
    # FM audio hardware attached (or no RNode, in the other branch) aborted
    # start() entirely and never reached the WiFi build a few lines below.
    # Confirmed live: launching with no radios connected at all left WiFi never
    # even attempted.
    if _transport == "fm":
        RNS.log("[cot-bridge] building FM audio interface (no KISS/radio "
                "handshake on this link)", RNS.LOG_NOTICE)
        try:
            _build_fm_interface()
        except Exception as e:
            RNS.log(f"[cot-bridge] FM interface could not be built: {e}", RNS.LOG_ERROR)
        _set_active_link(LINK_FM)
    else:
        if _radio_firmware == RADIO_MESHTASTIC:
            RNS.log("[cot-bridge] building Meshtastic interface (protobuf over "
                    "the same USB pipe; no KISS)", RNS.LOG_NOTICE)
            try:
                _build_mesh_interface()
            except Exception as e:
                RNS.log(f"[cot-bridge] Meshtastic interface could not be built: {e}", RNS.LOG_ERROR)
        else:
            RNS.log("[cot-bridge] building RNode interface (this is where the KISS detect/radio handshake happens)", RNS.LOG_NOTICE)
            try:
                _build_rnode_interface()  # connects to the RNode bridge socket itself, registers with RNS.Transport
            except Exception as e:
                RNS.log(f"[cot-bridge] RNode interface could not be built: {e}", RNS.LOG_ERROR)
        if _fm_backup_enabled:
            # LoRa-primary/FM-backup routing: also bring up FM, concurrently, as
            # a backup link rather than instead of LoRa. Built the same way
            # FM-only mode builds it -- same socket and interface machinery, just
            # a second interface this time instead of the only one.
            RNS.log("[cot-bridge] FM backup enabled: also building FM audio interface", RNS.LOG_NOTICE)
            try:
                _build_fm_interface()
            except Exception as e:
                RNS.log(f"[cot-bridge] FM backup interface could not be built: {e}", RNS.LOG_ERROR)
        _set_active_link(LINK_LORA)

    if _wifi_enabled:
        # Independent of _transport entirely -- WiFi is not a way of
        # reaching the LoRa radio the way USB/BLE are, it is its own link,
        # brought up concurrently alongside whatever was built above rather
        # than instead of it. Deliberately NOT made the initial active link
        # even when it comes up online immediately: final_init()'s
        # online=True is a fixed startup grace period, not evidence the mesh
        # actually works yet, and giving it a free pass around
        # _check_auto_switch()'s debounce would be inconsistent with how
        # every other promotion in this file is treated. It becomes active
        # once _check_auto_switch() has seen it healthy for
        # _FAILBACK_DEBOUNCE_S.
        try:
            RNS.log("[cot-bridge] WiFi enabled: building AutoInterface on the 802.11s mesh", RNS.LOG_NOTICE)
            _build_wifi_interface()
            # See the "IPv4 multicast discovery beacon" block above for why
            # this runs alongside AutoInterface's own (standard, unmodified)
            # IPv6 discovery rather than replacing it. Only worth starting
            # if the interface actually came up -- nothing for it to feed
            # add_peer() on otherwise.
            _start_ipv4_discovery_beacon()
        except Exception as e:
            # Best-effort: a mesh that is not up yet, or a misconfigured
            # interface name, must not take down LoRa/FM, which this
            # process may be the only working link for.
            RNS.log(f"[cot-bridge] WiFi interface could not be built: {e}", RNS.LOG_ERROR)

    RNS.log("[cot-bridge] radio interface(s) built, making broadcast destination", RNS.LOG_NOTICE)

    _built_links = [l for l in _LINK_PRIORITY if _link_ifaces.get(l) is not None]

    # Always re-asserted here, unconditionally -- see _startup_active_link()'s
    # own docstring for the two independent reasons this is required, not
    # merely a fallback for the unusual case: it corrects _active_link when
    # the ORIGINAL transport's own link never actually built, AND it
    # reasserts every interface's .OUT flag now that WiFi (always built
    # last, when enabled) has had a chance to set its own .OUT = True
    # alongside whatever _set_active_link() already correctly set earlier.
    _final_link = _startup_active_link(_active_link, _built_links)
    if _final_link is None:
        RNS.log("[cot-bridge] no radio interface came up at all -- "
                "nothing can transmit until one does", RNS.LOG_ERROR)
    else:
        _set_active_link(_final_link)

    if set(_built_links) != set(_gates.names()) or _radio_firmware == RADIO_MESHTASTIC:
        # Module scope only ever registered LoRa (the "there is one today"
        # comment on _gates, above) -- FM and/or WiFi are added here, once
        # each is known to actually exist, rather than unconditionally at
        # import time, so a LoRa-only install's status/budget reporting
        # never mentions a link it does not have. Rebuilds everything
        # downstream of _gates rather than mutating it in place:
        # relay.LinkBudgets and gateway_mod.Gateway both take the gate set
        # at construction, and a stale budget/policy dict left over from
        # the earlier build would silently omit a link everywhere that
        # matters.
        _gate_list = []
        if _lora_iface is not None:
            _gate_list.append(eligibility.lora_gate(
                _lora_link,
                mtu=(mt_link.hw_mtu() if _radio_firmware == RADIO_MESHTASTIC
                     else cot_codec.RNS_MAX_PAYLOAD)))
        if _fm_iface is not None:
            # THE PER-FRAME TIME CAP IS PASSED, not left to its default, and
            # that is a correctness fix rather than tuning.
            #
            # eligibility.FM_MAX_FRAME_S is 3.0 s, derived when FM airtime was
            # modelled as bytes over a bitrate ("3.0 s = 143 bytes"). On this
            # link the cost is FLAT: every frame is padded to the code's full
            # payload before modulation, so a 6 B frame and a 113 B frame both
            # cost one whole burst. The question the cap answers is therefore not
            # "how many bytes" but "how long is a burst" -- and that is a
            # property of the chosen configuration, not a constant.
            #
            # Leaving the default in place was a live bug the moment the
            # operating point moved to the 128 B code: `fast` lands at 2.95 s,
            # 0.05 s inside the cap, but the two HANDHELD-measured placements
            # (`narrow` and `wide`, both 250 Bd) land at 6.16 s and 4.66 s --
            # so selecting either from the tone picker would have made this gate
            # refuse every FM frame, including the gatewayed traffic that is the
            # only way an FM-only handheld reaches the LoRa mesh.
            #
            # No mtu= here, unlike LoRa's and WiFi's: fm_gate originates only
            # (no relaying, no fragmenting), so a frame that needs two
            # transmissions is already refused as a whole message and the
            # frame-size question never arises separately.
            #
            # THE 1% IS A FLOAT MARGIN, NOT SLACK. fm_link.bitrate_bps() is
            # derived by solving frame_s(capacity) == burst, so a cap of exactly
            # the burst makes this gate compare a value against itself through
            # two different floating-point paths -- and which way it lands then
            # depends on rounding. Measured: `fast` and `narrow` passed and
            # `wide` refused its own full frame, for no reason but that.
            _gate_list.append(eligibility.fm_gate(
                _fm_link, max_frame_s=fm_link.burst_duration_s() * 1.01))
        if _wifi_iface is not None:
            _gate_list.append(eligibility.wifi_gate(
                _wifi_link, mtu=min(cot_codec.RNS_MAX_PAYLOAD, _wifi_iface.HW_MTU)))
        if not _gate_list:
            # Nothing built at all -- eligibility.Gates() itself refuses an
            # empty gate set (a gate set needs at least one link), and there
            # is no correct set of gates to build for zero links anyway.
            # Leave the stale placeholder in place rather than raise here on
            # top of the ERROR already logged above; nothing can transmit
            # either way until a real interface exists to rebuild this from.
            RNS.log("[cot-bridge] no links to register -- gates left "
                    "unbuilt until a radio interface comes up", RNS.LOG_ERROR)
        else:
            _gates = eligibility.Gates(_gate_list)
            # WiFi exempted from both budgets, not merely generously
            # covered by one -- see relay.UnmeteredBudget's own docstring
            # for why airtime scarcity was never WiFi's real constraint,
            # and why leaving it to the bitrate ratio alone was fragile.
            _relay_budgets = relay.LinkBudgets(_gates.names(), unmetered=(LINK_WIFI,),
                                               burst_s=relay.DEFAULT_RELAY_BURST_S)
            _relay_policies = {}
            for _rebuilt_gate in _gates:
                _relay_policies[_rebuilt_gate.name] = (
                    relay.RelayPolicy.for_link(_rebuilt_gate.link,
                                               budget=_relay_budgets[_rebuilt_gate.name])
                    if _rebuilt_gate.relays else relay.NoRelay(_rebuilt_gate.name))
            _gateway_budgets = relay.LinkBudgets(_gates.names(),
                                                 duty=gateway_mod.DEFAULT_GATEWAY_DUTY,
                                                 unmetered=(LINK_WIFI,))
            _gateway = gateway_mod.Gateway(_gates, _gateway_budgets)
            # Reassigned rather than left pointing at the module-scope object:
            # _relay_policies is a fresh dict above, so the old _relay_policy
            # would otherwise keep counting cancellations (see _consider_relay)
            # on an object nothing else uses anymore, splitting the stats.
            # Keyed off _active_link (now corrected above to a link that
            # actually exists) rather than hardcoded LINK_LORA, which this
            # dict is no longer guaranteed to contain at all -- falling back
            # to relay.NoRelay() in the theoretical case _active_link still
            # is not one of _relay_policies' own keys, so this global is
            # never None (get_status()/the status loop call
            # .summary_line() on it unconditionally).
            _relay_policy = _relay_policies.get(_active_link) or relay.NoRelay(_active_link)
            RNS.log("[cot-bridge] registered links: %s" % _gates.summary_line(),
                    RNS.LOG_NOTICE)

    _destination = _make_destination(_reticulum)

    # Sized from the PRIMARY interface -- see _compute_max_packet()'s own
    # docstring for why this can't just trust RNS.Packet.PLAIN_MDU
    # unconditionally, and for why this stays LoRa's budget even in dual
    # mode rather than varying with _active_link. The encoder and
    # fragmenter get the inner budget, so a message sized to fit one packet
    # still fits one packet once the mesh envelope is on it.
    _max_packet = _compute_max_packet()
    _max_payload = _max_packet - mesh.OVERHEAD

    # FM's OWN budget, alongside the primary one above -- see _max_payload_fm's
    # own module-level comment. Computed whenever FM is built at all, not only in
    # FM-only mode: in FM-only mode it ends up identical to _max_payload/
    # _max_packet, computed the same way; in dual mode it is what the escalation
    # path and FM's own native one-shot sends actually budget against.
    if _fm_iface is not None:
        _max_packet_fm = min(cot_codec.RNS_MAX_PAYLOAD, _fm_iface.HW_MTU)
        _max_payload_fm = _max_packet_fm - mesh.OVERHEAD

    if _fm_iface is not None:
        # BOTH receive-side windows have to outlast the SENDER's resend schedule,
        # and neither default does -- they were sized for ARQ that finishes in
        # ~16 s, against a schedule that runs for minutes. Sized from
        # fm_resend.span_s() rather than written out, so they cannot drift apart
        # from it (the same reasoning that derives _REASSEMBLY_TIMEOUT_S from
        # reliable.max_retry_span_s).
        #
        # Getting either wrong is silent and user-visible:
        #   * duplicate window too short -> a late blind resend is delivered to
        #     ATAK again, so a chat message appears more than once.
        #   * reassembly timeout too short -> fragments stop accumulating across
        #     attempts, which at this budget takes a multi-fragment message from
        #     roughly 60% deliverable to under 2%.
        #
        # max() in both cases, never a bare assignment: on a slow LoRa preset the
        # ARQ span (~365 s) EXCEEDS FM's (~284 s), so overwriting with FM's
        # figure would under-size the window for the LoRa attempts of the same
        # message. Failover is N-way and one reassembler serves every link -- see
        # _reassembly_window_s.
        window_s = fm_resend.span_s() * 1.25
        _reliable_rx.ttl_s = max(_reliable_rx.ttl_s, window_s)
        # Through _reassembly_window_s rather than set directly to window_s,
        # because _build_rnode_interface() has already called
        # _resize_reassembly() at this point with FM not yet built -- so the
        # buffer may currently be sized for LoRa's ARQ span alone. That function
        # is the one definition of "the longest schedule this node may run", and
        # asking it for the answer again with FM included is a max rather than an
        # overwrite in either direction.
        #
        # set_timeout() rather than a fresh Reassembler: the same reason
        # _resize_reassembly uses it -- it keeps any partial already in flight
        # instead of discarding it at startup.
        reassembly_s = _reassembly_window_s(include_fm=True,
                                            airtime_s=_airtime_s)
        _reassembler.set_timeout(reassembly_s)
        RNS.log(
            f"[cot-bridge] FM: one-shot traffic gets {_fm_resend.attempts} "
            f"blind attempts over {fm_resend.span_s() / 60.0:.0f} min, with a "
            f"{window_s / 60.0:.0f} min duplicate window and a "
            f"{reassembly_s / 60.0:.0f} min reassembly window",
            RNS.LOG_NOTICE,
        )

    # Under the lock, because the UI thread may be entering a passphrase right
    # now: set_mesh_passphrase() sees _mesh is None and leaves the build to us,
    # so this must read _mesh_passphrase after it has been written, not before.
    with _mesh_lock:
        passphrase, source = _load_passphrase()
        _mesh = _build_mesh(passphrase)
    if source in ("generated", "ephemeral"):
        # Loud on purpose: this node cannot talk to anything until the same
        # passphrase reaches the other radios, and a silent "why is nobody
        # there" is exactly the bench session this is meant to prevent.
        RNS.log(
            f"[cot-bridge] NO MESH PASSPHRASE WAS CONFIGURED, so one was {source}: "
            f"{passphrase!r} -- this node is ISOLATED until the same passphrase is "
            f"set on the other nodes (stored at {_MESH_PASSPHRASE_PATH})",
            RNS.LOG_WARNING,
        )
    else:
        RNS.log(f"[cot-bridge] mesh passphrase loaded from {source}", RNS.LOG_NOTICE)
    RNS.log(
        f"[cot-bridge] mesh key {mesh.key_fingerprint(mesh.derive_key(passphrase))} "
        f"-- every node on this network must show the same four characters",
        RNS.LOG_NOTICE,
    )

    # Surfaces codec table gaps and fallback rates in logcat. The string
    # tables in cot_codec contain educated guesses that can only be corrected
    # empirically -- grep a bench run for "[cot-codec] .*_escape" and
    # "[cot-codec] fallback:" and fold the real values in.
    cot_codec.on_event = lambda kind, detail: RNS.log(
        f"[cot-codec] {kind}: {detail}", RNS.LOG_DEBUG
    )

    # Both phones must compute the SAME hash or every packet is transmitted
    # perfectly and silently dropped on arrival. Compare this line across
    # devices before suspecting anything else.
    RNS.log(
        f"[cot-bridge] broadcast destination {_destination.hexhash} "
        f"({APP_NAME}.{'.'.join(ASPECTS)})",
        RNS.LOG_NOTICE,
    )

    RNS.log(f"[cot-bridge] this node's beacon id is {_NODE_ID}", RNS.LOG_NOTICE)

    # One line combining facts that are each already known here but never
    # logged together -- so an exported field-test log is self-describing,
    # and two phones' logs can be diffed side by side without asking
    # separately what each was configured as.
    lora_bit = ""
    if _lora_iface is not None and _radio_firmware == RADIO_MESHTASTIC:
        # NOT _radio_params(): those are this app's own saved settings, which
        # are never applied to a Meshtastic board (it is configured on the
        # board). Printing them would put numbers in the log that describe
        # nothing the radio is doing, in the one line meant for diffing two
        # phones. The preset is the board's own, read at connect.
        lora_bit = " lora=meshtastic/%s/%dB" % (
            getattr(_lora_iface, "preset", "?"), _lora_iface.HW_MTU)
    elif _lora_iface is not None:
        p = _radio_params()
        lora_bit = " lora=%dHz/%dHz/SF%d/CR%d/%ddBm" % (
            p["frequency_hz"], p["bandwidth_hz"], p["spreading_factor"],
            p["coding_rate"], p["txpower_dbm"])
    # A RADIO'S OWN OPERATING POINT BELONGS IN THIS LINE: two phones' logs
    # diffed against each other is the only check that exists for a tone/rate/
    # code disagreement, whose failure mode is total silence rather than an
    # error. Every number here must match at the far end.
    fm_bit = ""
    if _fm_iface is not None:
        cfg = fm_link.config()
        fm_bit = " fm=%s@%s/%dBd/%dcopies/%.2fs/%dB" % (
            cfg.codename, cfg.profile.name, cfg.profile.symbol_rate_bd,
            cfg.repeats, cfg.burst_duration_s(), cfg.capacity_bytes())
    RNS.log(
        "[cot-bridge] session: node=%s build=%s protocol=v%d transport=%s "
        "fm_backup=%s wifi=%s%s%s"
        % (_node_name or "?", _build_stamp or "?", cot_codec.PROTOCOL_VERSION,
           _transport, _fm_backup_enabled, _wifi_enabled, lora_bit, fm_bit),
        RNS.LOG_NOTICE,
    )

    threading.Thread(target=_cot_listen_loop, daemon=True, name="cot-listen").start()
    threading.Thread(target=_rns_tx_loop, daemon=True, name="rns-tx").start()
    threading.Thread(target=_status_loop, daemon=True, name="status").start()
    threading.Thread(target=_beacon_loop, daemon=True, name="beacon").start()
    threading.Thread(target=_reliable_loop, daemon=True, name="reliable").start()
    threading.Thread(target=_display_loop, daemon=True, name="display").start()

    RNS.log(
        f"[cot-bridge] up: "
        f"{'FM audio' if _transport == 'fm' else 'Meshtastic' if _radio_firmware == RADIO_MESHTASTIC else 'RNode'} "
        f"interface attached, broadcast destination ready, "
        f"max payload {_max_payload} bytes",
        RNS.LOG_NOTICE,
    )
