"""
Milestone 2: turn CoT XML into something that fits in an RNS packet.

RNS.Packet.PLAIN_MDU is 464 bytes and Packet.pack() raises IOError above the
MTU, while a realistic ATAK position report is 600-900 bytes of XML. Milestone
1 relayed raw bytes, which means it raises on essentially every real event.
This module is what makes the link actually carry ATAK traffic:

  - a field-aware compact binary encoding for position-shaped events
    (~61 bytes for a PLI that is 617 bytes as XML), and
  - a zlib fallback with a preset dictionary for everything else, and
  - fragmentation, because RNS's own Resource/Link machinery is unavailable
    on the PLAIN broadcast destination this bridge uses.

It also owns the wire format for two things layered on top of that: the link
beacon (encode_beacon) and, for one-shot traffic that cannot self-heal, an
acknowledgement wrapper and ack frame (wrap_reliable / encode_ack). The state
machines driving those live in rns_bridge.py and reliable.py respectively;
everything here is pure bytes.

Deliberately free of RNS/Android imports so the whole thing is testable with a
plain desktop python3 (see test_cot_codec.py).

IMPORTANT: the on-device interpreter is CPython 3.8 (Chaquopy ships
libpython3.8.so), while the desktop used to develop this is much newer. Keep
this module 3.8-compatible: no match statements, no `X | Y` annotations, no
str.removeprefix, and in particular no datetime.fromisoformat -- it cannot
parse ATAK's "...31.123Z" on 3.8 but silently succeeds on 3.11+, so a parser
built on it passes every desktop test and then fails on the phone.
"""

import calendar
import collections
import hashlib
import os
import re
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zlib

# Generated; see train_zdict.py. Part of the wire format -- see _squeeze().
from compact_zdict import COMPACT_ZDICT

# 2: markers. Added <color> and <creator> to the compact encoding, which every
#    user-placed marker carries unconditionally (PlacePointTool sets both) and
#    which previously forced every one of them onto the zlib path at ~3x the
#    airtime. Old and new builds refuse each other rather than misreading --
#    decode() rejects a version it does not know, and the mesh envelope's
#    control byte embeds this too, so a mixed pair fails loudly at both layers.
# 3: the rest of a marker. <remarks>, <usericon> and <link> encoded, <marti>
#    dropped -- version 2 handled <color> and <creator> and markers still fell
#    back on these four. Adding them needed more than the sixteen flag bits the
#    format had, hence F_EXT.
# 4: session compression. A self-SA position stops restating the 32 bytes of
#    uid and callsign that are constant for the whole session, and delta-codes
#    time/stale/lat/lon against a keyframe. Takes a position report from 61 to
#    ~22 bytes and the mesh from 13.7% duty to ~5.9%, which is what buys the
#    airtime for relaying. The extended flags word also became a varint here,
#    so it costs one byte rather than two -- markers get that back as well.
# 5: relaying. The beacon carries a Bloom digest of the sender's neighbours,
#    which is what the coverage test in relay.py runs on. The bump is not
#    really about that trailing field -- an old decoder ignores it -- but about
#    what its ABSENCE would mean: a node with no digest reads as covering
#    nobody, so a v4 node in a v5 mesh would have every frame it sent relayed
#    by everyone, which is the one thing the relay budget cannot absorb.
# 6: GeoChat on the compact path. A chat says the same few things over and
#    over -- the sender's uid four times, the addressee five, the message id
#    twice -- so each is sent once and the repeats are rebuilt. 286 B on air
#    down to 130. <__serverdestination> joins the droppable list: it carries a
#    TAK server's address, and this bridge exists because there is no server.
# 7: dictionary compression on the compact path (T_ZCOMPACT). The structured
#    encoding removes what a schema can; this removes what is redundant ACROSS
#    frames. Measured on frames the dictionary had never seen: positions 1.33x,
#    markers 1.47x, chat 1.53x. The dictionary itself is part of the wire
#    format -- zlib carries its Adler-32 -- so retraining it is also a bump.
# 8: drawn shapes. Detail children may now repeat, which they must because a
#    shape's vertices are a list of <link point=>; and the vertices go as
#    zigzag deltas from the event's own point in two packed columns, with the
#    stroke and fill styling alongside. A rectangle: 452 B on air to 122.
# 9: LINE_STYLE_TABLE gains the leading "" every other table has. Version 8
#    omitted it, so "solid" sat at index 0, which _table_byte reads as "not in
#    the table" -- every shape escaped it to a 6-byte string. The indices move,
#    so v8 and v9 would read each other's stroke styles wrongly; hence a bump
#    rather than a quiet fix.
# 10: circles and ellipses. A centre and radii rather than a vertex list, and
#     the KML-era <shape> nest around them is almost entirely derivable -- the
#     style link's uid is the event's own plus ".Style", its type and relation
#     never vary. 460 B on air across two packets down to 131 B in one.
# 11: routes. Waypoints as delta columns like a shape's vertices, plus the
#     marker each one may carry, <link_attr>, and the <__routeinfo>/<__navcues>
#     nest -- which is empty on any route without recorded voice cues. An
#     eight-waypoint route: 651 B on air across two packets down to 230 B in
#     one. Eleven waypoints or more still fragments.
# 12: T_PARTIAL, so a receiver can say which fragments of a message it holds
#     and the sender resends only those.
# 13: plain control points in a route cost one bit instead of ~20 bytes -- a
#     second bitmap over the marked points, with the uid derived on receipt.
#     Bumped even though the frame TYPE is unchanged, because the change is
#     inside the compact route body: a v12 decoder reads the new subflag bit
#     as one it does not know and then misparses everything after it. A
#     format change with no version change is the failure the version byte
#     exists to prevent -- both radios claim to agree and neither does.
# 15: the frames stop repeating what the envelope already proves. Node ids go
#     as their 3 raw bytes instead of a length-prefixed hex string; the ack's
#     `acker`, the beacon's `node_id` and the reliable wrapper's `origin` come
#     off the wire entirely, because the mesh envelope carries an
#     AUTHENTICATED sender and those copies existed only to be checked against
#     it. An ack goes from 17 bytes to 6. The absolute timestamp drops from 4
#     bytes to 2 (see _TIME_MOD). The AES-CCM tag halves to 4 bytes (see
#     mesh.TAG_LEN). Every one of these changes the layout of an existing
#     frame, which is why they land together rather than one at a time.
#
#     THIS IS THE LAST VERSION THE NIBBLE HOLDS. The high nibble of the control
#     byte is 4 bits and 15 is its largest value, so version 16 needs an escape
#     hatch. It costs nothing to leave until then: version 0 has never been
#     used, so a future build can spell "0" in the nibble and put the real
#     version in a following byte. A v15 node reading that sees version 0,
#     reports `wrong_version`, and refuses the frame -- which is exactly the
#     right behaviour, since a version bump is supposed to stop mixed builds
#     talking. Do not spend version 0 on anything else.
#
#     Two additions since, WITHOUT a bump, each failing loudly between mixed
#     builds rather than silently: the marker packing of 5572565, and the
#     generic elements of 2026-10-14 (T_COMPACT_X / T_ZCOMPACT_X), which an
#     older build refuses by frame type. A frame with no generic elements is
#     unchanged, so mixed builds still exchange everything they could before.
PROTOCOL_VERSION = 15

# == RNS.Packet.PLAIN_MDU (Reticulum.MTU 500 - HEADER_MAXSIZE 35 - IFAC_MIN_SIZE 1).
# The single knob to turn if hardware testing shows large packets are
# unreliable over the air -- the fragmenter picks up whatever slack is left.
RNS_MAX_PAYLOAD = 464

FRAG_HEADER_LEN = 5
MAX_FRAGMENTS = 16
MAX_REASSEMBLED = 16384

DEFAULT_STALE_S = 120

# The real endpoint is an address on the *sender's* LAN, which by construction
# the receiving phone cannot reach -- that unreachability is the entire reason
# this bridge exists. Spending ~20 bytes to relay it would just hand the remote
# ATAK an address it can only fail to dial.
DEFAULT_ENDPOINT = "*:-1:stcp"

# <takv> is ~100 bytes of XML carrying cosmetic device/OS strings -- the worst
# byte-per-value item in a PLI. The wire slot below is fully implemented in
# both directions, so flipping this needs no version bump.
INCLUDE_TAKV = False

# <event> attributes to silently drop rather than treat as "unknown" (which
# would force the whole event onto the fallback path). Empty by default;
# candidates are access, qos, opex, caveat, releaseableTo.
DROP_EVENT_ATTRS = frozenset()

T_COMPACT = 0x0
T_ZXML = 0x1
T_FRAG = 0x2
# Link probe. Carries no CoT -- it exists so the radio link can be tested
# without ATAK in the loop at all. Deliberately rides the SAME destination as
# real traffic, so a beacon arriving proves the entire path CoT depends on:
# radio parameters, range, KISS framing, and the destination hash agreeing
# between devices.
T_BEACON = 0x3
# Wraps a complete frame of one of the types above, marking it as needing
# acknowledgement. Used for one-shot traffic only -- see reliable.py.
T_RELIABLE = 0x4
# The acknowledgement itself. Carries no CoT.
T_ACK = 0x5
# The mesh envelope: authenticated encryption plus the frame identity a mesh
# needs to suppress duplicates. Wraps a complete frame of ANY type above,
# including a fragment -- it is the outermost layer, so relays can recognise
# and forward without reassembling and without holding the plaintext. The
# format and the state machine live in mesh.py; this module only reserves the
# type, since it owns the frame vocabulary.
T_MESH = 0x6

# A compact frame, then squeezed against COMPACT_ZDICT. Emitted only when it
# actually comes out smaller, which is why this is a separate type rather than
# a flag: the encoder decides per frame and the decoder is told, so a frame the
# dictionary suits badly costs nothing beyond the attempt.
T_ZCOMPACT = 0x7

# A PARTIAL acknowledgement: "I hold these fragments of your message, send me
# the rest." Deliberately a separate type rather than an optional field on
# T_ACK, because the two say opposite things about whether a message arrived
# and the cost of confusing them is a delivery reported that never happened.
# Same shape as the T_ZCOMPACT decision: when the meaning differs, the type
# differs, and the decoder is told rather than left to infer.
T_PARTIAL = 0x8

# A reliable wrapper that NAMES the one node owing an acknowledgement.
#
# T_RELIABLE says "somebody ack this"; every receiver therefore acks, and when
# the sender wanted only one named node to answer (a direct chat) the rest of
# that airtime is pure waste -- 15 acks at 16 nodes, roughly twice the airtime
# of the message being confirmed. The addressee cannot be inferred on receipt:
# the wrapper carries origin and msg_id and nothing else, and the chat uid that
# would reveal it is inside the frame, which is not decoded until after the ack
# has been queued.
#
# A separate type rather than a flag bit in the msg_id, for the same reason
# T_PARTIAL is separate from T_ACK: an old decoder reading a flag it does not
# know would misparse everything after it, whereas an unknown TYPE hits the
# existing "unsupported frame type" branch and is dropped with a log line. That
# is the same trade milestone 4 made when it added T_RELIABLE and T_ACK to the
# existing version namespace, and it is why this needs no PROTOCOL_VERSION bump
# -- which matters more than usual here, because the version is a nibble and 14
# of the 15 usable values are already spent.
T_RELIABLE_TO = 0x9

# Several complete frames in one packet.
#
# Every transmission pays 31 bytes of header it cannot avoid -- 19 for
# Reticulum's packet header, 12 for the mesh envelope -- and the frames this
# link sends most often are far smaller than that. A 6-byte ack rides inside 31
# bytes of wrapping, so five sixths of a receipt is postage. Sending four of
# them separately costs four times the postage; sending them in one packet
# costs it once and a length byte each, which more than halves the airtime.
#
# It is worth most exactly when the radio is busiest, which is the one thing
# nothing else in this project addresses: the 2026-08-16 stress bench found
# that halving the BACKGROUND load did not move the collapse point at all,
# because the collapse is about how many packets are in the air, not how big
# they are. This is the only change here that reduces the count.
#
# THE HOP COUNT IS WHY THIS IS NOT SIMPLY "PACK WHATEVER IS QUEUED". One
# envelope carries one hop count, so everything inside a bundle is relayed
# together and must therefore be entitled to the same reach. A beacon (cap 0,
# because the coverage test depends on beacons never being forwarded) sharing a
# packet with a chat message (cap 3) would either flood the beacon and destroy
# the neighbour digest, or strand the chat. bundle() refuses to mix.
T_BUNDLE = 0xA

# One length byte per frame, so nothing over 255 bytes may be bundled. That is
# not a limitation worth engineering around: a frame that size already amortises
# its own header, and the whole point is the frames that do not.
BUNDLE_MAX_FRAME = 255

# A compact frame followed by a block of generic elements, and the same squeezed
# against the dictionary. Separate TYPES rather than a flag for the reason given
# in the generic-elements block: a v15 build that predates them ignores unknown
# flag bits and trailing bytes, and would show the event with those elements
# silently missing. It refuses an unknown type. A frame with no generic elements
# is still T_COMPACT, byte for byte what it was, so nothing that already worked
# between mixed builds stops working.
T_COMPACT_X = 0xB
T_ZCOMPACT_X = 0xC

# The mesh envelope WITHOUT encryption: the same 8-byte header as T_MESH, then
# the inner frame in plain, and no tag. For a radio service where the content
# must be readable by anyone listening (US amateur FM, Part 97.113(a)(4)), so
# it is only ever produced for the FM link and only with the operator's "send
# FM unencrypted" setting on -- see mesh.Mesh.to_clear().
T_CLEAR = 0xD

# Every spelling of "a compact CoT frame". Whether the dictionary was applied or
# generic elements ride along says nothing about what the frame carries, so
# anything deciding on the kind -- is this a bare position? -- tests this set.
COMPACT_KINDS = frozenset([T_COMPACT, T_ZCOMPACT, T_COMPACT_X, T_ZCOMPACT_X])

CTRL_COMPACT = (PROTOCOL_VERSION << 4) | T_COMPACT
CTRL_ZXML = (PROTOCOL_VERSION << 4) | T_ZXML
CTRL_FRAG = (PROTOCOL_VERSION << 4) | T_FRAG
CTRL_BEACON = (PROTOCOL_VERSION << 4) | T_BEACON
CTRL_RELIABLE = (PROTOCOL_VERSION << 4) | T_RELIABLE
CTRL_ACK = (PROTOCOL_VERSION << 4) | T_ACK
CTRL_MESH = (PROTOCOL_VERSION << 4) | T_MESH
CTRL_ZCOMPACT = (PROTOCOL_VERSION << 4) | T_ZCOMPACT
CTRL_PARTIAL = (PROTOCOL_VERSION << 4) | T_PARTIAL
CTRL_RELIABLE_TO = (PROTOCOL_VERSION << 4) | T_RELIABLE_TO
CTRL_BUNDLE = (PROTOCOL_VERSION << 4) | T_BUNDLE
CTRL_COMPACT_X = (PROTOCOL_VERSION << 4) | T_COMPACT_X
CTRL_ZCOMPACT_X = (PROTOCOL_VERSION << 4) | T_ZCOMPACT_X
CTRL_CLEAR = (PROTOCOL_VERSION << 4) | T_CLEAR


class CotCodecError(Exception):
    pass


class CotEncodeError(CotCodecError):
    pass


class CotDecodeError(CotCodecError):
    pass


# Optional callable(kind, detail). rns_bridge wires this to RNS.log, which is
# how table gaps and fallback rates become visible in logcat during a bench
# run -- the tables below contain educated guesses that can only be corrected
# empirically.
on_event = None


def _emit(kind, detail):
    if on_event is not None:
        try:
            on_event(kind, detail)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tables. APPEND-ONLY: a decoder rejects an index past the end of its own
# table, so adding entries is safe but reordering or removing them silently
# remaps every value. Index 0 is a reserved empty slot so that a 0x00 table
# byte unambiguously means "escape, a literal string follows".
# ---------------------------------------------------------------------------

TYPE_TABLE = [
    "",
    "a-f-G-U-C", "a-f-G-U-C-I", "a-f-G-U-C-V", "a-f-G-U-I", "a-f-G-U-T",
    "a-f-G-U", "a-f-G-E-V-C", "a-f-G", "a-f-A", "a-f-A-M-F-Q", "a-f-S",
    "a-h-G", "a-h-G-U-C", "a-h-A", "a-n-G", "a-n-G-U-C", "a-u-G",
    "a-u-G-U-C", "a-u-A", "a-u-S",
    "b-m-p-w-GOTO", "b-m-p-s-m", "b-m-p-s-p-i", "b-m-p-s-p-loc", "b-m-r",
    "b-m-p-c", "b-t-f", "b-a-o-tbl", "b-a-o-can", "b-a-o-pan", "b-d",
    "u-d-f", "u-d-r", "u-d-c-c",
    "t-x-c-t", "t-x-c-t-r", "t-x-takp-v",
]

HOW_TABLE = [
    "", "m-g", "h-g-i-g-o", "h-e", "m-p", "m-r", "m-i-g", "m-f-s", "h-t-l-f", "m-d",
]

# Nibble-coded tables: at most 15 real entries (index 0 is the escape slot).
TEAM_TABLE = [
    "", "White", "Yellow", "Orange", "Magenta", "Red", "Maroon", "Purple",
    "Dark Blue", "Blue", "Cyan", "Teal", "Green", "Dark Green", "Brown",
]

ROLE_TABLE = [
    "", "Team Member", "Team Lead", "HQ", "Sniper", "Medic",
    "Forward Observer", "RTO", "K9",
]

ALTSRC_TABLE = [
    "", "GPS", "DTED0", "DTED1", "DTED2", "DTED3", "SRTM1", "LIDAR", "PFI",
    "ESTIMATED", "USER", "CALC", "COT", "DEM", "RTK",
]

GEOPOINTSRC_TABLE = [
    "", "GPS", "USER", "CALC", "COT", "DTED", "SRTM", "PFI", "LIDAR", "RTK",
    "ESTIMATED", "GPS-RTK", "NETWORK", "FUSED", "UNKNOWN",
]

PLATFORM_TABLE = [
    "", "ATAK-CIV", "ATAK-MIL", "WinTAK", "WinTAK-CIV", "iTAK", "TAKX",
    "TAK-Server",
]

# CoT data-marking attribute. Carried rather than dropped: in some deployments
# this holds a real handling/classification marking, and silently discarding
# that is the kind of thing that is harmless on a bench and not harmless in the
# field. Table-coded, so the overwhelmingly common "Undefined" costs one byte.
ACCESS_TABLE = ["", "Undefined", "Unclassified", "Confidential", "Secret"]

# Detail children the compact encoder understands well enough to reconstruct.
KNOWN_DETAIL = frozenset([
    "contact", "__group", "status", "track", "takv", "precisionlocation", "uid",
    # Both unconditional on any user-placed marker -- see F_COLOR.
    "color", "creator",
    # The rest of what the 2026-08-04 bench found on a real marker.
    "remarks", "usericon", "link",
    # GeoChat. <chatgrp> is not listed because it is a child of <__chat>, not
    # of <detail>, so it never reaches _detail_children.
    "__chat",
    # Drawn shapes -- rectangles, polygons, freeform. <link> is already listed
    # above; on a shape it repeats and carries a vertex instead of provenance.
    "__shapeExtras", "strokeColor", "strokeWeight", "strokeStyle", "fillColor",
    "labels_on",
    # Circles and ellipses. <ellipse>, <link>, <Style> and the rest are
    # children of <shape>, not of <detail>, so they never reach here.
    "shape",
    # Routes. <__navcue> and <trigger> are nested inside <__routeinfo>, so
    # they never reach here.
    "link_attr", "__routeinfo",
])

# Detail children discarded before eligibility is judged.
#   _flow-tags_ : TAK-server provenance breadcrumbs ATAK ignores, and which
#                 grow on every hop -- dropping them is actively correct on a
#                 bandwidth-constrained relay.
#   archive     : an empty "persist this" marker. Consequence of dropping it:
#                 markers arriving over the bridge are not auto-archived on
#                 the receiving device.
#   marti       : a TAK SERVER routing directive -- commoncommo builds it in
#                 setTAKServerRecipients() and notes that its absence means the
#                 message "goes to everyone". ATAK groups it with _flow-tags_
#                 itself, in one line of AbstractCotEventImporter:
#
#                     // This are server-side tags that should be ignored
#                     this.crcBlacklist.add("marti");
#                     this.crcBlacklist.add(CotMapAdapter.FLOWTAG);
#
#                 and that blacklist entry is the ONLY reference to "marti" in
#                 the whole ATAK Java tree: nothing reads it inbound. There is
#                 no server here, which is the reason this bridge exists.
#
#                 THE CONSEQUENCE IS REAL AND IS NOT THE BYTES. <marti> is
#                 where ATAK records "send this marker to Bob, not everyone".
#                 This bridge broadcasts regardless, so that intent was already
#                 being ignored by the transport -- but dropping the tag throws
#                 away the only place the intent is written down. Targeted
#                 delivery is a genuine requirement, deferred rather than
#                 declined; see "Markers cannot be sent to specific people" in
#                 the README before treating this as settled.
DROPPABLE_DETAIL = frozenset([
    "_flow-tags_", "archive", "marti",
    # A TAK server's IP address and port, which is meaningless on a mesh that
    # has no server -- the entire reason this bridge exists. 83 bytes of XML on
    # every chat message describing a host no node here can route to.
    "__serverdestination",
])

# Presence bits for the compact frame's optional fields.
F_START = 0x0001
F_HAE = 0x0002
F_CE = 0x0004
F_LE = 0x0008
F_CALLSIGN = 0x0010
F_GROUP = 0x0020
F_BATTERY = 0x0040
F_TRACK = 0x0080
F_PRECISIONLOC = 0x0100
F_UID_DROID = 0x0200
F_TAKV = 0x0400
# Added after the first real-ATAK bench run, which showed this build stamps
# @access on every event -- which was disqualifying EVERY position report from
# the compact path (741 B -> 268 B on air, when it should have been ~65).
#
# Note this addition is backward-compatible without a version bump, but only
# because it is the HIGHEST flag and is therefore read last: decode_compact()
# reads fields in flag order and never checks for trailing bytes, so an older
# decoder ignores both the flag and the bytes it left unread. Any future field
# must keep that property (append a higher bit, never insert a lower one) or
# it needs a PROTOCOL_VERSION bump.
F_ACCESS = 0x0800

# The two every user-placed marker carries. ATAK's PlacePointTool sets both
# unconditionally -- setMetaInteger("color", ...) and setAuthorInformation() --
# so before these existed EVERY marker fell to the zlib path, at roughly three
# times the airtime of a compact one, on exactly the traffic multi-hop relaying
# exists to carry.
F_COLOR = 0x1000
F_CREATOR = 0x2000

# <status readiness="true"/> rides on every placed marker too. Before markers
# took the compact path this did not matter -- they fell back to zlib, which
# carries the XML verbatim. Adding <color> and <creator> without this would
# have silently dropped it, trading one bug for a quieter one.
F_READINESS = 0x4000

# The last bit of the first flags word, spent on an escape rather than on a
# field: set, it means a SECOND flags word follows immediately. Version 3
# needed three more flags and had one bit, so the choice was widening the field
# for every frame or paying only when the extra flags are used.
#
# The second word is a uvarint, not a fixed pair of bytes. Every flag defined
# so far is below 0x80, so it costs one byte; the width only grows if a future
# flag needs the high bits. Version 3 spent two bytes here unconditionally,
# which mattered once positions started setting X_BOUND and paying it too.
F_EXT = 0x8000

# Second-word flags. The first three ride on a placed marker and never on a
# position report; the last three are the reverse.
X_REMARKS = 0x0001
X_USERICON = 0x0002
X_LINK = 0x0004

# --- session compression: see the SaTx/SaRx docstrings ---
#
# A self-SA position report spends 32 of its 61 bytes restating an identity
# that does not change for the life of the session -- a 24-byte uid and an
# 8-byte callsign, retransmitted every minute, forever. These three flags let a
# sender say it once and then refer to it.
#
#   X_BIND   the uid and callsign carried here are my node identity; remember
#            them against the origin that authenticated this frame
#   X_BOUND  uid and callsign are absent; use the ones you were told
#   X_DELTA  time, stale and lat/lon are offsets from the base frame named by
#            the generation byte, not absolute values
#
# X_BIND and X_BOUND are what make the frame a *session* frame, and a session
# frame always carries a one-byte generation immediately after the flags.
X_BIND = 0x0008
X_BOUND = 0x0010
X_DELTA = 0x0020

# A GeoChat. Set, it means <__chat>, <chatgrp>, <remarks> and the parent <link>
# are ALL rebuilt from one block, and the event uid is rebuilt with them --
# see _chat_parts for why that is a saving rather than a complication.
X_CHAT = 0x0040

# A drawn shape: rectangles (u-d-r), polygons and freeform shapes (u-d-f).
# Set, it means the repeated <link point=...> children are a VERTEX LIST, and
# the stroke and fill styling rides with them. See _shape_parts.
X_SHAPE = 0x0080

# A circle or ellipse: a centre and radii rather than a vertex list, wrapped in
# the KML-era <shape> nest. See _circle_parts.
X_CIRCLE = 0x0100

# A route: waypoints that may also be markers, plus <link_attr> and the
# <__routeinfo>/<__navcues> nest. See _route_parts.
X_ROUTE = 0x0200

# <link> comes in two shapes (LinkDetailHandler): a parent link carrying
# provenance, which is what a placed marker has, and a URL link carrying an
# attachment (url/mime/remarks). Only the parent form is modelled; the other
# falls back rather than losing an attachment's address.
L_UID = 0x01
L_TYPE = 0x02
L_RELATION = 0x04
L_PRODUCTION_TIME = 0x08
L_PARENT_CALLSIGN = 0x10
LINK_ATTRS = ("uid", "type", "relation", "production_time", "parent_callsign")

# A placed marker carries <creator> AND a parent <link>, and they describe the
# same parent with different attribute names:
#
#     creator uid= type= callsign=        time=
#     link    uid= type= parent_callsign= production_time= relation=
#
# So the link is the creator plus a relation. This flag says exactly that, and
# turns ~35 bytes of repeated uid and callsign into one. Set only when every
# shared field genuinely matches, and the decoder rebuilds them from the
# creator it has already read -- which is why creator sits in the first flags
# word and link in the second.
L_LIKE_CREATOR = 0x20

# Presence bits inside a <creator>. ATAK's CreatorDetailHandler writes each
# attribute only when non-empty (see its setDetail), so all four are optional
# and a fixed layout would have to invent values it was never given.
C_UID = 0x01
C_TYPE = 0x02
C_CALLSIGN = 0x04
C_TIME = 0x08

# <color> is written as argb="" on a Marker and value="" on anything else.
# ATAK reads argb first and falls back to value, so either is understood --
# but which one was sent is recorded anyway rather than canonicalised, because
# reconstructing a known child faithfully is the contract for everything else
# in KNOWN_DETAIL and one byte is not worth breaking it for.
COLOR_ATTRS = ("argb", "value")

# CoT's "value unknown" convention for hae/ce/le.
UNKNOWN_SENTINEL = "9999999.0"
_UNKNOWN_THRESHOLD = 9999999.0
# 100 km of positional error is unknown by any practical definition, and
# capping here keeps the uvarint at 3 bytes.
_MAX_ERROR_M = 100000


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _uvarint(n):
    if n < 0:
        raise CotEncodeError("uvarint of negative %d" % n)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_uvarint(buf, off):
    n = 0
    shift = 0
    while True:
        if off >= len(buf):
            raise CotDecodeError("truncated uvarint")
        if shift > 63:
            raise CotDecodeError("uvarint too long")
        b = buf[off]
        off += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, off
        shift += 7


def _zigzag(n):
    # Not the C `(n << 1) ^ (n >> 63)` idiom: Python ints are arbitrary
    # precision, so that shift does not sign-extend the way it does in C.
    return (n << 1) if n >= 0 else ((-n << 1) - 1)


def _unzigzag(n):
    return (n >> 1) if not n & 1 else -((n + 1) >> 1)


def _svarint(n):
    return _uvarint(_zigzag(n))


def _read_svarint(buf, off):
    n, off = _read_uvarint(buf, off)
    return _unzigzag(n), off


NODE_ID_LEN = 3


def _nodeid(node_id):
    """A node id as its 3 raw bytes.

    It was carried as a length-prefixed hex STRING until v15 -- 7 bytes to say
    3, on every beacon, twice on every ack and once on every acknowledged
    message. The hex spelling is for humans reading logs; the wire does not
    need it.
    """
    try:
        raw = bytes.fromhex(node_id)
    except (ValueError, TypeError):
        raise CotEncodeError("node id %r is not hex" % (node_id,))
    if len(raw) != NODE_ID_LEN:
        raise CotEncodeError("node id %r is not %d bytes" % (node_id, NODE_ID_LEN))
    return raw


def _read_nodeid(buf, off):
    if off + NODE_ID_LEN > len(buf):
        raise CotDecodeError("truncated node id")
    return buf[off:off + NODE_ID_LEN].hex(), off + NODE_ID_LEN


# Absolute timestamps go as their low 24 bits, not all 32.
#
# A CoT time is a unix second, and its top byte is the same number for half a
# year at a stretch -- so sending it is sending what the receiver already
# knows. The receiver rebuilds the full value by picking the multiple of 2^24
# nearest its own clock.
#
# THE WINDOW IS WHY THIS IS THREE BYTES AND NOT TWO. Two bytes saves one more
# and spans 18 hours, which covers every frame this link will realistically
# carry -- and fails SILENTLY when it does not, decoding an older event to a
# date in the wrong week rather than refusing it. There is no way for a
# receiver to notice: any 16-bit value is a valid time in the current window.
# ATAK does emit events carrying a time from hours or days earlier (a saved
# marker, a form filled in that morning), so the failure is reachable, and a
# wrong date that renders confidently is exactly the class of bug the version
# byte and the "never guess" rule elsewhere in this file exist to prevent.
# Three bytes spans 194 days, which is past any plausible skew, and still saves
# a byte on every frame that carries an absolute time.
#
# Deliberately NOT the "drop it and stamp arrival time" that TAKPacket-SDK
# does. A retry can arrive 16 seconds after the message was written, and a
# multi-hop relay later still; stamping arrival would quietly relabel when
# things happened, which for a chat log is a lie rather than an approximation.
_TIME_MOD = 1 << 24
_TIME_MASK = _TIME_MOD - 1


def _time_from_low(low, now=None):
    """Low 24 bits of a unix time -> the full value nearest the receiver."""
    if now is None:
        now = int(time.time())
    base = (now // _TIME_MOD) * _TIME_MOD + low
    # The candidate one window either side; pick whichever is closest to now.
    return min((base - _TIME_MOD, base, base + _TIME_MOD),
               key=lambda t: abs(t - now))


def _lstr(s):
    raw = s.encode("utf-8")
    if len(raw) > 255:
        raise CotEncodeError("string too long for lstr: %d bytes" % len(raw))
    return bytes([len(raw)]) + raw


def _lbytes(raw):
    """Length-prefixed raw bytes. Separate from _lstr because that one decodes
    as UTF-8, and a Bloom digest is arbitrary bytes that will not survive it."""
    raw = bytes(raw or b"")
    if len(raw) > 255:
        raise CotEncodeError("too long for lbytes: %d" % len(raw))
    return bytes([len(raw)]) + raw


def _read_lbytes(buf, off):
    if off >= len(buf):
        raise CotDecodeError("truncated lbytes length")
    n = buf[off]
    off += 1
    if off + n > len(buf):
        raise CotDecodeError("truncated lbytes body")
    return bytes(buf[off:off + n]), off + n


def _read_lstr(buf, off):
    if off >= len(buf):
        raise CotDecodeError("truncated lstr length")
    n = buf[off]
    off += 1
    if off + n > len(buf):
        raise CotDecodeError("truncated lstr body")
    try:
        return buf[off:off + n].decode("utf-8"), off + n
    except UnicodeDecodeError:
        raise CotDecodeError("lstr is not valid utf-8")


def _table_byte(table, value, escape_kind):
    """0x00 = escape (an lstr follows), 0x01..0xFE = index into `table`."""
    try:
        idx = table.index(value)
    except ValueError:
        idx = 0
    if idx == 0:
        _emit(escape_kind, value)
        return b"\x00" + _lstr(value)
    return bytes([idx])


def _read_table_byte(table, buf, off):
    if off >= len(buf):
        raise CotDecodeError("truncated table byte")
    idx = buf[off]
    off += 1
    if idx == 0:
        return _read_lstr(buf, off)
    if idx >= len(table):
        # A newer sender used a table entry this build doesn't have. Dropping
        # the packet is the append-only contract: fail loudly, never guess.
        raise CotDecodeError("table index %d out of range (max %d)" % (idx, len(table) - 1))
    return table[idx], off


# ---------------------------------------------------------------------------
# Marker identifiers and icons (protocol 16)
# ---------------------------------------------------------------------------
# An ATAK marker spends most of its bytes on text ATAK derived from something
# else it also sends. Measured on four real captures (docs/open-questions.md,
# 2026-10-12): the event uid is a 36-character UUID, the sender's own id is
# "ANDROID-" + 16 hex digits, the icon path is 28-74 characters that mostly
# restate the event type or the colour, and the callsign is often the icon's
# file name plus a counter, or a letter plus the local time of day.
#
# Every form below is exact-or-literal: the encoder builds the short form, the
# decoder rebuilds the text, and the short form is used ONLY when that rebuild
# equals the original character for character. Anything else goes as before.

_UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ANDROID_TEXT = re.compile(r"^ANDROID-[0-9a-f]{16}$")
UID_ANDROID = 0xFE   # in place of a length byte: 8 raw bytes follow
UID_UUID = 0xFF      # in place of a length byte: 16 raw bytes follow


def _uidstr(s):
    """An identifier: packed when it is a lowercase UUID or an ANDROID-<16 hex>
    id, otherwise a length-prefixed string. The length byte can no longer be
    254 or 255, which only a string that long would notice -- and that string
    was a very large uid to begin with."""
    if _UUID_TEXT.match(s):
        return bytes([UID_UUID]) + bytes.fromhex(s.replace("-", ""))
    if _ANDROID_TEXT.match(s):
        return bytes([UID_ANDROID]) + bytes.fromhex(s[8:])
    if len(s.encode("utf-8")) > 253:
        raise CotEncodeError("uid too long: %d bytes" % len(s.encode("utf-8")))
    return _lstr(s)


def _read_uidstr(buf, off):
    if off >= len(buf):
        raise CotDecodeError("truncated uid")
    kind = buf[off]
    if kind == UID_UUID:
        if off + 17 > len(buf):
            raise CotDecodeError("truncated packed uuid")
        h = buf[off + 1:off + 17].hex()
        return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:]), off + 17
    if kind == UID_ANDROID:
        if off + 9 > len(buf):
            raise CotDecodeError("truncated packed android id")
        return "ANDROID-" + buf[off + 1:off + 9].hex(), off + 9
    return _read_lstr(buf, off)


# --- <usericon iconsetpath> ---
ICON_LITERAL = 0     # an lstr follows
ICON_2525C = 1       # COT_MAPPING_2525C/<first three of the type>/<type>
ICON_2525B = 2       # the same under the older 2525B name
ICON_SPOTMAP = 3     # COT_MAPPING_SPOTMAP/<type>/<the marker's own colour>
ICON_OTHER_SET = 4    # a set not in KNOWN_ICONSETS: <uuid>/<group>/<file>.png, the uuid sent as 16 bytes
ICON_KNOWN_SET = 0x10   # + index into KNOWN_ICONSETS: the same, uuid in 0 bytes
ICON_KNOWN_GROUP = 0x20  # + index into KNOWN_ICON_GROUPS: set AND group in 0 bytes
# Append-only, like every table here (a new entry goes on the END; reordering
# would change what an existing byte means). Read 2026-10-12 from the ATAK 5.6
# iconsets.sqlite on a Pixel 7 (/sdcard/atak/Databases): every set the app ships
# and every (set, group) pair in them, 10 sets and 51 pairs covering all 3677
# icons. The ids are the app's own, so they should match on any install, but
# that is one phone's evidence. Two ids are not UUID-shaped (GeoOps is 64 hex,
# Default has 37 characters); the table covers them regardless.
KNOWN_ICONSETS = (
    "34ae1613-9645-4222-a9d2-e5f243dea2865",    # Default
    "f8f7f666-8b28-4b57-9fbb-e48e61d33b79",    # FEMA Icons
    "6d180afb-89a6-4c07-b2b3-a89748b6a38f",    # FalconView
    "ad78aafb-83a6-4c07-b2b9-a897a8b6a38f",    # Generic Icons
    "83198b4872a8c34eb9c549da8a4de5a28f07821185b39a2277948f66c24ac17a",    # GeoOps
    "f7f71666-8b28-4b57-9fbb-e38e61d33b79",    # Google
    "db450cbe-2fec-47fb-bd2b-ba2db89b035e",    # Incident Management Icons
    "6d781afb-89a6-4c07-b2b9-a89748b6a38f",    # OSM
    "66f14976-4b62-4023-8edb-d8d2ebeaa336",    # Public Safety Air
    "de450cbf-2ffc-47fb-bd2b-ba2db89b035e",    # Responder Icons
)
# (index into KNOWN_ICONSETS, group folder inside that set)
KNOWN_ICON_GROUPS = (
    (0, 'Animals'),
    (0, 'Buildings'),
    (0, 'Flags'),
    (0, 'Hiking'),
    (0, 'Hunting'),
    (0, 'Military'),
    (0, 'People'),
    (0, 'Plants'),
    (0, 'Summer'),
    (0, 'Transportation'),
    (0, 'Weather'),
    (8, 'Public Safety Air'),
    (2, 'FalconView'),
    (7, 'General'),
    (7, 'Landmarks'),
    (7, 'Leisure'),
    (7, 'Lodging'),
    (7, 'Misc'),
    (7, 'Nautical'),
    (7, 'Service'),
    (7, 'Sport'),
    (7, 'Transport'),
    (7, 'Waypoints'),
    (4, 'WildFire'),
    (3, 'LetterPins'),
    (3, 'Markers'),
    (3, 'NumPins'),
    (3, 'Pointers'),
    (3, 'Shapes'),
    (3, 'ShapesClr'),
    (3, 'SqrLetters'),
    (3, 'SqrNum'),
    (3, 'Tacks'),
    (6, 'Incident Management'),
    (9, 'Access Hazards'),
    (9, 'Facilities'),
    (9, 'Hazardous Materials'),
    (9, 'Hazards'),
    (9, 'Human Caused Hazards'),
    (9, 'Incident'),
    (9, 'Infrastructure'),
    (9, 'Lifelines'),
    (9, 'NIMS Positions'),
    (9, 'Natural Hazards'),
    (9, 'PrePlan'),
    (9, 'Public Alert'),
    (9, 'Resources'),
    (9, 'Transportation'),
    (5, 'Google'),
    (1, 'ESF'),
    (1, 'Iron Sites'),
)


def _color_text(color_el):
    """The colour as the receiver will print it back (signed, as ATAK writes
    it), or None when there is no usable colour."""
    if color_el is None:
        return None
    for attr in COLOR_ATTRS:
        raw = color_el.get(attr)
        if raw is not None:
            try:
                argb = int(raw)
            except (TypeError, ValueError):
                return None
            if argb > 2147483647:
                argb -= 1 << 32
            return str(argb)
    return None


def _icon_parts(path):
    """(uuid, group, file stem) of an iconset path, or None."""
    parts = path.split("/")
    if (len(parts) == 3
            and (_UUID_TEXT.match(parts[0]) or parts[0] in KNOWN_ICONSETS)
            and parts[2].endswith(".png") and len(parts[2]) > 4
            and parts[1] and "/" not in parts[1]):
        return parts[0], parts[1], parts[2][:-4]
    return None


def _encode_icon(path, cot_type, color_text):
    if path == "COT_MAPPING_2525C/%s/%s" % (cot_type[:3], cot_type):
        return bytes([ICON_2525C])
    if path == "COT_MAPPING_2525B/%s/%s" % (cot_type[:3], cot_type):
        return bytes([ICON_2525B])
    if color_text is not None and path == "COT_MAPPING_SPOTMAP/%s/%s" % (
            cot_type, color_text):
        return bytes([ICON_SPOTMAP])
    parts = _icon_parts(path)
    if parts is not None:
        set_uuid, group, stem = parts
        if set_uuid in KNOWN_ICONSETS:
            i = KNOWN_ICONSETS.index(set_uuid)
            if (i, group) in KNOWN_ICON_GROUPS:
                return (bytes([ICON_KNOWN_GROUP + KNOWN_ICON_GROUPS.index((i, group))])
                        + _lstr(stem))
            head = bytes([ICON_KNOWN_SET + i])
        else:
            head = bytes([ICON_OTHER_SET]) + bytes.fromhex(set_uuid.replace("-", ""))
        return head + _lstr(group) + _lstr(stem)
    return bytes([ICON_LITERAL]) + _lstr(path)


def _read_icon(buf, off, cot_type, color_text):
    if off >= len(buf):
        raise CotDecodeError("truncated icon")
    kind = buf[off]
    off += 1
    if kind == ICON_LITERAL:
        return _read_lstr(buf, off)
    if kind in (ICON_2525C, ICON_2525B):
        return "COT_MAPPING_%s/%s/%s" % (
            "2525C" if kind == ICON_2525C else "2525B", cot_type[:3], cot_type), off
    if kind == ICON_SPOTMAP:
        if color_text is None:
            raise CotDecodeError("spot-map icon without a colour")
        return "COT_MAPPING_SPOTMAP/%s/%s" % (cot_type, color_text), off
    if kind == ICON_OTHER_SET:
        if off + 16 > len(buf):
            raise CotDecodeError("truncated iconset uuid")
        h = buf[off:off + 16].hex()
        set_uuid = "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])
        off += 16
    elif ICON_KNOWN_GROUP <= kind < ICON_KNOWN_GROUP + len(KNOWN_ICON_GROUPS):
        set_index, group = KNOWN_ICON_GROUPS[kind - ICON_KNOWN_GROUP]
        stem, off = _read_lstr(buf, off)
        return "%s/%s/%s.png" % (KNOWN_ICONSETS[set_index], group, stem), off
    elif ICON_KNOWN_SET <= kind < ICON_KNOWN_SET + len(KNOWN_ICONSETS):
        set_uuid = KNOWN_ICONSETS[kind - ICON_KNOWN_SET]
    else:
        # Append-only contract: a kind this build lacks fails loudly.
        raise CotDecodeError("unknown icon kind %d" % kind)
    group, off = _read_lstr(buf, off)
    stem, off = _read_lstr(buf, off)
    return "%s/%s/%s.png" % (set_uuid, group, stem), off


# --- <contact callsign> that ATAK built from other fields ---
# A leading length byte of 0 never occurs in a literal callsign (the encoder
# only writes one when non-empty), so it is free to mean "derived".
CS_FROM_ICON = 0     # <icon file name> + " " + counter
CS_FROM_TIME = 1     # <prefix>.<day>.<hhmmss> of the creator's local clock
_TZ_STEP_S = 900


def _local_suffix(ts, tz_steps):
    t = time.gmtime(ts + tz_steps * _TZ_STEP_S)
    return ".%02d.%02d%02d%02d" % (t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _derive_callsign(callsign, icon_path, creator):
    """The short spelling of `callsign`, or None when it is not derivable."""
    if icon_path:
        parts = _icon_parts(icon_path)
        if parts is not None:
            m = re.match(r"^%s (\d{1,6})$" % re.escape(parts[2]), callsign)
            if m and str(int(m.group(1))) == m.group(1):
                return bytes([0, CS_FROM_ICON]) + _uvarint(int(m.group(1)))
    if creator is not None and creator.get("time"):
        try:
            created = _parse_ts(creator.get("time"))
        except CotEncodeError:
            return None
        for tz in sorted(range(-48, 57), key=abs):
            suffix = _local_suffix(created, tz)
            if callsign.endswith(suffix) and len(callsign) > len(suffix):
                prefix = callsign[:-len(suffix)]
                if prefix == creator.get("callsign"):
                    prefix = ""
                return (bytes([0, CS_FROM_TIME]) + _lstr(prefix)
                        + struct.pack(">b", tz))
    return None


def _read_derived_callsign(buf, off):
    """Returns a ("from_icon", n) or ("from_time", prefix, tz) placeholder: the
    fields it needs are read later in the frame."""
    if off + 2 > len(buf):
        raise CotDecodeError("truncated derived callsign")
    kind = buf[off + 1]
    off += 2
    if kind == CS_FROM_ICON:
        n, off = _read_uvarint(buf, off)
        return ("from_icon", n), off
    if kind == CS_FROM_TIME:
        prefix, off = _read_lstr(buf, off)
        if off >= len(buf):
            raise CotDecodeError("truncated derived callsign")
        (tz,) = struct.unpack(">b", buf[off:off + 1])
        return ("from_time", prefix, tz), off + 1
    raise CotDecodeError("unknown derived callsign kind %d" % kind)


def _resolve_callsign(pending, icon_path, creator):
    if pending[0] == "from_icon":
        parts = _icon_parts(icon_path or "")
        if parts is None:
            raise CotDecodeError("callsign derived from an icon that is absent")
        return "%s %d" % (parts[2], pending[1])
    if not creator or not creator.get("time"):
        raise CotDecodeError("callsign derived from a creator time that is absent")
    prefix = pending[1] or creator.get("callsign")
    if prefix is None:
        raise CotDecodeError("callsign derived from a creator callsign that is absent")
    return prefix + _local_suffix(_parse_ts(creator["time"]), pending[2])


def _nibble_pair(table_a, value_a, kind_a, table_b, value_b, kind_b):
    """Two lookups in one byte. Nibble 0 = escape; an *empty* escaped string
    means the attribute was absent entirely."""
    escapes = b""

    def one(table, value, kind):
        nonlocal escapes
        if value is None:
            escapes += _lstr("")
            return 0
        try:
            idx = table.index(value)
        except ValueError:
            idx = 0
        if idx == 0 or idx > 15:
            _emit(kind, value)
            escapes += _lstr(value)
            return 0
        return idx

    hi = one(table_a, value_a, kind_a)
    lo = one(table_b, value_b, kind_b)
    return bytes([(hi << 4) | lo]) + escapes


def _read_nibble_pair(table_a, table_b, buf, off):
    if off >= len(buf):
        raise CotDecodeError("truncated nibble pair")
    packed = buf[off]
    off += 1
    out = []
    for table, idx in ((table_a, packed >> 4), (table_b, packed & 0x0F)):
        if idx == 0:
            value, off = _read_lstr(buf, off)
            out.append(value if value else None)
        elif idx >= len(table):
            raise CotDecodeError("nibble index %d out of range" % idx)
        else:
            out.append(table[idx])
    return out[0], out[1], off


# ---------------------------------------------------------------------------
# Timestamps. Hand-rolled because datetime.fromisoformat cannot parse a
# trailing "Z" (or 3-digit fractional seconds) on the device's Python 3.8.
# calendar.timegm rather than time.mktime: mktime would contaminate the result
# with the phone's local timezone.
# ---------------------------------------------------------------------------

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?\s*([Zz]|[+-]\d{2}:?\d{2})?$"
)


def _parse_ts(text):
    if text is None:
        raise CotEncodeError("missing timestamp")
    m = _TS_RE.match(text.strip())
    if not m:
        raise CotEncodeError("unparseable timestamp %r" % text)
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    try:
        ts = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    except (ValueError, OverflowError):
        raise CotEncodeError("out-of-range timestamp %r" % text)
    offset = m.group(8)
    if offset and offset not in ("Z", "z"):
        sign = 1 if offset[0] == "+" else -1
        body = offset[1:].replace(":", "")
        ts -= sign * (int(body[:2]) * 3600 + int(body[2:]) * 60)
    return ts


def _format_ts(ts):
    t = time.gmtime(ts)
    return "%04d-%02d-%02dT%02d:%02d:%02d.000Z" % (
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec
    )


def _as_float(text):
    if text is None:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Compact frame
# ---------------------------------------------------------------------------

class _Children(dict):
    """Detail children by tag, where a tag is allowed to repeat.

    A dict of tag -> first element, so every existing `children.get(tag)` reads
    the same as it always did, plus `all(tag)` for the cases where repetition
    is the point. A drawn shape is the reason this exists: `DrawingRectangle`
    emits four `<link point="lat,lon,hae"/>` children, one per corner, and a
    plain dict silently kept the last of them.

    Note that a shape's `<link>` and a marker's `<link>` share only the tag. A
    marker's carries provenance -- uid, type, relation; a shape's carries a
    point and nothing else. Anything reading `<link>` has to know which it has,
    which is why REPEATABLE_DETAIL is a deliberately short list.
    """

    def __init__(self):
        dict.__init__(self)
        self._all = {}

    def add(self, child):
        self._all.setdefault(child.tag, []).append(child)
        self.setdefault(child.tag, child)

    def all(self, tag):
        return self._all.get(tag, [])


# Tags a well-formed event may carry more than one of. Everything else that
# repeats is still a fallback, because silently keeping one of them is how a
# shape lost three of its four corners before this existed.
REPEATABLE_DETAIL = frozenset(["link"])


def _detail_children(root, all_generic=False):
    """Returns (_Children, generic elements), or None with the reason in
    `_rejected_detail`.

    A child no typed encoding models -- or a second copy of one that does not
    repeat -- goes on the generic list rather than refusing the event; see the
    generic-elements block. `all_generic` puts every child there, which is the
    second attempt when a typed encoding refused its input. Either way the
    droppable children are dropped, and so is <takv> unless INCLUDE_TAKV,
    because the typed path drops it too and the two attempts must agree on
    what an event loses.

    Only a node that is not an element still refuses here. The reason is
    carried out of band rather than raised because the caller treats "not
    compact-encodable" as an ordinary outcome, not an error.
    """
    detail = root.find("detail")
    found = _Children()
    generic = []
    if detail is None:
        return found, generic
    rejected = []
    # Text loose in <detail> rather than inside a child. No encoding rebuilds
    # it, and before this check every compact path dropped it without a word
    # (found 2026-10-14 by the generic elements' own tests). It goes to zlib.
    if (detail.text and detail.text.strip()) or any(
            c.tail and c.tail.strip() for c in detail):
        rejected.append("text between <detail> elements")
    for child in detail:
        if not isinstance(child.tag, str):
            # Comments and processing instructions have non-str tags.
            if "non-element node" not in rejected:
                rejected.append("non-element node")
        elif child.tag in DROPPABLE_DETAIL:
            continue
        elif child.tag == "takv" and not INCLUDE_TAKV and all_generic:
            continue
        elif (all_generic or child.tag not in KNOWN_DETAIL
              or (child.tag in found and child.tag not in REPEATABLE_DETAIL)):
            generic.append(child)
        else:
            found.add(child)

    if rejected:
        _rejected_detail.reason = ", ".join(rejected)
        return None
    return found, generic


# encode() runs on the CoT listener thread, but tests and any future caller may
# not; a thread-local keeps the reason attached to the call that produced it.
_rejected_detail = threading.local()

# Enough to see an event's whole story in one line, bounded so a pathological
# event cannot write an unbounded log entry on the listener thread. A dozen
# fits a line, and no real event carries that many unmodelled children.
_MAX_GENERIC_REPORTED = 12


def _describe_generic(elements, children):
    """What rode as generic elements, for the bench log.

    Generic elements cost more than a typed encoding would, so this line is
    the list of what is worth teaching a typed encoder next -- the job the
    fallback reason did when these events fell back whole. It names what is
    INSIDE a container, because `<__routeinfo>` alone says nothing about
    whether the work is an afternoon or a fortnight, and it tells a second
    copy of a modelled tag from a tag nothing models, since the two are
    different fixes."""
    seen = []
    for el in elements:
        if children.get(el.tag) is not None and children.get(el.tag) is not el:
            text = "repeated <%s>" % el.tag
        else:
            text = "<%s>" % el.tag
            inner = sorted(set(c.tag for c in el if isinstance(c.tag, str)))
            if inner:
                text += " containing %s" % ", ".join("<%s>" % t for t in inner[:4])
                if len(inner) > 4:
                    text += ", ..."
        if text not in seen:
            seen.append(text)
    line = ", ".join(seen[:_MAX_GENERIC_REPORTED])
    if len(seen) > _MAX_GENERIC_REPORTED:
        line += ", +%d more" % (len(seen) - _MAX_GENERIC_REPORTED)
    return line


# ---------------------------------------------------------------------------
# GeoChat
#
# A chat message was the largest frame left on this link -- 286 bytes on air
# against 39 for a position report, seven times the size, and all of it going
# the zlib route because <__chat> and <__serverdestination> were unmodelled.
#
# It is large because it says the same few things over and over. In a message
# of 914 bytes of XML, the SENDER'S UID appears four times (the event uid,
# <chatgrp uid0>, <link uid>, and inside the <remarks source>), the ADDRESSEE
# appears five times (the event uid, chatroom, id, <chatgrp uid1>, <remarks
# to>), and the MESSAGE ID appears twice (the event uid and <__chat
# messageId>). The only fields carrying information once are the sender's
# callsign and the message text itself.
#
# So the encoding sends each distinct value once and rebuilds the repeats.
# Deliberately NOT by leaning on the step 2 identity binding, which would be
# smaller still: a receiver that lacks the binding refuses the frame, and it
# would be acknowledged anyway -- the ack is queued before decoding -- so a
# chat message would be reported DELIVERED having never been rendered. That
# is exactly the silent loss milestone 4 exists to prevent. Positions can
# afford to be refused because another arrives within the minute; chat cannot.
# ---------------------------------------------------------------------------

CH_UUID_TEXT = 0x01        # the uid's last field is not a 36-char UUID
CH_CALLSIGN = 0x02
CH_ROOM = 0x04             # chatroom differs from the addressee
CH_GROUP_ID = 0x08         # <chatgrp id> differs from chatroom
CH_PARENT = 0x10           # parent is not the usual RootContactGroup
CH_GROUP_OWNER = 0x20
CH_LINK = 0x40             # a parent <link> was present

CHAT_PARENT_DEFAULT = "RootContactGroup"
CHAT_SOURCE_PREFIX = "BAO.F.ATAK."

# Room names, so the default broadcast room costs one byte instead of fifteen.
# Escapes to an lstr for anything else, which is what a direct message's
# addressee (a plain ATAK uid) takes -- one byte worse than sending it raw, in
# exchange for fourteen better on every message to the whole group.
CHAT_ROOM_TABLE = ["", "All Chat Rooms"]
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_CHAT_ATTRS = frozenset(["parent", "groupOwner", "messageId", "chatroom", "id",
                         "senderCallsign"])
_CHATGRP_ATTRS = frozenset(["uid0", "uid1", "id"])
_CHAT_REMARKS_ATTRS = frozenset(["source", "to", "time"])


def _pack_uuid(text):
    """A 36-character UUID as its 16 raw bytes, or None if it isn't one."""
    if not text or not _UUID_RE.match(text):
        return None
    try:
        return bytes(bytearray.fromhex(text.replace("-", "")))
    except ValueError:
        return None


def _unpack_uuid(raw):
    h = "".join("%02x" % b for b in bytearray(raw))
    return "-".join((h[:8], h[8:12], h[12:16], h[16:20], h[20:]))


def _chat_parts(root, children, uid, ts):
    """Pull a GeoChat apart into the values that actually differ, or return
    None if this is not exactly the shape we can rebuild.

    Strict on purpose. Everything here is reconstructed on the far side, so
    anything unrecognised has to fall back rather than be dropped -- the same
    rule the rest of the codec follows, and the reason a chat that carries
    something new stays readable instead of arriving mangled.
    """
    chat = children.get("__chat")
    remarks = children.get("remarks")
    if chat is None or remarks is None:
        return None
    if not uid.startswith("GeoChat."):
        return None
    fields = uid.split(".")
    if len(fields) != 4:
        # A room name containing a dot would land here. Rebuilding it would
        # need the split to be unambiguous, and it is not.
        return None
    _, sender, addressee, tail = fields
    if not (sender and addressee and tail):
        return None

    if set(chat.attrib) - _CHAT_ATTRS:
        return None
    if set(remarks.attrib) - _CHAT_REMARKS_ATTRS or len(remarks):
        return None

    groups = chat.findall("chatgrp")
    if len(groups) != 1 or len(chat) != 1:
        return None
    group = groups[0]
    if set(group.attrib) - _CHATGRP_ATTRS:
        return None

    # Every one of these has to be the value we would rebuild, or the message
    # is not the shape this encoding describes. A direct message may well
    # differ from the room message this was written against -- if it does, it
    # falls back to zlib exactly as it does today and the log names the reason,
    # which is how the marker encoding got finished.
    if group.get("uid0") != sender or group.get("uid1") != addressee:
        return None
    if chat.get("messageId") != tail:
        return None
    if remarks.get("to") != addressee:
        return None
    if remarks.get("source") != CHAT_SOURCE_PREFIX + sender:
        return None
    if remarks.get("time") and _parse_ts(remarks.get("time")) != ts:
        return None

    link = children.get("link")
    if link is not None:
        if link.get("uid") != sender or link.get("relation") != "p-p":
            return None
        if set(link.attrib) - {"uid", "type", "relation"}:
            return None

    return {
        "sender": sender,
        "addressee": addressee,
        "tail": tail,
        "message": remarks.text or "",
        "callsign": chat.get("senderCallsign"),
        "chatroom": chat.get("chatroom"),
        "group_id": group.get("id"),
        "parent": chat.get("parent"),
        "group_owner": chat.get("groupOwner") == "true",
        "link_type": link.get("type") if link is not None else None,
        "has_link": link is not None,
    }


# ---------------------------------------------------------------------------
# Drawn shapes
#
# A rectangle drawn on the 2026-08-05 bench went out at 452 B on air, 1.26 s --
# the most expensive frame this project has sent -- because every part of it
# was unmodelled. From DrawingRectangle.toCot() in the ATAK source:
#
#     <link point="lat,lon,hae"/>   x4, one per corner
#     <__shapeExtras editable= cpvis=/>
#     <strokeColor value=/> <strokeWeight value=/> <strokeStyle value=/>
#     <fillColor value=/>
#
# The vertices are the bulk of it, and they are all within a few hundred metres
# of the event's own point -- which is the shape's centre. So they go as
# zigzag varint deltas from that point, in two packed columns (every latitude,
# then every longitude) rather than interleaved pairs. Packing by column keeps
# same-magnitude numbers adjacent, and is what the prior art does for the same
# reason.
# ---------------------------------------------------------------------------

SH_HAE = 0x01              # vertices carry an altitude column
SH_STROKE_COLOR = 0x02
SH_STROKE_WEIGHT = 0x04
SH_STROKE_STYLE = 0x08
SH_FILL_COLOR = 0x10
SH_EXTRAS = 0x20           # <__shapeExtras>, two booleans
SH_LABELS = 0x40           # <labels_on value=>

# StrokeFillDetailHandler.LINE_STYLES, and "solid" is what ATAK falls back to
# for anything it does not recognise.
#
# The leading "" is not decoration: _table_byte treats index 0 as "not in the
# table" and escapes to a string. Version 8 shipped this list without it, which
# put "solid" -- the value nearly every shape carries -- at index 0, so every
# one escaped and cost 6 bytes instead of 1. The 2026-08-05 bench caught it in
# one line: `[cot-codec] stroke_style_escape: solid`.
LINE_STYLE_TABLE = ["", "solid", "dashed", "dotted", "outlined"]

SHAPE_STYLE_TAGS = ("strokeColor", "strokeWeight", "strokeStyle", "fillColor")
_MAX_VERTICES = 512


def _parse_point(text):
    """A shape link's `point`: "lat,lon" or "lat,lon,hae"."""
    if not text:
        return None
    parts = text.split(",")
    if len(parts) not in (2, 3):
        return None
    lat, lon = _as_float(parts[0]), _as_float(parts[1])
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    hae = _as_float(parts[2]) if len(parts) == 3 else None
    return lat, lon, hae


def _shape_parts(children):
    """The vertex list and styling of a drawn shape, or None if this is not
    one. Strict: everything here is rebuilt on the far side."""
    links = children.all("link")
    if len(links) < 2:
        return None
    vertices = []
    for link in links:
        if set(link.attrib) != {"point"}:
            # A marker's parent <link> carries uid/type/relation and is a
            # different thing entirely; anything else is unmodelled.
            return None
        point = _parse_point(link.get("point"))
        if point is None:
            return None
        vertices.append(point)
    if len(vertices) > _MAX_VERTICES:
        return None

    # Either every vertex has an altitude or none does -- a mixed list would
    # need a per-vertex flag for something ATAK does not produce.
    haes = [v[2] for v in vertices]
    if any(h is None for h in haes) and any(h is not None for h in haes):
        return None

    style = {}
    for tag in SHAPE_STYLE_TAGS:
        el = children.get(tag)
        if el is None:
            continue
        if set(el.attrib) != {"value"}:
            return None
        style[tag] = el.get("value")

    extras = children.get("__shapeExtras")
    if extras is not None and set(extras.attrib) - {"editable", "cpvis"}:
        return None
    labels = children.get("labels_on")
    if labels is not None and set(labels.attrib) != {"value"}:
        return None

    return {
        "vertices": vertices,
        "style": style,
        "extras": extras,
        "labels": labels.get("value") if labels is not None else None,
    }


def _encode_shape(shape, lat_i, lon_i):
    """Vertices as zigzag deltas from the event's own point, in two columns."""
    vertices = shape["vertices"]
    subflags = 0
    out = bytearray()
    body = bytearray(_uvarint(len(vertices)))
    body += b"".join(_svarint(int(round(v[0] * 1e7)) - lat_i) for v in vertices)
    body += b"".join(_svarint(int(round(v[1] * 1e7)) - lon_i) for v in vertices)
    if vertices[0][2] is not None:
        subflags |= SH_HAE
        body += b"".join(_svarint(int(round(v[2]))) for v in vertices)

    style = shape["style"]
    if "strokeColor" in style:
        subflags |= SH_STROKE_COLOR
        body += struct.pack(">i", _java_int(style["strokeColor"]))
    if "strokeWeight" in style:
        subflags |= SH_STROKE_WEIGHT
        # Tenths: ATAK writes a double but only ever uses values like 4.0.
        body += _uvarint(max(0, min(65535,
                                    int(round(float(style["strokeWeight"]) * 10)))))
    if "strokeStyle" in style:
        subflags |= SH_STROKE_STYLE
        body += _table_byte(LINE_STYLE_TABLE, style["strokeStyle"],
                            "stroke_style_escape")
    if "fillColor" in style:
        subflags |= SH_FILL_COLOR
        body += struct.pack(">i", _java_int(style["fillColor"]))

    extras = shape["extras"]
    if extras is not None:
        subflags |= SH_EXTRAS
        body += bytes([(1 if extras.get("editable") == "true" else 0)
                       | (2 if extras.get("cpvis") == "true" else 0)])
    if shape["labels"] is not None:
        subflags |= SH_LABELS
        body += bytes([1 if shape["labels"] == "true" else 0])

    out += bytes([subflags]) + body
    return bytes(out)


def _java_int(text):
    """ATAK writes colours as Java ints, so opaque white is "-1" and not
    4294967295. Normalised to the signed range the wire carries."""
    value = int(text)
    if not -2147483648 <= value <= 4294967295:
        raise CotEncodeError("colour %r out of range" % text)
    return value - (1 << 32) if value > 2147483647 else value


def _decode_shape(body, off, lat_i, lon_i):
    if off >= len(body):
        raise CotDecodeError("truncated shape block")
    subflags = body[off]
    off += 1
    count, off = _read_uvarint(body, off)
    if not 2 <= count <= _MAX_VERTICES:
        raise CotDecodeError("shape claims %d vertices" % count)

    lats, lons, haes = [], [], []
    for _ in range(count):
        d, off = _read_svarint(body, off)
        lats.append(lat_i + d)
    for _ in range(count):
        d, off = _read_svarint(body, off)
        lons.append(lon_i + d)
    if subflags & SH_HAE:
        for _ in range(count):
            d, off = _read_svarint(body, off)
            haes.append(d)

    style = {}
    if subflags & SH_STROKE_COLOR:
        if off + 4 > len(body):
            raise CotDecodeError("truncated strokeColor")
        style["strokeColor"] = str(struct.unpack(">i", body[off:off + 4])[0])
        off += 4
    if subflags & SH_STROKE_WEIGHT:
        tenths, off = _read_uvarint(body, off)
        style["strokeWeight"] = "%.1f" % (tenths / 10.0)
    if subflags & SH_STROKE_STYLE:
        style["strokeStyle"], off = _read_table_byte(LINE_STYLE_TABLE, body, off)
    if subflags & SH_FILL_COLOR:
        if off + 4 > len(body):
            raise CotDecodeError("truncated fillColor")
        style["fillColor"] = str(struct.unpack(">i", body[off:off + 4])[0])
        off += 4

    extras = None
    if subflags & SH_EXTRAS:
        if off >= len(body):
            raise CotDecodeError("truncated shape extras")
        extras = (bool(body[off] & 1), bool(body[off] & 2))
        off += 1
    labels = None
    if subflags & SH_LABELS:
        if off >= len(body):
            raise CotDecodeError("truncated labels_on")
        labels = "true" if body[off] else "false"
        off += 1

    return {
        "lats": lats, "lons": lons, "haes": haes,
        "style": style, "extras": extras, "labels": labels,
    }, off


def _build_shape_detail(detail, shape):
    for index in range(len(shape["lats"])):
        el = ET.SubElement(detail, "link")
        point = "%.7f,%.7f" % (shape["lats"][index] / 1e7,
                               shape["lons"][index] / 1e7)
        if shape["haes"]:
            point += ",%.1f" % shape["haes"][index]
        el.set("point", point)
    if shape["extras"] is not None:
        el = ET.SubElement(detail, "__shapeExtras")
        el.set("editable", "true" if shape["extras"][0] else "false")
        el.set("cpvis", "true" if shape["extras"][1] else "false")
    for tag in SHAPE_STYLE_TAGS:
        if tag in shape["style"]:
            ET.SubElement(detail, tag).set("value", shape["style"][tag])
    if shape["labels"] is not None:
        ET.SubElement(detail, "labels_on").set("value", shape["labels"])


# ---------------------------------------------------------------------------
# Circles and ellipses
#
# A different problem from a drawn polygon: there are no vertices, just a
# centre (the event's own point) and radii. From DrawingCircle.toCot(), whose
# own comment calls the layout "this dusty old format":
#
#   <shape>
#     <ellipse major= minor= angle= [swapAxis=]/>        one per ring
#     <link type="b-x-KmlStyle" uid="{event uid}.Style" relation="p-c">
#       <Style>
#         <LineStyle><color>ffffffff</color><width>4.0</width></LineStyle>
#         <PolyStyle><color>00000000</color></PolyStyle>
#       </Style>
#     </link>
#     <link relation="p-p-CenterAnchor" type= uid=/>      optional
#     <link relation="p-p-RadiusAnchor" type= uid=/>      optional
#   </shape>
#
# Almost all of it is constant or derivable. The style link's uid is the
# event's own uid with ".Style" appended, its type and relation never vary,
# and the KML nesting carries no information at all -- three colours' worth of
# hex and a width, wrapped in 150 bytes of ceremony. What is left is the radii.
#
# Radii go in CENTIMETRES. ATAK writes a Java double, so a 152.4 m radius can
# arrive as "152.4000244140625"; a centimetre is far below what anyone can draw
# or see, and this is the same trade already made for hae and speed. It does
# mean the re-emitted XML differs from what was sent, in the last decimal
# places of a number nobody reads.
# ---------------------------------------------------------------------------

CIRCLE_STYLE_TYPE = "b-x-KmlStyle"
CIRCLE_STYLE_RELATION = "p-c"
CIRCLE_STYLE_SUFFIX = ".Style"
ANCHOR_RELATION_TABLE = ["", "p-p-CenterAnchor", "p-p-RadiusAnchor"]

CI_STYLE = 0x01            # the KML <link><Style> block is present
CI_LINE_COLOR = 0x02
CI_LINE_WIDTH = 0x04
CI_POLY_COLOR = 0x08

CE_ROUND = 0x01            # minor == major, i.e. a circle rather than an ellipse
CE_ANGLE_360 = 0x02
CE_SWAP_AXIS = 0x04

_MAX_RINGS = 16
_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def _pack_colour_hex(text):
    """ATAK's getColorString(): exactly eight lower-case hex digits."""
    if not text or not _HEX8.match(text):
        raise CotEncodeError("colour %r is not 8 hex digits" % text)
    return struct.pack(">I", int(text, 16))


def _uid_field(uid):
    """A uid as 16 raw bytes when it is a UUID, else a string. The leading byte
    says which, so an anchor marker named anything at all still round-trips."""
    raw = _pack_uuid(uid)
    if raw is not None:
        return b"\x00" + raw
    return b"\x01" + _lstr(uid)


def _read_uid_field(buf, off):
    if off >= len(buf):
        raise CotDecodeError("truncated uid field")
    kind = buf[off]
    off += 1
    if kind == 0:
        if off + 16 > len(buf):
            raise CotDecodeError("truncated uuid")
        return _unpack_uuid(buf[off:off + 16]), off + 16
    return _read_lstr(buf, off)


def _circle_parts(children, uid):
    """The radii and styling of a circle or ellipse, or None if this <shape> is
    not one we can rebuild."""
    shape = children.get("shape")
    if shape is None or shape.attrib:
        return None

    rings, style, anchors = [], None, []
    for child in shape:
        if child.tag == "ellipse":
            if set(child.attrib) - {"major", "minor", "angle", "swapAxis"}:
                return None
            major, minor = _as_float(child.get("major")), _as_float(child.get("minor"))
            angle = _as_float(child.get("angle"))
            if major is None or minor is None or angle is None:
                return None
            if not (0 <= major < 1e7 and 0 <= minor < 1e7):
                return None
            rings.append((major, minor, angle,
                          child.get("swapAxis") == "true"))
        elif child.tag == "link" and child.get("type") == CIRCLE_STYLE_TYPE:
            if style is not None:
                return None
            if child.get("uid") != uid + CIRCLE_STYLE_SUFFIX:
                return None
            if child.get("relation") != CIRCLE_STYLE_RELATION:
                return None
            if set(child.attrib) - {"type", "uid", "relation"}:
                return None
            style = _kml_style(child)
            if style is None:
                return None
        elif child.tag == "link":
            if set(child.attrib) - {"relation", "type", "uid"} or len(child):
                return None
            if child.get("relation") not in ANCHOR_RELATION_TABLE[1:]:
                return None
            if not child.get("uid"):
                return None
            anchors.append(child)
        else:
            return None

    if not rings or len(rings) > _MAX_RINGS or len(anchors) > 2:
        return None
    return {"rings": rings, "style": style, "anchors": anchors}


def _kml_style(link):
    """The <Style><LineStyle>/<PolyStyle> nest -> the three values it carries.

    Everything else about it is ceremony: ~150 bytes of nesting that says the
    same thing on every shape ATAK has ever drawn.
    """
    kids = list(link)
    if len(kids) != 1 or kids[0].tag != "Style" or kids[0].attrib:
        return None
    out = {}
    for part in kids[0]:
        if part.attrib:
            return None
        if part.tag == "LineStyle":
            for sub in part:
                if sub.attrib or len(sub):
                    return None
                if sub.tag == "color":
                    out["line_color"] = (sub.text or "").strip()
                elif sub.tag == "width":
                    width = _as_float(sub.text)
                    if width is None or not 0 <= width < 6553:
                        return None
                    out["line_width"] = width
                else:
                    return None
        elif part.tag == "PolyStyle":
            for sub in part:
                if sub.attrib or len(sub) or sub.tag != "color":
                    return None
                out["poly_color"] = (sub.text or "").strip()
        else:
            return None
    return out


def _encode_circle(circle):
    subflags = 0
    body = bytearray(_uvarint(len(circle["rings"])))
    for major, minor, angle, swap in circle["rings"]:
        eflags = 0
        fields = bytearray(_uvarint(int(round(major * 100))))
        if minor == major:
            eflags |= CE_ROUND
        else:
            fields += _uvarint(int(round(minor * 100)))
        if angle == 360.0:
            eflags |= CE_ANGLE_360
        else:
            fields += _uvarint(int(round(angle * 10)) % 3600)
        if swap:
            eflags |= CE_SWAP_AXIS
        body += bytes([eflags]) + fields

    style = circle["style"]
    if style is not None:
        subflags |= CI_STYLE
        if "line_color" in style:
            subflags |= CI_LINE_COLOR
            body += _pack_colour_hex(style["line_color"])
        if "line_width" in style:
            subflags |= CI_LINE_WIDTH
            body += _uvarint(int(round(style["line_width"] * 10)))
        if "poly_color" in style:
            subflags |= CI_POLY_COLOR
            body += _pack_colour_hex(style["poly_color"])

    body += bytes([len(circle["anchors"])])
    for link in circle["anchors"]:
        body += _table_byte(ANCHOR_RELATION_TABLE, link.get("relation"),
                            "anchor_relation_escape")
        body += _table_byte(TYPE_TABLE, link.get("type") or "",
                            "anchor_type_escape")
        body += _uid_field(link.get("uid"))
    return bytes([subflags]) + bytes(body)


def _decode_circle(body, off):
    if off >= len(body):
        raise CotDecodeError("truncated circle block")
    subflags = body[off]
    off += 1
    count, off = _read_uvarint(body, off)
    if not 1 <= count <= _MAX_RINGS:
        raise CotDecodeError("circle claims %d rings" % count)

    rings = []
    for _ in range(count):
        if off >= len(body):
            raise CotDecodeError("truncated ring")
        eflags = body[off]
        off += 1
        major_cm, off = _read_uvarint(body, off)
        if eflags & CE_ROUND:
            minor_cm = major_cm
        else:
            minor_cm, off = _read_uvarint(body, off)
        if eflags & CE_ANGLE_360:
            angle = 360.0
        else:
            tenths, off = _read_uvarint(body, off)
            angle = tenths / 10.0
        rings.append((major_cm / 100.0, minor_cm / 100.0, angle,
                      bool(eflags & CE_SWAP_AXIS)))

    style = None
    if subflags & CI_STYLE:
        style = {}
        if subflags & CI_LINE_COLOR:
            if off + 4 > len(body):
                raise CotDecodeError("truncated line colour")
            style["line_color"] = "%08x" % struct.unpack(">I", body[off:off + 4])[0]
            off += 4
        if subflags & CI_LINE_WIDTH:
            tenths, off = _read_uvarint(body, off)
            style["line_width"] = tenths / 10.0
        if subflags & CI_POLY_COLOR:
            if off + 4 > len(body):
                raise CotDecodeError("truncated poly colour")
            style["poly_color"] = "%08x" % struct.unpack(">I", body[off:off + 4])[0]
            off += 4

    if off >= len(body):
        raise CotDecodeError("truncated anchor count")
    n_anchors = body[off]
    off += 1
    if n_anchors > 2:
        raise CotDecodeError("circle claims %d anchors" % n_anchors)
    anchors = []
    for _ in range(n_anchors):
        relation, off = _read_table_byte(ANCHOR_RELATION_TABLE, body, off)
        kind, off = _read_table_byte(TYPE_TABLE, body, off)
        anchor_uid, off = _read_uid_field(body, off)
        anchors.append((relation, kind, anchor_uid))

    return {"rings": rings, "style": style, "anchors": anchors}, off


def _build_circle_detail(detail, circle, uid):
    shape = ET.SubElement(detail, "shape")
    for major, minor, angle, swap in circle["rings"]:
        el = ET.SubElement(shape, "ellipse")
        el.set("major", _trim_float(major))
        el.set("minor", _trim_float(minor))
        el.set("angle", _trim_float(angle))
        if swap:
            el.set("swapAxis", "true")

    style = circle["style"]
    if style is not None:
        link = ET.SubElement(shape, "link")
        link.set("type", CIRCLE_STYLE_TYPE)
        link.set("uid", uid + CIRCLE_STYLE_SUFFIX)
        link.set("relation", CIRCLE_STYLE_RELATION)
        node = ET.SubElement(link, "Style")
        if "line_color" in style or "line_width" in style:
            line = ET.SubElement(node, "LineStyle")
            if "line_color" in style:
                ET.SubElement(line, "color").text = style["line_color"]
            if "line_width" in style:
                ET.SubElement(line, "width").text = _trim_float(style["line_width"])
        if "poly_color" in style:
            poly = ET.SubElement(node, "PolyStyle")
            ET.SubElement(poly, "color").text = style["poly_color"]

    for relation, kind, anchor_uid in circle["anchors"]:
        el = ET.SubElement(shape, "link")
        el.set("relation", relation)
        if kind:
            el.set("type", kind)
        el.set("uid", anchor_uid)


def _trim_float(value):
    """ATAK writes Java doubles, so a whole number reads "360.0" and not
    "360". Matching that keeps a round trip textually stable."""
    return "%.1f" % value if value == int(value) else repr(round(value, 2))


# ---------------------------------------------------------------------------
# Routes
#
# The most expensive event on this link: 1615 B of XML, 651 B on air across two
# packets on the 2026-08-07 bench -- and because it fragments, an ARQ retry
# sends both fragments again. That run needed one, so a single route cost four
# transmissions and pushed a two-node channel to 12.3% duty.
#
# Three parts, from EditablePolyline.toCot(), Route.toCot() and
# NavigationCueHandler:
#
#   <link point="lat,lon,hae" [type= uid= relation="c" callsign= remarks=]/>
#   <link_attr color= stroke= style= type= method= direction= routetype=
#              order= planningmethod= prefix=/>
#   <__routeinfo><__navcues>[<__navcue id= voice= text=>
#                              <trigger mode= value=/>]</__navcues></__routeinfo>
#
# The waypoints dominate, and they get the same treatment as a drawn shape:
# zigzag deltas from the event's own point, in packed columns. What a route
# adds is that a waypoint may also be a marker, carrying a uid, callsign and
# remarks -- so those ride in a second pass over a bitmap of which waypoints
# have them.
#
# `<__navcues>` is empty unless the operator recorded voice cues, which is the
# ordinary case and costs one byte.
# ---------------------------------------------------------------------------

ROUTE_METHOD_TABLE = ["", "Driving", "Walking", "Flying", "Swimming"]
ROUTE_DIRECTION_TABLE = ["", "Infil", "Exfil"]
ROUTE_TYPE_TABLE = ["", "Primary", "Secondary", "Tertiary"]
ROUTE_ORDER_TABLE = ["", "Ascending Check Points", "Descending Check Points"]

LINK_ATTR_ORDER = ("color", "stroke", "style", "type", "method", "direction",
                   "routetype", "order", "planningmethod", "prefix")

RT_HAE = 0x01
RT_MARKERS = 0x02
RT_LINK_ATTR = 0x04
RT_NAVCUES = 0x08
# A second bitmap over the marked points, saying which are PLAIN CONTROL
# POINTS: type b-m-p-c, no callsign, no remarks, and a uid nothing can
# reference. Those cost one bit each instead of ~20 bytes.
#
# The 2026-08-12 bench measured a real 55-point ATAK route: 3 waypoints and 52
# control points, every control point unnamed. Their uids alone were 884 of
# 2568 bytes, and a route is the largest thing this link carries.
RT_PLAIN_CP = 0x10

# ATAK's own names for the two kinds of point in a route (Route.java).
# The distinction is not cosmetic and it is what makes deriving safe: a
# WAYPOINT becomes a Marker in a map group and RouteImporter looks it up by uid
# with deepFindUID, so another message can reference it. A CONTROL POINT, in
# ATAK's own comment, is "never added to a mapgroup" -- so nothing can ever
# refer to its uid, and inventing one on receipt is unobservable.
WAYPOINT_TYPE = "b-m-p-w"
CONTROLPOINT_TYPE = "b-m-p-c"

# Namespace for uids derived rather than transmitted. Derived from the route's
# OWN uid and the point's index, so the answer is stable across retransmissions
# and re-sends: ATAK finds the control point it already has and moves it,
# rather than accumulating a fresh set every time the route is shared.
_DERIVED_UID_NS = uuid.UUID("6f1d2c4e-8a3b-4f5c-9d0e-7a6b5c4d3e2f")


def derive_waypoint_uid(route_uid, index):
    """The uid a plain control point gets when none was sent."""
    return str(uuid.uuid5(_DERIVED_UID_NS, "%s:%d" % (route_uid, index)))

WAYPOINT_RELATION = "c"
_MAX_WAYPOINTS = 512
_MAX_NAVCUES = 256


def _route_parts(children, ts):
    """A route's waypoints, styling and navigation cues, or None."""
    links = children.all("link")
    if not links:
        return None

    waypoints = []
    for link in links:
        unknown = set(link.attrib) - {"point", "type", "uid", "relation",
                                      "callsign", "remarks"}
        if unknown:
            return None
        point = _parse_point(link.get("point"))
        if point is None:
            return None
        marker = None
        if link.get("uid") is not None:
            if link.get("relation") != WAYPOINT_RELATION:
                return None
            marker = (link.get("type") or "", link.get("uid"),
                      link.get("callsign") or "", link.get("remarks") or "")
        elif set(link.attrib) != {"point"}:
            return None
        waypoints.append((point, marker))
    if len(waypoints) > _MAX_WAYPOINTS:
        return None

    haes = [w[0][2] for w in waypoints]
    if any(h is None for h in haes) and any(h is not None for h in haes):
        return None

    attrs = None
    link_attr = children.get("link_attr")
    if link_attr is not None:
        if set(link_attr.attrib) - set(LINK_ATTR_ORDER):
            return None
        attrs = dict(link_attr.attrib)

    cues = None
    info = children.get("__routeinfo")
    if info is not None:
        cues = _navcues(info)
        if cues is None:
            return None

    if attrs is None and cues is None and all(w[1] is None for w in waypoints):
        # Bare point links with no route furniture: that is a drawn shape, and
        # _shape_parts encodes it more cheaply.
        return None
    return {"waypoints": waypoints, "attrs": attrs, "cues": cues}


def _navcues(info):
    """<__routeinfo> -> the list of navigation cues, or None if it holds
    anything else. Empty is the ordinary case: ATAK writes <__navcues/> for
    every route whether or not the operator recorded any."""
    if info.attrib:
        return None
    kids = list(info)
    if not kids:
        return []
    if len(kids) != 1 or kids[0].tag != "__navcues" or kids[0].attrib:
        return None
    out = []
    for cue in kids[0]:
        if cue.tag != "__navcue" or set(cue.attrib) - {"id", "voice", "text"}:
            return None
        if not cue.get("id"):
            return None
        triggers = []
        for trigger in cue:
            if trigger.tag != "trigger" or set(trigger.attrib) - {"mode", "value"}:
                return None
            mode = trigger.get("mode")
            if mode is None or not mode.lstrip("-").isdigit():
                return None
            triggers.append((int(mode), trigger.get("value") or ""))
        out.append((cue.get("id"), cue.get("voice") or "",
                    cue.get("text") or "", triggers))
        if len(out) > _MAX_NAVCUES:
            return None
    return out


def _is_plain_control_point(marker):
    """A control point with nothing worth transmitting.

    Every part of it is recoverable: the type is the constant, callsign and
    remarks are empty, and the uid is one ATAK generated at random for an item
    it never puts in a map group. A named control point, or one carrying
    remarks, is NOT plain -- the operator put something there and it goes on
    the wire.
    """
    kind, _uid, callsign, remarks = marker
    return kind == CONTROLPOINT_TYPE and not callsign and not remarks


def _encode_route(route, lat_i, lon_i):
    waypoints = route["waypoints"]
    subflags = 0
    body = bytearray(_uvarint(len(waypoints)))
    body += b"".join(_svarint(int(round(w[0][0] * 1e7)) - lat_i) for w in waypoints)
    body += b"".join(_svarint(int(round(w[0][1] * 1e7)) - lon_i) for w in waypoints)
    if waypoints[0][0][2] is not None:
        subflags |= RT_HAE
        body += b"".join(_svarint(int(round(w[0][2]))) for w in waypoints)

    marked = [i for i, w in enumerate(waypoints) if w[1] is not None]
    if marked:
        subflags |= RT_MARKERS
        bitmap = bytearray((len(waypoints) + 7) // 8)
        for i in marked:
            bitmap[i // 8] |= 1 << (i % 8)
        body += bitmap

        # Plain control points carry nothing but their position. Everything
        # else about them is either fixed (the type) or empty (callsign,
        # remarks) or unreferenceable (the uid), so a bit is the whole record.
        plain = [i for i in marked if _is_plain_control_point(waypoints[i][1])]
        if plain:
            subflags |= RT_PLAIN_CP
            cp_bitmap = bytearray((len(waypoints) + 7) // 8)
            for i in plain:
                cp_bitmap[i // 8] |= 1 << (i % 8)
            body += cp_bitmap
        plain_set = set(plain)

        for i in marked:
            if i in plain_set:
                continue
            kind, uid, callsign, remarks = waypoints[i][1]
            body += _table_byte(TYPE_TABLE, kind, "waypoint_type_escape")
            body += _uid_field(uid)
            body += _lstr(callsign) + _lstr(remarks)

    attrs = route["attrs"]
    if attrs is not None:
        subflags |= RT_LINK_ATTR
        body += _svarint(_java_int(attrs["color"]) if attrs.get("color") else 0)
        body += _uvarint(max(0, min(65535, int(float(attrs.get("stroke") or 0)))))
        body += _uvarint(max(0, min(255, int(float(attrs.get("style") or 0)))))
        body += _lstr(attrs.get("type") or "")
        for key, table in (("method", ROUTE_METHOD_TABLE),
                           ("direction", ROUTE_DIRECTION_TABLE),
                           ("routetype", ROUTE_TYPE_TABLE),
                           ("order", ROUTE_ORDER_TABLE)):
            body += _table_byte(table, attrs.get(key) or "",
                                "route_%s_escape" % key)
        body += _lstr(attrs.get("planningmethod") or "")
        body += _lstr(attrs.get("prefix") or "")

    if route["cues"] is not None:
        subflags |= RT_NAVCUES
        body += _uvarint(len(route["cues"]))
        for cue_id, voice, text, triggers in route["cues"]:
            body += _uid_field(cue_id) + _lstr(voice) + _lstr(text)
            body += _uvarint(len(triggers))
            for mode, value in triggers:
                body += _svarint(mode) + _lstr(value)
    return bytes([subflags]) + bytes(body)


def _decode_route(body, off, lat_i, lon_i, route_uid=""):
    if off >= len(body):
        raise CotDecodeError("truncated route block")
    subflags = body[off]
    off += 1
    count, off = _read_uvarint(body, off)
    if not 1 <= count <= _MAX_WAYPOINTS:
        raise CotDecodeError("route claims %d waypoints" % count)

    lats, lons, haes = [], [], []
    for _ in range(count):
        d, off = _read_svarint(body, off)
        lats.append(lat_i + d)
    for _ in range(count):
        d, off = _read_svarint(body, off)
        lons.append(lon_i + d)
    if subflags & RT_HAE:
        for _ in range(count):
            d, off = _read_svarint(body, off)
            haes.append(d)

    markers = {}
    if subflags & RT_MARKERS:
        width = (count + 7) // 8
        if off + width > len(body):
            raise CotDecodeError("truncated waypoint bitmap")
        bitmap = body[off:off + width]
        off += width

        cp_bitmap = None
        if subflags & RT_PLAIN_CP:
            if off + width > len(body):
                raise CotDecodeError("truncated control-point bitmap")
            cp_bitmap = body[off:off + width]
            off += width

        for i in range(count):
            if not bitmap[i // 8] & (1 << (i % 8)):
                continue
            if cp_bitmap is not None and cp_bitmap[i // 8] & (1 << (i % 8)):
                # A plain control point: everything about it is recoverable.
                # The uid is invented here rather than transmitted, which is
                # safe precisely because ATAK never puts a control point in a
                # map group -- see RT_PLAIN_CP. Derived from the route's own
                # uid so the answer is the same every time this route arrives.
                markers[i] = (CONTROLPOINT_TYPE,
                              derive_waypoint_uid(route_uid, i), "", "")
                continue
            kind, off = _read_table_byte(TYPE_TABLE, body, off)
            marker_uid, off = _read_uid_field(body, off)
            callsign, off = _read_lstr(body, off)
            remarks, off = _read_lstr(body, off)
            markers[i] = (kind, marker_uid, callsign, remarks)

    attrs = None
    if subflags & RT_LINK_ATTR:
        attrs = {}
        colour, off = _read_svarint(body, off)
        attrs["color"] = str(colour)
        stroke, off = _read_uvarint(body, off)
        attrs["stroke"] = str(stroke)
        style, off = _read_uvarint(body, off)
        attrs["style"] = str(style)
        attrs["type"], off = _read_lstr(body, off)
        for key, table in (("method", ROUTE_METHOD_TABLE),
                           ("direction", ROUTE_DIRECTION_TABLE),
                           ("routetype", ROUTE_TYPE_TABLE),
                           ("order", ROUTE_ORDER_TABLE)):
            attrs[key], off = _read_table_byte(table, body, off)
        attrs["planningmethod"], off = _read_lstr(body, off)
        attrs["prefix"], off = _read_lstr(body, off)

    cues = None
    if subflags & RT_NAVCUES:
        n, off = _read_uvarint(body, off)
        if n > _MAX_NAVCUES:
            raise CotDecodeError("route claims %d nav cues" % n)
        cues = []
        for _ in range(n):
            cue_id, off = _read_uid_field(body, off)
            voice, off = _read_lstr(body, off)
            text, off = _read_lstr(body, off)
            t_count, off = _read_uvarint(body, off)
            triggers = []
            for _ in range(t_count):
                mode, off = _read_svarint(body, off)
                value, off = _read_lstr(body, off)
                triggers.append((mode, value))
            cues.append((cue_id, voice, text, triggers))

    return {"lats": lats, "lons": lons, "haes": haes, "markers": markers,
            "attrs": attrs, "cues": cues}, off


def _build_route_detail(detail, route):
    for index in range(len(route["lats"])):
        el = ET.SubElement(detail, "link")
        point = "%.7f,%.7f" % (route["lats"][index] / 1e7,
                               route["lons"][index] / 1e7)
        if route["haes"]:
            point += ",%.1f" % route["haes"][index]
        el.set("point", point)
        marker = route["markers"].get(index)
        if marker is not None:
            kind, uid, callsign, remarks = marker
            if kind:
                el.set("type", kind)
            el.set("uid", uid)
            el.set("relation", WAYPOINT_RELATION)
            el.set("callsign", callsign)
            el.set("remarks", remarks)

    if route["attrs"] is not None:
        el = ET.SubElement(detail, "link_attr")
        for key in LINK_ATTR_ORDER:
            el.set(key, route["attrs"].get(key, ""))

    if route["cues"] is not None:
        info = ET.SubElement(detail, "__routeinfo")
        cues = ET.SubElement(info, "__navcues")
        for cue_id, voice, text, triggers in route["cues"]:
            node = ET.SubElement(cues, "__navcue")
            node.set("id", cue_id)
            node.set("voice", voice)
            node.set("text", text)
            for mode, value in triggers:
                trigger = ET.SubElement(node, "trigger")
                trigger.set("mode", str(mode))
                trigger.set("value", value)


def _encode_chat(chat):
    """The chat block: subflags, then only the values that are not rebuilt."""
    subflags = 0
    tail = _pack_uuid(chat["tail"])
    if tail is None:
        subflags |= CH_UUID_TEXT
        tail = _lstr(chat["tail"])
    fields = bytearray(_lstr(chat["sender"])
                       + _table_byte(CHAT_ROOM_TABLE, chat["addressee"],
                                     "chat_addressee_escape")
                       + tail)
    fields += _lstr(chat["message"])

    if chat["callsign"]:
        subflags |= CH_CALLSIGN
        fields += _lstr(chat["callsign"])
    if chat["chatroom"] != chat["addressee"]:
        subflags |= CH_ROOM
        fields += _lstr(chat["chatroom"] or "")
    if chat["group_id"] != chat["chatroom"]:
        subflags |= CH_GROUP_ID
        fields += _lstr(chat["group_id"] or "")
    if chat["parent"] and chat["parent"] != CHAT_PARENT_DEFAULT:
        subflags |= CH_PARENT
        fields += _lstr(chat["parent"])
    if chat["group_owner"]:
        subflags |= CH_GROUP_OWNER
    if chat["has_link"]:
        subflags |= CH_LINK
        fields += _table_byte(TYPE_TABLE, chat["link_type"] or "",
                              "chat_link_type_escape")
    return bytes([subflags]) + bytes(fields)


def _decode_chat(body, off, ts):
    """The chat block -> the parts a GeoChat is rebuilt from."""
    if off >= len(body):
        raise CotDecodeError("truncated chat block")
    subflags = body[off]
    off += 1
    sender, off = _read_lstr(body, off)
    addressee, off = _read_table_byte(CHAT_ROOM_TABLE, body, off)
    if subflags & CH_UUID_TEXT:
        tail, off = _read_lstr(body, off)
    else:
        if off + 16 > len(body):
            raise CotDecodeError("truncated chat message id")
        tail = _unpack_uuid(body[off:off + 16])
        off += 16
    message, off = _read_lstr(body, off)

    callsign = None
    if subflags & CH_CALLSIGN:
        callsign, off = _read_lstr(body, off)
    chatroom = addressee
    if subflags & CH_ROOM:
        chatroom, off = _read_lstr(body, off)
    group_id = chatroom
    if subflags & CH_GROUP_ID:
        group_id, off = _read_lstr(body, off)
    parent = CHAT_PARENT_DEFAULT
    if subflags & CH_PARENT:
        parent, off = _read_lstr(body, off)
    link_type = None
    if subflags & CH_LINK:
        link_type, off = _read_table_byte(TYPE_TABLE, body, off)

    return {
        "uid": "GeoChat.%s.%s.%s" % (sender, addressee, tail),
        "sender": sender,
        "addressee": addressee,
        "tail": tail,
        "message": message,
        "callsign": callsign,
        "chatroom": chatroom,
        "group_id": group_id,
        "parent": parent,
        "group_owner": bool(subflags & CH_GROUP_OWNER),
        "has_link": bool(subflags & CH_LINK),
        "link_type": link_type,
    }, off


def _build_chat_detail(detail, chat, ts):
    """Rebuild <__chat>, <chatgrp>, <remarks> and the parent <link>."""
    el = ET.SubElement(detail, "__chat")
    el.set("parent", chat["parent"])
    el.set("groupOwner", "true" if chat["group_owner"] else "false")
    el.set("messageId", chat["tail"])
    el.set("chatroom", chat["chatroom"])
    el.set("id", chat["group_id"])
    if chat["callsign"]:
        el.set("senderCallsign", chat["callsign"])
    group = ET.SubElement(el, "chatgrp")
    group.set("uid0", chat["sender"])
    group.set("uid1", chat["addressee"])
    group.set("id", chat["group_id"])

    if chat["has_link"]:
        link = ET.SubElement(detail, "link")
        link.set("uid", chat["sender"])
        if chat["link_type"]:
            link.set("type", chat["link_type"])
        link.set("relation", "p-p")

    rem = ET.SubElement(detail, "remarks")
    rem.set("source", CHAT_SOURCE_PREFIX + chat["sender"])
    rem.set("to", chat["addressee"])
    rem.set("time", _format_ts(ts))
    rem.text = chat["message"] or None


# ---------------------------------------------------------------------------
# Session compression
#
# Everything here applies to ONE kind of traffic: a device's own self-SA
# position report, the frame every node sends every minute for as long as it is
# switched on. Nothing else uses it. A marker carries a uid that is unique to
# the marker and a callsign that names the marker, so there is nothing constant
# to bind; chat and drawings go out through the reliable path, which never
# passes a session at all.
#
# The self-SA frame is different because 32 of its 61 bytes -- a 24-byte
# ANDROID-<imei> uid and the callsign -- are identical in every frame the node
# will ever send, and the mesh envelope already carries an authenticated
# `origin` that identifies the sender for free. So the identity is sent once
# and referred to afterwards.
#
# Two independent mechanisms, deliberately kept independent:
#
#   the BINDING   origin -> (uid, callsign). Established by a frame carrying
#                 X_BIND, valid until replaced. Losing it costs the receiver
#                 every position from that node until the next keyframe,
#                 because a CoT event without a uid is not something ATAK can
#                 render and not something this codec will guess at.
#
#   the BASE      a keyframe's absolute time/stale/lat/lon, named by a
#                 one-byte generation. Delta frames are offsets from it.
#
# Deltas reference the KEYFRAME, not the immediately preceding frame. Against
# the previous frame the deltas are marginally smaller, but a single lost frame
# then desyncs the receiver until the next keyframe -- every subsequent delta
# is measured from a base it never reconstructed. Against the keyframe, losing
# an ordinary frame costs exactly that frame. Measured over a 5-minute keyframe
# interval the robust form costs 3.55 s/min against 3.45 at 16 nodes -- 5.9%
# duty against 5.7% -- because varint boundaries are coarse enough that the
# extra magnitude usually does not cross one. Two tenths of a point is not a
# price worth paying for a scheme that breaks on the first lost packet.
# ---------------------------------------------------------------------------

# A keyframe every five minutes. The cost is the 32 identity bytes plus
# absolute rather than delta-coded position; the benefit is the bound on how
# long a node that missed one stays invisible. Five was picked off the
# measured curve: 3 minutes costs 6.2% duty and 10 minutes buys only 5.7%, so
# the knee is flat and the shorter recovery is worth more than the byte.
DEFAULT_KEYFRAME_FRAMES = 5

# A delta whose time offset exceeds this is treated as a desync rather than
# applied. The generation byte wraps every 256 keyframes -- around 21 hours at
# the default interval -- and this is what stops a wrapped generation from
# silently matching a base from yesterday and placing a node where it was then.
_MAX_DELTA_S = 3600


class SaRx(object):
    """What a receiver remembers about one origin's self-SA stream.

    One instance per origin. The bridge owns the mapping; this class knows
    nothing about how origins are authenticated, only that whoever hands it a
    frame has already established which sender the frame came from.
    """

    __slots__ = ("uid", "callsign", "gen", "ts", "stale_d", "lat_i", "lon_i")

    def __init__(self):
        self.uid = None
        self.callsign = None
        self.gen = None
        self.ts = self.stale_d = self.lat_i = self.lon_i = None

    def bind(self, uid, callsign):
        self.uid = uid
        self.callsign = callsign

    def rebase(self, gen, ts, stale_d, lat_i, lon_i):
        self.gen = gen
        self.ts, self.stale_d = ts, stale_d
        self.lat_i, self.lon_i = lat_i, lon_i


class SaTx(object):
    """The sending half: what this node has told the mesh, and when to say it
    again.

    `pending` exists because encoding is not the same as transmitting. A
    compact frame can still be discarded -- encode() falls back to zlib if it
    somehow exceeds the budget -- and a session that advanced for a frame that
    was never sent would leave every receiver decoding deltas against a base
    they were never given. So the state change is staged here and committed by
    the caller that actually emits the frame.
    """

    def __init__(self, keyframe_every=DEFAULT_KEYFRAME_FRAMES):
        self.keyframe_every = keyframe_every
        self.uid = None
        self.callsign = None
        self.gen = 0xFF          # so the first keyframe is generation 0
        self.ts = self.stale_d = self.lat_i = self.lon_i = None
        self.since_keyframe = 0
        self.forced = True       # nothing has been bound yet
        self.pending = None

    def force_keyframe(self):
        """Send identity in full on the next self-SA.

        Called when a node appears that may never have heard this one bind --
        which is the case a fixed interval handles worst, since a node joining
        just after a keyframe would otherwise stay invisible for the rest of
        the interval.
        """
        self.forced = True

    def commit(self):
        if self.pending is None:
            return
        for key, value in self.pending.items():
            setattr(self, key, value)
        self.pending = None

    def abort(self):
        self.pending = None


def _sa_plan(session, uid, callsign, ts, stale_d, lat_i, lon_i):
    """Decide how a self-SA frame refers to what the receiver already knows.

    Returns (ext_bits, generation, deltas) where `deltas` is None for an
    absolute frame. Never returns a delta that is not strictly cheaper than
    the absolute form it replaces.
    """
    keyframe = (session.forced
                or session.uid != uid
                or session.callsign != callsign
                or session.since_keyframe >= session.keyframe_every - 1)

    if not keyframe:
        # stale is inherited wholesale by a delta frame, so a change to it is
        # one of the things that forces a new base.
        if session.ts is not None and session.stale_d == stale_d:
            d_ts = ts - session.ts
            if 0 <= d_ts <= _MAX_DELTA_S:
                packed = (_svarint(d_ts) + _svarint(lat_i - session.lat_i)
                          + _svarint(lon_i - session.lon_i))
                # 14 = what the absolute form costs: 4 for time, 8 for lat/lon
                # and at least 2 for the stale offset a delta frame inherits
                # instead of carrying. A delta that is not cheaper than that is
                # not worth the base it would tie the receiver to.
                if len(packed) < 14:
                    session.pending = {
                        "since_keyframe": session.since_keyframe + 1}
                    return X_BOUND | X_DELTA, session.gen, packed

        # A delta was not viable -- the node jumped, or its staleness changed.
        # Send absolutes, but there is still no reason to restate the identity,
        # so this stays a bound frame and merely opens a new base.
        gen = (session.gen + 1) & 0xFF
        session.pending = {
            "gen": gen, "ts": ts, "stale_d": stale_d,
            "lat_i": lat_i, "lon_i": lon_i,
            "since_keyframe": session.since_keyframe + 1,
        }
        return X_BOUND, gen, None

    gen = (session.gen + 1) & 0xFF
    session.pending = {
        "uid": uid, "callsign": callsign, "gen": gen,
        "ts": ts, "stale_d": stale_d, "lat_i": lat_i, "lon_i": lon_i,
        "since_keyframe": 0, "forced": False,
    }
    return X_BIND, gen, None


# ---------------------------------------------------------------------------
# Generic elements: the detail children no typed encoding above models
#
# Until this existed, ONE unfamiliar detail child sent the whole event to the
# zlib'd-XML path. Run against the 47 CoT fixtures Meshtastic's TAKPacket-SDK
# publishes (2026-10-14), 20 fell back that way at a median 280 B on air, 18 of
# them two packets, where Meshtastic's own encoding carried the same events in
# a median 97 B. The causes were mostly small: a rectangle's <tog enabled="0"/>,
# iTAK spelling it <precisionLocation>, and whole classes nothing here models --
# <emergency>, <task>, <range>/<bearing>, <bullseye>, <sensor>, <environment>,
# TAK Talk, aircraft, a medline casevac.
#
# This is the general-purpose answer rather than one typed encoder per class:
# an element is its tag, its attributes in order, its text and its children,
# with tag and attribute NAMES from append-only tables and each VALUE stored in
# whichever typed form rebuilds it character for character -- an integer, a
# decimal with its exact digit count, a coordinate list, a timestamp relative
# to the event, a packed UUID, a boolean -- and as literal text otherwise. The
# encoder decodes every typed value and compares before using it, so it is
# exact-or-literal like everything else in this codec, and LOSSLESS where the
# typed encoders above each drop the attributes they do not model.
#
# It is used two ways (see encode_compact):
#   - alongside the typed encoders, for the children they do not know, and
#   - for the WHOLE detail when a typed encoder refuses its input -- a chat or
#     route in a shape this codec cannot rebuild exactly -- so the event head
#     (uid, type, time, point) still rides compact instead of everything
#     dropping to zlib'd XML.
#
# Whitespace-only text is formatting and is not carried; text BETWEEN sibling
# elements (mixed content), namespaced tags, comments and processing
# instructions are refused, and refusal still means the zlib'd-XML path.
#
# A frame carrying generic elements is a different frame TYPE (T_COMPACT_X),
# not a flag bit, because a v15 decoder ignores flag bits it does not know and
# never checks for trailing bytes: a flag would make an older build show an
# <emergency> as a plain dot, silently. An unknown type it refuses -- which is
# the contract the protocol version exists to keep, and the version nibble has
# no room left to bump.
# ---------------------------------------------------------------------------

# APPEND-ONLY, index 0 the escape, like every table in this module. Seeded from
# the tags and attribute names in the Meshtastic fixtures plus the ones ATAK
# 5.6 writes on the typed paths above, since a refused typed encoding sends
# those through here too. An unlisted name costs its spelling; it still works.
GENERIC_TAG_TABLE = [
    "",
    # what the typed paths model, for when the whole detail goes generic
    "contact", "__group", "status", "track", "takv", "precisionlocation",
    "uid", "color", "creator", "remarks", "usericon", "link", "__chat",
    "chatgrp", "__shapeExtras", "strokeColor", "strokeWeight", "strokeStyle",
    "fillColor", "labels_on", "shape", "ellipse", "Style", "LineStyle",
    "PolyStyle", "width", "link_attr", "__routeinfo", "__navcues", "__navcue",
    "trigger",
    # what fell back on the 2026-10-14 comparison
    "tog", "precisionLocation", "emergency", "task", "range", "bearing",
    "inclination", "bullseye", "environment", "sensor", "_medevac_",
    "zMistsMap", "zMist", "_aircot_", "UID", "_radio", "voice", "callsign",
    "lang", "text", "chatroom-id", "voice_profile_id", "roomId",
    "sender-callsign", "chatroom-name", "chatroom-participants", "Ea",
    # other ATAK detail children a node can plausibly send
    "height", "height_unit", "__video", "ConnectionEntry", "__geofence",
    "fileshare", "hierarchy", "attachment_list", "rangeUnits",
    "bearingUnits", "northRef", "__milsym", "__milicon", "medevac",
]

GENERIC_ATTR_TABLE = [
    "",
    "point", "callsign", "value", "type", "uid", "relation", "altsrc",
    "geopointsrc", "id", "Droid", "argb", "parent_callsign", "production_time",
    "endpoint", "role", "name", "speed", "course", "battery", "readiness",
    "iconsetpath", "senderCallsign", "parent", "groupOwner", "messageId",
    "chatroom", "chatRoom", "uid0", "uid1", "source", "to", "time", "os",
    "version", "device", "platform", "phone", "major", "minor", "angle",
    "enabled", "method", "direction", "prefix", "stroke", "order",
    "routetype", "color", "style", "planningmethod", "title", "priority",
    "status", "assignee", "note", "cancel", "distance", "bearingRef",
    "rangeRingVisible", "hasRangeRings", "edgeToCenter", "mils",
    "temperature", "windDirection", "windSpeed", "azimuth", "range", "fov",
    "vfov", "elevation", "roll", "model", "medline_remarks", "freq",
    "precedence", "urgent", "hoist", "extraction_equipment", "ventilator",
    "blood", "litter", "ambulatory", "security", "hlz_marking",
    "zone_prot_marker", "us_military", "us_civilian", "non_us_military",
    "non_us_civilian", "epw", "child", "terrain_slope", "terrain_slope_dir",
    "terrain_rough", "terrain_loose", "terrain_trees", "terrain_wires",
    "terrain_other", "z", "m", "i", "s", "t", "flight", "reg", "cat", "icao",
    "cot_host_id", "rssi", "gps", "squawk", "url", "mime", "remarks", "cpvis",
    "editable", "height", "unit", "visible", "ringRadius", "group",
    "sender", "dest", "mode", "label", "lat", "lon", "hae", "ce", "le",
]

# One header byte per value: the kind in the low nibble, a parameter in the
# high one (a digit count, or how many numbers a list holds).
GV_STR = 0x0       # uvarint length, UTF-8
GV_UUID = 0x1      # 16 raw bytes, lowercase canonical text
GV_ANDROID = 0x2   # 8 raw bytes, "ANDROID-" + 16 lowercase hex
GV_INT = 0x3       # svarint
GV_DEC = 0x4       # svarint mantissa; param = digits after the point (1-15)
GV_TRUE = 0x5
GV_FALSE = 0x6
GV_EMPTY = 0x7
GV_EVENT_UID = 0x8  # the event's own uid
GV_TIME = 0x9      # svarint, 10^-param s from the event time, ".fffZ" form
GV_NUMS = 0xA      # param = count (2-15): comma-separated numbers, each
                   # one byte of digits-after-point and an svarint mantissa

# Bounds on what a decoder will rebuild, so a hostile or corrupt frame cannot
# make it recurse or allocate without limit. Nothing ATAK sends nests deeper
# than five (a circle's <shape><link><Style><LineStyle><color>).
_GENERIC_MAX_DEPTH = 8
_GENERIC_MAX_ELEMENTS = 512

_INT_TEXT = re.compile(r"^-?(?:0|[1-9]\d{0,17})$")
_DEC_TEXT = re.compile(r"^-?\d{1,18}\.\d{1,15}$")


_GENERIC_TAG_INDEX = dict((n, i) for i, n in enumerate(GENERIC_TAG_TABLE) if i)
_GENERIC_ATTR_INDEX = dict((n, i) for i, n in enumerate(GENERIC_ATTR_TABLE) if i)


def _generic_name(index, name):
    """_table_byte's wire form, without its per-escape log line: one event can
    carry hundreds of unlisted names, and _describe_generic already says which
    elements rode generic in a single bounded line."""
    i = index.get(name)
    return bytes([i]) if i else b"\x00" + _lstr(name)


class _GenericRefused(CotEncodeError):
    """The element cannot be carried exactly; the event goes to zlib'd XML."""


def _number_parts(text):
    """(digits after the point, integer mantissa) for a decimal or integer
    string, or None. Exactness is checked by the caller's round trip."""
    if _INT_TEXT.match(text):
        return 0, int(text)
    if _DEC_TEXT.match(text) and len(text) <= 34:
        whole, frac = text.split(".")
        return len(frac), int(whole.replace("-", "") + frac) * (
            -1 if text.startswith("-") else 1)
    return None


def _number_text(digits, mantissa):
    if digits == 0:
        return str(mantissa)
    sign = "-" if mantissa < 0 else ""
    raw = str(abs(mantissa)).rjust(digits + 1, "0")
    return "%s%s.%s" % (sign, raw[:-digits], raw[-digits:])


def _time_text(ts, digits, units):
    """ISO text for a value `units` of 10^-digits seconds from `ts`."""
    scale = 10 ** digits
    whole, frac = divmod(ts * scale + units, scale)
    t = time.gmtime(whole)
    text = "%04d-%02d-%02dT%02d:%02d:%02d" % (
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    if digits:
        text += "." + str(frac).rjust(digits, "0")
    return text + "Z"


def _gvalue_candidates(text, ctx):
    """Typed forms worth trying for `text`, smallest-likely first."""
    if text == "":
        yield bytes([GV_EMPTY])
        return
    if text == "true":
        yield bytes([GV_TRUE])
    elif text == "false":
        yield bytes([GV_FALSE])
    if text == ctx["uid"]:
        yield bytes([GV_EVENT_UID])
    if _UUID_TEXT.match(text):
        yield bytes([GV_UUID]) + bytes.fromhex(text.replace("-", ""))
    elif _ANDROID_TEXT.match(text):
        yield bytes([GV_ANDROID]) + bytes.fromhex(text[8:])
    parts = _number_parts(text)
    if parts is not None:
        digits, mantissa = parts
        if digits == 0:
            yield bytes([GV_INT]) + _svarint(mantissa)
        else:
            yield bytes([GV_DEC | digits << 4]) + _svarint(mantissa)
    m = _TS_RE.match(text)
    if m and text.endswith("Z"):
        digits = len(m.group(7) or "")
        if digits <= 9:
            try:
                whole = _parse_ts(text)
            except CotEncodeError:
                whole = None
            if whole is not None:
                frac = int(m.group(7) or "0")
                units = (whole - ctx["ts"]) * 10 ** digits + frac
                yield bytes([GV_TIME | digits << 4]) + _svarint(units)
    pieces = text.split(",")
    if 2 <= len(pieces) <= 15:
        packed = bytearray([GV_NUMS | len(pieces) << 4])
        for piece in pieces:
            p = _number_parts(piece)
            if p is None:
                break
            packed += bytes([p[0]]) + _svarint(p[1])
        else:
            yield bytes(packed)


def _encode_gvalue(text, ctx):
    """The smallest form of `text` that decodes back to exactly `text`."""
    best = None
    for candidate in _gvalue_candidates(text, ctx):
        try:
            back, end = _read_gvalue(candidate, 0, ctx)
        except CotDecodeError:
            continue
        if back == text and end == len(candidate) and (
                best is None or len(candidate) < len(best)):
            best = candidate
    raw = text.encode("utf-8")
    literal = bytes([GV_STR]) + _uvarint(len(raw)) + raw
    return best if best is not None and len(best) < len(literal) else literal


def _read_gvalue(buf, off, ctx):
    if off >= len(buf):
        raise CotDecodeError("truncated generic value")
    head = buf[off]
    kind, param = head & 0x0F, head >> 4
    off += 1
    if kind == GV_STR:
        n, off = _read_uvarint(buf, off)
        if off + n > len(buf):
            raise CotDecodeError("truncated generic string")
        try:
            return buf[off:off + n].decode("utf-8"), off + n
        except UnicodeDecodeError:
            raise CotDecodeError("generic string is not valid utf-8")
    if kind == GV_UUID:
        if off + 16 > len(buf):
            raise CotDecodeError("truncated generic uuid")
        h = buf[off:off + 16].hex()
        return ("%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:]),
                off + 16)
    if kind == GV_ANDROID:
        if off + 8 > len(buf):
            raise CotDecodeError("truncated generic android id")
        return "ANDROID-" + buf[off:off + 8].hex(), off + 8
    if kind == GV_INT:
        n, off = _read_svarint(buf, off)
        return str(n), off
    if kind == GV_DEC:
        if not 1 <= param <= 15:
            raise CotDecodeError("generic decimal with %d digits" % param)
        n, off = _read_svarint(buf, off)
        return _number_text(param, n), off
    if kind == GV_TRUE:
        return "true", off
    if kind == GV_FALSE:
        return "false", off
    if kind == GV_EMPTY:
        return "", off
    if kind == GV_EVENT_UID:
        if ctx["uid"] is None:
            raise CotDecodeError("generic value refers to an absent event uid")
        return ctx["uid"], off
    if kind == GV_TIME:
        if param > 9:
            raise CotDecodeError("generic time with %d digits" % param)
        units, off = _read_svarint(buf, off)
        try:
            return _time_text(ctx["ts"], param, units), off
        except (ValueError, OverflowError, OSError):
            raise CotDecodeError("generic time out of range")
    if kind == GV_NUMS:
        if not 2 <= param <= 15:
            raise CotDecodeError("generic number list of %d" % param)
        out = []
        for _ in range(param):
            if off >= len(buf):
                raise CotDecodeError("truncated generic number list")
            digits = buf[off]
            if digits > 15:
                raise CotDecodeError("generic number with %d digits" % digits)
            n, off = _read_svarint(buf, off + 1)
            out.append(_number_text(digits, n))
        return ",".join(out), off
    raise CotDecodeError("unknown generic value kind 0x%x" % kind)


def _encode_generic(el, ctx, depth=0):
    if not isinstance(el.tag, str):
        raise _GenericRefused("a comment or processing instruction in <detail>")
    if el.tag.startswith("{"):
        raise _GenericRefused("namespaced <%s>" % el.tag)
    if depth >= _GENERIC_MAX_DEPTH:
        raise _GenericRefused("<%s> nests deeper than %d"
                              % (el.tag, _GENERIC_MAX_DEPTH))
    for child in el:
        if child.tail and child.tail.strip():
            raise _GenericRefused("text between elements inside <%s>" % el.tag)
    text = el.text if el.text and el.text.strip() else None
    children = list(el)
    out = bytearray(_generic_name(_GENERIC_TAG_INDEX, el.tag))
    out += _uvarint(len(el.attrib) << 2 | (2 if children else 0)
                    | (1 if text is not None else 0))
    for name, value in el.attrib.items():
        if name.startswith("{"):
            raise _GenericRefused("namespaced attribute on <%s>" % el.tag)
        out += _generic_name(_GENERIC_ATTR_INDEX, name)
        out += _encode_gvalue(value, ctx)
    if text is not None:
        out += _encode_gvalue(text, ctx)
    if children:
        out += _uvarint(len(children))
        for child in children:
            out += _encode_generic(child, ctx, depth + 1)
    return bytes(out)


def _read_generic(buf, off, ctx, parent, depth=0):
    if depth >= _GENERIC_MAX_DEPTH:
        raise CotDecodeError("generic element nests deeper than %d"
                             % _GENERIC_MAX_DEPTH)
    ctx["count"] += 1
    if ctx["count"] > _GENERIC_MAX_ELEMENTS:
        raise CotDecodeError("more than %d generic elements"
                             % _GENERIC_MAX_ELEMENTS)
    tag, off = _read_table_byte(GENERIC_TAG_TABLE, buf, off)
    if not tag:
        raise CotDecodeError("generic element with an empty tag")
    header, off = _read_uvarint(buf, off)
    el = ET.SubElement(parent, tag)
    for _ in range(header >> 2):
        name, off = _read_table_byte(GENERIC_ATTR_TABLE, buf, off)
        value, off = _read_gvalue(buf, off, ctx)
        if name in el.attrib:
            raise CotDecodeError("generic <%s> repeats attribute %s" % (tag, name))
        el.set(name, value)
    if header & 1:
        el.text, off = _read_gvalue(buf, off, ctx)
    if header & 2:
        count, off = _read_uvarint(buf, off)
        if count > len(buf) - off:
            # Every child costs at least two bytes, so this bounds the loop
            # by the frame rather than by a number the frame claims.
            raise CotDecodeError("generic child count exceeds the frame")
        for _ in range(count):
            off = _read_generic(buf, off, ctx, el, depth + 1)
    return off


def _encode_generic_block(elements, ctx):
    out = bytearray(_uvarint(len(elements)))
    for el in elements:
        out += _encode_generic(el, ctx)
    return bytes(out)


def _read_generic_block(buf, off, ctx, detail):
    count, off = _read_uvarint(buf, off)
    if count == 0:
        raise CotDecodeError("generic block with no elements")
    if count > len(buf) - off:
        raise CotDecodeError("generic element count exceeds the frame")
    for _ in range(count):
        off = _read_generic(buf, off, ctx, detail)
    return off


def encode_compact(root, session=None):
    """Returns a complete compact frame, or None if this event isn't one the
    compact codec fully understands (caller falls back to zlib'd XML).

    Two attempts. First the typed encodings, with any detail child none of
    them models carried as a generic element. If a typed encoding refuses its
    input, the whole detail goes generic instead, which keeps the event head
    compact and never carries session state. Only what neither can carry
    exactly -- see the generic-elements block -- reaches zlib'd XML."""
    if session is not None:
        session.abort()
    frame = _encode_compact(root, session, all_generic=False)
    if frame is _RETRY_ALL_GENERIC:
        if session is not None:
            session.abort()
        frame = _encode_compact(root, None, all_generic=True)
    return frame


def _with_unconsumed(root, generic, children, chat, shape, circle, route):
    """`generic` plus every known child no typed encoding will write, in
    document order.

    KNOWN_DETAIL says a tag is understood SOMEWHERE, not that every frame
    carrying it writes it: <strokeColor> and <labels_on> are written only by
    the vertex-shape encoding, so a circle, a route or a bullseye carrying them
    lost them without a word. That was masked while any unknown sibling sent
    the whole event to zlib'd XML, and the generic path removed the mask --
    the 2026-10-14 comparison caught a bullseye losing all three. Each tag
    below is written only when its owner is present; everything else in
    KNOWN_DETAIL is written unconditionally by the body."""
    owners = {"__chat": chat, "shape": circle,
              "link_attr": route, "__routeinfo": route}
    for tag in SHAPE_STYLE_TAGS + ("__shapeExtras", "labels_on"):
        owners[tag] = shape
    extra = []
    for tag, elements in children._all.items():
        if tag == "link":
            # A vertex list or route takes every one; a marker or chat its one.
            taken = len(elements) if shape is not None or route is not None else 1
            extra.extend(elements[taken:])
        elif tag in owners and owners[tag] is None:
            extra.extend(elements)
    if not extra:
        return generic
    order = {id(el): i for i, el in enumerate(root.find("detail"))}
    return sorted(generic + extra, key=lambda el: order[id(el)])


# Returned by the typed attempt when its refusal was about the detail, which
# the all-generic attempt can still carry; a refusal about the event itself
# (no point, bad time, unknown event attribute) would fail the same way twice.
_RETRY_ALL_GENERIC = object()


def _encode_compact(root, session, all_generic):
    """One attempt for encode_compact: a frame, None, or _RETRY_ALL_GENERIC.

    `all_generic` carries every detail child as a generic element and models
    none of them; `session` must then be None. Otherwise `session` is an SaTx to compress this node's own self-SA against what it
    has already told the mesh; see the session-compression block above. It is
    consulted only for events carrying <takv>, i.e. the ones describing the
    sending device itself, and it is left STAGED rather than advanced -- the
    caller commits once the frame is actually going out.
    """
    if session is not None:
        # Any staging left by a call that fell back is stale by definition.
        session.abort()
    if root.tag != "event":
        _emit("fallback", "root tag is %r, not event" % root.tag)
        return None

    unknown_attrs = set(root.attrib) - {
        "version", "uid", "type", "time", "start", "stale", "how", "access"
    } - set(DROP_EVENT_ATTRS)
    if unknown_attrs:
        _emit("fallback", "unknown event attributes: %s" % sorted(unknown_attrs))
        return None

    uid = root.get("uid")
    cot_type = root.get("type")
    if not uid or not cot_type:
        _emit("fallback", "missing uid or type")
        return None

    points = root.findall("point")
    if len(points) != 1:
        _emit("fallback", "expected exactly one <point>, found %d" % len(points))
        return None
    point = points[0]

    lat = _as_float(point.get("lat"))
    lon = _as_float(point.get("lon"))
    if lat is None or lon is None:
        # Never substitute 0.0 -- a marker at null island is far worse than a
        # slightly larger packet.
        _emit("fallback", "missing or unparseable lat/lon")
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        _emit("fallback", "lat/lon out of range")
        return None

    split = _detail_children(root, all_generic)
    if split is None:
        _emit("fallback", "unhandled <detail> child: %s"
              % getattr(_rejected_detail, "reason", "unknown"))
        return None
    children, generic = split

    try:
        ts = _parse_ts(root.get("time"))
        stale = _parse_ts(root.get("stale")) if root.get("stale") else ts + DEFAULT_STALE_S
        start = _parse_ts(root.get("start")) if root.get("start") else None
    except CotEncodeError as e:
        _emit("fallback", str(e))
        return None
    if not 0 <= ts <= 0xFFFFFFFF:
        _emit("fallback", "time out of uint32 range")
        return None

    how = root.get("how") or ""

    flags = 0
    body = bytearray()

    lat_i, lon_i = int(round(lat * 1e7)), int(round(lon * 1e7))
    stale_d = stale - ts
    contact = children.get("contact")
    callsign = contact.get("callsign") if contact is not None else None

    try:
        # <takv> describes the TAK application that produced the event, so it
        # marks the events a device emits about ITSELF -- the only ones whose
        # uid and callsign are the sender's own and therefore the only ones
        # worth binding. A marker's uid belongs to the marker.
        sa_ext, gen, deltas = 0, None, None
        if session is not None and children.get("takv") is not None:
            sa_ext, gen, deltas = _sa_plan(
                session, uid, callsign, ts, stale_d, lat_i, lon_i)

        # Decided before the head is built, because a chat rebuilds its uid
        # from the block below and must not also carry it literally.
        chat = None
        if children.get("__chat") is not None:
            chat = _chat_parts(root, children, uid, ts)
            if chat is None:
                raise CotEncodeError("<__chat> is not a shape we can rebuild")

        # A repeated <link> is a vertex list, which is a different element from
        # the single parent <link> a marker carries. Decided here so the marker
        # path below can assume there is at most one of its own kind.
        circle = None
        if children.get("shape") is not None:
            circle = _circle_parts(children, uid)
            if circle is None:
                raise CotEncodeError("<shape> is not a circle we can rebuild")

        # A route is dispatched on its own furniture rather than on the link
        # count: its waypoints look like a shape's vertices until one of them
        # turns out to be a marker.
        route = None
        if (children.get("link_attr") is not None
                or children.get("__routeinfo") is not None):
            route = _route_parts(children, ts)
            if route is None:
                raise CotEncodeError("<link_attr>/<__routeinfo> is not a route "
                                     "we can rebuild")

        shape = None
        if route is None and len(children.all("link")) > 1:
            shape = _shape_parts(children)
            if shape is None:
                raise CotEncodeError(
                    "repeated <link> is not a vertex list we can rebuild")

        generic = _with_unconsumed(root, generic, children, chat, shape,
                                   circle, route)

        # --- mandatory, in wire order ---
        head = bytearray()
        if sa_ext:
            head += bytes([gen])
        head += _table_byte(TYPE_TABLE, cot_type, "type_escape")
        if deltas is not None:
            # time, lat and lon as offsets from the base named by `gen`; the
            # stale offset is inherited from it, which is why a change to
            # staleness is one of the things that ends a delta run.
            head += deltas
        else:
            head += struct.pack(">I", ts & _TIME_MASK)[1:]
            # stale is unconditional on purpose: if it were flagged, a clear
            # flag would have to mean stale == time, i.e. already expired, and
            # the receiving ATAK would drop the track the instant it arrived.
            head += _svarint(stale_d)
            head += struct.pack(">ii", lat_i, lon_i)
        if not sa_ext & X_BOUND and chat is None:
            head += _uidstr(uid)
        head += _table_byte(HOW_TABLE, how, "how_escape")

        # --- optional, ascending flag order ---
        if start is not None and start != ts:
            flags |= F_START
            body += _svarint(start - ts)

        hae = _as_float(point.get("hae"))
        if hae is not None and abs(hae) < _UNKNOWN_THRESHOLD:
            flags |= F_HAE
            body += _svarint(int(round(hae)))

        for attr, flag in (("ce", F_CE), ("le", F_LE)):
            value = _as_float(point.get(attr))
            if value is not None and 0 <= value < _MAX_ERROR_M:
                flags |= flag
                body += _uvarint(int(round(value)))

        if callsign and not sa_ext & X_BOUND:
            flags |= F_CALLSIGN
            icon_el = children.get("usericon")
            derived = _derive_callsign(
                callsign, icon_el.get("iconsetpath") if icon_el is not None else None,
                children.get("creator"))
            body += derived if derived is not None else _lstr(callsign)

        group = children.get("__group")
        if group is not None:
            flags |= F_GROUP
            body += _nibble_pair(
                TEAM_TABLE, group.get("name"), "team_escape",
                ROLE_TABLE, group.get("role"), "role_escape",
            )

        status = children.get("status")
        if status is not None:
            # Guarded rather than ignored. Every other known child still drops
            # attributes it does not model, which is a real gap -- but <status>
            # is the one that reaches the compact path on every marker, so an
            # unmodelled attribute here would vanish from traffic that used to
            # carry it verbatim on the zlib path.
            unknown = set(status.attrib) - {"battery", "readiness"}
            if unknown:
                raise CotEncodeError("<status> has unknown attributes: %s"
                                     % sorted(unknown))
            battery = _as_float(status.get("battery"))
            if battery is not None:
                flags |= F_BATTERY
                body += bytes([max(0, min(100, int(round(battery))))])
            # readiness is written further down, with the other 0x1000+ flags,
            # to keep the body in ascending flag order.

        track = children.get("track")
        if track is not None:
            course = _as_float(track.get("course")) or 0.0
            speed = _as_float(track.get("speed")) or 0.0
            flags |= F_TRACK
            body += struct.pack(
                ">HH",
                max(0, min(36000, int(round(course * 100)))),
                max(0, min(65535, int(round(speed * 100)))),
            )

        ploc = children.get("precisionlocation")
        if ploc is not None:
            flags |= F_PRECISIONLOC
            body += _nibble_pair(
                ALTSRC_TABLE, ploc.get("altsrc"), "altsrc_escape",
                GEOPOINTSRC_TABLE, ploc.get("geopointsrc"), "geopointsrc_escape",
            )

        uid_el = children.get("uid")
        if uid_el is not None:
            # Zero-byte flag meaning "emit <uid Droid="{callsign}"/>". Any
            # other shape is something we can't reconstruct.
            if list(uid_el.attrib) != ["Droid"] or uid_el.get("Droid") != callsign:
                raise CotEncodeError("<uid> is not the Droid=callsign shape")
            flags |= F_UID_DROID

        takv = children.get("takv")
        if takv is not None and INCLUDE_TAKV:
            flags |= F_TAKV
            body += _table_byte(PLATFORM_TABLE, takv.get("platform") or "", "platform_escape")
            os_value = _as_float(takv.get("os"))
            body += _uvarint(max(0, int(os_value)) if os_value is not None else 0)
            body += _lstr(takv.get("version") or "")
            body += _lstr(takv.get("device") or "")

        color = children.get("color")
        if color is not None:
            for index, attr in enumerate(COLOR_ATTRS):
                raw = color.get(attr)
                if raw is not None:
                    break
            else:
                raise CotEncodeError("<color> has neither argb nor value")
            try:
                argb = int(raw)
            except (TypeError, ValueError):
                raise CotEncodeError("<color> value %r is not an integer" % raw)
            if not -2147483648 <= argb <= 4294967295:
                raise CotEncodeError("<color> value %r out of range" % raw)
            flags |= F_COLOR
            # Signed on the wire because ATAK writes Java ints, so the common
            # opaque white is "-1" rather than "4294967295".
            body += bytes([index]) + struct.pack(">i", argb - (1 << 32)
                                                 if argb > 2147483647 else argb)

        creator = children.get("creator")
        if creator is not None:
            unknown = set(creator.attrib) - {"uid", "type", "callsign", "time"}
            if unknown:
                raise CotEncodeError("<creator> has unknown attributes: %s"
                                     % sorted(unknown))
            subflags = 0
            fields = bytearray()
            if creator.get("uid"):
                subflags |= C_UID
                fields += _uidstr(creator.get("uid"))
            if creator.get("type"):
                subflags |= C_TYPE
                fields += _table_byte(TYPE_TABLE, creator.get("type"),
                                      "creator_type_escape")
            if creator.get("callsign"):
                subflags |= C_CALLSIGN
                fields += _lstr(creator.get("callsign"))
            if creator.get("time"):
                subflags |= C_TIME
                # Relative to the event's own time: a marker is almost always
                # sent the moment it is placed, so this is usually one byte
                # instead of the twenty-four an ISO timestamp costs.
                fields += _svarint(_parse_ts(creator.get("time")) - ts)
            flags |= F_CREATOR
            body += bytes([subflags]) + fields

        if status is not None and status.get("readiness") is not None:
            flags |= F_READINESS
            body += bytes([1 if status.get("readiness") == "true" else 0])

        # --- second flags word, ascending, still ahead of F_ACCESS ---
        ext = sa_ext
        remarks = children.get("remarks")
        if chat is not None:
            # Rebuilt from the chat block, along with <link> below.
            remarks = None
        if remarks is not None:
            if len(remarks) or remarks.attrib:
                raise CotEncodeError("<remarks> has children or attributes")
            ext |= X_REMARKS
            body += _lstr(remarks.text or "")

        usericon = children.get("usericon")
        if usericon is not None:
            unknown = set(usericon.attrib) - {"iconsetpath"}
            if unknown:
                raise CotEncodeError("<usericon> has unknown attributes: %s"
                                     % sorted(unknown))
            ext |= X_USERICON
            body += _encode_icon(usericon.get("iconsetpath") or "", cot_type,
                                 _color_text(children.get("color")))

        link = (children.get("link")
                if (chat is None and shape is None and route is None) else None)
        if link is not None:
            unknown = set(link.attrib) - set(LINK_ATTRS)
            if unknown:
                # A URL link (url/mime/remarks) rather than a parent link.
                # Falling back keeps the attachment's address rather than
                # silently dropping it.
                raise CotEncodeError("<link> has unknown attributes: %s"
                                     % sorted(unknown))
            subflags = 0
            fields = bytearray()
            echoes_creator = creator is not None and all((
                link.get("uid") == creator.get("uid"),
                link.get("type") == creator.get("type"),
                link.get("parent_callsign") == creator.get("callsign"),
                link.get("production_time") == creator.get("time"),
                # All four have to be present as well as equal: two absent
                # fields are equal too, and reconstructing them from the
                # creator would invent attributes ATAK never sent.
                link.get("uid") is not None,
            ))
            if echoes_creator:
                subflags |= L_LIKE_CREATOR
            else:
                if link.get("uid"):
                    subflags |= L_UID
                    fields += _uidstr(link.get("uid"))
                if link.get("type"):
                    subflags |= L_TYPE
                    fields += _table_byte(TYPE_TABLE, link.get("type"),
                                          "link_type_escape")
                if link.get("production_time"):
                    subflags |= L_PRODUCTION_TIME
                    fields += _svarint(_parse_ts(link.get("production_time")) - ts)
                if link.get("parent_callsign"):
                    subflags |= L_PARENT_CALLSIGN
                    fields += _lstr(link.get("parent_callsign"))
            if link.get("relation"):
                subflags |= L_RELATION
                fields += _lstr(link.get("relation"))
            ext |= X_LINK
            body += bytes([subflags]) + fields

        if chat is not None:
            ext |= X_CHAT
            body += _encode_chat(chat)

        if shape is not None:
            ext |= X_SHAPE
            body += _encode_shape(shape, lat_i, lon_i)

        if circle is not None:
            ext |= X_CIRCLE
            body += _encode_circle(circle)

        if route is not None:
            ext |= X_ROUTE
            body += _encode_route(route, lat_i, lon_i)

        if ext:
            flags |= F_EXT

        # Must stay last -- see the note on F_ACCESS. Only the generic block
        # follows it, and only in a T_COMPACT_X frame, which a decoder that
        # does not know the block refuses by type.
        access = root.get("access")
        if access is not None:
            flags |= F_ACCESS
            body += _table_byte(ACCESS_TABLE, access, "access_escape")

        if generic:
            body += _encode_generic_block(generic, {"uid": uid, "ts": ts})
    except CotEncodeError as e:
        if session is not None:
            session.abort()
        if not all_generic and not isinstance(e, _GenericRefused):
            # A typed encoding refused something in the detail. The event
            # head was fine, so the detail can still go as generic elements.
            _emit("generic_detail", str(e))
            return _RETRY_ALL_GENERIC
        _emit("fallback", str(e))
        return None

    if generic:
        _emit("generic", _describe_generic(generic, children))
    head_bytes = struct.pack(">H", flags)
    if flags & F_EXT:
        head_bytes += _uvarint(ext)
    ctrl = CTRL_COMPACT_X if generic else CTRL_COMPACT
    return bytes([ctrl]) + head_bytes + bytes(head) + bytes(body)


def _stale_at(ts, stale_d, min_stale_s, now=None):
    """When the receiving ATAK should stop believing this event.

    ATAK drops a marker the moment `stale` passes, so whatever the sender put
    there sets how often it must transmit merely to stay on the map -- roughly
    two minutes by default, which is what forced a position report every
    minute out of a node that had not moved an inch. That is the single largest
    line in this link's airtime budget and it buys nothing but a timer reset.

    So the RECEIVER extends it. `min_stale_s` is how long this node is willing
    to keep believing a peer between check-ins, and it belongs here rather than
    on the wire for three reasons: it costs no bytes, it needs no agreement
    between the two ends, and the receiver is the side that actually knows
    whether it can still hear the sender. The bridge withdraws the marker
    itself when its own peer table says the node has gone quiet -- which is a
    better liveness signal than a timestamp chosen a minute earlier by
    somebody who could not know they were about to go off the air.

    Never SHORTENS a stale time. A sender that deliberately set a long one --
    an alert meant to persist -- keeps it.
    """
    natural = ts + stale_d
    if not min_stale_s:
        return natural
    if now is None:
        now = int(time.time())
    return max(natural, now + int(min_stale_s))


def decode_compact(body, session=None, min_stale_s=0, generic=False, now=None):
    """`body` excludes the control byte.

    `generic` says a block of generic elements follows the compact fields, as
    in a T_COMPACT_X frame; it must then end the frame exactly.

    `session` is the SaRx for the origin this frame was authenticated as. It
    is required to read a frame that refers to a binding or a delta base;
    without one, such a frame is refused rather than guessed at.

    `min_stale_s` extends how long the receiving ATAK keeps the marker -- see
    _stale_at. Zero leaves the sender's own value alone.

    `now` is the receiver's clock, seconds since the epoch. A short timestamp
    is rebuilt relative to it (see _time_from_low); it defaults to the wall
    clock and is injectable so tests are exact rather than dependent on the
    day they run.
    """
    if len(body) < 2:
        raise CotDecodeError("compact frame too short")
    (flags,) = struct.unpack(">H", body[:2])
    off = 2
    ext = 0
    if flags & F_EXT:
        ext, off = _read_uvarint(body, off)

    gen = None
    if ext & (X_BIND | X_BOUND):
        if off >= len(body):
            raise CotDecodeError("truncated session generation")
        gen = body[off]
        off += 1

    cot_type, off = _read_table_byte(TYPE_TABLE, body, off)

    if ext & X_DELTA:
        if session is None:
            raise CotDecodeError("delta frame from an unknown sender")
        if session.gen != gen or session.ts is None:
            raise CotDecodeError(
                "delta frame references generation %s, last base is %s"
                % (gen, session.gen))
        d_ts, off = _read_svarint(body, off)
        if not 0 <= d_ts <= _MAX_DELTA_S:
            # The generation byte wraps; a base old enough for that to matter
            # is not a base worth applying an offset to.
            raise CotDecodeError("delta of %ds is beyond the base's life" % d_ts)
        d_lat, off = _read_svarint(body, off)
        d_lon, off = _read_svarint(body, off)
        ts = session.ts + d_ts
        stale_d = session.stale_d
        lat_i, lon_i = session.lat_i + d_lat, session.lon_i + d_lon
    else:
        if off + 3 > len(body):
            raise CotDecodeError("truncated time")
        (low,) = struct.unpack(">I", b"\x00" + body[off:off + 3])
        ts = _time_from_low(low, now)
        off += 3
        stale_d, off = _read_svarint(body, off)
        if off + 8 > len(body):
            raise CotDecodeError("truncated lat/lon")
        lat_i, lon_i = struct.unpack(">ii", body[off:off + 8])
        off += 8

    if ext & X_CHAT:
        # Rebuilt from the chat block, which is read further down -- the uid is
        # three of its fields joined back together.
        uid = None
    elif ext & X_BOUND:
        if session is None or session.uid is None:
            # No binding, so no uid, and a CoT event without one is not
            # something ATAK can render or this codec will invent. The sender
            # rebinds on its next keyframe.
            raise CotDecodeError("bound frame from a sender that never bound")
        uid = session.uid
    else:
        uid, off = _read_uidstr(body, off)
    how, off = _read_table_byte(HOW_TABLE, body, off)

    start = ts
    if flags & F_START:
        delta, off = _read_svarint(body, off)
        start = ts + delta

    hae = None
    if flags & F_HAE:
        hae, off = _read_svarint(body, off)
    ce = None
    if flags & F_CE:
        ce, off = _read_uvarint(body, off)
    le = None
    if flags & F_LE:
        le, off = _read_uvarint(body, off)

    callsign = None
    if ext & X_BOUND:
        callsign = session.callsign
    elif flags & F_CALLSIGN:
        if off < len(body) and body[off] == 0:
            callsign, off = _read_derived_callsign(body, off)
        else:
            callsign, off = _read_lstr(body, off)

    team = role = None
    if flags & F_GROUP:
        team, role, off = _read_nibble_pair(TEAM_TABLE, ROLE_TABLE, body, off)

    battery = None
    if flags & F_BATTERY:
        if off >= len(body):
            raise CotDecodeError("truncated battery")
        battery = body[off]
        off += 1

    course = speed = None
    if flags & F_TRACK:
        if off + 4 > len(body):
            raise CotDecodeError("truncated track")
        course_i, speed_i = struct.unpack(">HH", body[off:off + 4])
        off += 4
        course, speed = course_i / 100.0, speed_i / 100.0

    altsrc = geopointsrc = None
    if flags & F_PRECISIONLOC:
        altsrc, geopointsrc, off = _read_nibble_pair(
            ALTSRC_TABLE, GEOPOINTSRC_TABLE, body, off
        )

    takv = None
    if flags & F_TAKV:
        platform, off = _read_table_byte(PLATFORM_TABLE, body, off)
        os_value, off = _read_uvarint(body, off)
        version, off = _read_lstr(body, off)
        device, off = _read_lstr(body, off)
        takv = (platform, os_value, version, device)

    color = None
    if flags & F_COLOR:
        if off + 5 > len(body):
            raise CotDecodeError("truncated color")
        attr_index = body[off]
        if attr_index >= len(COLOR_ATTRS):
            raise CotDecodeError("unknown color attribute %d" % attr_index)
        (argb,) = struct.unpack(">i", body[off + 1:off + 5])
        off += 5
        color = (COLOR_ATTRS[attr_index], argb)

    creator = None
    if flags & F_CREATOR:
        if off >= len(body):
            raise CotDecodeError("truncated creator")
        subflags = body[off]
        off += 1
        creator = {}
        if subflags & C_UID:
            creator["uid"], off = _read_uidstr(body, off)
        if subflags & C_TYPE:
            creator["type"], off = _read_table_byte(TYPE_TABLE, body, off)
        if subflags & C_CALLSIGN:
            creator["callsign"], off = _read_lstr(body, off)
        if subflags & C_TIME:
            delta, off = _read_svarint(body, off)
            creator["time"] = _format_ts(ts + delta)

    readiness = None
    if flags & F_READINESS:
        if off >= len(body):
            raise CotDecodeError("truncated readiness")
        readiness = "true" if body[off] else "false"
        off += 1

    remarks = None
    if ext & X_REMARKS:
        remarks, off = _read_lstr(body, off)

    usericon = None
    if ext & X_USERICON:
        usericon, off = _read_icon(
            body, off, cot_type, str(color[1]) if color is not None else None)
    if isinstance(callsign, tuple):
        callsign = _resolve_callsign(callsign, usericon, creator)

    link = None
    if ext & X_LINK:
        if off >= len(body):
            raise CotDecodeError("truncated link")
        subflags = body[off]
        off += 1
        link = {}
        if subflags & L_LIKE_CREATOR:
            if creator is None:
                raise CotDecodeError("link references a creator that is absent")
            link["uid"] = creator.get("uid")
            link["type"] = creator.get("type")
            link["parent_callsign"] = creator.get("callsign")
            link["production_time"] = creator.get("time")
        else:
            if subflags & L_UID:
                link["uid"], off = _read_uidstr(body, off)
            if subflags & L_TYPE:
                link["type"], off = _read_table_byte(TYPE_TABLE, body, off)
            if subflags & L_PRODUCTION_TIME:
                delta, off = _read_svarint(body, off)
                link["production_time"] = _format_ts(ts + delta)
            if subflags & L_PARENT_CALLSIGN:
                link["parent_callsign"], off = _read_lstr(body, off)
        if subflags & L_RELATION:
            link["relation"], off = _read_lstr(body, off)

    chat = None
    if ext & X_CHAT:
        chat, off = _decode_chat(body, off, ts)
        uid = chat["uid"]

    shape = None
    if ext & X_SHAPE:
        shape, off = _decode_shape(body, off, lat_i, lon_i)

    circle = None
    if ext & X_CIRCLE:
        circle, off = _decode_circle(body, off)

    route = None
    if ext & X_ROUTE:
        route, off = _decode_route(body, off, lat_i, lon_i, uid)

    access = None
    if flags & F_ACCESS:
        access, off = _read_table_byte(ACCESS_TABLE, body, off)

    # Read before the session moves, for the reason given just below. The
    # elements land in the detail after everything the typed fields rebuild.
    extra = ET.Element("detail")
    if generic:
        off = _read_generic_block(body, off, {"uid": uid, "ts": ts, "count": 0},
                                  extra)
        if off != len(body):
            raise CotDecodeError("%d bytes after the generic elements"
                                 % (len(body) - off))

    # Only now, with the whole frame parsed: a session advanced by a frame that
    # turned out to be truncated would carry the damage into every delta that
    # follows it.
    if session is not None:
        if ext & X_BIND:
            session.bind(uid, callsign)
        if ext & (X_BIND | X_BOUND) and not ext & X_DELTA:
            session.rebase(gen, ts, stale_d, lat_i, lon_i)

    root = ET.Element("event")
    root.set("version", "2.0")
    root.set("uid", uid)
    root.set("type", cot_type)
    root.set("time", _format_ts(ts))
    root.set("start", _format_ts(start))
    root.set("stale", _format_ts(_stale_at(ts, stale_d, min_stale_s, now)))
    if how:
        root.set("how", how)
    if access is not None:
        root.set("access", access)

    point = ET.SubElement(root, "point")
    point.set("lat", "%.7f" % (lat_i / 1e7))
    point.set("lon", "%.7f" % (lon_i / 1e7))
    point.set("hae", UNKNOWN_SENTINEL if hae is None else "%.1f" % hae)
    point.set("ce", UNKNOWN_SENTINEL if ce is None else "%.1f" % ce)
    point.set("le", UNKNOWN_SENTINEL if le is None else "%.1f" % le)

    detail = ET.Element("detail")
    if takv is not None:
        el = ET.SubElement(detail, "takv")
        el.set("os", str(takv[1]))
        el.set("version", takv[2])
        el.set("device", takv[3])
        el.set("platform", takv[0])
    if callsign is not None:
        el = ET.SubElement(detail, "contact")
        el.set("endpoint", DEFAULT_ENDPOINT)
        el.set("callsign", callsign)
    if flags & F_UID_DROID:
        ET.SubElement(detail, "uid").set("Droid", callsign or "")
    if altsrc is not None or geopointsrc is not None:
        el = ET.SubElement(detail, "precisionlocation")
        if altsrc is not None:
            el.set("altsrc", altsrc)
        if geopointsrc is not None:
            el.set("geopointsrc", geopointsrc)
    if team is not None or role is not None:
        el = ET.SubElement(detail, "__group")
        if role is not None:
            el.set("role", role)
        if team is not None:
            el.set("name", team)
    if battery is not None or readiness is not None:
        el = ET.SubElement(detail, "status")
        if battery is not None:
            el.set("battery", str(battery))
        if readiness is not None:
            el.set("readiness", readiness)
    if course is not None:
        el = ET.SubElement(detail, "track")
        el.set("course", "%.2f" % course)
        el.set("speed", "%.2f" % speed)
    if color is not None:
        ET.SubElement(detail, "color").set(color[0], str(color[1]))
    if creator is not None:
        el = ET.SubElement(detail, "creator")
        # Written in ATAK's own attribute order so a diff against a
        # locally-generated event is readable.
        for key in ("uid", "type", "callsign", "time"):
            if key in creator:
                el.set(key, creator[key])
    if remarks is not None:
        ET.SubElement(detail, "remarks").text = remarks or None
    if usericon is not None:
        ET.SubElement(detail, "usericon").set("iconsetpath", usericon)
    if link is not None:
        el = ET.SubElement(detail, "link")
        for key in LINK_ATTRS:
            if link.get(key) is not None:
                el.set(key, link[key])
    if chat is not None:
        _build_chat_detail(detail, chat, ts)
    if shape is not None:
        _build_shape_detail(detail, shape)
    if circle is not None:
        _build_circle_detail(detail, circle, uid)
    if route is not None:
        _build_route_detail(detail, route)
    detail.extend(list(extra))
    if len(detail):
        root.append(detail)

    # encoding="unicode" specifically: encoding="utf-8" would prepend an XML
    # declaration into ATAK's socket, and the bare default is us-ascii, which
    # escapes non-ASCII callsigns into numeric character references.
    return ET.tostring(root, encoding="unicode").encode("utf-8")


# ---------------------------------------------------------------------------
# zlib fallback
# ---------------------------------------------------------------------------

# Common CoT boilerplate, most-frequent strings placed LAST because deflate
# favours nearer back-references. Worth ~2.3x on the fallback path: it takes a
# typical GeoChat message from 468 bytes (over the 464 budget, so it would have
# to be fragmented into two unacknowledged packets) down to ~284, i.e. one
# packet.
#
# This is part of the wire format. zlib embeds the dictionary's Adler-32 in the
# stream header, so a mismatch between two builds fails loudly at decompress()
# rather than silently corrupting -- but CHANGING IT REQUIRES BUMPING
# PROTOCOL_VERSION.
COT_ZDICT = (
    b'<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
    b'<__group role="Team Member" name="'
    b'<remarks source="BAO.F.ATAK." to="All Chat Rooms" time="'
    b'<__chat parent="RootContactGroup" groupOwner="false" messageId="" '
    b'chatroom="All Chat Rooms" id="All Chat Rooms" senderCallsign="'
    b'<chatgrp uid0="" uid1="All Chat Rooms" id="All Chat Rooms"/>'
    b'<link uid="" type="a-f-G-U-C" relation="p-p"/>'
    b'<__serverdestination destinations="'
    b'<strokeColor value=""/><fillColor value=""/><strokeWeight value=""/>'
    b'<usericon iconsetpath=""/><archive/><_flow-tags_ '
    b'<takv os="" version="" device="" platform="ATAK-CIV"/>'
    b'<contact endpoint="*:-1:stcp" callsign="" phone=""/><uid Droid="'
    b'hae="" ce="9.5" le="9999999.0"/><detail>'
    b'<status battery="" readiness="true"/><track course="" speed="'
    b'<event version="2.0" uid="ANDROID-" type="a-f-G-U-C" '
    b'time="" start="" stale="" how="m-g"><point lat="" lon="'
)

_ZARGS = (9, zlib.DEFLATED, zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, zlib.Z_DEFAULT_STRATEGY)


def encode_zxml(xml_bytes):
    # Deliberately compresses the bytes as received rather than re-serializing
    # through ElementTree: the entire point of this path is faithful
    # passthrough of events the compact codec doesn't understand, and a
    # round-trip through ET risks quietly altering namespaces or escaping.
    c = zlib.compressobj(*_ZARGS, COT_ZDICT)
    return bytes([CTRL_ZXML]) + c.compress(xml_bytes) + c.flush()


def decode_zxml(body):
    try:
        d = zlib.decompressobj(zlib.MAX_WBITS, COT_ZDICT)
        out = d.decompress(body, MAX_REASSEMBLED)
        # decompress(max_length) parks the overflow in unconsumed_tail rather
        # than raising, and flush() would hand it all back -- so the cap only
        # means anything if we check for the tail and refuse.
        if d.unconsumed_tail:
            raise CotDecodeError("decompressed payload exceeds %d bytes" % MAX_REASSEMBLED)
        return out + d.flush()
    except zlib.error as e:
        raise CotDecodeError("zlib: %s" % e)


# ---------------------------------------------------------------------------
# Squeezing the compact path
#
# The structured encoding above removes the things a schema can remove: tag
# names, repeated identity, type strings. What it cannot remove is redundancy
# ACROSS frames -- that every marker in the world contains a similar-looking
# uid, a similar iconset path, a similar creator block. A dictionary trained on
# real frame shapes takes those, and it is where the remaining factor of two
# lives.
#
# Measured against a corpus of synthetic-but-real-shaped frames, mean bytes:
#
#     position   31 -> 21   (1.5x)
#     marker    167 -> 78   (2.1x)
#     chat      122 -> 61   (2.0x)
#
# Two things to know about those numbers. They are an upper bound -- the corpus
# is variations of four fixtures, and real traffic is more varied, so retraining
# on captured frames is worth doing. And WITHOUT a dictionary this does not work
# at all: plain zlib makes a compact frame BIGGER (61 -> 70 on a position),
# because the frames are already dense and all that is left to add is a stream
# header. The dictionary is the whole mechanism, not an optimisation of it.
#
# zlib rather than zstd, which is what the prior art uses, for two reasons: it
# is in the standard library, so there is no question of whether a native wheel
# exists for Chaquopy's arm64 Python; and measured on this corpus it wins on
# chat (61 B against zstd's 69) and loses only modestly on markers (78 against
# 66). A native dependency is not worth 15% on one frame type.
# ---------------------------------------------------------------------------

# The dictionary is 16 KB, imported at the top. Enough of the gain to not care
# about the rest: 4 KB gives 78 B markers, 16 KB gives 75, and spending the
# whole 32 KB window gives 74.5.
#
# Never emit a compressed frame that is not actually smaller. This is what
# makes a badly-suited dictionary cost nothing but the attempt, and it is why
# positions -- which gain least -- need no special-casing anywhere.
_ZCOMPACT_MIN_SAVING = 1


def _squeeze(frame):
    """A compact frame -> a T_ZCOMPACT frame, or the original if that is no
    smaller. `frame` includes its control byte; the compressed form replaces
    it, so the body is the compact frame's own bytes from index 1."""
    try:
        z = zlib.compressobj(*_ZARGS, COMPACT_ZDICT)
        body = z.compress(frame[1:]) + z.flush()
    except zlib.error:
        return frame
    if len(body) + 1 + _ZCOMPACT_MIN_SAVING > len(frame):
        return frame
    ctrl = CTRL_ZCOMPACT_X if frame[0] == CTRL_COMPACT_X else CTRL_ZCOMPACT
    return bytes([ctrl]) + body


def _unsqueeze(body):
    """A T_ZCOMPACT body -> the compact frame's body."""
    try:
        d = zlib.decompressobj(zlib.MAX_WBITS, COMPACT_ZDICT)
        out = d.decompress(body, MAX_REASSEMBLED)
        if d.unconsumed_tail:
            raise CotDecodeError("compact frame exceeds %d bytes" % MAX_REASSEMBLED)
        return out + d.flush()
    except zlib.error as e:
        # A build whose dictionary differs lands here rather than decoding
        # garbage: zlib carries the dictionary's Adler-32 in the stream header.
        raise CotDecodeError("compact zlib: %s" % e)


_XML_DECL_RE = re.compile(br"^\s*<\?xml[^>]*\?>\s*")


def _strip_declaration(raw):
    return _XML_DECL_RE.sub(b"", raw).strip()


def _parse_event(raw):
    """Returns (root, cleaned_bytes).

    Kotlin's pumpAtakToPython splits ATAK's stream on </event> and hands us
    everything from the start of its pending buffer -- so a non-event frame
    with no </event> in it (ATAK's <TakControl> protocol negotiation, say)
    stays in that buffer and gets glued onto the front of the next real event,
    which ElementTree rejects as "junk after document element". The fast path
    costs nothing; the recovery only runs on already-broken input.

    Returning the cleaned bytes matters: the zlib fallback compresses what it
    is given, so handing it the original would ship the junk prefix over the
    air and hand the remote ATAK the same unparseable XML we just repaired.
    """
    raw = _strip_declaration(raw)
    if not raw:
        raise CotEncodeError("empty payload")
    try:
        return ET.fromstring(raw), raw
    except ET.ParseError:
        pass
    i = raw.rfind(b"<event")
    j = raw.rfind(b"</event>")
    if i < 0 or j < i:
        raise CotEncodeError("no parseable <event> element")
    cleaned = raw[i:j + len(b"</event>")]
    try:
        return ET.fromstring(cleaned), cleaned
    except ET.ParseError as e:
        raise CotEncodeError("unparseable event: %s" % e)


# ---------------------------------------------------------------------------
# Summary, for send-policy decisions
# ---------------------------------------------------------------------------

EventSummary = collections.namedtuple(
    "EventSummary", "uid type time lat lon callsign team role is_position is_self"
)


def _chat_uid_field(uid, index):
    """ATAK chat uids look like `GeoChat.<sender>.<destination>.<guid>`.

    The destination is the recipient's ATAK uid for a direct message and a room
    name ("All Chat Rooms") for a broadcast. Both fields are returned raw --
    deciding whether a destination names a known node is the caller's job, and
    that lookup failing is what makes a room name harmlessly indistinguishable
    from an unknown peer here.
    """
    if not uid or not uid.startswith("GeoChat."):
        return None
    parts = uid.split(".")
    if len(parts) < 4:
        return None
    return parts[index] or None


def chat_sender(uid):
    """Who sent a GeoChat, by ATAK uid, or None."""
    return _chat_uid_field(uid, 1)


def chat_addressee(uid):
    """Who a GeoChat is addressed to -- an ATAK uid for a direct message, a
    room name for a broadcast -- or None if this isn't a chat uid."""
    return _chat_uid_field(uid, 2)


def summarize(xml_bytes):
    """Pull out just the fields a send policy needs to decide whether an event
    is worth airtime. Raises CotEncodeError on input that isn't parseable CoT.

    Kept here rather than in the policy module so there is exactly one piece of
    code in the project that knows how to read a CoT event.
    """
    root, _ = _parse_event(xml_bytes)
    return summarize_root(root)


def summarize_root(root):
    cot_type = root.get("type") or ""
    point = root.find("point")
    lat = _as_float(point.get("lat")) if point is not None else None
    lon = _as_float(point.get("lon")) if point is not None else None

    try:
        ts = _parse_ts(root.get("time")) if root.get("time") else None
    except CotEncodeError:
        ts = None

    contact = root.find("detail/contact")
    group = root.find("detail/__group")

    # <takv> is the TAK application describing itself, so it is present on a
    # device's own self-SA and on nothing else it emits. That makes it the only
    # honest test for "this event is a periodic report about the sender" --
    # see is_position below for what went wrong without it.
    has_takv = root.find("detail/takv") is not None

    return EventSummary(
        uid=root.get("uid") or "",
        type=cot_type,
        time=ts,
        lat=lat,
        lon=lon,
        callsign=contact.get("callsign") if contact is not None else None,
        team=group.get("name") if group is not None else None,
        role=group.get("role") if group is not None else None,
        # A periodic position report, meaning one a later event supersedes --
        # the only traffic where throttling is safe, and the only traffic sent
        # unacknowledged. Everything else is user-initiated and one-shot, so
        # losing one loses information permanently.
        #
        # This USED to test cot_type.startswith("a-"), on the reasoning that
        # CoT "atoms" are tracked entities sending periodic reports. They are
        # not: a- is the atoms prefix, and it covers anything a user drops on
        # the map as well as the sender itself. So every user-placed marker
        # (a-u-G, and equally a-h-G / a-n-G / a-f-G) was classified as a
        # position, and inherited all three things positions get:
        #
        #   - no acknowledgement and no retry, because positions self-heal;
        #   - rate limiting against the movement throttle;
        #   - encoding into the self-SA delta stream, whose keyframe/generation
        #     machinery exists to describe ONE repeating entity.
        #
        # The 2026-08-15 range test is what surfaced it. Six markers placed,
        # all six transmitted, two lost to the air with no retry and no report;
        # one throttled as "moved 0m"; one re-sent as a position "heartbeat".
        #
        # takv is the correct test, and it was already being computed for
        # is_self. The a- check is kept because a self-SA is always an atom,
        # so the two together say exactly what is meant.
        is_position=has_takv and cot_type.startswith("a-")
        and lat is not None and lon is not None,
        # Also takv: used to learn the local callsign without accidentally
        # adopting somebody else's.
        is_self=has_takv,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def encode(xml_bytes, max_len=RNS_MAX_PAYLOAD, session=None):
    """One CoT event -> one frame. Raises CotEncodeError on input we refuse.

    Commits `session` only on the compact path: falling back to zlib means the
    frame the receiver gets carries no binding and no base, so a session that
    advanced anyway would be describing a frame that never existed.
    """
    root, cleaned = _parse_event(xml_bytes)
    frame = encode_compact(root, session)
    fallback = None
    if frame is not None:
        if len(frame) <= max_len:
            if session is not None:
                session.commit()
            # Squeezed after the length check, not before: the budget the
            # caller gave is for the compact form, and a frame that only fits
            # once compressed would be one whose delivery depended on the
            # dictionary suiting it.
            return _squeeze(frame)

        # Too big for one packet. That used to end the compact path here, on
        # the reasoning that the caller's budget was a requirement -- but a
        # frame this size is going to be fragmented whichever form it takes,
        # so the budget is not a limit any more and the only question left is
        # which form is smaller.
        #
        # Discarding it was expensive on exactly the traffic the compact
        # encoders were built for. The 2026-08-12 bench sent a real 55-point
        # ATAK route: 911 B compact, thrown away, and 2568 B of zlib'd XML sent
        # instead -- 2.8x the size and 6 packets against 3. Every route long
        # enough to fragment had been paying that, which is to say every route
        # the work on routes was aimed at.
        #
        # NOT for a frame carrying session state. A staged session means this
        # is a self-SA report binding identity or delta-coding against a base,
        # and those are fire-and-forget: no ARQ, no retry, nothing to notice a
        # loss. Fragmenting one multiplies the chance the receiver never sees
        # it while the SENDER has already advanced -- which strands every later
        # delta against a base that never arrived, the `last base is None`
        # failure the same bench spent an hour on. Routes, drawings and chat
        # stage nothing here and are retried anyway, which is why they can take
        # the win and a position cannot.
        if session is not None and session.pending is not None:
            session.abort()
            return encode_zxml(cleaned)

        squeezed = _squeeze(frame)
        fallback = encode_zxml(cleaned)
        if len(squeezed) <= len(fallback):
            if session is not None:
                session.commit()
            return squeezed
    if session is not None:
        session.abort()
    return fallback if fallback is not None else encode_zxml(cleaned)


def expire(xml):
    """The same CoT event, marked already stale.

    How a receiver tells its own ATAK to stop showing something. Used when the
    peer table decides a node has gone quiet: its marker was given a long stale
    time on arrival (see _stale_at) precisely so that the radio would not have
    to keep refreshing it, and this is what closes that loop.

    Deliberately NOT a `t-x-d-d` delete. That is what ATAK emits when a USER
    deletes an item and it removes the thing for good; a peer who walks back
    into range should reappear on their next position report rather than stay
    deleted. Expiring says "I no longer vouch for this", which is exactly true.
    """
    root = ET.fromstring(xml)
    now = int(time.time())
    # start stays put: when the event began is history and did not change.
    root.set("stale", _format_ts(now - 1))
    return ET.tostring(root, encoding="utf-8")


def decode(frame, session=None, min_stale_s=0, now=None):
    """One reassembled frame -> CoT XML. Raises CotDecodeError on anything
    it does not fully understand -- it never guesses.

    `min_stale_s` extends how long the receiving ATAK keeps what this frame
    describes -- see _stale_at.

    `now` is the receiver's clock in seconds since the epoch, threaded to
    _time_from_low and _stale_at; it defaults to the wall clock and is
    injectable so tests can pin it."""
    if not frame:
        raise CotDecodeError("empty frame")
    ctrl = frame[0]
    version = ctrl >> 4
    if version != PROTOCOL_VERSION:
        raise CotDecodeError("unsupported protocol version %d" % version)
    kind = ctrl & 0x0F
    if kind == T_COMPACT:
        return decode_compact(frame[1:], session, min_stale_s, now=now)
    if kind == T_ZCOMPACT:
        return decode_compact(_unsqueeze(frame[1:]), session, min_stale_s,
                              now=now)
    if kind == T_COMPACT_X:
        return decode_compact(frame[1:], session, min_stale_s, generic=True,
                              now=now)
    if kind == T_ZCOMPACT_X:
        return decode_compact(_unsqueeze(frame[1:]), session, min_stale_s,
                              generic=True, now=now)
    if kind == T_ZXML:
        xml = decode_zxml(frame[1:])
        if not xml.strip().startswith(b"<"):
            raise CotDecodeError("decompressed payload is not XML")
        return xml
    raise CotDecodeError("unsupported frame type 0x%x" % kind)


# ---------------------------------------------------------------------------
# Fragmentation. RNS's own Resource/Channel/Buffer would do this (with bz2
# compression, sequencing and ACKs), but they all require a Link, and
# Link.py:231 refuses any destination that is not SINGLE -- a PLAIN
# destination cannot hold an identity, so it can never be SINGLE.
# ---------------------------------------------------------------------------

Beacon = collections.namedtuple(
    "Beacon", "node_id seq callsign name digest heard position fix",
    defaults=(None,))

# A range-test fix on a beacon: where the sender was, and WHEN that fix was
# taken. See encode_beacon's `fix`. int32 lat/lon in 1e-7 degrees (~1 cm, the
# same scale ATAK and Meshtastic use) and the fix time as uint32 epoch seconds.
# 12 bytes, carried only while the sender is running a range test.
BeaconFix = collections.namedtuple("BeaconFix", "lat lon time")
_BEACON_FIX = struct.Struct(">iiI")
BEACON_FIX_LEN = _BEACON_FIX.size


def pack_beacon_fix(lat, lon, taken):
    """A fix as a beacon's `fix` field. Raises CotEncodeError for anything that
    is not a real coordinate, rather than putting a wrapped integer on the air
    that the far end would believe."""
    try:
        lat, lon, taken = float(lat), float(lon), int(taken)
    except (TypeError, ValueError):
        raise CotEncodeError("beacon fix is not numeric")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise CotEncodeError("beacon fix out of range: %r, %r" % (lat, lon))
    if not 0 <= taken <= 0xFFFFFFFF:
        raise CotEncodeError("beacon fix time out of range: %r" % taken)
    return _BEACON_FIX.pack(int(round(lat * 1e7)), int(round(lon * 1e7)), taken)


def unpack_beacon_fix(raw):
    """The inverse, or None for anything that is not exactly one fix -- an
    older or newer sender's field, or padding read as one."""
    if not raw or len(raw) != BEACON_FIX_LEN:
        return None
    lat, lon, taken = _BEACON_FIX.unpack(raw)
    lat, lon = lat / 1e7, lon / 1e7
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return BeaconFix(lat, lon, taken)

# Bytes per entry in a beacon's `heard` list. See heard_tag().
HEARD_TAG_LEN = 2


def heard_tag(origin, msg_id):
    """A 2-byte tag naming one message this node received whole.

    WHY A HASH RATHER THAN THE MESSAGE ID ITSELF, which would be free: the
    id is only unique per SENDER, so node A's message 7 and node B's
    message 7 are the same number. A sender seeing "7" reported back could
    not tell whether it was its own message that arrived or somebody
    else's, and on the FM link that mistake is expensive in one direction
    only -- a false positive stops the blind resend schedule early
    (fm_resend.py), silently abandoning a message that never arrived,
    which is precisely the failure the resends exist to prevent.

    Folding the origin in makes a collision an ordinary 1-in-65536 hash
    accident rather than a systematic ambiguity between nodes. That
    residual risk is accepted deliberately: against it, the alternative is
    never confirming delivery on this link at all, and a sender that has
    already transmitted at least once is not worse off than the
    no-feedback behaviour except in that rare case.
    """
    raw = ("%s:%d" % (origin or "", msg_id & 0xFFFF)).encode("utf-8")
    return hashlib.sha256(raw).digest()[:HEARD_TAG_LEN]


def pack_heard(tags):
    """Concatenate heard tags for the wire."""
    out = b""
    for t in tags or ():
        if len(t) != HEARD_TAG_LEN:
            raise CotEncodeError("heard tag must be %d bytes" % HEARD_TAG_LEN)
        out += t
    return out


def unpack_heard(raw):
    """The inverse. Trailing bytes that do not form a whole tag are
    dropped rather than raising: this field rides a beacon that may have
    been padded or truncated, and a malformed tail is not worth losing the
    peer's identity over."""
    raw = raw or b""
    return [raw[i:i + HEARD_TAG_LEN]
            for i in range(0, len(raw) - HEARD_TAG_LEN + 1, HEARD_TAG_LEN)]


def encode_beacon(node_id, seq, size=0, callsign=None, name=None, digest=None,
                  heard=None, position=None, fix=None):
    """A link probe: who sent it, which one it is, who they are, and
    optionally padded to `size` bytes.

    `position` rides along as an opaque, already-encoded compact position
    frame (whatever decode()/decode_compact() would accept on its own) --
    2026-09-22, piggybacking the sender's most recent self-SA DELTA (never
    a keyframe; see the caller in rns_bridge.py for why) onto the beacon
    that already proves the link works. The beacon has repeatedly measured
    more reliable than the position's own separate one-shot transmission
    on this link (smaller frame, same reasoning fm_interface.py's whole
    duration-sensitivity history gives), so a delta that rides here reaches
    the far end for free on every beacon that lands, on top of whatever the
    separate heartbeat path delivers on its own. Not a replacement for that
    path -- additive, and deliberately so: this is one more independent
    chance at delivery, not the only one.

    `fix` is the sender's own GPS fix as pack_beacon_fix() bytes, carried
    ONLY while the sender runs a range test (2026-11-08). The position path
    above is no use to a range test at drive speed: it is a delta against a
    session the receiver may not hold, it rides one beacon and is cleared, and
    the self-SA it comes from is rate-limited on air to about one a minute --
    on the 2026-11-08 drive ATAK handed the bridge a fresh fix every 3-7 s
    while the far phone's copy was 300-400 m stale. A beacon IS the range
    test's sample, so the fix that annotates it should ride on it. 13 bytes,
    paid only for the length of a test; the rangetest module docstring's
    objection was to paying it on every beacon forever.

    Two names are carried, and the distinction matters:

    `name` is derived from the DEVICE (its make and model) and is the node's
    primary identity. It is available whether or not ATAK is running, which is
    the whole point -- when ATAK is the thing that's broken, you still need to
    know which physical radio you are talking to in order to confirm the link
    is fine.

    `callsign` comes from ATAK and is secondary: useful for correlating a node
    with what you see on the map, absent whenever ATAK is not running, and
    never something to depend on for identifying a node.

    Both sit after the sequence number and before any padding, appended in
    order, which keeps the format backward-compatible in the direction that
    matters: an older decoder reads node_id and seq and ignores everything
    after, so a newer sender is understood fine.

    The padding exists because a link that carries small frames is not proven
    to carry large ones -- the first successful bench run proved delivery at 31
    bytes while the CoT packets that had been failing were 264. Sweeping the
    size is what distinguishes "the radio works" from "the radio works for
    packets small enough to survive a single USB bulk transfer". It is
    high-entropy so it also exercises KISS escaping, which ASCII CoT never did.

    Note the one mixed-version wrinkle: an OLD sender padding a beacon
    produces random bytes where a new decoder looks for the callsign, so it
    reads garbage or nothing. Harmless (callsign comes back None or junk for
    one frame) and only reachable in the opt-in padded diagnostic mode, since
    the default ladder is unpadded.
    """
    # node_id is accepted and deliberately not written: the mesh envelope
    # carries an authenticated sender, and this copy could only ever be checked
    # against it. 7 bytes on the frame every node sends most often.
    head = bytes([CTRL_BEACON]) + struct.pack(">I", seq & 0xFFFFFFFF)
    head += _lstr(callsign or "")
    head += _lstr(name or "")
    # The neighbour digest (milestone 6 step 3) rides here rather than on a
    # position report, and the choice was deliberate. It is bridge metadata,
    # not anything ATAK said, and the beacon is the bridge's own frame -- the
    # same reasoning that kept the device name out of the compact codec in
    # step 2. It also means the coverage test costs nothing on the frames that
    # actually carry traffic.
    head += _lbytes(digest)
    # APPENDED, and that is what keeps it compatible: decode_beacon() reads
    # these fields in order and stops cleanly at the end of the frame, so a
    # node running an older build reads node_id/seq/callsign/name/digest
    # exactly as before and never notices this one. Only added when there
    # is something to report, so a beacon with no heard list is byte-for-
    # byte what it always was.
    # `position` is appended AFTER heard, so once it is present the heard
    # slot has to be written even when heard is itself empty -- otherwise
    # decode_beacon()'s fixed read order (which always tries heard before
    # position) would read the position bytes as if they were heard tags
    # and leave nothing behind for position to read. A beacon with neither
    # field is still byte-for-byte what it always was.
    # Same rule one field further along: `fix` follows `position`, so once a
    # fix is present both earlier slots are written, empty if need be.
    if heard or position or fix:
        head += _lbytes(pack_heard(heard))
    if position or fix:
        head += _lbytes(position)
    if fix:
        head += _lbytes(fix)
    if size > len(head):
        head += os.urandom(size - len(head))
    return head


def bundle(frames):
    """Several complete frames -> one frame. See T_BUNDLE.

    Refuses rather than truncates: a caller that hands over something
    unbundlable has made a mistake worth seeing, and silently sending a subset
    of the acks it asked for would be an invisible one.
    """
    frames = [f for f in frames if f]
    if len(frames) < 2:
        raise CotEncodeError("a bundle needs at least two frames")
    out = bytearray([CTRL_BUNDLE])
    for f in frames:
        if len(f) > BUNDLE_MAX_FRAME:
            raise CotEncodeError("frame of %d bytes is too big to bundle" % len(f))
        if is_bundle(f):
            # Nesting would make the hop-count rule unenforceable one level
            # down and buys nothing: a flat bundle holds the same frames for
            # one fewer byte.
            raise CotEncodeError("bundles do not nest")
        out.append(len(f))
        out += f
    return bytes(out)


def is_bundle(frame):
    return bool(frame) and frame[0] == CTRL_BUNDLE


def unbundle(frame):
    """One bundle -> the frames inside it, in the order they were packed."""
    if not is_bundle(frame):
        raise CotDecodeError("not a bundle frame")
    frames = []
    off = 1
    while off < len(frame):
        n = frame[off]
        off += 1
        if n == 0:
            raise CotDecodeError("bundle holds an empty frame")
        if off + n > len(frame):
            raise CotDecodeError("bundle is truncated")
        frames.append(frame[off:off + n])
        off += n
    if len(frames) < 2:
        raise CotDecodeError("bundle holds %d frame(s)" % len(frames))
    return frames


def bundle_overhead(count):
    """Bytes bundling `count` frames adds, so a caller can check it still fits."""
    return 1 + count


def is_beacon(frame):
    return bool(frame) and frame[0] == CTRL_BEACON


def decode_beacon(frame, node_id=None):
    """Returns a Beacon. Raises CotDecodeError if seq is malformed; a missing
    or unreadable callsign is tolerated as None, since that is what a padded
    frame looks like.

    `node_id` is the authenticated sender from the mesh envelope -- see
    decode_ack. A caller that does not pass it gets None."""
    if not is_beacon(frame):
        raise CotDecodeError("not a beacon frame")
    off = 1
    if off + 4 > len(frame):
        raise CotDecodeError("truncated beacon sequence")
    (seq,) = struct.unpack(">I", frame[off:off + 4])
    off += 4

    callsign = name = digest = position = fix = None
    heard = []
    try:
        value, off = _read_lstr(frame, off)
        callsign = value or None
        value, off = _read_lstr(frame, off)
        name = value or None
        value, off = _read_lbytes(frame, off)
        digest = value or None
        # Absent from every beacon an older sender builds, and from any
        # beacon with nothing to report -- so a failure to read it here is
        # the ordinary case, not an error, and lands in the same tolerant
        # except below as the fields before it.
        value, off = _read_lbytes(frame, off)
        heard = unpack_heard(value)
        # Same tolerance, same reason: absent from a beacon with no fresh
        # position to piggyback, from every beacon padded by the size
        # ladder, and from an older sender's beacon entirely.
        value, off = _read_lbytes(frame, off)
        position = value or None
        # Only on a range-test sender's beacon. Anything that is not exactly
        # one fix -- padding read as a length, say -- decodes as no fix.
        value, off = _read_lbytes(frame, off)
        fix = unpack_beacon_fix(value)
    except CotDecodeError:
        pass  # older sender, or padding where these fields would be

    return Beacon(node_id, seq, callsign, name, digest, heard, position, fix)


# ---------------------------------------------------------------------------
# Acknowledged delivery.
#
# Position reports self-heal: ATAK re-emits them every few seconds, so a lost
# one costs nothing. Chat, drawings and routes are one-shot and user-initiated
# -- there is no later event to supersede a dropped one, so a single lost
# packet loses the message permanently and tells neither user.
#
# RNS's own acknowledgement machinery is unavailable here for the same reason
# Resource/Link are: Packet.prove() needs destination.identity.prv, and a PLAIN
# destination cannot hold an identity. So acknowledgement is built at the app
# layer, on the same broadcast destination as everything else. The state
# machines that drive it live in reliable.py; this module only owns the bytes.
# ---------------------------------------------------------------------------

Reliable = collections.namedtuple(
    "Reliable", "origin msg_id frame any_ack addressee")
Ack = collections.namedtuple("Ack", "origin msg_id acker")


# Top bit of the wrapped msg_id, carrying "any one ack will do" to receivers.
# Not a spare byte in the header, because it does not need one: msg_ids are
# random and at most DEFAULT_MAX_INFLIGHT are live at once, so 15 bits is
# ample and the flag rides for free.
#
# It has to be on the wire at all because only the SENDER knows the answer. A
# marker broadcast to everyone is satisfied by any single ack, so the other
# receivers can stay quiet; a direct chat is owed an ack by one named node, and
# a bystander answering first would let the real addressee fall silent and the
# message be reported undelivered while it sat on the recipient's screen.
ANY_ACK_BIT = 0x8000
MSG_ID_MASK = 0x7FFF


def wrap_reliable(frame, origin, msg_id=None, any_ack=False, addressee=None):
    """Mark a complete frame as needing acknowledgement.

    `any_ack` tells receivers that one acknowledgement from anybody satisfies
    this message, so a receiver that hears somebody else answer first may stay
    quiet. See ANY_ACK_BIT.

    `addressee` names the single node that owes the answer, and silences
    everybody else -- see T_RELIABLE_TO. Mutually exclusive with `any_ack` by
    construction: one says any node may answer, the other says exactly one
    must, and a frame claiming both would leave receivers with no rule at all.

    Wraps *inside* fragmentation, so the receiver reassembles first and then
    acknowledges the whole message. That means a lost fragment costs a
    retransmission of everything rather than just the missing piece -- accepted,
    because selective retry needs a per-fragment bitmap and the traffic this
    protects fits in one packet anyway.

    `origin` is carried because every frame here is broadcast: an
    acknowledgement has to name whose message it answers, and msg_id alone
    collides across senders.
    """
    if not frame:
        raise CotEncodeError("cannot wrap an empty frame")
    if addressee and any_ack:
        raise CotEncodeError("a frame cannot be both addressed and any_ack")
    if msg_id is None:
        msg_id = struct.unpack(">H", os.urandom(2))[0]
    msg_id &= MSG_ID_MASK
    on_wire = msg_id | (ANY_ACK_BIT if any_ack else 0)
    if addressee:
        head = (bytes([CTRL_RELIABLE_TO]) + struct.pack(">H", on_wire)
                + _nodeid(addressee))
    else:
        head = bytes([CTRL_RELIABLE]) + struct.pack(">H", on_wire)
    return msg_id, head + frame


def reliable_overhead(origin=None, addressee=None):
    """Bytes wrap_reliable() will add, so a caller can shrink the budget it
    hands to encode() and keep a single-packet message single-packet.

    `addressee` must be passed whenever it will be passed to wrap_reliable(),
    or a message sized against the smaller header can spill into a second
    fragment -- which for one-shot traffic costs a retransmission of both.

    `origin` is accepted and ignored: it stopped going on the wire at v15.
    """
    return 3 + (NODE_ID_LEN if addressee else 0)


def is_reliable(frame):
    return bool(frame) and frame[0] in (CTRL_RELIABLE, CTRL_RELIABLE_TO)


def unwrap_reliable(frame, origin=None):
    """Returns a Reliable. `frame` is the inner frame, ready for decode().

    `origin` is the authenticated sender from the mesh envelope. It stopped
    riding inside the wrapper at v15 -- see encode_ack for why."""
    if not is_reliable(frame):
        raise CotDecodeError("not a reliable frame")
    if len(frame) < 3:
        raise CotDecodeError("truncated reliable header")
    (on_wire,) = struct.unpack(">H", frame[1:3])
    off = 3
    addressee = None
    if frame[0] == CTRL_RELIABLE_TO:
        addressee, off = _read_nodeid(frame, off)
        if not int(addressee, 16):
            # The whole point of the type is the name in it. An all-zero id is
            # the raw-bytes spelling of naming nobody, and it would silence
            # every receiver including the node that owes the answer.
            raise CotDecodeError("addressed reliable frame names nobody")
    if off >= len(frame):
        raise CotDecodeError("reliable frame carries no payload")
    return Reliable(origin, on_wire & MSG_ID_MASK, frame[off:],
                    bool(on_wire & ANY_ACK_BIT), addressee)


def encode_ack(origin, msg_id, acker=None):
    """SIX bytes, down from 17 -- about 0.15 s of airtime instead of 0.18 s.

    `origin` is whose message this answers and has to be here: acks ride a
    broadcast, and msg_id alone collides across senders.

    `acker` -- who is doing the acknowledging -- is accepted for the caller's
    convenience and DELIBERATELY NOT WRITTEN. The mesh envelope already carries
    an authenticated sender, and _reject_impostor() existed only to check that
    this copy agreed with it. A field whose sole purpose is to be checked
    against a better copy of itself is 7 bytes of nothing; the receiver reads
    the envelope instead.
    """
    return (bytes([CTRL_ACK]) + _nodeid(origin)
            + struct.pack(">H", msg_id & MSG_ID_MASK))


def is_ack(frame):
    return bool(frame) and frame[0] == CTRL_ACK


def decode_ack(frame, acker=None):
    """`acker` is the authenticated sender from the mesh envelope. It is no
    longer on the wire, so a caller that does not pass it gets None."""
    if not is_ack(frame):
        raise CotDecodeError("not an ack frame")
    origin, off = _read_nodeid(frame, 1)
    if off + 2 > len(frame):
        raise CotDecodeError("truncated ack msg_id")
    (msg_id,) = struct.unpack(">H", frame[off:off + 2])
    return Ack(origin, msg_id, acker)


Partial = collections.namedtuple("Partial", "origin msg_id acker count have")


def encode_partial(origin, msg_id, acker=None, count=0, have=()):
    """"I hold these fragments of your message; send me the rest."

    ~20 bytes against the ~0.4 s a redundant fragment costs, so it pays for
    itself the first time it prevents one being resent.

    `have` is a set of fragment indices, carried as a 16-bit mask because
    MAX_FRAGMENTS is 16 -- one bit per fragment, and the whole point is that
    this stays small enough to be worth sending speculatively.

    `count` rides along so the sender can tell a stale report from a live one.
    A receiver's view is only meaningful against the fragmentation the sender
    actually used, and if the two disagree the honest answer is to ignore the
    report and resend everything rather than trust a bitmap indexed against a
    different split.
    """
    mask = 0
    for index in have:
        if 0 <= index < MAX_FRAGMENTS:
            mask |= 1 << index
    return (bytes([CTRL_PARTIAL]) + _nodeid(origin)
            + struct.pack(">H", msg_id & 0xFFFF)
            + struct.pack(">BH", count & 0xFF, mask))


def is_partial(frame):
    return bool(frame) and frame[0] == CTRL_PARTIAL


def decode_partial(frame, acker=None):
    """`acker` comes from the mesh envelope -- see decode_ack."""
    if not is_partial(frame):
        raise CotDecodeError("not a partial-ack frame")
    origin, off = _read_nodeid(frame, 1)
    if off + 2 > len(frame):
        raise CotDecodeError("truncated partial msg_id")
    (msg_id,) = struct.unpack(">H", frame[off:off + 2])
    off += 2
    if off + 3 > len(frame):
        raise CotDecodeError("truncated partial bitmap")
    count, mask = struct.unpack(">BH", frame[off:off + 3])
    if count == 0 or count > MAX_FRAGMENTS:
        raise CotDecodeError("partial names %d fragments" % count)
    have = frozenset(i for i in range(count) if mask & (1 << i))
    return Partial(origin, msg_id, acker, count, have)


def fragment(frame, mtu=RNS_MAX_PAYLOAD, msg_id=None):
    if len(frame) <= mtu:
        return [frame]
    per = mtu - FRAG_HEADER_LEN
    if per <= 0:
        raise CotEncodeError("mtu %d too small for a fragment header" % mtu)
    n = (len(frame) + per - 1) // per
    if n > MAX_FRAGMENTS:
        # A hard refusal, not a truncation: MAX_FRAGMENTS fragments is ~20s of
        # continuous unacknowledged transmission during which the channel is
        # unusable for position reports, and the odds all of them arrive are
        # poor.
        _emit("frag_oversize", "%d bytes needs %d fragments" % (len(frame), n))
        raise CotEncodeError("message needs %d fragments (max %d)" % (n, MAX_FRAGMENTS))
    if msg_id is None:
        msg_id = struct.unpack(">H", os.urandom(2))[0]
    return [
        bytes([CTRL_FRAG]) + struct.pack(">HBB", msg_id, i, n) + frame[i * per:(i + 1) * per]
        for i in range(n)
    ]


def build_packets(xml_bytes, mtu=RNS_MAX_PAYLOAD, session=None):
    """One CoT event -> the list of RNS payloads that carry it."""
    return fragment(encode(xml_bytes, mtu, session), mtu)


def fragment_reliable(wrapped, mtu=RNS_MAX_PAYLOAD):
    """Fragment an acknowledged message under an id taken from the message
    itself, so every retransmission carries the SAME fragment identity.

    Without this a retry is a different message as far as the receiver is
    concerned: fragment() picks a random id per call, the Reassembler keys on
    (msg_id, count), and so fragments that arrived on the first attempt are
    orphaned the moment the second arrives under a new id. A five-fragment
    drawing that lost one piece had to land all five again, and the abandoned
    partial sat in one of the sixteen reassembly slots until it timed out.

    Deriving the id from the reliable wrapper costs nothing -- the id is
    already there, already unique among this sender's in-flight messages, and
    already what the acknowledgement names. It makes retries CUMULATIVE, which
    is the property a per-fragment ack bitmap needs before it can mean
    anything.

    Falls back to a random id for anything that is not a reliable wrapper,
    which is every fire-and-forget frame and is exactly the old behaviour: a
    position is never retried, so there is nothing to correlate.
    """
    msg_id = None
    if is_reliable(wrapped) and len(wrapped) >= 3:
        (msg_id,) = struct.unpack(">H", wrapped[1:3])
    return fragment(wrapped, mtu, msg_id=msg_id)


class Reassembler(object):
    """Collects fragments until a frame is whole.

    offer() runs on the RNode serial read thread, so everything here is O(1)
    and never raises -- decode() is the only thing on the RX path that does,
    which keeps the caller's error handling to a single try block.
    """

    def __init__(self, timeout=30.0, max_messages=16, clock=time.monotonic):
        self._timeout = timeout
        self._max_messages = max_messages
        self._clock = clock
        self._pending = collections.OrderedDict()
        # Messages already handed over whole, so a fragment arriving late for
        # one is recognised as spent rather than mistaken for the start of a
        # new message.
        #
        # Selective retransmission is what made this matter. A retry aimed at
        # the one peer still missing a fragment is BROADCAST, so every peer
        # that already has the message hears it too -- and without this, each
        # of them opened a fresh reassembly holding that single fragment,
        # stalled, and asked the sender for the rest of something they had
        # already delivered. Observed on the 2026-08-12 bench: a second node
        # asking for fragment 0 seven seconds after it had passed the whole
        # route to ATAK. On a two-node bench that is one wasted report; at 16
        # nodes it is fifteen, plus fifteen buffers held for the full timeout,
        # which is the airtime the feature exists to save spent somewhere else.
        self._completed = collections.OrderedDict()
        self._lock = threading.Lock()
        self._stats = {
            "completed": 0, "dropped": 0, "collisions": 0,
            "timeouts": 0, "duplicates": 0,
        }

    def stats(self):
        with self._lock:
            out = dict(self._stats)
            out["pending"] = len(self._pending)
            return out

    def set_timeout(self, timeout):
        """Re-size the buffer once the real link speed is known.

        The timeout has to outlive the sender's whole retry schedule, and that
        schedule is measured in airtime -- so it is not knowable at import,
        when the radio has not yet reported its bitrate. Constructing with an
        estimate and correcting here is what keeps the two from drifting apart
        when the radio parameters change; see the caller in rns_bridge.

        Applies to messages already part-received as well as new ones, because
        expiry is computed against this value at sweep time rather than stored
        per message.
        """
        with self._lock:
            self._timeout = float(timeout)

    def stalled(self, idle_s):
        """Messages part-received and gone quiet, as (sender, msg_id, count,
        have) -- everything encode_partial needs to ask for the rest.

        A stall is the only reliable moment to speak up. Reporting on the last
        fragment of a burst would never fire when the last fragment is the one
        lost, which is as likely as any other; reporting on a duplicate would
        only fire once the sender had already begun resending everything, which
        is the airtime this exists to save.

        Reported at most once per stall: `reported` is cleared whenever a
        fragment arrives, so a sender that is still feeding us gets no report,
        and one that has genuinely stopped gets exactly one. Without that, a
        message stuck behind a lost fragment would emit a report every tick for
        the whole reassembly timeout.

        Never raises, and takes no action of its own -- the caller decides
        whether the airtime is worth it.
        """
        out = []
        try:
            now = self._clock()
            with self._lock:
                for (msg_id, count), entry in self._pending.items():
                    if entry.get("sender") is None:
                        continue          # nothing to address the report to
                    if now - entry["last_seen"] < idle_s:
                        continue          # still arriving; let it finish
                    if entry.get("reported") is not None:
                        continue          # already asked since the last arrival
                    entry["reported"] = now
                    have = frozenset(i for i, part in enumerate(entry["parts"])
                                     if part is not None)
                    out.append((entry["sender"], msg_id, count, have))
        except Exception as e:
            _emit("frag_drop", "stalled scan: %s" % e)
        return out

    def _expire(self, now):
        # Entries are inserted in first-seen order and never reordered, so
        # walking from the head and stopping at the first live entry is
        # amortized O(1). Any O(n) scan here would stall radio reads.
        while self._pending:
            key, entry = next(iter(self._pending.items()))
            if now - entry["first_seen"] < self._timeout:
                return
            self._pending.pop(key)
            self._stats["timeouts"] += 1
            _emit("frag_timeout", "msg_id=0x%04x, had %d/%d" % (key[0], entry["have"], key[1]))

    def _expire_completed(self, now):
        """Forget completions once no retry could still be carrying a fragment
        of them. Same amortised O(1) walk as _expire: insertion-ordered, and it
        stops at the first live entry."""
        while self._completed:
            key, done_at = next(iter(self._completed.items()))
            if now - done_at < self._timeout:
                return
            self._completed.pop(key)

    def offer(self, payload, sender=None):
        """Returns a complete frame, or None. Non-fragment payloads pass
        straight through so the call site is one unbranched line.

        `sender` is the authenticated origin the fragment arrived from, kept so
        stalled() can name who to ask for the missing pieces. A relay does not
        re-wrap, so this is the ORIGINATOR at every hop -- which is exactly the
        node that still holds the fragments and can resend them.
        """
        try:
            if not payload:
                with self._lock:
                    self._stats["dropped"] += 1
                return None
            if (payload[0] >> 4) != PROTOCOL_VERSION or (payload[0] & 0x0F) != T_FRAG:
                return payload
            if len(payload) <= FRAG_HEADER_LEN:
                with self._lock:
                    self._stats["dropped"] += 1
                _emit("frag_drop", "fragment with no body")
                return None

            msg_id, index, count = struct.unpack(">HBB", payload[1:FRAG_HEADER_LEN])
            chunk = payload[FRAG_HEADER_LEN:]
            if count == 0 or index >= count or count > MAX_FRAGMENTS:
                with self._lock:
                    self._stats["dropped"] += 1
                _emit("frag_drop", "bad header index=%d count=%d" % (index, count))
                return None
            if count * len(chunk) > MAX_REASSEMBLED:
                with self._lock:
                    self._stats["dropped"] += 1
                _emit("frag_drop", "reassembled size would exceed %d" % MAX_REASSEMBLED)
                return None

            key = (msg_id, count)
            with self._lock:
                now = self._clock()
                self._expire(now)
                self._expire_completed(now)
                if key in self._completed:
                    # Already delivered whole. A late fragment is the tail of
                    # somebody else's selective retry, not the start of a new
                    # message, and opening a reassembly for it would have this
                    # node ask for pieces of something it has already read.
                    self._stats["late"] = self._stats.get("late", 0) + 1
                    return None
                entry = self._pending.get(key)
                if entry is None:
                    if len(self._pending) >= self._max_messages:
                        old_key, old = self._pending.popitem(last=False)
                        self._stats["dropped"] += 1
                        _emit("frag_drop", "evicted msg_id=0x%04x to make room" % old_key[0])
                    entry = {
                        "parts": [None] * count,
                        "have": 0,
                        "first_seen": self._clock(),
                        "last_seen": self._clock(),
                        "sender": sender,
                        "reported": None,
                    }
                    self._pending[key] = entry

                existing = entry["parts"][index]
                if existing is not None:
                    if existing == chunk:
                        # RNS legitimately delivers duplicates.
                        self._stats["duplicates"] += 1
                        return None
                    # Two senders picked the same msg_id. Losing both messages
                    # is strictly better than emitting a spliced one.
                    self._pending.pop(key, None)
                    self._stats["collisions"] += 1
                    _emit("frag_collision", "msg_id=0x%04x index=%d" % (msg_id, index))
                    return None

                entry["parts"][index] = chunk
                entry["have"] += 1
                entry["last_seen"] = self._clock()
                if sender is not None:
                    entry["sender"] = sender
                # A fragment arrived, so whatever was reported before is stale
                # and the next stall deserves a fresh report.
                entry["reported"] = None
                if entry["have"] < count:
                    return None
                self._pending.pop(key, None)
                self._completed[key] = self._clock()
                while len(self._completed) > self._max_messages * 4:
                    self._completed.popitem(last=False)
                self._stats["completed"] += 1
                return b"".join(entry["parts"])
        except Exception as e:
            _emit("frag_drop", "unexpected: %s" % e)
            return None
