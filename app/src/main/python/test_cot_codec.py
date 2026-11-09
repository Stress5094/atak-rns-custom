"""
Desktop test suite for cot_codec. No Android, no RNS, no hardware:

    python3 -m unittest discover -s app/src/main/python -p 'test_*.py' -v

This lives in src/main/python (and so ships in the APK, ~20 KB) rather than
src/test/python on purpose: it makes `import cot_codec` work with no sys.path
shim, and it means the suite can be run *on device* through Chaquopy if a
desktop-vs-device divergence is ever suspected. The device runs Python 3.8 and
development happens on something much newer, so that ability is worth the KB.

What this suite CANNOT prove is in the README's "still needs hardware
verification" list -- above all, that real ATAK accepts the decoder's output.
"""

import os
import random
import struct
import time
import unittest
import xml.etree.ElementTree as ET
import zlib

import cot_codec as codec

# The fixtures in this file carry fixed dates from July-August 2026 (PLI,
# GEOCHAT, self_sa). A short timestamp in a frame is rebuilt relative to the
# receiver's clock, so decoding them against the wall clock would silently
# re-date them once the wall clock drifts more than half a _TIME_MOD window
# (about 97 days) past them. Pin the receiver clock to the fixture era so the
# round-trip assertions are exact on any day this suite runs.
NOW = 1786000000

class FakeClock(object):
    """Injected so stall and expiry tests are exact rather than slept for."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


PLI = (
    b'<event version="2.0" uid="ANDROID-352214111111111" type="a-f-G-U-C" '
    b'time="2026-07-30T18:22:31.123Z" start="2026-07-30T18:22:31.123Z" '
    b'stale="2026-07-30T18:24:46.123Z" how="m-g">'
    b'<point lat="35.1234567" lon="-79.7654321" hae="123.45" ce="9.5" le="9999999.0"/>'
    b'<detail>'
    b'<takv os="29" version="5.1.0.0 (7d0d1b2).1738000000-CIV" device="GOOGLE PIXEL 6" platform="ATAK-CIV"/>'
    b'<contact endpoint="*:-1:stcp" callsign="BRAVO01" phone=""/>'
    b'<uid Droid="BRAVO01"/>'
    b'<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
    b'<__group role="Team Member" name="Cyan"/>'
    b'<status battery="87"/>'
    b'<track course="271.34" speed="1.2345"/>'
    b'</detail></event>'
)

# A marker with every child the 2026-08-04 bench named. Version 2 handled
# <color> and <creator> and markers STILL fell back, on the four this now
# carries -- which is why this fixture is the bench's list and not a reading of
# PlacePointTool.
MARKER = (
    b'<event version="2.0" uid="0ef9c3a9-bf54-46e5-9655-29956a0d80ba" '
    b'type="a-u-G" how="h-e" time="2026-08-04T09:55:58.000Z" '
    b'start="2026-08-04T09:55:58.000Z" stale="2026-08-04T10:55:58.000Z">'
    b'<point lat="20.9089800" lon="-140.5829100" hae="381.0" ce="9.5" le="9999999.0"/>'
    b'<detail><status readiness="true"/><archive/><color argb="-1"/>'
    b'<creator uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" callsign="pixel" '
    b'time="2026-08-04T09:55:58.000Z"/>'
    b'<contact callsign="Marker 1"/>'
    b'<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
    b'<remarks>observed movement north</remarks>'
    b'<usericon iconsetpath="COT_MAPPING_2525B/a-u/a-u-G"/>'
    b'<link uid="ANDROID-0123456789abcdef" production_time="2026-08-04T09:55:58.000Z" '
    b'type="a-f-G-U-C" parent_callsign="pixel" relation="p-p"/>'
    b'<marti><dest callsign="OnePlus"/></marti>'
    b'</detail></event>'
)

GEOCHAT = (
    b'<event version="2.0" uid="GeoChat.ANDROID-352214111111111.All Chat Rooms.'
    b'7f3a2b19-1c4d-4e2a-9f88-2b7c1d0e5a63" type="b-t-f" '
    b'time="2026-07-30T18:30:02.415Z" start="2026-07-30T18:30:02.415Z" '
    b'stale="2026-07-30T18:31:02.415Z" how="h-g-i-g-o">'
    b'<point lat="35.1234567" lon="-79.7654321" hae="123.45" ce="9.5" le="9999999.0"/>'
    b'<detail><__chat parent="RootContactGroup" groupOwner="false" '
    b'messageId="7f3a2b19-1c4d-4e2a-9f88-2b7c1d0e5a63" chatroom="All Chat Rooms" '
    b'id="All Chat Rooms" senderCallsign="BRAVO01">'
    b'<chatgrp uid0="ANDROID-352214111111111" uid1="All Chat Rooms" id="All Chat Rooms"/>'
    b'</__chat><link uid="ANDROID-352214111111111" type="a-f-G-U-C" relation="p-p"/>'
    b'<remarks source="BAO.F.ATAK.ANDROID-352214111111111" to="All Chat Rooms" '
    b'time="2026-07-30T18:30:02.415Z">moving to phase line bravo, eta 10 mikes</remarks>'
    b'<__serverdestination destinations="192.168.55.5:4242:tcp:ANDROID-352214111111111"/>'
    b'</detail></event>'
)

# A chat carrying one attribute the chat encoding cannot rebuild. It used to
# take the zlib route; since the generic elements it is carried whole as one,
# which is what the tests using it now check.
CHAT_FALLBACK = GEOCHAT.replace(b'<__chat parent=', b'<__chat spooky="yes" parent=')

# A chat-sized event that genuinely still takes the zlib route: @qos is a real
# CoT attribute deliberately not implemented, and an unknown EVENT attribute is
# one of the few things the generic elements cannot absorb. The fallback path
# still needs a chat-sized event to be tested against -- that is what sizes
# COT_ZDICT.
ZXML_CHAT = CHAT_FALLBACK.replace(b'<event version="2.0"',
                                  b'<event qos="1-r-c" version="2.0"')

SERVER_PLI = (
    b'<event version="2.0" uid="ANDROID-352214111111111" type="a-f-G-U-C" '
    b'time="2026-07-30T18:22:31.123Z" stale="2026-07-30T18:24:46.123Z" how="m-g">'
    b'<point lat="35.1234567" lon="-79.7654321" hae="123.45" ce="9.5" le="9999999.0"/>'
    b'<detail><contact endpoint="*:-1:stcp" callsign="BRAVO01"/>'
    b'<__group role="Team Member" name="Cyan"/>'
    b'<_flow-tags_ TAK-Server-a1b2="2026-07-30T18:22:31Z"/><archive/>'
    b'</detail></event>'
)

# The exact shape buildKeepaliveEvent() in CotTakServerBridgeService.kt emits.
KEEPALIVE = (
    b'<event version="2.0" uid="rns-bridge-ping" type="t-x-c-t" '
    b'time="2026-07-30T18:22:31Z" start="2026-07-30T18:22:31Z" '
    b'stale="2026-07-30T18:22:51Z" how="m-g">'
    b'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
    b'</event>'
)

PLI_UNICODE = PLI.replace(b"BRAVO01", "ÅNGSTRÖM-01".encode("utf-8"))

WAYPOINT = (
    b'<event version="2.0" uid="c0ffee-1234" type="b-m-p-w-GOTO" '
    b'time="2026-07-30T18:22:31Z" stale="2026-07-30T19:22:31Z" how="h-e">'
    b'<point lat="35.5" lon="-79.5" hae="9999999.0" ce="9999999.0" le="9999999.0"/>'
    b'<detail><contact callsign="CP1"/></detail></event>'
)

# Deliberately incompressible detail, so this actually needs fragmenting at the
# real MTU rather than zlib-ing down into one packet.
_NOISE = "".join("%02x" % b for b in os.urandom(700))
DRAWING = (
    '<event version="2.0" uid="drawing-1" type="u-d-f" '
    'time="2026-07-30T18:22:31Z" stale="2026-07-30T19:22:31Z" how="h-e">'
    '<point lat="35.5" lon="-79.5" hae="0.0" ce="1.0" le="1.0"/>'
    '<detail><link point="%s"/><strokeColor value="-1"/></detail></event>' % _NOISE
).encode("utf-8")


def compact_path(frame):
    """True if this took the structured path rather than the zlib'd-XML one.

    Every spelling counts: whether a compact frame was then squeezed against
    the dictionary is a size decision made per frame, and whether generic
    elements ride with it is a question for the tests that ask it -- no test
    asserting "this did not fall back" means to care which.
    """
    return bool(frame) and (frame[0] & 0x0F) in codec.COMPACT_KINDS


def unsqueezed(frame):
    """A compact frame's bytes, decompressed if it was squeezed.

    Tests that assert on layout -- flag words, field order, what a field costs
    -- want the ENCODING, not the compression of it. Compression is measured
    separately, in TestDictionaryCompression.
    """
    if frame and (frame[0] & 0x0F) == codec.T_ZCOMPACT:
        return bytes([codec.CTRL_COMPACT]) + codec._unsqueeze(frame[1:])
    if frame and (frame[0] & 0x0F) == codec.T_ZCOMPACT_X:
        return bytes([codec.CTRL_COMPACT_X]) + codec._unsqueeze(frame[1:])
    return frame


def detail_tree(xml):
    """An event's <detail> as nested (tag, attributes IN ORDER, text,
    children) tuples, less what the codec drops on purpose. Whitespace-only
    text is formatting and does not count."""
    def walk(el):
        return (el.tag, tuple(el.attrib.items()),
                (el.text or "").strip() or None, tuple(walk(c) for c in el))
    detail = ET.fromstring(codec._strip_declaration(xml)).find("detail")
    skip = set(codec.DROPPABLE_DETAIL)
    if not codec.INCLUDE_TAKV:
        skip.add("takv")
    return [] if detail is None else [walk(c) for c in detail
                                      if c.tag not in skip]


def assert_carried_exactly(test, raw, because=None):
    """`raw` went as generic elements -- not zlib'd XML -- and its detail came
    back element for element, attribute for attribute, in order.

    This replaced "falls back to zlib", which these tests used to assert for
    input a typed encoding refuses. The point of falling back was never the
    zlib; it was not losing anything, and this checks that more strictly than
    a uid comparison did."""
    frame = codec.encode(raw)
    test.assertIn(frame[0] & 0x0F, (codec.T_COMPACT_X, codec.T_ZCOMPACT_X),
                  because)
    out = codec.decode(frame, now=NOW)
    test.assertEqual(detail_tree(out), detail_tree(raw), because)
    test.assertEqual(attrs(out).get("uid"), attrs(raw).get("uid"))
    test.assertEqual(attrs(out).get("type"), attrs(raw).get("type"))
    return frame


class CollectingHook(object):
    """Captures on_event callbacks for the duration of a with-block."""

    def __init__(self):
        self.events = []

    def __enter__(self):
        self._prev = codec.on_event
        codec.on_event = lambda kind, detail: self.events.append((kind, detail))
        return self

    def __exit__(self, *exc):
        codec.on_event = self._prev
        return False

    def kinds(self):
        return [k for k, _ in self.events]

    def details(self):
        return [d for _, d in self.events]


def attrs(xml_bytes):
    return ET.fromstring(xml_bytes)


class TestCompactPath(unittest.TestCase):

    def test_pli_round_trip(self):
        out = codec.decode(codec.encode(PLI), now=NOW)
        root = attrs(out)
        self.assertEqual(root.get("uid"), "ANDROID-352214111111111")
        self.assertEqual(root.get("type"), "a-f-G-U-C")
        self.assertEqual(root.get("how"), "m-g")
        self.assertEqual(root.get("time"), "2026-07-30T18:22:31.000Z")
        self.assertEqual(root.get("start"), root.get("time"))
        self.assertEqual(
            codec._parse_ts(root.get("stale")) - codec._parse_ts(root.get("time")), 135
        )

        point = root.find("point")
        self.assertAlmostEqual(float(point.get("lat")), 35.1234567, places=7)
        self.assertAlmostEqual(float(point.get("lon")), -79.7654321, places=7)
        self.assertAlmostEqual(float(point.get("hae")), 123.45, delta=1.0)
        self.assertAlmostEqual(float(point.get("ce")), 9.5, delta=1.0)
        self.assertEqual(point.get("le"), codec.UNKNOWN_SENTINEL)

        self.assertEqual(root.find("detail/contact").get("callsign"), "BRAVO01")
        self.assertEqual(root.find("detail/__group").get("name"), "Cyan")
        self.assertEqual(root.find("detail/__group").get("role"), "Team Member")
        self.assertEqual(root.find("detail/status").get("battery"), "87")
        self.assertAlmostEqual(float(root.find("detail/track").get("course")), 271.34, places=2)
        self.assertAlmostEqual(float(root.find("detail/track").get("speed")), 1.23, places=2)
        self.assertEqual(root.find("detail/uid").get("Droid"), "BRAVO01")
        self.assertEqual(root.find("detail/precisionlocation").get("altsrc"), "GPS")

    def test_pli_is_compact_frame(self):
        self.assertTrue(compact_path(codec.encode(PLI)))

    def test_pli_size_budget(self):
        # A hard number so a regression that re-enables <takv> or the real
        # contact endpoint shows up here rather than as mysterious airtime
        # months later. Measured actual: 61.
        self.assertLessEqual(len(codec.encode(PLI)), 100)
        self.assertEqual(len(codec.build_packets(PLI)), 1)

    def test_decoded_event_is_not_self_closing(self):
        # Both Kotlin's splitter and ATAK's own look for a literal </event>.
        self.assertIn(b"</event>", codec.decode(codec.encode(PLI)))

    def test_decoded_output_has_no_xml_declaration(self):
        self.assertFalse(codec.decode(codec.encode(PLI)).startswith(b"<?xml"))

    def test_unicode_callsign_round_trip(self):
        out = codec.decode(codec.encode(PLI_UNICODE))
        self.assertEqual(
            attrs(out).find("detail/contact").get("callsign"), "ÅNGSTRÖM-01"
        )
        # us-ascii serialization would have produced &#197; here instead.
        self.assertNotIn(b"&#", out)

    def test_keepalive_round_trip(self):
        frame = codec.encode(KEEPALIVE)
        self.assertTrue(compact_path(frame))
        self.assertLess(len(frame), 40)
        self.assertEqual(attrs(codec.decode(frame)).get("type"), "t-x-c-t")

    def test_type_and_how_escape(self):
        src = PLI.replace(b'type="a-f-G-U-C"', b'type="x-custom-plugin-thing"') \
                 .replace(b'how="m-g"', b'how="q-z-z"')
        with CollectingHook() as hook:
            root = attrs(codec.decode(codec.encode(src)))
        self.assertEqual(root.get("type"), "x-custom-plugin-thing")
        self.assertEqual(root.get("how"), "q-z-z")
        self.assertIn("type_escape", hook.kinds())
        self.assertIn("how_escape", hook.kinds())

    def test_team_role_escape(self):
        src = PLI.replace(b'role="Team Member" name="Cyan"',
                          b'role="Drone Operator" name="Chartreuse"')
        with CollectingHook() as hook:
            group = attrs(codec.decode(codec.encode(src))).find("detail/__group")
        self.assertEqual(group.get("name"), "Chartreuse")
        self.assertEqual(group.get("role"), "Drone Operator")
        self.assertIn("team_escape", hook.kinds())
        self.assertIn("role_escape", hook.kinds())

    def test_missing_optional_attributes(self):
        root = attrs(codec.decode(codec.encode(WAYPOINT)))
        self.assertIsNone(root.find("detail/track"))
        self.assertIsNone(root.find("detail/status"))
        self.assertIsNone(root.find("detail/precisionlocation"))
        self.assertEqual(root.get("start"), root.get("time"))
        self.assertEqual(root.find("detail/contact").get("callsign"), "CP1")

    def test_ce_le_sentinel_and_clamp(self):
        def ce_of(raw):
            return attrs(codec.decode(codec.encode(raw))).find("point").get("ce")

        self.assertEqual(ce_of(PLI.replace(b'ce="9.5"', b'ce="9999999.0"')),
                         codec.UNKNOWN_SENTINEL)
        self.assertEqual(float(ce_of(PLI.replace(b'ce="9.5"', b'ce="3.5"'))), 4.0)
        self.assertEqual(ce_of(PLI.replace(b' ce="9.5"', b"")), codec.UNKNOWN_SENTINEL)
        self.assertEqual(ce_of(PLI.replace(b'ce="9.5"', b'ce="250000"')),
                         codec.UNKNOWN_SENTINEL)

    def test_lat_lon_precision(self):
        for lat, lon in [(0.0, 0.0), (-89.9999999, 179.9999999),
                         (35.1234567, -79.7654321), (0.0000001, -0.0000001)]:
            src = PLI.replace(b'lat="35.1234567" lon="-79.7654321"',
                              ('lat="%.7f" lon="%.7f"' % (lat, lon)).encode())
            point = attrs(codec.decode(codec.encode(src))).find("point")
            self.assertAlmostEqual(float(point.get("lat")), lat, delta=1e-7)
            self.assertAlmostEqual(float(point.get("lon")), lon, delta=1e-7)

    def test_timestamp_formats(self):
        base = codec._parse_ts("2026-07-30T18:22:31Z")
        self.assertEqual(codec._parse_ts("2026-07-30T18:22:31.123Z"), base)
        self.assertEqual(codec._parse_ts("2026-07-30T18:22:31.123456Z"), base)
        self.assertEqual(codec._parse_ts("2026-07-30T18:22:31+00:00"), base)
        self.assertEqual(codec._parse_ts("2026-07-30T18:22:31"), base)
        self.assertEqual(codec._parse_ts("2026-07-30T13:22:31-05:00"), base)
        self.assertEqual(codec._parse_ts("2026-07-30T18:22:31.500-05:00"), base + 5 * 3600)
        for bad in ["", "not a time", "2026-07-30", "2026-13-99T99:99:99Z"]:
            self.assertRaises(codec.CotEncodeError, codec._parse_ts, bad)

    def test_no_fromisoformat_anywhere(self):
        # The device runs Python 3.8, where fromisoformat cannot parse a
        # trailing "Z" -- but every desktop interpreter used for development
        # accepts it, so this failure is invisible until it is on hardware.
        # Matches the call form, so the comments warning against it don't trip.
        with open(codec.__file__, "r") as f:
            self.assertNotIn("fromisoformat(", f.read())

    def test_table_integrity(self):
        byte_tables = [codec.TYPE_TABLE, codec.HOW_TABLE, codec.PLATFORM_TABLE]
        nibble_tables = [codec.TEAM_TABLE, codec.ROLE_TABLE,
                         codec.ALTSRC_TABLE, codec.GEOPOINTSRC_TABLE]
        for table in byte_tables + nibble_tables:
            self.assertEqual(table[0], "", "index 0 must be the reserved escape slot")
            self.assertEqual(len(table), len(set(table)), "duplicate entry in table")
        for table in byte_tables:
            self.assertLessEqual(len(table), 255)
        for table in nibble_tables:
            self.assertLessEqual(len(table), 16, "nibble table cannot exceed 15 entries")


class TestMarker(unittest.TestCase):
    """PROTOCOL_VERSION 2. Before it, every user-placed marker took the zlib
    path at ~3x the airtime, because ATAK stamps <color> and <creator> on all
    of them."""

    def test_a_real_marker_takes_the_compact_path(self):
        with CollectingHook() as hook:
            frame = codec.encode(MARKER)
        self.assertTrue(compact_path(frame))
        self.assertEqual(hook.kinds(), [], "no fallback and no table escapes")

    def test_a_real_marker_fits_in_one_packet(self):
        self.assertEqual(len(codec.build_packets(MARKER)), 1)

    def test_nothing_ATAK_sent_is_silently_lost(self):
        out = codec.decode(codec.encode(MARKER))
        for expected in (b'uid="0ef9c3a9-bf54-46e5-9655-29956a0d80ba"',
                         b'type="a-u-G"', b'color argb="-1"',
                         b'uid="ANDROID-0123456789abcdef"',
                         b'callsign="pixel"', b'callsign="Marker 1"',
                         b'readiness="true"', b'altsrc="GPS"'):
            self.assertIn(expected, out)

    def test_readiness_survives_the_compact_path(self):
        # The regression this milestone would otherwise have introduced: before
        # markers were compact-encodable, <status readiness> rode the zlib path
        # verbatim. The encoder only ever modelled status/@battery.
        out = codec.decode(codec.encode(MARKER))
        self.assertIn(b'readiness="true"', out)

    def test_readiness_false_round_trips(self):
        src = MARKER.replace(b'readiness="true"', b'readiness="false"')
        self.assertIn(b'readiness="false"', codec.decode(codec.encode(src)))

    def test_an_unmodelled_status_attribute_is_carried_rather_than_vanishing(self):
        src = MARKER.replace(b'<status readiness="true"/>',
                             b'<status readiness="true" mystery="7"/>')
        with CollectingHook() as hook:
            assert_carried_exactly(self, src)
        self.assertIn("mystery", " ".join(hook.details()))

    def test_color_value_form_round_trips(self):
        # ColorDetailHandler writes argb= on a Marker and value= on anything
        # else. ATAK reads argb first, so either is understood -- but which was
        # sent is preserved rather than canonicalised.
        src = MARKER.replace(b'<color argb="-1"/>', b'<color value="-16711936"/>')
        self.assertIn(b'<color value="-16711936"', codec.decode(codec.encode(src)))

    def test_color_survives_the_full_signed_range(self):
        for argb in (-2147483648, -1, 0, 2147483647):
            src = MARKER.replace(b'argb="-1"', b'argb="%d"' % argb)
            self.assertIn(b'argb="%d"' % argb, codec.decode(codec.encode(src)))

    def test_unsigned_color_is_accepted_and_normalised(self):
        # Some producers write the unsigned form of the same 32 bits.
        src = MARKER.replace(b'argb="-1"', b'argb="4294967295"')
        self.assertIn(b'argb="-1"', codec.decode(codec.encode(src)))

    def test_a_nonsense_color_is_carried_instead_of_guessed(self):
        for bad in (b'argb="mauve"', b'argb="99999999999"', b'rgb="1"'):
            src = MARKER.replace(b'argb="-1"', bad)
            assert_carried_exactly(self, src, bad)

    def test_creator_fields_are_individually_optional(self):
        src = MARKER.replace(
            b'<creator uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" '
            b'callsign="pixel" time="2026-08-04T09:55:58.000Z"/>',
            b'<creator callsign="pixel"/>')
        out = codec.decode(codec.encode(src))
        self.assertIn(b'<creator callsign="pixel"', out)
        self.assertNotIn(b'creator uid', out)

    def test_creator_time_is_relative_and_cheap(self):
        # The whole point of encoding it as a delta: a marker sent the moment
        # it is placed costs one byte, not the 24 an ISO timestamp needs.
        same = codec.encode(MARKER)
        later = codec.encode(MARKER.replace(
            b'<creator uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" '
            b'callsign="pixel" time="2026-08-04T09:55:58.000Z"/>',
            b'<creator uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" '
            b'callsign="pixel" time="2026-08-04T09:55:58.000Z"/>'))
        self.assertEqual(len(same), len(later))
        # And an hour-old marker still round-trips its real timestamp.
        old = MARKER.replace(b'<creator uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" '
                             b'callsign="pixel" time="2026-08-04T09:55:58.000Z"/>',
                             b'<creator uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" '
                             b'callsign="pixel" time="2026-08-04T08:55:58.000Z"/>')
        self.assertIn(b'time="2026-08-04T08:55:58.000Z"', codec.decode(codec.encode(old)))

    def test_an_unknown_creator_attribute_goes_generic(self):
        src = MARKER.replace(b'<creator uid=', b'<creator surprise="1" uid=')
        with CollectingHook() as hook:
            assert_carried_exactly(self, src)
        self.assertIn("surprise", " ".join(hook.details()))

    def test_an_unusual_creator_type_escapes_rather_than_failing(self):
        src = MARKER.replace(b'type="a-f-G-U-C" callsign="pixel"',
                             b'type="a-f-X-NOVEL" callsign="pixel"')
        with CollectingHook() as hook:
            out = codec.decode(codec.encode(src))
        self.assertIn(b'type="a-f-X-NOVEL"', out)
        self.assertIn("creator_type_escape", hook.kinds())

    def test_remarks_round_trip(self):
        root = attrs(codec.decode(codec.encode(MARKER)))
        self.assertEqual(root.find("detail/remarks").text, "observed movement north")

    def test_empty_remarks_round_trip(self):
        # ATAK emits <remarks/> with no text on a marker with none typed.
        src = MARKER.replace(b"<remarks>observed movement north</remarks>",
                             b"<remarks/>")
        root = attrs(codec.decode(codec.encode(src)))
        self.assertIsNotNone(root.find("detail/remarks"))

    def test_remarks_with_attributes_goes_generic(self):
        # GeoChat's <remarks source= to= time=> is a different animal and must
        # not be flattened into a bare string.
        src = MARKER.replace(b"<remarks>", b'<remarks source="BAO.F.ATAK.x">')
        assert_carried_exactly(self, src)

    def test_usericon_round_trip(self):
        root = attrs(codec.decode(codec.encode(MARKER)))
        self.assertEqual(root.find("detail/usericon").get("iconsetpath"),
                         "COT_MAPPING_2525B/a-u/a-u-G")

    def test_link_round_trips_every_attribute(self):
        # Attribute ORDER is not preserved and does not need to be; the values
        # are what ATAK reads.
        link = attrs(codec.decode(codec.encode(MARKER))).find("detail/link")
        self.assertEqual(link.get("uid"), "ANDROID-0123456789abcdef")
        self.assertEqual(link.get("type"), "a-f-G-U-C")
        self.assertEqual(link.get("relation"), "p-p")
        self.assertEqual(link.get("parent_callsign"), "pixel")
        self.assertEqual(link.get("production_time"), "2026-08-04T09:55:58.000Z")

    def test_a_link_echoing_the_creator_costs_one_byte(self):
        # The whole point of L_LIKE_CREATOR: a placed marker says the same
        # parent twice, in two spellings.
        without = MARKER.replace(
            b'<link uid="ANDROID-0123456789abcdef" production_time="2026-08-04T09:55:58.000Z" '
            b'type="a-f-G-U-C" parent_callsign="pixel" relation="p-p"/>', b"")
        # Its whole cost is the subflag byte plus an lstr'd "p-p": five
        # bytes, against ~35 for spelling the parent out a second time.
        self.assertEqual(len(unsqueezed(codec.encode(MARKER)))
                         - len(unsqueezed(codec.encode(without))), 5)

    def test_a_link_that_differs_from_the_creator_is_sent_in_full(self):
        src = MARKER.replace(b'parent_callsign="pixel"',
                             b'parent_callsign="somebody-else"')
        link = attrs(codec.decode(codec.encode(src))).find("detail/link")
        self.assertEqual(link.get("parent_callsign"), "somebody-else")
        self.assertEqual(link.get("uid"), "ANDROID-0123456789abcdef")

    def test_a_link_with_no_creator_present_is_sent_in_full(self):
        src = MARKER.replace(
            b'<creator uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" callsign="pixel" '
            b'time="2026-08-04T09:55:58.000Z"/>', b"")
        link = attrs(codec.decode(codec.encode(src))).find("detail/link")
        self.assertEqual(link.get("uid"), "ANDROID-0123456789abcdef")
        self.assertEqual(link.get("parent_callsign"), "pixel")

    def test_a_url_link_is_carried_rather_than_losing_the_attachment(self):
        src = MARKER.replace(b'relation="p-p"',
                             b'relation="r-u" url="http://x/y" mime="image/jpeg"')
        with CollectingHook() as hook:
            assert_carried_exactly(self, src)
        self.assertIn("url", " ".join(hook.details()))

    def test_marti_is_dropped(self):
        # A TAK-SERVER routing directive that no ATAK reads inbound. See
        # DROPPABLE_DETAIL, and the README note on targeted delivery.
        out = codec.decode(codec.encode(MARKER))
        self.assertNotIn(b"marti", out)
        self.assertNotIn(b"OnePlus", out)

    def test_marti_alone_does_not_force_a_fallback(self):
        self.assertTrue(compact_path(codec.encode(MARKER)))

    def test_extended_flags_cost_nothing_on_a_position_report(self):
        # F_EXT exists so the high-volume traffic does not pay for marker
        # fields it never carries.
        self.assertEqual(codec.encode(PLI)[1] & (codec.F_EXT >> 8), 0)

    def test_a_marker_is_much_smaller_than_it_used_to_be(self):
        compact = len(codec.encode(MARKER))
        zlib_only = len(codec.encode_zxml(MARKER))
        self.assertLess(compact, zlib_only,
                        "the compact path must beat the fallback it replaced")
        # A ceiling, not a target: it exists so a regression that drops the
        # marker back onto zlib, or spells the parent out twice again, is
        # caught here rather than on the radio.
        self.assertLess(compact, 190)


class TestFallbackPath(unittest.TestCase):

    def test_a_detail_a_typed_encoding_refuses_goes_generic_whole(self):
        # A position carrying a <__chat> the chat encoding cannot rebuild. It
        # used to send the whole event to zlib; now the detail goes generic and
        # the head stays compact.
        src = PLI.replace(b"</detail>", b'<__chat id="x"/></detail>')
        with CollectingHook() as hook:
            assert_carried_exactly(self, src)
        self.assertIn("generic_detail", hook.kinds())
        self.assertNotIn("fallback", hook.kinds())

    def test_an_unknown_child_rides_beside_the_typed_fields(self):
        # The common case on the 2026-10-14 comparison: a modelled event plus
        # one child nothing models. The typed fields must still be used, so the
        # unknown child costs its own bytes and not the whole event's.
        plain = codec.encode(PLI)
        src = PLI.replace(b"</detail>", b'<sensor fov="45" range="100"/></detail>')
        frame = codec.encode(src)
        self.assertIn(frame[0] & 0x0F, (codec.T_COMPACT_X, codec.T_ZCOMPACT_X))
        (flags,) = struct.unpack(">H", unsqueezed(frame)[1:3])
        self.assertTrue(flags & codec.F_CALLSIGN, "the typed contact was used")
        out = attrs(codec.decode(frame))
        self.assertEqual(out.find("detail/sensor").attrib,
                         {"fov": "45", "range": "100"})
        self.assertEqual(out.find("detail/contact").get("callsign"), "BRAVO01")
        self.assertLess(len(frame) - len(plain), 12)

    def test_a_frame_with_nothing_generic_is_unchanged(self):
        # What keeps mixed builds working for everything they could already
        # exchange: no generic elements, no new frame type.
        for src in (PLI, MARKER, GEOCHAT, RECTANGLE):
            self.assertIn(codec.encode(src)[0] & 0x0F,
                          (codec.T_COMPACT, codec.T_ZCOMPACT))

    def test_what_still_falls_back_and_stays_lossless(self):
        cases = (
            # An unknown EVENT attribute: the head has a slot per attribute.
            PLI.replace(b'<event version="2.0"', b'<event qos="1-r-c" version="2.0"'),
            # Text between two elements -- mixed content, which an element
            # tree with whitespace dropped cannot say where to put back.
            PLI.replace(b"</detail>", b'<sensor/>loose text<x/></detail>'),
            # A namespaced element.
            PLI.replace(b"</detail>", b'<n:x xmlns:n="urn:x"/></detail>'),
        )
        for src in cases:
            frame = codec.encode(src)
            self.assertEqual(frame[0], codec.CTRL_ZXML, src)
            self.assertEqual(codec.decode(frame), src)

    def test_the_log_names_the_unmodelled_child(self):
        # The first hardware run of milestone 6 logged "unknown <detail> child"
        # for every marker, each costing ~3x the airtime a compact one would,
        # with no way to tell from the log which tag to add. The tag name is
        # the entire actionable content of that message.
        src = PLI.replace(b"</detail>", b'<sensor fov="45"/></detail>')
        with CollectingHook() as hook:
            codec.encode(src)
        self.assertIn("sensor", " ".join(hook.details()))

    def test_the_log_names_every_unmodelled_child_not_just_the_first(self):
        # The 2026-08-04 bench reported only <color> for a marker that also
        # carries <creator>, because color comes first in ATAK's element order.
        # Acting on that would have meant adding one, rebuilding, going back to
        # the radio, and finding the next.
        src = PLI.replace(
            b"</detail>",
            b'<sensor fov="45"/><video url="rtsp://x"/></detail>')
        with CollectingHook() as hook:
            codec.encode(src)
        detail = " ".join(hook.details())
        self.assertIn("sensor", detail)
        self.assertIn("video", detail)

    def test_the_log_names_what_is_inside_a_container(self):
        """`<__routeinfo>` on its own says nothing about whether the work is an
        afternoon or a fortnight -- the answer is entirely in its children."""
        src = PLI.replace(
            b"</detail>",
            b'<__someplugin><config mode="x"/><state/></__someplugin></detail>')
        with CollectingHook() as hook:
            codec.encode(src)
        detail = " ".join(hook.details())
        self.assertIn("__someplugin", detail)
        self.assertIn("config", detail)
        self.assertIn("state", detail)

    def test_the_log_stays_bounded(self):
        # Runs on the CoT listener thread; a hostile or broken event must not
        # be able to write an unbounded log line there.
        extra = b"".join(b"<x%d/>" % i for i in range(50))
        src = PLI.replace(b"</detail>", extra + b"</detail>")
        with CollectingHook() as hook:
            codec.encode(src)
        detail = " ".join(hook.details())
        self.assertIn("more", detail)
        self.assertLess(len(detail), 200)

    def test_the_log_distinguishes_a_repeat_from_an_unknown(self):
        # Different fixes: one needs a new table entry, the other a typed
        # format that can hold a second copy -- the typed one has one slot per
        # tag, which is why the repeat rides generic.
        src = PLI.replace(b"</detail>", b'<contact callsign="B"/></detail>')
        with CollectingHook() as hook:
            codec.encode(src)
        self.assertIn("repeated", " ".join(hook.details()))

    def test_a_chat_we_cannot_rebuild_is_carried_exactly(self):
        # GeoChat takes the compact path; this one carries an attribute the
        # chat encoding will not invent. It used to be the zlib route's case,
        # and lossless still matters most here: it is how the codec stays safe
        # when it meets something new.
        frame = assert_carried_exactly(self, CHAT_FALLBACK)
        self.assertLess(len(frame), len(codec.encode_zxml(CHAT_FALLBACK)))

    def test_the_zlib_path_is_lossless_and_one_packet(self):
        frame = codec.encode(ZXML_CHAT)
        self.assertEqual(frame[0], codec.CTRL_ZXML)
        self.assertEqual(codec.decode(frame), ZXML_CHAT)
        # Without the preset dictionary plain zlib gives ~468 bytes -- over the
        # 464 budget, so this would need two unacknowledged packets. This
        # assertion is what proves COT_ZDICT is earning its keep.
        self.assertEqual(len(codec.build_packets(ZXML_CHAT)), 1)

    def test_drop_allowlist_stays_compact(self):
        with CollectingHook() as hook:
            frame = codec.encode(SERVER_PLI)
        self.assertTrue(compact_path(frame))
        self.assertNotIn("fallback", hook.kinds())
        out = codec.decode(frame)
        self.assertNotIn(b"_flow-tags_", out)
        self.assertNotIn(b"archive", out)

    def test_access_attribute_stays_compact(self):
        # Real ATAK (bench run 2026-07-31) stamps @access on every event. Before
        # it was carried, this disqualified EVERY position report from the
        # compact path -- 741 B of CoT went out as 268 B of zlib instead of ~65.
        src = PLI.replace(b'<event version="2.0"', b'<event access="Undefined" version="2.0"')
        with CollectingHook() as hook:
            frame = codec.encode(src)
        self.assertTrue(compact_path(frame))
        self.assertNotIn("fallback", hook.kinds())
        self.assertEqual(attrs(codec.decode(frame)).get("access"), "Undefined")
        # One byte for the table index, and nothing else.
        self.assertEqual(len(unsqueezed(frame)),
                         len(unsqueezed(codec.encode(PLI))) + 1)

    def test_access_escape_round_trips(self):
        src = PLI.replace(b'<event version="2.0"', b'<event access="NATO-RESTRICTED" version="2.0"')
        with CollectingHook() as hook:
            frame = codec.encode(src)
        self.assertEqual(attrs(codec.decode(frame)).get("access"), "NATO-RESTRICTED")
        self.assertIn("access_escape", hook.kinds())

    def test_access_is_written_last(self):
        """F_ACCESS was added without a PROTOCOL_VERSION bump, which was only
        safe because it is read last: an older decoder ignores the flag and
        leaves its bytes unread as harmless trailing data.

        This used to assert F_ACCESS was the *highest* flag, which was a proxy
        for "written last" while the body ran in ascending flag order. Version 2
        added three higher flags, so the proxy no longer holds -- but the
        property it stood for still does, and that is what is checked here.
        Everything except F_ACCESS stays in ascending order; F_ACCESS is pinned
        to the end whatever its value."""
        ordered = [v for k, v in vars(codec).items()
                   if k.startswith("F_") and isinstance(v, int)
                   and v != codec.F_ACCESS]
        self.assertEqual(sorted(ordered), ordered,
                         "flag definitions must stay in body order")
        self.assertGreater(max(ordered), codec.F_ACCESS,
                           "if F_ACCESS is highest again, restore the simpler check")

        # Demonstrate the degradation directly: strip the flag bit and the
        # frame must still decode, just without @access.
        src = PLI.replace(b'<event version="2.0"', b'<event access="Undefined" version="2.0"')
        frame = bytearray(unsqueezed(codec.encode(src)))
        flags_val = (frame[1] << 8) | frame[2]
        flags_val &= ~codec.F_ACCESS
        frame[1], frame[2] = flags_val >> 8, flags_val & 0xFF
        out = attrs(codec.decode(bytes(frame)))
        self.assertIsNone(out.get("access"))
        self.assertEqual(out.get("uid"), "ANDROID-352214111111111")

    def test_unknown_event_attribute_forces_fallback(self):
        # @qos is a real CoT attribute deliberately not implemented, so it
        # stands in for "whatever this ATAK build stamps that we haven't seen
        # yet". (@access used to serve this role, until a bench run showed real
        # ATAK sends it on every event and it earned a compact slot.)
        src = PLI.replace(b'<event version="2.0"', b'<event qos="9-r-c" version="2.0"')
        self.assertEqual(codec.encode(src)[0], codec.CTRL_ZXML)
        prev = codec.DROP_EVENT_ATTRS
        codec.DROP_EVENT_ATTRS = frozenset(["qos"])
        try:
            frame = codec.encode(src)
            self.assertTrue(compact_path(frame))
            self.assertNotIn(b"qos", codec.decode(frame))
        finally:
            codec.DROP_EVENT_ATTRS = prev

    def test_zdict_mismatch_is_detected(self):
        body = codec.encode_zxml(PLI)[1:]
        d = zlib.decompressobj(zlib.MAX_WBITS, b"a completely different dictionary")
        self.assertRaises(zlib.error, d.decompress, body)

    def test_measured_sizes(self):
        self.assertLessEqual(len(codec.encode(PLI)), 100)
        self.assertLessEqual(len(codec.encode_zxml(PLI)), 220)
        self.assertLessEqual(len(codec.encode_zxml(GEOCHAT)), 330)


class TestFragmentation(unittest.TestCase):

    def test_no_fragment_header_when_it_fits(self):
        packets = codec.build_packets(PLI)
        self.assertEqual(len(packets), 1)
        self.assertTrue(compact_path(packets[0]))

    def test_drawing_actually_fragments_at_real_mtu(self):
        packets = codec.build_packets(DRAWING)
        self.assertGreater(len(packets), 1)
        self.assertTrue(all(len(p) <= codec.RNS_MAX_PAYLOAD for p in packets))

    def test_fragmentation_round_trip(self):
        frame = codec.encode(DRAWING)
        packets = codec.fragment(frame, mtu=120)
        self.assertGreater(len(packets), 1)
        self.assertTrue(all(len(p) <= 120 for p in packets))
        self.assertTrue(all(p[0] == codec.CTRL_FRAG for p in packets))

        headers = [p[1:5] for p in packets]
        self.assertEqual(sorted(h[2] for h in headers), list(range(len(packets))))
        self.assertEqual(len(set(h[:2] for h in headers)), 1, "msg_id must match")
        self.assertEqual(len(set(h[3] for h in headers)), 1, "count must match")

        r = codec.Reassembler()
        done = [x for x in (r.offer(p) for p in packets) if x is not None]
        self.assertEqual(len(done), 1)
        self.assertEqual(codec.decode(done[0]), DRAWING)

    def test_reassembly_out_of_order(self):
        packets = codec.fragment(codec.encode(DRAWING), mtu=120)
        random.Random(1234).shuffle(packets)
        r = codec.Reassembler()
        done = [x for x in (r.offer(p) for p in packets) if x is not None]
        self.assertEqual(len(done), 1)
        self.assertEqual(codec.decode(done[0]), DRAWING)

    def test_interleaved_duplicates_are_suppressed(self):
        packets = codec.fragment(codec.encode(DRAWING), mtu=120)
        r = codec.Reassembler()
        done = []
        for p in packets:
            # RNS legitimately redelivers; a repeat of a fragment already held
            # must not corrupt or double-complete the message.
            for _ in range(2):
                out = r.offer(p)
                if out is not None:
                    done.append(out)
        self.assertEqual(len(done), 1)
        self.assertGreater(r.stats()["duplicates"], 0)
        self.assertEqual(r.stats()["completed"], 1)

    def test_fragment_loss_frees_memory(self):
        clock = [0.0]
        r = codec.Reassembler(timeout=30.0, clock=lambda: clock[0])
        packets = codec.fragment(codec.encode(DRAWING), mtu=120)
        for p in packets[:-1]:
            self.assertIsNone(r.offer(p))
        self.assertEqual(r.stats()["pending"], 1)

        clock[0] = 100.0
        other = codec.fragment(b"\x11" + b"Z" * 300, mtu=120, msg_id=0x4321)
        r.offer(other[0])
        self.assertEqual(r.stats()["pending"], 1)
        self.assertEqual(r.stats()["timeouts"], 1)

    def test_set_timeout_resizes_a_message_already_in_flight(self):
        """The buffer is built before the radio has reported its bitrate, so
        the correction has to reach messages that are already part-received --
        not just the next one. See _build_rnode_interface()."""
        clock = [0.0]
        r = codec.Reassembler(timeout=30.0, clock=lambda: clock[0])
        packets = codec.fragment(codec.encode(DRAWING), mtu=120)
        for p in packets[:-1]:
            self.assertIsNone(r.offer(p))

        r.set_timeout(120.0)

        # Past the old timeout, inside the new one: the partial message has to
        # still be there for the sender's late retry to complete.
        clock[0] = 60.0
        self.assertIsNotNone(r.offer(packets[-1]))
        self.assertEqual(r.stats()["timeouts"], 0)

    def test_reassembler_bounded(self):
        r = codec.Reassembler(max_messages=16)
        for msg_id in range(100):
            r.offer(codec.fragment(b"\x11" + b"Z" * 300, mtu=120, msg_id=msg_id)[0])
        self.assertLessEqual(r.stats()["pending"], 16)

    def test_msgid_collision_discards_both(self):
        a = codec.fragment(b"\x11" + b"A" * 300, mtu=120, msg_id=0x1234)
        b = codec.fragment(b"\x11" + b"B" * 300, mtu=120, msg_id=0x1234)
        r = codec.Reassembler()
        results = [r.offer(p) for p in (a[0], b[0], a[1], b[1])]
        # Losing both is strictly better than emitting a spliced frame.
        self.assertTrue(all(x is None for x in results))
        self.assertGreaterEqual(r.stats()["collisions"], 1)

    def test_oversize_message_refused(self):
        with CollectingHook() as hook:
            self.assertRaises(
                codec.CotEncodeError,
                codec.fragment, b"\x11" + b"Z" * (20 * 459), codec.RNS_MAX_PAYLOAD,
            )
        self.assertIn("frag_oversize", hook.kinds())

    def test_fragment_of_compact_still_decodes(self):
        # The encoder never fragments a compact frame, but the decoder must not
        # depend on that invariant.
        frame = codec.encode(PLI)
        r = codec.Reassembler()
        done = [x for x in (r.offer(p) for p in codec.fragment(frame, mtu=30))
                if x is not None]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0], frame)
        self.assertTrue(codec.decode(done[0]))

    def test_non_fragment_passes_through_untouched(self):
        frame = codec.encode(PLI)
        self.assertEqual(codec.Reassembler().offer(frame), frame)


class TestRobustness(unittest.TestCase):
    """The 'one malformed event kills nothing' property, which the error
    handling in rns_bridge.py depends on being structural."""

    BAD_XML = [
        b"", b"   ", b"<event foo", b"not xml", b"<event/>",
        b"<event><point/></event>", b"<notanevent/>", b"\x00\x01\x02",
        b"<event uid='a' type='b' time='nope'><point lat='1' lon='2'/></event>",
        b"<event uid='a' type='b' time='2026-07-30T18:22:31Z'><point lat='x' lon='y'/></event>",
    ]

    def test_malformed_xml_raises_only_encode_error(self):
        for raw in self.BAD_XML + [os.urandom(4096)]:
            try:
                codec.encode(raw)
            except codec.CotEncodeError:
                pass
            except Exception as e:
                self.fail("encode(%r) raised %s: %s" % (raw[:32], type(e).__name__, e))

    def test_build_packets_raises_only_cot_encode_error(self):
        for raw in self.BAD_XML + [os.urandom(4096)]:
            try:
                codec.build_packets(raw)
            except codec.CotEncodeError:
                pass
            except Exception as e:
                self.fail("build_packets(%r) raised %s" % (raw[:32], type(e).__name__))

    def test_takcontrol_prefix_recovered(self):
        src = b'<TakControl><TakProtocolSupport version="1"/></TakControl>' + PLI
        frame = codec.encode(src)
        self.assertEqual(attrs(codec.decode(frame)).get("uid"), "ANDROID-352214111111111")

    def test_junk_prefix_stripped_on_fallback_path_too(self):
        # The compact path repairs a junk prefix by construction (it rebuilds
        # the XML). The fallback path compresses what it is given, so it must
        # be handed the repaired bytes -- otherwise the junk crosses the air
        # and the remote ATAK gets the same unparseable XML we just fixed.
        src = b'<TakControl><TakProtocolSupport version="1"/></TakControl>' + ZXML_CHAT
        frame = codec.encode(src)
        self.assertEqual(frame[0], codec.CTRL_ZXML)
        out = codec.decode(frame)
        self.assertNotIn(b"TakControl", out)
        self.assertEqual(out, ZXML_CHAT)
        self.assertTrue(attrs(out) is not None)

    def test_decompression_bomb_is_bounded(self):
        bomb = codec.encode_zxml(b"<event>" + b"A" * (codec.MAX_REASSEMBLED * 4) + b"</event>")
        self.assertRaises(codec.CotDecodeError, codec.decode, bomb)

    def test_xml_declaration_prefix(self):
        src = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + PLI
        self.assertEqual(attrs(codec.decode(codec.encode(src))).get("uid"),
                         "ANDROID-352214111111111")

    def test_decode_is_total(self):
        compact = codec.encode(PLI)
        zxml = codec.encode(GEOCHAT)
        corpus = [
            b"", b"\x10", b"\x10\x00", b"\x11", b"\x12", b"\xff\xff", b"\x99abc",
            b"\x20" + compact[1:],            # unsupported version
            b"\x1f" + b"x",                   # unsupported frame type
            b"\x11" + b"not zlib at all",
            b"\x11" + zlib.compress(b"not xml"),
            b"\x10" + b"\xff\xff" + b"\x00" * 8,   # every flag set, truncated body
            b"\x14", b"\x15",                 # wrapper types decode() must refuse
        ]
        # Truncating a valid frame at every possible length is the highest-value
        # fuzz available here and costs two lines.
        corpus += [compact[:i] for i in range(len(compact))]
        corpus += [zxml[:i] for i in range(len(zxml))]
        wrapped = codec.wrap_reliable(zxml, "0a0b0c")[1]
        ack = codec.encode_ack("0a0b0c", 4242, "def456")
        corpus += [wrapped[:i] for i in range(len(wrapped) + 1)]
        corpus += [ack[:i] for i in range(len(ack) + 1)]
        rng = random.Random(99)
        corpus += [bytes(rng.randrange(256) for _ in range(rng.randrange(1, 64)))
                   for _ in range(500)]

        r = codec.Reassembler()
        for raw in corpus:
            for fn in (codec.decode, codec.unwrap_reliable, codec.decode_ack):
                try:
                    fn(raw)
                except codec.CotDecodeError:
                    pass
                except Exception as e:
                    self.fail("%s(%r) raised %s: %s"
                              % (fn.__name__, raw[:32], type(e).__name__, e))
            try:
                r.offer(raw)
            except Exception as e:
                self.fail("offer(%r) raised %s" % (raw[:32], type(e).__name__))

    def test_out_of_range_table_index_is_dropped(self):
        # The append-only forward-compatibility contract: a newer sender's
        # table entry must be dropped by an older receiver, not crash it.
        frame = bytearray(codec.encode(PLI))
        frame[3] = len(codec.TYPE_TABLE) + 5
        self.assertRaises(codec.CotDecodeError, codec.decode, bytes(frame))


class TestKissEscaping(unittest.TestCase):
    """Milestone 2 puts the first high-entropy bytes onto the USB link.

    KISS reserves 0xC0 as its frame delimiter and 0xDB as its escape byte, so
    those values have to be substituted on the way out and restored on the way
    in. Milestone 1 payloads were ASCII XML, which tops out around 0x79 --
    below both reserved values -- so the escape path was a no-op on every
    frame ever sent and has never actually been exercised.

    This reimplements RNS's own unescape (which lives inline in
    RNodeInterface.readLoop and can't be imported on a desktop without the
    serial stack) and round-trips real codec output through it. It does NOT
    cover the Kotlin USB driver, which is Android-only.
    """

    FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD

    def escape(self, data):
        # Byte-for-byte what RNS's KISS.escape does. Order matters: 0xDB must
        # be escaped BEFORE 0xC0, or the 0xDB introduced by escaping 0xC0
        # would itself get escaped and corrupt the stream.
        data = data.replace(bytes([0xDB]), bytes([0xDB, 0xDD]))
        data = data.replace(bytes([0xC0]), bytes([0xDB, 0xDC]))
        return data

    def unescape(self, data):
        out = bytearray()
        escaping = False
        for byte in data:
            if byte == self.FESC:
                escaping = True
            elif escaping:
                out.append(self.FEND if byte == self.TFEND
                           else self.FESC if byte == self.TFESC
                           else byte)
                escaping = False
            else:
                out.append(byte)
        return bytes(out)

    def test_escape_round_trip_on_real_frames(self):
        frames = [codec.encode(PLI), codec.encode(GEOCHAT), codec.encode(WAYPOINT)]
        frames += codec.build_packets(DRAWING)
        for frame in frames:
            self.assertEqual(self.unescape(self.escape(frame)), frame)

    def test_escaped_frame_contains_no_bare_delimiter(self):
        # The property that actually matters: after escaping, a frame must
        # contain no bare 0xC0, or the receiver truncates it there.
        for frame in [codec.encode(GEOCHAT)] + codec.build_packets(DRAWING):
            self.assertNotIn(self.FEND, self.escape(frame))

    def test_escape_round_trip_on_adversarial_payloads(self):
        for raw in [bytes([0xC0]), bytes([0xDB]), bytes([0xDB, 0xDC]),
                    bytes([0xC0, 0xDB, 0xC0]), bytes([0xDB, 0xDD] * 20),
                    bytes(range(256)), b"\xc0" * 100, b"\xdb" * 100]:
            self.assertEqual(self.unescape(self.escape(raw)), raw)
        rng = random.Random(4242)
        for _ in range(500):
            raw = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 480)))
            self.assertEqual(self.unescape(self.escape(raw)), raw)

    def test_escaped_frame_stays_under_hw_mtu(self):
        # RNodeInterface truncates inbound frames past HW_MTU (508) -- but it
        # counts UNESCAPED bytes, so the budget that matters is the RNS packet
        # size, not the on-wire size. Confirm the worst case still fits.
        worst = codec.RNS_MAX_PAYLOAD + 19  # + RNS HEADER_1
        self.assertLess(worst, 508)


if __name__ == "__main__":
    unittest.main()


class TestMapFreshness(unittest.TestCase):
    """A receiver decides how long to keep a peer's marker, so the sender does
    not have to keep transmitting merely to reset somebody else's timer."""

    def stale_of(self, xml):
        return codec._parse_ts(ET.fromstring(xml).get("stale"))

    def test_a_receiver_can_hold_a_marker_longer_than_the_sender_asked(self):
        held = self.stale_of(codec.decode(codec.encode(PLI), min_stale_s=600))
        self.assertGreaterEqual(held, int(time.time()) + 599)

    def test_zero_leaves_the_senders_own_value_alone(self):
        # To the second: the compact path has never carried sub-second
        # precision, which is unrelated to this change.
        plain = codec.decode(codec.encode(PLI), now=NOW)
        self.assertEqual(self.stale_of(plain),
                         codec._parse_ts(ET.fromstring(PLI).get("stale")))

    def test_it_never_shortens_a_longer_stale(self):
        """A sender that deliberately set a long stale -- an alert meant to
        persist -- must keep it."""
        far = int(time.time()) + 86400
        self.assertEqual(codec._stale_at(far - 10, 10, 600), far)

    def test_expire_marks_an_event_already_stale(self):
        xml = codec.decode(codec.encode(PLI), min_stale_s=600)
        expired = codec.expire(xml)
        self.assertLess(self.stale_of(expired), int(time.time()) + 1)

    def test_expire_changes_nothing_else(self):
        """It has to be the SAME item, or ATAK expires a different marker and
        leaves the real one on the map."""
        xml = codec.decode(codec.encode(PLI), min_stale_s=600)
        before, after = ET.fromstring(xml), ET.fromstring(codec.expire(xml))
        for attr in ("uid", "type", "time", "start", "how"):
            self.assertEqual(before.get(attr), after.get(attr), attr)
        self.assertEqual(before.find("point").get("lat"),
                         after.find("point").get("lat"))


class TestBundling(unittest.TestCase):
    """Several frames in one packet. The saving is the packet header and the
    mesh envelope, paid once instead of once each."""

    def acks(self, n):
        return [codec.encode_ack("a1b2c3", i) for i in range(1, n + 1)]

    def test_round_trip(self):
        frames = self.acks(4)
        packed = codec.bundle(frames)
        self.assertTrue(codec.is_bundle(packed))
        self.assertEqual(codec.unbundle(packed), frames)

    def test_it_costs_one_byte_per_frame(self):
        frames = self.acks(4)
        packed = codec.bundle(frames)
        self.assertEqual(len(packed),
                         sum(len(f) for f in frames) + codec.bundle_overhead(4))

    def test_frames_keep_their_own_identity_inside(self):
        """Everything downstream must handle a bundled frame exactly as it
        would an unbundled one, so the frames must come out untouched."""
        for frame in codec.unbundle(codec.bundle(self.acks(3))):
            self.assertTrue(codec.is_ack(frame))
            self.assertEqual(codec.decode_ack(frame, "d4e5f6").origin, "a1b2c3")

    def test_bundles_do_not_nest(self):
        """A nested bundle would put frames one level below the hop-count rule
        bundle() enforces, and would cost a byte rather than save one."""
        inner = codec.bundle(self.acks(2))
        with self.assertRaises(codec.CotEncodeError):
            codec.bundle([inner, codec.encode_ack("a1b2c3", 9)])

    def test_a_single_frame_is_not_a_bundle(self):
        """Bundling one frame adds two bytes and saves nothing, so it is a
        caller mistake rather than a no-op."""
        with self.assertRaises(codec.CotEncodeError):
            codec.bundle(self.acks(1))

    def test_an_oversize_frame_is_refused_not_truncated(self):
        big = bytes([codec.CTRL_ZXML]) + b"x" * codec.BUNDLE_MAX_FRAME
        with self.assertRaises(codec.CotEncodeError):
            codec.bundle([big, codec.encode_ack("a1b2c3", 1)])

    def test_truncation_is_refused_rather_than_guessed(self):
        packed = codec.bundle(self.acks(3))
        for cut in range(2, len(packed)):
            trimmed = packed[:cut]
            try:
                got = codec.unbundle(trimmed)
            except codec.CotDecodeError:
                continue
            # Whatever survives must be whole frames, never a partial one.
            for frame in got:
                self.assertTrue(codec.is_ack(frame))

    def test_a_bundle_is_not_mistaken_for_anything_else(self):
        packed = codec.bundle(self.acks(2))
        for wrong in (codec.is_ack, codec.is_beacon, codec.is_partial,
                      codec.is_reliable):
            self.assertFalse(wrong(packed), wrong.__name__)


class TestShortTimestamps(unittest.TestCase):
    """v15 carries the low 24 bits of a unix time and rebuilds the rest from
    the receiver's clock."""

    def test_it_reconstructs_exactly_near_the_receiver_clock(self):
        for offset in (0, 1, -1, 3600, -3600, 86400 * 30, -86400 * 30):
            now = 1786000000
            ts = now + offset
            self.assertEqual(
                codec._time_from_low(ts & codec._TIME_MASK, now), ts, offset)

    def test_it_reconstructs_across_a_window_boundary(self):
        """The case a naive mask-and-add gets wrong: the sender's time and the
        receiver's clock sit on opposite sides of a multiple of 2^24."""
        boundary = (1786000000 // codec._TIME_MOD + 1) * codec._TIME_MOD
        for offset in (-5, -1, 0, 1, 5):
            ts = boundary + offset
            for now in (boundary - 60, boundary + 60):
                self.assertEqual(
                    codec._time_from_low(ts & codec._TIME_MASK, now), ts,
                    (offset, now - boundary))

    def test_the_window_is_wide_enough_to_be_safe(self):
        """Two bytes would have been 18 hours, and would have mis-dated an
        older event silently. Guard the reason three was chosen."""
        self.assertGreater(codec._TIME_MOD / 2 / 86400, 90)

    def test_a_real_event_keeps_its_own_time(self):
        xml = codec.decode(codec.encode(PLI), now=NOW)
        root = ET.fromstring(xml)
        self.assertEqual(root.get("time"), "2026-07-30T18:22:31.000Z")


class TestBeacon(unittest.TestCase):
    """The link probe. Rides the same destination as real traffic on purpose,
    so a beacon arriving proves the whole path CoT depends on."""

    def test_beacon_round_trip(self):
        frame = codec.encode_beacon("a1b2c3", 42)
        self.assertTrue(codec.is_beacon(frame))
        # node_id is supplied by the caller from the envelope since v15; the
        # frame itself no longer repeats it.
        b = codec.decode_beacon(frame, "a1b2c3")
        self.assertEqual((b.node_id, b.seq), ("a1b2c3", 42))
        self.assertIsNone(b.callsign)

    def test_beacon_carries_callsign(self):
        b = codec.decode_beacon(
            codec.encode_beacon("a1b2c3", 7, callsign="BRAVO01"), "a1b2c3")
        self.assertEqual(b.callsign, "BRAVO01")
        self.assertEqual(b.node_id, "a1b2c3")
        self.assertEqual(b.seq, 7)

    def test_beacon_carries_device_name(self):
        """The device name is the node's PRIMARY identity and must survive
        independently of the ATAK callsign -- when ATAK is what's broken, this
        is what still tells you which radio you're talking to."""
        b = codec.decode_beacon(
            codec.encode_beacon("a1b2c3", 7, name="Pixel 7"))
        self.assertEqual(b.name, "Pixel 7")
        self.assertIsNone(b.callsign)

    def test_beacon_carries_both_names_independently(self):
        b = codec.decode_beacon(
            codec.encode_beacon("a1b2c3", 7, callsign="BRAVO01", name="OnePlus CPH2451"))
        self.assertEqual(b.name, "OnePlus CPH2451")
        self.assertEqual(b.callsign, "BRAVO01")

    def test_beacon_without_callsign_field_still_decodes(self):
        """What an older sender's beacon looks like: node_id and seq, nothing
        after. Must degrade to callsign=None rather than raising."""
        stub = bytes([codec.CTRL_BEACON]) + b"\x00\x00\x00\x05"
        b = codec.decode_beacon(stub, "a1b2c3")
        self.assertEqual((b.node_id, b.seq, b.callsign, b.name),
                         ("a1b2c3", 5, None, None))

    def test_beacon_is_tiny(self):
        self.assertLessEqual(len(codec.encode_beacon("a1b2c3", 1)), 16)
        # A callsign is worth its handful of bytes for a readable node list.
        self.assertLessEqual(len(codec.encode_beacon("a1b2c3", 1, callsign="BRAVO01")), 24)

    def test_beacon_not_confused_with_cot(self):
        self.assertFalse(codec.is_beacon(codec.encode(PLI)))
        self.assertFalse(codec.is_beacon(codec.encode(GEOCHAT)))
        # decode() must refuse a beacon rather than emit bogus XML.
        self.assertRaises(codec.CotDecodeError, codec.decode, codec.encode_beacon("x", 1))

    def test_decode_beacon_is_total(self):
        for raw in [b"", b"\x13", b"\x13\x05", b"\x13\x02ab", b"\x10\x00",
                    codec.encode_beacon("x", 1)[:-1], os.urandom(20)]:
            try:
                codec.decode_beacon(raw)
            except codec.CotDecodeError:
                pass
            except Exception as e:
                self.fail("decode_beacon(%r) raised %s" % (raw[:16], type(e).__name__))

    def test_seq_wraps_without_error(self):
        frame = codec.encode_beacon("x", 2 ** 32 + 5)
        self.assertEqual(codec.decode_beacon(frame)[1], 5)

    def test_beacon_carries_a_piggybacked_position_frame(self):
        # Opaque on purpose -- encode_beacon does not care what `position`
        # is, only that it round-trips. rns_bridge.py is what gives it
        # meaning (an already-encoded compact position delta).
        payload = b"\x11\x02\xab\xcd\xef"
        b = codec.decode_beacon(
            codec.encode_beacon("a1b2c3", 3, position=payload), "a1b2c3")
        self.assertEqual(b.position, payload)

    def test_a_beacon_with_nothing_to_piggyback_is_unchanged(self):
        # The whole point of appending it last: a beacon that has no
        # position to carry is byte-for-byte what it was before this field
        # existed.
        with_none = codec.encode_beacon("a1b2c3", 3, callsign="BRAVO01")
        with_empty = codec.encode_beacon("a1b2c3", 3, callsign="BRAVO01",
                                         position=b"")
        self.assertEqual(with_none, with_empty)
        b = codec.decode_beacon(with_none, "a1b2c3")
        self.assertIsNone(b.position)

    def test_position_survives_alongside_heard_tags(self):
        # The two most recently added optional fields, both appended in
        # order -- a real risk of one shadowing or truncating the other.
        tags = [codec.heard_tag("x", 1), codec.heard_tag("x", 2)]
        payload = b"\x11\x00\x01\x02"
        b = codec.decode_beacon(
            codec.encode_beacon("a1b2c3", 3, heard=tags, position=payload),
            "a1b2c3")
        self.assertEqual(b.heard, tags)
        self.assertEqual(b.position, payload)

    def test_an_older_decoder_shape_ignores_the_new_field_gracefully(self):
        # decode_beacon() itself (not a hypothetical older build) must not
        # choke on a beacon padded past the position field -- the same
        # tolerance test_beacon_without_callsign_field_still_decodes already
        # covers for the fields before it.
        frame = codec.encode_beacon("a1b2c3", 3, position=b"\x11\x02\xab")
        truncated = frame[:-1]
        b = codec.decode_beacon(truncated, "a1b2c3")
        self.assertIsNone(b.position)


class TestBeaconRangeTestFix(unittest.TestCase):
    """The fix a range-testing node puts on every beacon (2026-11-08) -- the
    drive that day had 4 usable rows of 25 because the far end's copy of the
    moving phone's position was 300-400 m stale."""

    LAT, LON, TAKEN = 20.9089800, -140.5829100, 1794196000

    def _fix(self):
        return codec.pack_beacon_fix(self.LAT, self.LON, self.TAKEN)

    def test_round_trip(self):
        b = codec.decode_beacon(
            codec.encode_beacon("a1b2c3", 9, callsign="BRAVO01",
                                name="Pixel 7", fix=self._fix()), "a1b2c3")
        self.assertAlmostEqual(b.fix.lat, self.LAT, places=7)
        self.assertAlmostEqual(b.fix.lon, self.LON, places=7)
        self.assertEqual(b.fix.time, self.TAKEN)
        # The fields before it are untouched -- what an older decoder reads.
        self.assertEqual((b.seq, b.callsign, b.name), (9, "BRAVO01", "Pixel 7"))
        self.assertEqual(b.heard, [])
        self.assertIsNone(b.position)

    def test_it_coexists_with_heard_and_position(self):
        """Three appended fields in a fixed read order: each must land in its
        own slot, with nothing shadowing the next."""
        tags = [codec.heard_tag("x", 1)]
        payload = b"\x11\x00\x01\x02"
        b = codec.decode_beacon(codec.encode_beacon(
            "a1b2c3", 3, heard=tags, position=payload, fix=self._fix()))
        self.assertEqual(b.heard, tags)
        self.assertEqual(b.position, payload)
        self.assertEqual(b.fix.time, self.TAKEN)

    def test_no_fix_leaves_the_beacon_byte_for_byte_as_before(self):
        self.assertEqual(codec.encode_beacon("a1b2c3", 3, callsign="B"),
                         codec.encode_beacon("a1b2c3", 3, callsign="B", fix=None))
        self.assertIsNone(codec.decode_beacon(
            codec.encode_beacon("a1b2c3", 3, callsign="B")).fix)

    def test_it_costs_thirteen_bytes(self):
        """The figure the FM budget below and the docstring rely on."""
        plain = codec.encode_beacon("a1b2c3", 3, heard=[codec.heard_tag("x", 1)],
                                    position=b"\x11\x02")
        with_fix = codec.encode_beacon("a1b2c3", 3, heard=[codec.heard_tag("x", 1)],
                                       position=b"\x11\x02", fix=self._fix())
        self.assertEqual(len(with_fix) - len(plain), 1 + codec.BEACON_FIX_LEN)
        self.assertEqual(codec.BEACON_FIX_LEN, 12)

    def test_the_worst_case_beacon_with_a_fix_fits_fm(self):
        """Largest name and callsign, three heard tags, a piggybacked delta and
        the fix together still fit the FM operating point's 113 B budget."""
        import fm_config
        delta = b"\x00" * 24            # generous for a one-packet delta
        frame = codec.encode_beacon(
            "a1b2c3", 2 ** 31, callsign="C" * 12, name="N" * 16,
            heard=[codec.heard_tag("x", i) for i in range(3)],
            position=delta, fix=self._fix())
        # 12 B: the mesh envelope around it.
        self.assertLessEqual(len(frame) + 12,
                             fm_config.OPERATING_POINT.capacity_bytes())

    def test_impossible_fixes_are_refused_at_the_sender(self):
        for lat, lon, t in ((91.0, 0.0, 1), (0.0, 181.0, 1), (1.0, 1.0, -1),
                            (None, 1.0, 1), (1.0, 1.0, 2 ** 32)):
            with self.assertRaises(codec.CotEncodeError, msg=(lat, lon, t)):
                codec.pack_beacon_fix(lat, lon, t)

    def test_a_field_that_is_not_one_fix_decodes_as_no_fix(self):
        self.assertIsNone(codec.unpack_beacon_fix(b""))
        self.assertIsNone(codec.unpack_beacon_fix(b"\x00" * 11))
        self.assertIsNone(codec.unpack_beacon_fix(b"\x00" * 13))
        # Well-formed length, impossible latitude.
        self.assertIsNone(codec.unpack_beacon_fix(
            struct.pack(">iiI", 95 * 10 ** 7, 0, 1)))

    def test_a_truncated_fix_does_not_break_the_beacon(self):
        frame = codec.encode_beacon("a1b2c3", 3, callsign="B", fix=self._fix())
        b = codec.decode_beacon(frame[:-1])
        self.assertEqual((b.seq, b.callsign), (3, "B"))
        self.assertIsNone(b.fix)


class TestChatUidParsing(unittest.TestCase):
    """ATAK chat uids carry their own routing, which is what lets a direct
    chat demand an ack only from the node it was addressed to."""

    DIRECT = ("GeoChat.ANDROID-0123456789abcdef.ANDROID-fedcba9876543210."
              "1b9c5f84-f277-49a9-83cf-52a4f8e409cb")
    ROOM = ("GeoChat.ANDROID-0123456789abcdef.All Chat Rooms."
            "4ea962ff-ccc9-4a1b-a0d2-17379d972078")

    def test_direct_message_fields(self):
        self.assertEqual(codec.chat_sender(self.DIRECT), "ANDROID-0123456789abcdef")
        self.assertEqual(codec.chat_addressee(self.DIRECT), "ANDROID-fedcba9876543210")

    def test_room_message_destination_is_the_room(self):
        # Returned raw. It will simply fail to match any known node, which is
        # what makes a room message fall back to expecting every peer.
        self.assertEqual(codec.chat_addressee(self.ROOM), "All Chat Rooms")

    def test_non_chat_uids_yield_nothing(self):
        for uid in ("ANDROID-0123456789abcdef", "", None, "GeoChat", "GeoChat.a.b",
                    "b-t-f", "GeoChat..", "x.y.z.w"):
            self.assertIsNone(codec.chat_addressee(uid), repr(uid))
            self.assertIsNone(codec.chat_sender(uid), repr(uid))

    def test_room_name_containing_dots_still_parses_a_destination(self):
        # Over-splitting must not raise or silently shift the fields.
        uid = "GeoChat.ANDROID-1.Team.Alpha.guid-here"
        self.assertEqual(codec.chat_sender(uid), "ANDROID-1")
        self.assertEqual(codec.chat_addressee(uid), "Team")

    def test_parsing_never_raises(self):
        for uid in (None, "", "GeoChat.", "GeoChat...", "." * 50, "GeoChat" + "." * 8):
            codec.chat_sender(uid)
            codec.chat_addressee(uid)


class TestFragmentIdentityAcrossRetries(unittest.TestCase):
    """A retransmission has to be the SAME message to the receiver.

    fragment() picks a random id per call, and the Reassembler keys on
    (msg_id, count), so before fragment_reliable() a retry began a second
    reassembly and orphaned the first -- every fragment that had already
    arrived was thrown away, and the abandoned partial held one of the sixteen
    slots until it timed out. A five-fragment drawing that lost one piece had
    to land all five again.
    """

    def _frag_id(self, packet):
        return struct.unpack(">HBB", packet[1:5])[0]

    def _wrapped(self, msg_id=0x4242):
        return codec.wrap_reliable(codec.encode(DRAWING), "a1b2c3",
                                   msg_id=msg_id)

    def test_the_fragment_id_is_stable_across_retransmissions(self):
        _, wrapped = self._wrapped()
        first = codec.fragment_reliable(wrapped, mtu=120)
        again = codec.fragment_reliable(wrapped, mtu=120)
        self.assertGreater(len(first), 1)
        self.assertEqual(self._frag_id(first[0]), self._frag_id(again[0]))

    def test_the_fragment_id_is_the_message_id_the_ack_names(self):
        """Anything else would need a second identity on the wire to correlate
        the two, for no gain -- this one is already unique among the sender's
        in-flight messages."""
        msg_id, wrapped = self._wrapped(msg_id=0x1234)
        packets = codec.fragment_reliable(wrapped, mtu=120)
        self.assertEqual(self._frag_id(packets[0]), msg_id)

    def test_a_retry_completes_a_partial_instead_of_starting_over(self):
        _, wrapped = self._wrapped()
        packets = codec.fragment_reliable(wrapped, mtu=120)
        self.assertGreater(len(packets), 2)

        r = codec.Reassembler()
        # First attempt: everything but the last fragment arrives.
        for p in packets[:-1]:
            self.assertIsNone(r.offer(p))
        # The retry re-sends all of them; the ones already held are recognised
        # as duplicates and the message completes on the missing one.
        out = None
        for p in codec.fragment_reliable(wrapped, mtu=120):
            got = r.offer(p)
            if got is not None:
                out = got
        self.assertEqual(out, wrapped)
        self.assertEqual(r.stats()["duplicates"], len(packets) - 1)

    def test_a_retry_leaves_no_orphaned_partial_behind(self):
        """The abandoned reassembly used to hold one of sixteen slots for the
        full timeout, so a few retries could evict live messages."""
        _, wrapped = self._wrapped()
        r = codec.Reassembler()
        packets = codec.fragment_reliable(wrapped, mtu=120)
        r.offer(packets[0])
        for p in codec.fragment_reliable(wrapped, mtu=120):
            r.offer(p)
        self.assertEqual(r.stats()["pending"], 0)

    def test_fire_and_forget_traffic_keeps_a_random_id(self):
        """A position is never retried, so there is nothing to correlate, and
        two senders sharing a fixed id would collide forever."""
        frame = codec.encode(DRAWING)
        self.assertNotEqual(self._frag_id(codec.fragment_reliable(frame, 120)[0]),
                            self._frag_id(codec.fragment_reliable(frame, 120)[0]))

    def test_an_unfragmented_message_is_unaffected(self):
        _, wrapped = codec.wrap_reliable(codec.encode(PLI), "a1b2c3")
        self.assertEqual(len(codec.fragment_reliable(wrapped)), 1)


class TestPartialAck(unittest.TestCase):
    """"I hold these fragments, send me the rest." ~20 bytes against the ~0.4 s
    a redundant fragment costs, so it pays for itself the first time it
    prevents one."""

    def test_round_trip(self):
        frame = codec.encode_partial("a1b2c3", 0x1234, "d4e5f6", 5, {0, 1, 3})
        got = codec.decode_partial(frame, "d4e5f6")
        self.assertEqual((got.origin, got.msg_id, got.acker, got.count),
                         ("a1b2c3", 0x1234, "d4e5f6", 5))
        self.assertEqual(got.have, frozenset({0, 1, 3}))

    def test_it_stays_small_enough_to_send_speculatively(self):
        frame = codec.encode_partial("a1b2c3", 0xFFFF, "d4e5f6",
                                     codec.MAX_FRAGMENTS,
                                     set(range(codec.MAX_FRAGMENTS)))
        self.assertLessEqual(len(frame), 24)

    def test_every_fragment_index_survives(self):
        have = set(range(codec.MAX_FRAGMENTS))
        got = codec.decode_partial(
            codec.encode_partial("0a0b0c", 1, "0d0e0f", codec.MAX_FRAGMENTS, have))
        self.assertEqual(got.have, frozenset(have))

    def test_holding_nothing_is_expressible(self):
        got = codec.decode_partial(codec.encode_partial("0a0b0c", 1, "0d0e0f", 4, set()))
        self.assertEqual(got.have, frozenset())

    def test_it_is_not_an_ack_and_an_ack_is_not_it(self):
        """The two say opposite things about whether a message arrived, and
        confusing them reports a delivery that never happened. A distinct type
        is what makes that a decode error rather than a silent misread."""
        partial = codec.encode_partial("0a0b0c", 1, "0d0e0f", 4, {0})
        ack = codec.encode_ack("0a0b0c", 1, "0d0e0f")
        self.assertTrue(codec.is_partial(partial))
        self.assertFalse(codec.is_ack(partial))
        self.assertTrue(codec.is_ack(ack))
        self.assertFalse(codec.is_partial(ack))
        self.assertRaises(codec.CotDecodeError, codec.decode_ack, partial)
        self.assertRaises(codec.CotDecodeError, codec.decode_partial, ack)

    def test_truncation_is_refused_rather_than_guessed(self):
        frame = codec.encode_partial("a1b2c3", 1, "d4e5f6", 4, {0, 2})
        for cut in range(1, len(frame)):
            self.assertRaises(codec.CotDecodeError,
                              codec.decode_partial, frame[:cut])

    def test_a_nonsense_fragment_count_is_refused(self):
        frame = bytearray(codec.encode_partial("0a0b0c", 1, "0d0e0f", 4, {0}))
        frame[-3] = 0
        self.assertRaises(codec.CotDecodeError, codec.decode_partial, bytes(frame))
        frame[-3] = codec.MAX_FRAGMENTS + 1
        self.assertRaises(codec.CotDecodeError, codec.decode_partial, bytes(frame))


class TestStalledReassembly(unittest.TestCase):
    """When to speak up. A stall is the only reliable moment: reporting on the
    last fragment never fires when the last fragment is the one lost, and
    reporting on a duplicate only fires once the sender has already begun
    resending everything."""

    def setUp(self):
        self.clock = FakeClock()
        self.r = codec.Reassembler(clock=self.clock)
        _, self.wrapped = codec.wrap_reliable(codec.encode(DRAWING), "a1b2c3",
                                              msg_id=0x4242)
        self.packets = codec.fragment_reliable(self.wrapped, mtu=120)
        self.assertGreater(len(self.packets), 2)

    def test_nothing_is_reported_while_fragments_are_still_arriving(self):
        for p in self.packets[:-1]:
            self.r.offer(p, sender="a1b2c3")
            self.clock.advance(0.1)
        self.assertEqual(self.r.stalled(1.5), [])

    def test_a_stalled_message_names_what_is_missing(self):
        for p in self.packets[:-1]:
            self.r.offer(p, sender="a1b2c3")
        self.clock.advance(2.0)
        stalled = self.r.stalled(1.5)
        self.assertEqual(len(stalled), 1)
        sender, msg_id, count, have = stalled[0]
        self.assertEqual((sender, msg_id, count), ("a1b2c3", 0x4242,
                                                   len(self.packets)))
        self.assertEqual(sorted(set(range(count)) - have), [count - 1])

    def test_it_reports_once_per_stall_not_once_per_tick(self):
        """Otherwise a message stuck behind a lost fragment emits a report
        every tick for the whole reassembly timeout."""
        self.r.offer(self.packets[0], sender="a1b2c3")
        self.clock.advance(2.0)
        self.assertEqual(len(self.r.stalled(1.5)), 1)
        for _ in range(5):
            self.clock.advance(2.0)
            self.assertEqual(self.r.stalled(1.5), [])

    def test_a_new_fragment_earns_a_fresh_report(self):
        self.r.offer(self.packets[0], sender="a1b2c3")
        self.clock.advance(2.0)
        self.r.stalled(1.5)
        self.r.offer(self.packets[1], sender="a1b2c3")
        self.clock.advance(2.0)
        self.assertEqual(len(self.r.stalled(1.5)), 1)

    def test_a_completed_message_is_never_reported(self):
        for p in self.packets:
            self.r.offer(p, sender="a1b2c3")
        self.clock.advance(10.0)
        self.assertEqual(self.r.stalled(1.5), [])

    def test_a_late_fragment_of_a_finished_message_is_not_a_new_one(self):
        """Selective retransmission is broadcast, so a retry aimed at the one
        peer still missing a fragment is heard by every peer that already has
        the message. Without this each of them opened a reassembly holding that
        single fragment and asked the sender for the rest of something they had
        already delivered -- seen on the 2026-08-12 bench, seven seconds after
        the node in question had passed the whole route to ATAK."""
        for p in self.packets:
            self.r.offer(p, sender="a1b2c3")
        self.assertEqual(self.r.stats()["completed"], 1)

        # The straggler's retry arrives here too.
        self.assertIsNone(self.r.offer(self.packets[1], sender="a1b2c3"))
        self.assertEqual(self.r.stats()["pending"], 0)
        self.clock.advance(10.0)
        self.assertEqual(self.r.stalled(1.5), [])

    def test_a_genuinely_new_message_still_reassembles_after_one_completes(self):
        """The memory is per message, not a blanket refusal of anything seen."""
        for p in self.packets:
            self.r.offer(p, sender="a1b2c3")
        _, other = codec.wrap_reliable(codec.encode(DRAWING), "a1b2c3",
                                       msg_id=0x9999)
        out = None
        for p in codec.fragment_reliable(other, mtu=120):
            got = self.r.offer(p, sender="a1b2c3")
            if got is not None:
                out = got
        self.assertEqual(out, other)

    def test_a_completion_is_forgotten_once_no_retry_could_carry_it(self):
        for p in self.packets:
            self.r.offer(p, sender="a1b2c3")
        self.clock.advance(self.r._timeout + 1)
        self.assertIsNone(self.r.offer(self.packets[0], sender="a1b2c3"))
        self.assertEqual(self.r.stats()["pending"], 1)

    def test_nothing_is_reported_without_a_sender_to_ask(self):
        self.r.offer(self.packets[0])
        self.clock.advance(2.0)
        self.assertEqual(self.r.stalled(1.5), [])


class TestReliableWireFormat(unittest.TestCase):
    """The acknowledgement wrapper and ack frame (milestone 4).

    These carry no CoT of their own -- the wrapper is a header in front of an
    ordinary frame, and the ack is pure bookkeeping. The state machine that
    uses them lives in reliable.py and is tested separately.
    """

    def test_wrap_round_trip(self):
        inner = codec.encode(GEOCHAT)
        msg_id, wrapped = codec.wrap_reliable(inner, "a1b2c3")
        self.assertTrue(codec.is_reliable(wrapped))
        r = codec.unwrap_reliable(wrapped, "a1b2c3")
        self.assertEqual(r.origin, "a1b2c3")
        self.assertEqual(r.msg_id, msg_id)
        self.assertEqual(r.frame, inner)
        # ...and the unwrapped frame is exactly what decode() expects.
        self.assertEqual(codec.decode(r.frame), codec.decode(inner))

    def test_explicit_msg_id_is_honoured(self):
        # 15 bits, not 16: the top one carries the any-ack flag. 0x3EEF here
        # rather than the 0xBEEF this used to use, which is the same value
        # with the flag bit set.
        msg_id, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c", 0x3EEF)
        self.assertEqual(msg_id, 0x3EEF)
        self.assertEqual(codec.unwrap_reliable(wrapped).msg_id, 0x3EEF)

    def test_the_any_ack_flag_survives_the_round_trip(self):
        for any_ack in (True, False):
            _, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c", 0x3EEF,
                                             any_ack=any_ack)
            self.assertEqual(codec.unwrap_reliable(wrapped).any_ack, any_ack)

    def test_the_flag_bit_never_leaks_into_the_msg_id(self):
        """The id the sender tracks and the id an ack names have to be the
        same number, or every acknowledgement misses."""
        msg_id, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c",
                                              0xBEEF, any_ack=True)
        self.assertEqual(msg_id, 0xBEEF & codec.MSG_ID_MASK)
        r = codec.unwrap_reliable(wrapped)
        self.assertEqual(r.msg_id, msg_id)
        self.assertTrue(r.any_ack)
        ack = codec.decode_ack(codec.encode_ack("0a0b0c", msg_id, "0d0e0f"))
        self.assertEqual(ack.msg_id, msg_id)

    def test_a_random_msg_id_never_sets_the_flag_bit(self):
        """wrap_reliable generates its own id when not given one, and that id
        must not accidentally claim any-ack."""
        for _ in range(200):
            msg_id, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c")
            self.assertEqual(msg_id & codec.ANY_ACK_BIT, 0)
            self.assertFalse(codec.unwrap_reliable(wrapped).any_ack)

    def test_overhead_matches_reality(self):
        inner = codec.encode(PLI)
        _, wrapped = codec.wrap_reliable(inner, "a1b2c3")
        self.assertEqual(len(wrapped) - len(inner),
                         codec.reliable_overhead("a1b2c3"))

    def test_an_addressed_wrapper_names_its_acker(self):
        inner = codec.encode(PLI)
        _, wrapped = codec.wrap_reliable(inner, "0a0b0c", 0x3EEF,
                                         addressee="0d0e0f")
        r = codec.unwrap_reliable(wrapped)
        self.assertEqual(r.addressee, "0d0e0f")
        self.assertEqual(r.msg_id, 0x3EEF)
        self.assertFalse(r.any_ack)
        # Still just a wrapper: the frame inside is untouched.
        self.assertEqual(codec.decode(r.frame), codec.decode(inner))

    def test_an_unaddressed_wrapper_names_nobody(self):
        """None, not empty string. A bystander decides whether to stay quiet by
        testing this field, and "" would have to be special-cased everywhere it
        is read."""
        _, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c")
        self.assertIsNone(codec.unwrap_reliable(wrapped).addressee)

    def test_addressed_and_any_ack_cannot_both_be_claimed(self):
        """They are contradictory instructions -- "anyone may answer" against
        "exactly this node must" -- and a receiver holding both has no rule."""
        with self.assertRaises(codec.CotEncodeError):
            codec.wrap_reliable(codec.encode(PLI), "0a0b0c", 1,
                                any_ack=True, addressee="0d0e0f")

    def test_an_addressed_frame_is_still_a_reliable_frame(self):
        """Everything upstream of the addressee check routes on is_reliable(),
        so a frame that fails this test is not merely un-acked -- it is never
        unwrapped and never reaches ATAK at all."""
        _, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c", 1,
                                         addressee="0d0e0f")
        self.assertTrue(codec.is_reliable(wrapped))
        self.assertFalse(codec.is_ack(wrapped))

    def test_addressed_overhead_matches_reality(self):
        inner = codec.encode(PLI)
        _, wrapped = codec.wrap_reliable(inner, "a1b2c3", addressee="d4e5f6")
        self.assertEqual(len(wrapped) - len(inner),
                         codec.reliable_overhead("a1b2c3", "d4e5f6"))
        # And it really is bigger, so a caller that forgets to pass the
        # addressee undersizes the header rather than harmlessly over-sizing.
        self.assertGreater(codec.reliable_overhead("a1b2c3", "d4e5f6"),
                           codec.reliable_overhead("a1b2c3"))

    def test_an_addressed_frame_naming_nobody_is_rejected(self):
        """An empty name would silence every receiver including the one that
        owes the answer, so it must not decode to "unaddressed"."""
        inner = codec.encode(PLI)
        bad = (bytes([codec.CTRL_RELIABLE_TO]) + b"\x00\x01"
               + b"\x00\x00\x00" + inner)
        with self.assertRaises(codec.CotDecodeError):
            codec.unwrap_reliable(bad)

    def test_ack_round_trip(self):
        frame = codec.encode_ack("a1b2c3", 4242, "d4e5f6")
        self.assertTrue(codec.is_ack(frame))
        # acker is the envelope's authenticated sender since v15, not a field.
        ack = codec.decode_ack(frame, "d4e5f6")
        self.assertEqual((ack.origin, ack.msg_id, ack.acker),
                         ("a1b2c3", 4242, "d4e5f6"))

    def test_v15_sizes_are_what_the_bump_was_for(self):
        """The whole point of the v15 layout change. Guard the numbers: these
        are what the airtime estimates were built on."""
        self.assertEqual(len(codec.encode_ack("a1b2c3", 1, "d4e5f6")), 6)
        _, wrapped = codec.wrap_reliable(b"\x11x", "a1b2c3")
        self.assertEqual(len(wrapped) - 2, 3)          # ctrl + msg_id only
        _, addressed = codec.wrap_reliable(b"\x11x", "a1b2c3",
                                           addressee="d4e5f6")
        self.assertEqual(len(addressed) - len(wrapped), codec.NODE_ID_LEN)

    def test_a_node_id_that_is_not_three_bytes_is_refused(self):
        """Raw ids have no length prefix, so a wrong-sized one would silently
        eat the bytes after it instead of failing."""
        for bad in ("abc", "a1", "a1b2c3d4", "zzzzzz", ""):
            with self.assertRaises(codec.CotEncodeError):
                codec.encode_ack(bad, 1)

    def test_ack_is_tiny(self):
        # The whole argument for broadcast-plus-ack over a Link per peer is
        # that the acknowledgement is nearly free. Guard the number.
        self.assertLessEqual(len(codec.encode_ack("a1b2c3", 1, "d4e5f6")), 20)

    def test_frame_types_are_distinguishable(self):
        pli = codec.encode(PLI)
        _, wrapped = codec.wrap_reliable(pli, "0a0b0c")
        ack = codec.encode_ack("0a0b0c", 1, "0d0e0f")
        beacon = codec.encode_beacon("0a0b0c", 1)
        for frame in (pli, ack, beacon):
            self.assertFalse(codec.is_reliable(frame))
        for frame in (pli, wrapped, beacon):
            self.assertFalse(codec.is_ack(frame))
        for frame in (pli, wrapped, ack):
            self.assertFalse(codec.is_beacon(frame))

    def test_decode_refuses_wrappers(self):
        # decode() must never be handed a wrapper -- the bridge unwraps first,
        # because it has to acknowledge before delivering. If one does reach
        # decode(), refusing is the only safe answer.
        _, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c")
        self.assertRaises(codec.CotDecodeError, codec.decode, wrapped)
        self.assertRaises(codec.CotDecodeError, codec.decode,
                          codec.encode_ack("0a0b0c", 1, "0d0e0f"))

    def test_empty_frame_refused(self):
        self.assertRaises(codec.CotEncodeError, codec.wrap_reliable, b"", "0a0b0c")

    def test_wrapper_with_no_payload_refused(self):
        # A header that decodes cleanly but carries nothing would otherwise be
        # acknowledged and then delivered as an empty event.
        header = codec.wrap_reliable(b"\x10", "0a0b0c")[1][:-1]
        self.assertRaises(codec.CotDecodeError, codec.unwrap_reliable, header)

    def test_reliable_message_survives_fragmentation(self):
        # A wrapped frame is fragmented like any other, so the receiver
        # reassembles first and acknowledges the whole message.
        inner = codec.encode(DRAWING)
        msg_id, wrapped = codec.wrap_reliable(inner, "a1b2c3")
        packets = codec.fragment(wrapped, 120)
        self.assertGreater(len(packets), 1)
        r = codec.Reassembler()
        out = [r.offer(p) for p in packets]
        whole = [x for x in out if x is not None]
        self.assertEqual(len(whole), 1)
        got = codec.unwrap_reliable(whole[0])
        self.assertEqual(got.msg_id, msg_id)
        self.assertEqual(got.frame, inner)

    def test_older_decoder_drops_rather_than_crashes(self):
        # The forward-compatibility story for a mixed-version bench: a build
        # that predates these types sees an unknown frame type and drops it
        # with a log line. No PROTOCOL_VERSION bump was needed, which is what
        # keeps positions working across the same version gap.
        _, wrapped = codec.wrap_reliable(codec.encode(PLI), "0a0b0c")
        self.assertEqual(wrapped[0] >> 4, codec.PROTOCOL_VERSION)
        with self.assertRaises(codec.CotDecodeError) as cm:
            codec.decode(wrapped)
        self.assertIn("frame type", str(cm.exception))


# A self-SA at a given minute and position, built from the same shape as PLI so
# the sizes here are directly comparable with test_pli_size_budget.
def self_sa(minute=0, lat=35.1234567, lon=-79.7654321, callsign="BRAVO01",
            uid="ANDROID-352214111111111", stale_s=135):
    ts = 1785000000 + 60 * minute
    return (
        '<event version="2.0" uid="%s" type="a-f-G-U-C" time="%s" start="%s" '
        'stale="%s" how="m-g">'
        '<point lat="%.7f" lon="%.7f" hae="123.45" ce="9.5" le="9999999.0"/>'
        '<detail>'
        '<takv os="29" version="5.1.0.0 (7d0d1b2).1738000000-CIV" '
        'device="GOOGLE PIXEL 6" platform="ATAK-CIV"/>'
        '<contact endpoint="*:-1:stcp" callsign="%s" phone=""/>'
        '<uid Droid="%s"/>'
        '<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
        '<__group role="Team Member" name="Cyan"/>'
        '<status battery="87"/>'
        '<track course="271.34" speed="1.2345"/>'
        '</detail></event>'
        % (uid, codec._format_ts(ts), codec._format_ts(ts),
           codec._format_ts(ts + stale_s), lat, lon, callsign, callsign)
    ).encode()


def ext_of(frame):
    """The extended flags word of a compact frame, or 0 if it has none."""
    frame = unsqueezed(frame)
    if not frame[1] & (codec.F_EXT >> 8):
        return 0
    return codec._read_uvarint(frame, 3)[0]


class TestSessionCompression(unittest.TestCase):
    """Milestone 6 step 2. A self-SA stops restating the 32 bytes of uid and
    callsign that never change, and delta-codes its position against a
    keyframe."""

    def setUp(self):
        self.tx = codec.SaTx()
        self.rx = codec.SaRx()

    def send(self, minute=0, **kw):
        return codec.encode(self_sa(minute, **kw), codec.RNS_MAX_PAYLOAD, self.tx)

    # --- the shape of a run ---

    def test_first_frame_binds_and_carries_identity(self):
        frame = self.send(0)
        self.assertTrue(ext_of(frame) & codec.X_BIND)
        self.assertFalse(ext_of(frame) & codec.X_BOUND)
        self.assertIn(b"ANDROID-352214111111111", unsqueezed(frame))

    def test_following_frames_are_bound_deltas(self):
        self.send(0)
        for minute in (1, 2, 3, 4):
            frame = self.send(minute)
            self.assertTrue(ext_of(frame) & codec.X_BOUND, minute)
            self.assertTrue(ext_of(frame) & codec.X_DELTA, minute)
            self.assertNotIn(b"ANDROID-352214111111111", unsqueezed(frame))
            self.assertNotIn(b"BRAVO01", unsqueezed(frame))

    def test_keyframe_returns_on_the_interval(self):
        kinds = []
        for minute in range(11):
            kinds.append(bool(ext_of(self.send(minute)) & codec.X_BIND))
        # A cycle is one keyframe and keyframe_every-1 deltas.
        self.assertEqual(kinds, [True, False, False, False, False] * 2 + [True])

    def test_a_bound_delta_is_a_third_of_a_full_position(self):
        full = len(codec.encode(self_sa(0)))          # no session
        self.send(0)
        delta = len(self.send(1))
        # Measured: 61 -> 21. The assertion is deliberately loose about the
        # exact figure and tight about the ratio, which is the thing the relay
        # budget in milestone 6 depends on.
        self.assertLess(delta * 2, full)
        self.assertLessEqual(delta, 30)

    # --- round trips ---

    def test_bound_delta_restores_everything(self):
        self.rx_decode(self.send(0))
        out = attrs(self.rx_decode(self.send(1, lat=35.2000000)))
        self.assertEqual(out.get("uid"), "ANDROID-352214111111111")
        self.assertEqual(out.find("detail/contact").get("callsign"), "BRAVO01")
        self.assertAlmostEqual(float(out.find("point").get("lat")), 35.2, places=7)
        self.assertEqual(out.get("time"), codec._format_ts(1785000060))
        self.assertEqual(
            codec._parse_ts(out.get("stale")) - codec._parse_ts(out.get("time")), 135)

    def rx_decode(self, frame):
        return codec.decode(frame, self.rx, now=NOW)

    def test_a_whole_run_round_trips(self):
        for minute in range(11):
            lat = 35.1234567 + 0.000757 * minute
            out = attrs(self.rx_decode(self.send(minute, lat=lat)))
            self.assertEqual(out.get("uid"), "ANDROID-352214111111111", minute)
            self.assertAlmostEqual(
                float(out.find("point").get("lat")), lat, places=7)

    # --- loss ---

    def test_losing_one_delta_does_not_desync_the_next(self):
        """The reason deltas reference the keyframe and not the previous frame.
        Against the previous frame this test fails for every frame up to the
        next keyframe, not just the lost one."""
        frames = [self.send(m, lat=35.1234567 + 0.000757 * m) for m in range(5)]
        self.rx_decode(frames[0])
        self.rx_decode(frames[1])
        # frames[2] never arrives
        out = attrs(self.rx_decode(frames[3]))
        self.assertAlmostEqual(float(out.find("point").get("lat")),
                               35.1234567 + 0.000757 * 3, places=7)

    def test_a_receiver_that_missed_the_keyframe_refuses(self):
        self.send(0)                      # keyframe, lost on the air
        frame = self.send(1)
        with self.assertRaises(codec.CotDecodeError) as cm:
            codec.decode(frame, codec.SaRx())
        self.assertIn("generation", str(cm.exception))

    def test_a_bound_frame_without_a_binding_refuses_rather_than_guesses(self):
        self.send(0)
        self.send(1)
        # An absolute-but-bound frame: the base is fine, the identity is not.
        frame = self.send(2, stale_s=200)
        self.assertTrue(ext_of(frame) & codec.X_BOUND)
        self.assertFalse(ext_of(frame) & codec.X_DELTA)
        with self.assertRaises(codec.CotDecodeError) as cm:
            codec.decode(frame, codec.SaRx())
        self.assertIn("never bound", str(cm.exception))

    def test_no_session_at_all_refuses_a_bound_frame(self):
        self.send(0)
        with self.assertRaises(codec.CotDecodeError):
            codec.decode(self.send(1))

    def test_recovery_needs_only_the_next_keyframe(self):
        cold = codec.SaRx()
        for minute in range(1, 5):
            self.send(minute)
        with self.assertRaises(codec.CotDecodeError):
            codec.decode(self.send(5), cold)
        # The 6th frame this session sends is a keyframe again.
        out = attrs(codec.decode(self.send(6), cold))
        self.assertEqual(out.get("uid"), "ANDROID-352214111111111")

    # --- what ends a delta run ---

    def test_a_changed_callsign_rebinds(self):
        self.send(0)
        self.send(1)
        frame = self.send(2, callsign="BRAVO02")
        self.assertTrue(ext_of(frame) & codec.X_BIND)
        self.assertIn(b"BRAVO02", unsqueezed(frame))
        out = attrs(codec.decode(frame, self.rx))
        self.assertEqual(out.find("detail/contact").get("callsign"), "BRAVO02")

    def test_a_changed_uid_rebinds(self):
        self.send(0)
        frame = self.send(1, uid="ANDROID-999999999999999")
        self.assertTrue(ext_of(frame) & codec.X_BIND)

    def test_a_changed_stale_stays_bound_but_goes_absolute(self):
        self.rx_decode(self.send(0))
        frame = self.send(1, stale_s=600)
        # A delta frame inherits the stale offset wholesale, so a change to it
        # has to open a new base -- but there is still nothing new to say about
        # the identity, so the frame stays bound.
        self.assertTrue(ext_of(frame) & codec.X_BOUND)
        self.assertFalse(ext_of(frame) & codec.X_DELTA)
        self.assertFalse(ext_of(frame) & codec.X_BIND)
        out = attrs(self.rx_decode(frame))
        self.assertEqual(
            codec._parse_ts(out.get("stale")) - codec._parse_ts(out.get("time")), 600)

    def test_force_keyframe_takes_effect_on_the_next_frame(self):
        self.send(0)
        self.assertFalse(ext_of(self.send(1)) & codec.X_BIND)
        self.tx.force_keyframe()
        self.assertTrue(ext_of(self.send(2)) & codec.X_BIND)

    def test_even_a_half_world_jump_still_deltas(self):
        """A delta is capped at 5 bytes per coordinate by the varint, against 4
        fixed -- and it replaces the 2-byte stale offset too. So the encoder's
        'is the delta actually cheaper' guard never fires on distance alone,
        and a node teleporting across the planet still sends a small frame."""
        self.rx_decode(self.send(0))
        frame = self.send(1, lat=-35.0, lon=140.0)
        self.assertTrue(ext_of(frame) & codec.X_DELTA)
        out = attrs(self.rx_decode(frame))
        self.assertAlmostEqual(float(out.find("point").get("lat")), -35.0, places=7)
        self.assertAlmostEqual(float(out.find("point").get("lon")), 140.0, places=7)

    def test_a_gap_beyond_the_base_lifetime_goes_absolute(self):
        self.send(0)
        frame = self.send(120)            # two hours later
        self.assertFalse(ext_of(frame) & codec.X_DELTA)

    # --- what must NOT be bound ---

    def test_a_marker_leaves_the_session_alone(self):
        frame = codec.encode(MARKER, codec.RNS_MAX_PAYLOAD, self.tx)
        self.assertEqual(ext_of(frame) & (codec.X_BIND | codec.X_BOUND
                                          | codec.X_DELTA), 0)
        self.assertIsNone(self.tx.uid)
        # The uid rides as 16 raw bytes (UID_UUID), not as text.
        self.assertIn(bytes([codec.UID_UUID])
                      + bytes.fromhex("0ef9c3a9bf5446e5965529956a0d80ba"),
                      unsqueezed(frame))

    def test_a_marker_between_positions_does_not_disturb_the_run(self):
        self.rx_decode(self.send(0))
        codec.encode(MARKER, codec.RNS_MAX_PAYLOAD, self.tx)
        out = attrs(self.rx_decode(self.send(1)))
        self.assertEqual(out.get("uid"), "ANDROID-352214111111111")

    def test_chat_never_leans_on_the_identity_binding(self):
        """Deliberate, and the reason a chat carries its sender's uid in full.

        A receiver missing the binding refuses the frame -- and would ack it
        anyway, because the ack is queued before decoding -- so the message
        would be reported DELIVERED having never been rendered. A position can
        afford that; another arrives within the minute. A chat cannot."""
        frame = codec.encode(GEOCHAT, codec.RNS_MAX_PAYLOAD)
        self.assertEqual(ext_of(frame) & (codec.X_BIND | codec.X_BOUND
                                          | codec.X_DELTA), 0)
        # ...and it decodes with no session at all.
        self.assertEqual(attrs(codec.decode(frame)).get("uid"),
                         attrs(GEOCHAT).get("uid"))

    # --- staging: encoding is not transmitting ---

    def test_a_fallback_leaves_the_session_untouched(self):
        self.send(0)
        before = (self.tx.gen, self.tx.since_keyframe, self.tx.ts)
        # A budget too small for any compact frame forces the zlib path.
        codec.encode(self_sa(1), 8, self.tx)
        self.assertEqual((self.tx.gen, self.tx.since_keyframe, self.tx.ts), before)

    def test_an_unencodable_event_leaves_the_session_untouched(self):
        self.send(0)
        before = (self.tx.gen, self.tx.since_keyframe, self.tx.ts)
        broken = self_sa(1).replace(b'<status battery="87"/>',
                                    b'<status battery="87" mood="grim"/>')
        codec.encode(broken, codec.RNS_MAX_PAYLOAD, self.tx)
        self.assertEqual((self.tx.gen, self.tx.since_keyframe, self.tx.ts), before)

    def test_a_self_sa_with_a_generic_child_still_binds_and_deltas(self):
        # pli_with_sensor in the Meshtastic fixtures: a position report with a
        # sensor cone. The cone rides generic; the identity binding and the
        # delta coding must carry on underneath it, or every such node pays a
        # keyframe on every report.
        rx = codec.SaRx()
        sensor = b'<sensor fov="45" range="100"/></detail>'
        frames = [codec.encode(self_sa(m, lat=35.1234567 + m * 1e-4)
                               .replace(b"</detail>", sensor),
                               codec.RNS_MAX_PAYLOAD, self.tx)
                  for m in range(3)]
        for frame in frames:
            self.assertIn(frame[0] & 0x0F,
                          (codec.T_COMPACT_X, codec.T_ZCOMPACT_X))
            out = attrs(codec.decode(frame, rx))
            self.assertEqual(out.get("uid"), "ANDROID-352214111111111")
            self.assertEqual(out.find("detail/sensor").attrib,
                             {"fov": "45", "range": "100"})
        self.assertTrue(ext_of(frames[0]) & codec.X_BIND)
        self.assertTrue(ext_of(frames[2]) & codec.X_DELTA)
        # ...and the cone costs its own bytes on top of the delta, no more.
        plain_tx = codec.SaTx()
        plain = [codec.encode(self_sa(m, lat=35.1234567 + m * 1e-4),
                              codec.RNS_MAX_PAYLOAD, plain_tx) for m in range(3)]
        self.assertLess(len(frames[2]) - len(plain[2]), 12)

    def test_encode_compact_alone_stages_without_committing(self):
        root, _ = codec._parse_event(self_sa(0))
        codec.encode_compact(root, self.tx)
        self.assertIsNone(self.tx.uid)          # staged, not applied
        self.tx.commit()
        self.assertEqual(self.tx.uid, "ANDROID-352214111111111")

    # --- the extended flags word ---

    def test_extended_flags_cost_one_byte(self):
        # Version 3 spent two bytes here unconditionally. Positions set X_BOUND
        # and would have paid it on every frame.
        self.send(0)
        frame = unsqueezed(self.send(1))
        self.assertTrue(frame[1] & (codec.F_EXT >> 8))
        self.assertLess(frame[3], 0x80)

    def test_marker_kept_its_extended_flags(self):
        frame = codec.encode(MARKER)
        self.assertTrue(ext_of(frame) & codec.X_REMARKS)
        self.assertTrue(ext_of(frame) & codec.X_USERICON)
        self.assertTrue(ext_of(frame) & codec.X_LINK)


class TestBeaconDigest(unittest.TestCase):
    """The neighbour digest rides the beacon rather than a position report --
    it is bridge metadata, not anything ATAK said, and the beacon is the
    bridge's own frame. Same reasoning that kept the device name out of the
    compact codec in step 2."""

    DIGEST = bytes([0x00, 0xFF, 0x10, 0x80, 0x7F, 0x01, 0xAB, 0xCD])

    def test_digest_round_trips(self):
        b = codec.decode_beacon(codec.encode_beacon(
            "a1b2c3", 7, callsign="BRAVO01", name="Pixel 7", digest=self.DIGEST))
        self.assertEqual(b.digest, self.DIGEST)
        self.assertEqual(b.callsign, "BRAVO01")
        self.assertEqual(b.name, "Pixel 7")

    def test_arbitrary_bytes_survive(self):
        # _lstr would not: it decodes as UTF-8 and a Bloom filter is not text.
        raw = bytes(range(8))
        self.assertEqual(
            codec.decode_beacon(codec.encode_beacon("a1", 1, digest=raw)).digest,
            raw)

    def test_a_beacon_without_one_reports_none(self):
        self.assertIsNone(codec.decode_beacon(codec.encode_beacon("a1", 1)).digest)

    def test_it_costs_nine_bytes(self):
        plain = len(codec.encode_beacon("a1b2c3", 7, callsign="B", name="P"))
        with_digest = len(codec.encode_beacon("a1b2c3", 7, callsign="B",
                                              name="P", digest=self.DIGEST))
        self.assertEqual(with_digest - plain, len(self.DIGEST))

    def test_a_padded_beacon_still_parses(self):
        # Padding lands after the digest, so the diagnostic size ladder and the
        # coverage test do not interfere with each other.
        b = codec.decode_beacon(codec.encode_beacon(
            "a1b2c3", 7, size=200, callsign="B", name="P", digest=self.DIGEST))
        self.assertEqual(b.digest, self.DIGEST)


DIRECT_CHAT = (
    b'<event version="2.0" uid="GeoChat.ANDROID-0123456789abcdef.'
    b'ANDROID-fedcba9876543210.ee992581-4e37-4218-801a-e71feec6c397" type="b-t-f" '
    b'time="2026-08-04T15:21:32.000Z" start="2026-08-04T15:21:32.000Z" '
    b'stale="2026-08-04T15:22:32.000Z" how="h-g-i-g-o">'
    b'<point lat="20.9089800" lon="-140.5829100" hae="381.0" ce="9.5" le="9999999.0"/>'
    b'<detail><__chat parent="RootContactGroup" groupOwner="false" '
    b'messageId="ee992581-4e37-4218-801a-e71feec6c397" chatroom="OnePlus" '
    b'id="ANDROID-fedcba9876543210" senderCallsign="pixel">'
    b'<chatgrp uid0="ANDROID-0123456789abcdef" uid1="ANDROID-fedcba9876543210" '
    b'id="ANDROID-fedcba9876543210"/>'
    b'</__chat><link uid="ANDROID-0123456789abcdef" type="a-f-G-U-C" relation="p-p"/>'
    b'<remarks source="BAO.F.ATAK.ANDROID-0123456789abcdef" '
    b'to="ANDROID-fedcba9876543210" '
    b'time="2026-08-04T15:21:32.000Z">direct message test</remarks>'
    b'<__serverdestination destinations="192.168.55.5:4242:tcp:ANDROID-0123456789abcdef"/>'
    b'</detail></event>'
)


class TestGeoChatCompact(unittest.TestCase):
    """A chat message was the largest frame on this link -- 286 B on air
    against 39 for a position -- and all of it went the zlib route because
    <__chat> and <__serverdestination> were unmodelled."""

    def test_a_room_message_takes_the_compact_path(self):
        self.assertTrue(compact_path(codec.encode(GEOCHAT)))

    def test_a_direct_message_takes_the_compact_path(self):
        self.assertTrue(compact_path(codec.encode(DIRECT_CHAT)))

    def test_it_is_less_than_half_what_zlib_managed(self):
        # zlib got 914 B of XML to 270; measured actual for this is 114.
        self.assertLess(len(codec.encode(GEOCHAT)), 135)
        self.assertEqual(len(codec.build_packets(GEOCHAT)), 1)

    def test_every_attribute_survives_a_room_message(self):
        before = attrs(GEOCHAT)
        after = attrs(codec.decode(codec.encode(GEOCHAT)))
        self.assertEqual(before.get("uid"), after.get("uid"))
        self.assertEqual(before.get("type"), after.get("type"))
        for path in ("detail/__chat", "detail/__chat/chatgrp", "detail/link"):
            self.assertEqual(before.find(path).attrib,
                             after.find(path).attrib, path)
        self.assertEqual(before.find("detail/remarks").text,
                         after.find("detail/remarks").text)
        self.assertEqual(before.find("detail/remarks").get("source"),
                         after.find("detail/remarks").get("source"))
        self.assertEqual(before.find("detail/remarks").get("to"),
                         after.find("detail/remarks").get("to"))

    def test_every_attribute_survives_a_direct_message(self):
        before = attrs(DIRECT_CHAT)
        after = attrs(codec.decode(codec.encode(DIRECT_CHAT)))
        self.assertEqual(before.get("uid"), after.get("uid"))
        for path in ("detail/__chat", "detail/__chat/chatgrp"):
            self.assertEqual(before.find(path).attrib,
                             after.find(path).attrib, path)

    def test_the_server_address_is_dropped(self):
        out = codec.decode(codec.encode(GEOCHAT))
        self.assertNotIn(b"__serverdestination", out)
        self.assertNotIn(b"192.168", out)

    def test_the_message_text_is_the_only_thing_that_grows(self):
        """Everything else in a chat is either the sender, the addressee or the
        message id, each sent exactly once."""
        short = GEOCHAT.replace(b"moving to phase line bravo, eta 10 mikes", b"ok")
        delta = (len(unsqueezed(codec.encode(GEOCHAT)))
                 - len(unsqueezed(codec.encode(short))))
        self.assertEqual(delta, len(b"moving to phase line bravo, eta 10 mikes") - 2)

    def test_unicode_in_a_message_survives(self):
        raw = GEOCHAT.replace(b"moving to phase line bravo, eta 10 mikes",
                              "moving — eta 10°".encode("utf-8"))
        out = attrs(codec.decode(codec.encode(raw)))
        self.assertEqual(out.find("detail/remarks").text, "moving — eta 10°")

    def test_chat_still_gets_acknowledged_delivery(self):
        # It is not a position, so milestone 4 still applies to it. Losing a
        # chat message loses it permanently, whatever it costs on the air.
        self.assertFalse(codec.summarize(GEOCHAT).is_position)

    # -- the shapes that must fall back rather than arrive mangled ----------

    def test_an_unknown_chat_attribute_goes_generic(self):
        assert_carried_exactly(self, 
            GEOCHAT.replace(b'<__chat parent=', b'<__chat spooky="yes" parent='),
            "an attribute we cannot rebuild")

    def test_a_message_id_that_disagrees_with_the_uid_goes_generic(self):
        assert_carried_exactly(self, 
            GEOCHAT.replace(b'messageId="7f3a2b19-1c4d-4e2a-9f88-2b7c1d0e5a63"',
                            b'messageId="something-else"'),
            "messageId is rebuilt from the uid, so it has to match it")

    def test_a_remarks_source_that_is_not_the_sender_goes_generic(self):
        assert_carried_exactly(self, 
            GEOCHAT.replace(b'source="BAO.F.ATAK.ANDROID-352214111111111"',
                            b'source="BAO.F.ATAK.SOMEBODY-ELSE"'),
            "source is rebuilt from the sender")

    def test_a_chatgrp_that_disagrees_with_the_uid_goes_generic(self):
        assert_carried_exactly(self, 
            GEOCHAT.replace(b'<chatgrp uid0="ANDROID-352214111111111"',
                            b'<chatgrp uid0="ANDROID-999999999999999"'),
            "uid0 is rebuilt from the uid's sender field")

    def test_a_room_name_containing_a_dot_goes_generic(self):
        assert_carried_exactly(self, 
            GEOCHAT.replace(b"All Chat Rooms", b"Team.Alpha"),
            "the uid could not be split back apart unambiguously")

    def test_an_extra_child_inside_chat_goes_generic(self):
        assert_carried_exactly(self, 
            GEOCHAT.replace(b"</__chat>", b"<hierarchy/></__chat>"),
            "a child we would silently drop")

    def test_a_link_that_is_not_the_sender_goes_generic(self):
        assert_carried_exactly(self, 
            GEOCHAT.replace(b'<link uid="ANDROID-352214111111111" type="a-f-G-U-C" '
                            b'relation="p-p"/>',
                            b'<link uid="OTHER" type="a-f-G-U-C" relation="p-p"/>'),
            "the link is rebuilt as the sender")


class TestChatUuidPacking(unittest.TestCase):

    def test_a_uuid_round_trips_as_sixteen_bytes(self):
        text = "7f3a2b19-1c4d-4e2a-9f88-2b7c1d0e5a63"
        raw = codec._pack_uuid(text)
        self.assertEqual(len(raw), 16)
        self.assertEqual(codec._unpack_uuid(raw), text)

    def test_anything_that_is_not_a_uuid_is_refused(self):
        for bad in ("", None, "not-a-uuid", "7f3a2b19-1c4d-4e2a-9f88",
                    "7F3A2B19-1C4D-4E2A-9F88-2B7C1D0E5A63"):
            self.assertIsNone(codec._pack_uuid(bad), bad)

    def test_a_non_uuid_message_id_still_encodes(self):
        raw = GEOCHAT.replace(b"7f3a2b19-1c4d-4e2a-9f88-2b7c1d0e5a63", b"0a0b0c")
        out = attrs(codec.decode(codec.encode(raw)))
        self.assertTrue(out.get("uid").endswith("0a0b0c"))
        self.assertEqual(out.find("detail/__chat").get("messageId"), "0a0b0c")


class TestDictionaryCompression(unittest.TestCase):
    """The compact encoding removes what a schema can remove. This removes what
    is redundant ACROSS frames -- that every marker carries a similar-looking
    uid, iconset path and creator block -- which is where the remaining factor
    of ~1.4x lives."""

    def test_frames_round_trip_through_the_squeeze(self):
        for name, raw in (("PLI", PLI), ("marker", MARKER),
                          ("room chat", GEOCHAT), ("direct chat", DIRECT_CHAT)):
            frame = codec.encode(raw)
            self.assertEqual(attrs(codec.decode(frame)).get("uid"),
                             attrs(raw).get("uid"), name)

    def test_the_squeezed_form_decodes_to_the_same_thing_as_the_plain_one(self):
        squeezed = codec.encode(MARKER)
        plain = unsqueezed(squeezed)
        self.assertEqual(squeezed[0] & 0x0F, codec.T_ZCOMPACT)
        self.assertEqual(codec.decode(squeezed), codec.decode(plain))

    def test_it_is_never_larger_than_the_frame_it_replaces(self):
        """The whole safety property. A dictionary that suits the traffic
        badly then costs nothing beyond the attempt, which is what makes it
        safe to ship one trained on synthetic frames and retrain later."""
        for raw in (PLI, MARKER, GEOCHAT, DIRECT_CHAT):
            frame = codec.encode(raw)
            self.assertLessEqual(len(frame), len(unsqueezed(frame)))

    def test_incompressible_bytes_are_left_alone(self):
        """Tested on the guard directly rather than by trying to build a CoT
        event that triggers it: a real event large enough to hold meaningful
        noise still has 140-odd bytes of highly compressible structure around
        it, so the compressed form wins anyway. The property that matters is
        that _squeeze declines when it does not."""
        rng = random.Random(5)
        noise = bytes(bytearray(rng.randrange(256) for _ in range(200)))
        frame = bytes([codec.CTRL_COMPACT]) + noise
        self.assertIs(codec._squeeze(frame), frame)

    def test_a_session_run_survives_compression(self):
        tx, rx = codec.SaTx(), codec.SaRx()
        for minute in range(11):
            lat = 35.1234567 + 0.000757 * minute
            frame = codec.encode(self_sa(minute, lat=lat), codec.RNS_MAX_PAYLOAD, tx)
            out = attrs(codec.decode(frame, rx))
            self.assertAlmostEqual(float(out.find("point").get("lat")), lat,
                                   places=7, msg=minute)

    def test_a_wrong_dictionary_fails_loudly_rather_than_quietly(self):
        """zlib carries the dictionary's Adler-32 in the stream header, which is
        what makes a mismatched build refuse rather than decode garbage. This is
        the property that lets the dictionary be retrained later."""
        frame = codec.encode(MARKER)
        self.assertEqual(frame[0] & 0x0F, codec.T_ZCOMPACT)
        real = codec.COMPACT_ZDICT
        try:
            codec.COMPACT_ZDICT = b"a different dictionary entirely" * 100
            with self.assertRaises(codec.CotDecodeError) as cm:
                codec.decode(frame)
            self.assertIn("zlib", str(cm.exception))
        finally:
            codec.COMPACT_ZDICT = real

    def test_truncation_is_refused_not_guessed(self):
        frame = codec.encode(MARKER)
        with self.assertRaises(codec.CotDecodeError):
            codec.decode(frame[:len(frame) // 2])

    def test_the_dictionary_is_the_size_it_claims(self):
        # Part of the wire format, so a change here is a change both ends must
        # make together -- pinned so it cannot drift silently.
        self.assertEqual(len(codec.COMPACT_ZDICT), 16384)

    def test_it_beats_the_uncompressed_form_on_real_shapes(self):
        # Measured on frames the dictionary has NOT seen: 1.33x on positions,
        # 1.47x on markers, 1.53x on chat. These fixtures are in the training
        # corpus so they flatter it; the assertion is only that it helps.
        # 0.9, not 0.8, since uids are packed to raw bytes: that removed the
        # most compressible text a marker had (a 36-character UUID) and left
        # 16 bytes of noise that nothing can squeeze.
        for raw in (MARKER, GEOCHAT):
            frame = codec.encode(raw)
            self.assertLess(len(frame), len(unsqueezed(frame)) * 0.9)


# Built from DrawingRectangle.toCot() in the ATAK source rather than guessed:
# four <link point=> corners, __shapeExtras, and the stroke/fill styling that
# StrokeFillDetailHandler adds. The 2026-08-05 bench reported exactly these
# tags for a u-d-r that fell back at 452 B on air.
RECTANGLE = (
    b'<event version="2.0" uid="c6eab758-76a6-442d-8c8e-b9888763411c" '
    b'type="u-d-r" how="h-e" time="2026-08-05T21:16:47.000Z" '
    b'start="2026-08-05T21:16:47.000Z" stale="2026-08-06T21:16:47.000Z">'
    b'<point lat="20.9089800" lon="-140.5829100" hae="381.0" ce="9.5" le="9999999.0"/>'
    b'<detail>'
    b'<link point="20.9109800,-140.5849100,381.0"/>'
    b'<link point="20.9109800,-140.5809100,381.0"/>'
    b'<link point="20.9069800,-140.5809100,381.0"/>'
    b'<link point="20.9069800,-140.5849100,381.0"/>'
    b'<__shapeExtras editable="true" cpvis="true"/>'
    b'<strokeColor value="-1"/><strokeWeight value="4.0"/>'
    b'<strokeStyle value="solid"/><fillColor value="-1761607681"/>'
    b'<contact callsign="Rectangle 1"/><archive/><labels_on value="false"/>'
    b'<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
    b'</detail></event>'
)


def corners(root):
    return [link.get("point") for link in root.findall("detail/link")]


class TestRepeatedDetailChildren(unittest.TestCase):
    """A shape's vertices are a repeated <link>, and a plain dict kept only the
    last of them -- so a rectangle silently lost three corners before this."""

    def test_a_repeated_link_is_collected_not_overwritten(self):
        root, _ = codec._parse_event(RECTANGLE)
        children, _ = codec._detail_children(root)
        self.assertEqual(len(children.all("link")), 4)

    def test_get_still_returns_the_first(self):
        root, _ = codec._parse_event(RECTANGLE)
        children, _ = codec._detail_children(root)
        self.assertIs(children.get("link"), children.all("link")[0])

    def test_a_tag_that_must_not_repeat_keeps_every_copy(self):
        # Only <link> is repeatable. Two <contact> is a shape of event this
        # codec has never seen, and keeping one of them would be a silent loss
        # -- so the typed encoding takes the first and the second rides as a
        # generic element.
        raw = MARKER.replace(b'<contact callsign="Marker 1"/>',
                             b'<contact callsign="A"/><contact callsign="B"/>')
        frame = codec.encode(raw)
        self.assertIn(frame[0] & 0x0F, (codec.T_COMPACT_X, codec.T_ZCOMPACT_X))
        out = attrs(codec.decode(frame))
        self.assertEqual([c.get("callsign") for c in out.findall("detail/contact")],
                         ["A", "B"])

    def test_a_marker_with_one_link_is_unaffected(self):
        # The regression that matters: a marker's single parent <link> carries
        # provenance and must keep going down the marker path.
        out = attrs(codec.decode(codec.encode(MARKER)))
        link = out.find("detail/link")
        self.assertEqual(link.get("relation"), "p-p")
        self.assertEqual(link.get("uid"), "ANDROID-0123456789abcdef")


class TestDrawnShapes(unittest.TestCase):

    def test_a_rectangle_takes_the_compact_path(self):
        self.assertTrue(compact_path(codec.encode(RECTANGLE)))

    def test_every_corner_survives_exactly(self):
        out = attrs(codec.decode(codec.encode(RECTANGLE)))
        self.assertEqual(corners(out), corners(attrs(RECTANGLE)))

    def test_the_styling_survives(self):
        before, after = attrs(RECTANGLE), attrs(codec.decode(codec.encode(RECTANGLE)))
        for tag in ("strokeColor", "strokeWeight", "strokeStyle", "fillColor",
                    "labels_on", "__shapeExtras"):
            self.assertEqual(before.find("detail/" + tag).attrib,
                             after.find("detail/" + tag).attrib, tag)

    def test_it_is_a_quarter_of_what_it_cost_before(self):
        # The bench measured 452 B on air for a real one. Measured actual here
        # is 122 B including the 16-byte mesh envelope.
        self.assertLess(len(codec.encode(RECTANGLE)) + 16, 160)

    def test_vertices_are_deltas_so_position_on_earth_does_not_matter(self):
        """Stored as offsets from the event's own point, so the same shape
        costs the same wherever it is drawn."""
        # A whole-degree shift, so every vertex moves by the same amount and
        # the deltas are unchanged. (Shifting by a fraction changes the spacing
        # between corners, which changes the deltas and is a different test.)
        far = (RECTANGLE.replace(b"20.9", b"70.9")
                        .replace(b"-140.5", b"-40.5"))
        self.assertNotEqual(far, RECTANGLE)
        self.assertEqual(len(unsqueezed(codec.encode(far))),
                         len(unsqueezed(codec.encode(RECTANGLE))))

    def test_a_polygon_with_many_vertices_still_fits_one_packet(self):
        points = b"".join(
            b'<link point="%s,%s,381.0"/>'
            % (("20.9075%03d" % i).encode(), ("-140.5733%03d" % i).encode())
            for i in range(40))
        raw = RECTANGLE.replace(
            b'<link point="20.9109800,-140.5849100,381.0"/>'
            b'<link point="20.9109800,-140.5809100,381.0"/>'
            b'<link point="20.9069800,-140.5809100,381.0"/>'
            b'<link point="20.9069800,-140.5849100,381.0"/>', points)
        frame = codec.encode(raw)
        self.assertTrue(compact_path(frame))
        self.assertEqual(len(codec.build_packets(raw)), 1)
        self.assertEqual(len(corners(attrs(codec.decode(frame)))), 40)

    def test_a_shape_without_altitudes_round_trips(self):
        raw = RECTANGLE.replace(b",381.0\"/>", b"\"/>")
        out = attrs(codec.decode(codec.encode(raw)))
        self.assertEqual(corners(out), corners(attrs(raw)))

    # -- what goes generic rather than arrive wrong ---------------------------

    def test_mixed_altitudes_go_generic(self):
        assert_carried_exactly(self, 
            RECTANGLE.replace(b'<link point="20.9069800,-140.5809100,381.0"/>',
                              b'<link point="20.9069800,-140.5809100"/>'),
            "a per-vertex altitude flag for something ATAK does not produce")

    def test_a_vertex_link_with_extra_attributes_goes_generic(self):
        assert_carried_exactly(self, 
            RECTANGLE.replace(b'<link point="20.9109800,-140.5849100,381.0"/>',
                              b'<link point="20.9109800,-140.5849100,381.0" '
                              b'uid="something"/>'),
            "an attribute we would drop")

    def test_an_unparseable_vertex_goes_generic(self):
        assert_carried_exactly(self, 
            RECTANGLE.replace(b'point="20.9109800,-140.5849100,381.0"',
                              b'point="over there"'),
            "a point we cannot read is not one to guess at")

    def test_a_style_tag_with_an_unexpected_attribute_goes_generic(self):
        assert_carried_exactly(self, 
            RECTANGLE.replace(b'<strokeWeight value="4.0"/>',
                              b'<strokeWeight value="4.0" units="px"/>'),
            "an attribute we would drop")

    def test_an_unknown_stroke_style_survives_as_itself(self):
        # The table escapes rather than refuses, because ATAK's own parser
        # falls back to solid for anything it does not know -- so carrying the
        # original string is strictly better than losing it.
        raw = RECTANGLE.replace(b'<strokeStyle value="solid"/>',
                                b'<strokeStyle value="squiggly"/>')
        out = attrs(codec.decode(codec.encode(raw)))
        self.assertEqual(out.find("detail/strokeStyle").get("value"), "squiggly")


class TestTableConvention(unittest.TestCase):
    """_table_byte reserves index 0 for "not in the table" and escapes to a
    string. Every table therefore has to start with a placeholder -- and
    version 8 shipped LINE_STYLE_TABLE without one, putting "solid" at index 0
    so that the value nearly every shape carries escaped to 6 bytes instead of
    1. The bench found it; this stops the next one being found the same way."""

    def tables(self):
        return {name: value for name, value in vars(codec).items()
                if name.endswith("_TABLE") and isinstance(value, list)}

    def test_every_table_reserves_index_zero(self):
        self.assertTrue(self.tables())
        for name, table in self.tables().items():
            self.assertEqual(table[0], "",
                             "%s[0] must be the empty escape sentinel" % name)

    def test_no_table_has_duplicates(self):
        for name, table in self.tables().items():
            self.assertEqual(len(table), len(set(table)),
                             "%s maps two indices to one value" % name)

    def test_a_solid_stroke_costs_one_byte(self):
        with CollectingHook() as hook:
            frame = codec.encode(RECTANGLE)
        self.assertNotIn("stroke_style_escape", hook.kinds())
        dashed = RECTANGLE.replace(b'value="solid"', b'value="dashed"')
        self.assertEqual(len(unsqueezed(frame)),
                         len(unsqueezed(codec.encode(dashed))))


# From DrawingCircle.toCot(). The style link's uid is the event's own with
# ".Style" appended, its type and relation never vary, and the KML nest carries
# three values inside ~150 bytes of ceremony.
CIRCLE = (
    b'<event version="2.0" uid="929534ac-1f17-415a-bd51-4919e2a07fc0" '
    b'type="u-d-c-c" how="h-e" time="2026-08-05T21:53:11.000Z" '
    b'start="2026-08-05T21:53:11.000Z" stale="2026-08-06T21:53:11.000Z">'
    b'<point lat="20.9089800" lon="-140.5829100" hae="381.0" ce="9.5" le="9999999.0"/>'
    b'<detail><shape>'
    b'<ellipse major="152.4" minor="152.4" angle="360.0"/>'
    b'<link type="b-x-KmlStyle" '
    b'uid="929534ac-1f17-415a-bd51-4919e2a07fc0.Style" relation="p-c">'
    b'<Style><LineStyle><color>ffffffff</color><width>4.0</width></LineStyle>'
    b'<PolyStyle><color>00000000</color></PolyStyle></Style></link>'
    b'<link relation="p-p-CenterAnchor" type="u-d-c-c" '
    b'uid="11111111-2222-3333-4444-555555555555"/>'
    b'<link relation="p-p-RadiusAnchor" type="u-d-c-c" '
    b'uid="66666666-7777-8888-9999-aaaaaaaaaaaa"/>'
    b'</shape>'
    b'<__shapeExtras editable="true" cpvis="true"/>'
    b'<contact callsign="Circle 1"/><archive/><labels_on value="false"/>'
    b'<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
    b'</detail></event>'
)


class TestCircles(unittest.TestCase):
    """A circle has no vertices -- a centre and radii, wrapped in what
    DrawingCircle's own comment calls "this dusty old format"."""

    def test_a_circle_takes_the_compact_path(self):
        self.assertTrue(compact_path(codec.encode(CIRCLE)))

    def test_it_fits_one_packet_where_it_used_to_need_two(self):
        # The 2026-08-05 bench measured 460 B on air across two packets.
        self.assertEqual(len(codec.build_packets(CIRCLE)), 1)
        self.assertLess(len(codec.encode(CIRCLE)) + 16, 200)

    def test_the_radii_and_angle_survive(self):
        out = attrs(codec.decode(codec.encode(CIRCLE)))
        self.assertEqual(out.find("detail/shape/ellipse").attrib,
                         attrs(CIRCLE).find("detail/shape/ellipse").attrib)

    def test_the_kml_style_nest_is_rebuilt_whole(self):
        before = attrs(CIRCLE).find("detail/shape")
        after = attrs(codec.decode(codec.encode(CIRCLE))).find("detail/shape")
        for path in ("link/Style/LineStyle/color", "link/Style/LineStyle/width",
                     "link/Style/PolyStyle/color"):
            self.assertEqual(before.find(path).text, after.find(path).text, path)

    def test_the_style_uid_is_derived_from_the_event(self):
        after = attrs(codec.decode(codec.encode(CIRCLE)))
        link = after.find("detail/shape/link")
        self.assertEqual(link.get("uid"), after.get("uid") + ".Style")

    def test_the_anchor_markers_survive(self):
        def anchors(root):
            return [(l.get("relation"), l.get("type"), l.get("uid"))
                    for l in root.findall("detail/shape/link")
                    if (l.get("relation") or "").startswith("p-p")]
        self.assertEqual(anchors(attrs(codec.decode(codec.encode(CIRCLE)))),
                         anchors(attrs(CIRCLE)))

    def test_an_ellipse_keeps_its_axes_and_angle(self):
        raw = CIRCLE.replace(
            b'<ellipse major="152.4" minor="152.4" angle="360.0"/>',
            b'<ellipse major="200.0" minor="75.5" angle="42.5" swapAxis="true"/>')
        out = attrs(codec.decode(codec.encode(raw))).find("detail/shape/ellipse")
        self.assertEqual(out.get("major"), "200.0")
        self.assertEqual(out.get("minor"), "75.5")
        self.assertEqual(out.get("angle"), "42.5")
        self.assertEqual(out.get("swapAxis"), "true")

    def test_concentric_rings_all_survive(self):
        rings = b"".join(
            b'<ellipse major="%d.0" minor="%d.0" angle="360.0"/>' % (r, r)
            for r in (100, 200, 300))
        raw = CIRCLE.replace(
            b'<ellipse major="152.4" minor="152.4" angle="360.0"/>', rings)
        out = attrs(codec.decode(codec.encode(raw)))
        self.assertEqual([e.get("major") for e in out.findall("detail/shape/ellipse")],
                         ["100.0", "200.0", "300.0"])

    def test_a_circle_with_no_anchors_round_trips(self):
        raw = CIRCLE
        for rel in (b"p-p-CenterAnchor", b"p-p-RadiusAnchor"):
            start = raw.index(b'<link relation="' + rel)
            end = raw.index(b"/>", start) + 2
            raw = raw[:start] + raw[end:]
        out = attrs(codec.decode(codec.encode(raw)))
        self.assertEqual(len(out.findall("detail/shape/link")), 1)  # style only

    def test_radii_keep_centimetre_precision(self):
        # ATAK writes a Java double, so a radius can arrive as
        # "152.4000244140625". A centimetre is far below what can be drawn or
        # seen; this pins the trade rather than leaving it to be discovered.
        raw = CIRCLE.replace(b'major="152.4"', b'major="152.4000244140625"')
        out = attrs(codec.decode(codec.encode(raw))).find("detail/shape/ellipse")
        self.assertAlmostEqual(float(out.get("major")), 152.4, places=2)

    # -- what goes generic rather than typed ----------------------------------

    def test_a_style_uid_that_is_not_derived_goes_generic(self):
        assert_carried_exactly(self, 
            CIRCLE.replace(b'uid="929534ac-1f17-415a-bd51-4919e2a07fc0.Style"',
                           b'uid="something-else.Style"'),
            "the style uid is rebuilt from the event uid, so it has to match")

    def test_an_unknown_child_of_shape_goes_generic(self):
        assert_carried_exactly(self, 
            CIRCLE.replace(b"</shape>", b"<hatchFill pattern=\"x\"/></shape>"),
            "a child we would silently drop")

    def test_an_unknown_child_of_style_goes_generic(self):
        assert_carried_exactly(self, 
            CIRCLE.replace(b"</Style>", b"<IconStyle/></Style>"),
            "a style we would silently drop")

    def test_a_colour_that_is_not_eight_hex_digits_goes_generic(self):
        assert_carried_exactly(self, 
            CIRCLE.replace(b"<color>ffffffff</color>", b"<color>white</color>"),
            "getColorString always writes eight lower-case hex digits")


def _waypoint(i):
    return (b'<link point="%s,%s,381.0" type="b-m-p-w" '
            b'uid="aaaaaaaa-bbbb-cccc-dddd-%012d" relation="c" '
            b'callsign="CP%d" remarks=""/>'
            % (("%.7f" % (20.9089800 + 0.0009 * i)).encode(),
               ("%.7f" % (-140.5829100 + 0.0007 * i)).encode(), i, i))


# From EditablePolyline.toCot() + Route.toCot() + NavigationCueHandler. The
# 2026-08-07 bench measured a real one at 1615 B of XML, 651 B on air across
# two packets, and it needed an ARQ retry -- four transmissions for one route.
ROUTE = (
    b'<event version="2.0" uid="f0919683-150b-402f-9518-540eae3513bf" '
    b'type="b-m-r" how="h-e" time="2026-08-07T15:32:35.000Z" '
    b'start="2026-08-07T15:32:35.000Z" stale="2026-08-08T15:32:35.000Z">'
    b'<point lat="20.9089800" lon="-140.5829100" hae="381.0" ce="9.5" le="9999999.0"/>'
    b'<detail>'
    + b"".join(_waypoint(i) for i in range(8)) +
    b'<link_attr color="-1" stroke="3" style="0" type="On Foot" '
    b'method="Walking" direction="Infil" routetype="Primary" '
    b'order="Ascending Check Points" planningmethod="Infil" prefix="CP"/>'
    b'<__routeinfo><__navcues/></__routeinfo>'
    b'<contact callsign="Route 1"/><archive/>'
    b'<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
    b'<color argb="-1"/><labels_on value="false"/>'
    b'</detail></event>'
)


def waypoints(root):
    return [(l.get("point"), l.get("type"), l.get("uid"), l.get("relation"),
             l.get("callsign"), l.get("remarks"))
            for l in root.findall("detail/link")]


def _real_marker(cot_type, callsign, icon=None, argb="-1",
                 uid="6835cd7b-92fc-437d-9dc3-7fe00bdd7634",
                 made="2026-10-12T22:34:24.042Z"):
    """The shape ATAK 5.6 produced for a hand-placed marker (captured
    2026-10-12 on a Pixel 7), with the position moved off the real one."""
    icon_xml = ('<usericon iconsetpath="%s"/>' % icon) if icon else ""
    return (
        '<event version="2.0" uid="%s" type="%s" time="2026-10-12T22:34:27.747Z" '
        'start="2026-10-12T22:34:27.747Z" stale="2027-10-12T22:34:27.747Z" '
        'how="h-g-i-g-o" access="Undefined">'
        '<point lat="10.0000001" lon="20.0000002" hae="339.417" ce="9999999.0" '
        'le="9999999.0"/><detail><status readiness="true"/><archive/><archive/>'
        '<precisionlocation altsrc="SRTM1"/><remarks/><color argb="%s"/>'
        '<contact callsign="%s"/>'
        '<link uid="ANDROID-2222222222222222" production_time="%s" '
        'type="a-f-G-U-C" parent_callsign="pixel " relation="p-p"/>%s'
        '<creator uid="ANDROID-2222222222222222" callsign="pixel " time="%s" '
        'type="a-f-G-U-C"/><marti><dest callsign="oneplus"/></marti></detail></event>'
        % (uid, cot_type, argb, callsign, made, icon_xml, made)).encode()


# What fits in one FM burst after the 12 B envelope and the 3 B reliable
# wrapper. Measured 2026-10-12; docs/open-questions.md has the derivation.
ONE_PACKET_FM_B = 110


class TestMarkerIdentifiersAndIcons(unittest.TestCase):
    """The four marker kinds ATAK 5.6 sends, which all cost two bursts before
    the uid, icon and callsign were derived rather than spelled out."""

    CASES = {
        "2525 unknown": ("a-u-G", "U.12.151812",
                         "COT_MAPPING_2525C/a-u/a-u-G", "-1"),
        "spot map": ("b-m-p-s-m", "G 1",
                     "COT_MAPPING_SPOTMAP/b-m-p-s-m/-16711936", "-16711936"),
        "waypoint": ("b-m-p-w-GOTO", "pixel .12.153411", None, "-1"),
        "helo": ("a-n-G", "Helo Landing Site 1",
                 "f8f7f666-8b28-4b57-9fbb-e48e61d33b79/Iron Sites/"
                 "Helo Landing Site.png", "-1"),
        "command post": ("a-n-G", "incident command post 1",
                         "db450cbe-2fec-47fb-bd2b-ba2db89b035e/"
                         "Incident Management/incident command post.png", "-1"),
    }

    # The creator time in each case above is what the callsign's clock reads.
    MADE = {"U.12.151812": "2026-10-12T22:18:12.171Z",
            "pixel .12.153411": "2026-10-12T22:34:11.998Z"}

    def _event(self, name):
        cot_type, callsign, icon, argb = self.CASES[name]
        return _real_marker(cot_type, callsign, icon, argb,
                            made=self.MADE.get(callsign,
                                               "2026-10-12T22:34:24.042Z"))

    def _fields(self, xml_bytes):
        root = ET.fromstring(xml_bytes)
        d = root.find("detail")
        return {
            "uid": root.get("uid"),
            "callsign": d.find("contact").get("callsign"),
            "icon": (d.find("usericon").get("iconsetpath")
                     if d.find("usericon") is not None else None),
            "creator": dict(d.find("creator").attrib),
            "link": d.find("link").get("uid"),
        }

    def test_every_kind_is_one_packet_and_round_trips_its_identity(self):
        for name in self.CASES:
            raw = self._event(name)
            frame = codec.encode(raw)
            self.assertLessEqual(len(frame), ONE_PACKET_FM_B, name)
            before, after = self._fields(raw), self._fields(codec.decode(frame))
            # The creator's time loses its milliseconds on every path; that is
            # not what is under test here.
            for f in (before, after):
                f["creator"].pop("time")
            self.assertEqual(before, after, name)

    def test_the_uuid_and_the_android_id_ride_as_raw_bytes(self):
        frame = codec.encode(self._event("waypoint"))
        body = unsqueezed(frame)
        self.assertIn(bytes([codec.UID_UUID]) + bytes.fromhex(
            "6835cd7b92fc437d9dc37fe00bdd7634"), body)
        self.assertIn(bytes([codec.UID_ANDROID]) + bytes.fromhex(
            "2222222222222222"), body)
        self.assertNotIn(b"ANDROID-", body)

    def test_a_derived_callsign_is_not_spelled_out(self):
        for name in ("helo", "command post", "waypoint", "2525 unknown"):
            cs = self.CASES[name][1].encode()
            self.assertNotIn(cs, unsqueezed(codec.encode(self._event(name))), name)

    def test_a_local_clock_east_of_utc_is_found_too(self):
        # 22:34:11Z + 5:30 is 04:04:11 on the 13th.
        raw = _real_marker("b-m-p-w-GOTO", "N.13.040411",
                           made="2026-10-12T22:34:11.998Z")
        out = self._fields(codec.decode(codec.encode(raw)))
        self.assertEqual(out["callsign"], "N.13.040411")
        raw = _real_marker("b-m-p-w-GOTO", "N.13.040411",
                           made="2026-10-12T22:34:11.998Z")
        self.assertNotIn(b"N.13", unsqueezed(codec.encode(raw)))

    # --- what must stay literal ---

    def test_a_callsign_that_is_not_the_creator_clock_is_sent_as_written(self):
        # One minute off: "derived" would invent the wrong name.
        raw = _real_marker("b-m-p-w-GOTO", "pixel .12.153511",
                           made="2026-10-12T22:34:11.998Z")
        self.assertEqual(
            self._fields(codec.decode(codec.encode(raw)))["callsign"],
            "pixel .12.153511")

    def test_a_counter_with_a_leading_zero_is_not_a_counter(self):
        icon = ("f8f7f666-8b28-4b57-9fbb-e48e61d33b79/Iron Sites/"
                "Helo Landing Site.png")
        for name in ("Helo Landing Site 01", "Helo Landing Site", "helo landing site 1",
                     "Helo Landing Site 1 "):
            raw = _real_marker("a-n-G", name, icon)
            self.assertEqual(
                self._fields(codec.decode(codec.encode(raw)))["callsign"], name)

    def test_a_spot_map_icon_in_a_different_colour_than_the_marker_is_literal(self):
        raw = _real_marker("b-m-p-s-m", "G 1",
                           "COT_MAPPING_SPOTMAP/b-m-p-s-m/-65536", "-16711936")
        self.assertEqual(
            self._fields(codec.decode(codec.encode(raw)))["icon"],
            "COT_MAPPING_SPOTMAP/b-m-p-s-m/-65536")

    def test_a_2525_icon_for_another_type_is_literal(self):
        raw = _real_marker("a-u-G", "x", "COT_MAPPING_2525C/a-f/a-f-G")
        self.assertEqual(
            self._fields(codec.decode(codec.encode(raw)))["icon"],
            "COT_MAPPING_2525C/a-f/a-f-G")

    def test_an_iconset_we_have_never_seen_still_round_trips(self):
        icon = "01234567-89ab-cdef-0123-456789abcdef/Some Group/thing.png"
        raw = _real_marker("a-n-G", "thing 7", icon)
        out = self._fields(codec.decode(codec.encode(raw)))
        self.assertEqual((out["icon"], out["callsign"]), (icon, "thing 7"))

    def test_a_known_iconset_with_another_group_keeps_its_group(self):
        icon = ("f8f7f666-8b28-4b57-9fbb-e48e61d33b79/Elsewhere/Thing.png")
        raw = _real_marker("a-n-G", "Thing 2", icon)
        self.assertEqual(
            self._fields(codec.decode(codec.encode(raw)))["icon"], icon)

    def test_an_uppercase_uuid_is_not_repacked_into_lowercase(self):
        uid = "6835CD7B-92FC-437D-9DC3-7FE00BDD7634"
        out = self._fields(codec.decode(codec.encode(_real_marker("a-n-G", "x", uid=uid))))
        self.assertEqual(out["uid"], uid)

    def test_an_odd_uid_is_literal_and_a_huge_one_falls_back_whole(self):
        for uid in ("ANDROID-222222222222222", "ANDROID-2222222222222222",
                    "plain-uid", "a" * 253):
            out = self._fields(codec.decode(codec.encode(
                _real_marker("a-n-G", "x", uid=uid))))
            self.assertEqual(out["uid"], uid)
        uid = "b" * 300
        out = self._fields(codec.decode(codec.encode(
            _real_marker("a-n-G", "x", uid=uid))))
        self.assertEqual(out["uid"], uid)     # via the zlib'd XML path

    def test_every_known_set_and_group_pair_costs_one_byte_plus_the_name(self):
        for i, set_uuid in enumerate(codec.KNOWN_ICONSETS):
            for set_index, group in codec.KNOWN_ICON_GROUPS:
                if set_index != i:
                    continue
                path = "%s/%s/Thing.png" % (set_uuid, group)
                packed = codec._encode_icon(path, "a-n-G", None)
                self.assertEqual(len(packed), 1 + 1 + len("Thing"), path)
                self.assertEqual(codec._read_icon(packed, 0, "a-n-G", None),
                                 (path, len(packed)))

    def test_the_tables_are_well_formed(self):
        sets = codec.KNOWN_ICONSETS
        self.assertEqual(len(sets), len(set(sets)))
        self.assertEqual(len(codec.KNOWN_ICON_GROUPS),
                         len(set(codec.KNOWN_ICON_GROUPS)))
        self.assertTrue(all(0 <= i < len(sets) and g and "/" not in g
                            for i, g in codec.KNOWN_ICON_GROUPS))
        # One control byte each, and the ranges must not run into each other.
        self.assertLess(codec.ICON_KNOWN_SET + len(sets), codec.ICON_KNOWN_GROUP)
        self.assertLess(codec.ICON_KNOWN_GROUP + len(codec.KNOWN_ICON_GROUPS), 256)

    def test_the_two_sets_whose_ids_are_not_uuids_still_round_trip(self):
        geoops = [u for u in codec.KNOWN_ICONSETS if len(u) == 64]
        odd = [u for u in codec.KNOWN_ICONSETS if len(u) == 37]
        self.assertEqual((len(geoops), len(odd)), (1, 1))
        for set_uuid in geoops + odd:
            icon = "%s/Not A Group/Pin 3.png" % set_uuid     # group not in table
            raw = _real_marker("a-n-G", "Pin 3 3", icon)
            out = self._fields(codec.decode(codec.encode(raw)))
            self.assertEqual((out["icon"], out["callsign"]), (icon, "Pin 3 3"))
            self.assertNotIn(set_uuid.encode(), unsqueezed(codec.encode(raw)))

    def test_a_non_uuid_set_we_do_not_know_stays_literal(self):
        icon = "not-a-uuid-set/Group/Thing.png"
        raw = _real_marker("a-n-G", "Thing 1", icon)
        self.assertEqual(
            self._fields(codec.decode(codec.encode(raw)))["icon"], icon)

    def test_an_icon_kind_this_build_lacks_is_refused_not_guessed(self):
        with self.assertRaises(codec.CotDecodeError):
            codec._read_icon(bytes([0x7F]), 0, "a-u-G", None)
        with self.assertRaises(codec.CotDecodeError):
            codec._read_icon(bytes([codec.ICON_SPOTMAP]), 0, "b-m-p-s-m", None)


class TestRoutes(unittest.TestCase):
    """The most expensive event on this link, and the only one measured needing
    a retransmission -- so the packet count matters as much as the size."""

    def test_a_route_takes_the_compact_path(self):
        self.assertTrue(compact_path(codec.encode(ROUTE)))

    def test_it_fits_one_packet_where_it_used_to_need_two(self):
        self.assertEqual(len(codec.build_packets(ROUTE)), 1)
        self.assertLess(len(codec.encode(ROUTE)) + 16, 300)

    def test_every_waypoint_survives_with_its_marker(self):
        out = attrs(codec.decode(codec.encode(ROUTE)))
        self.assertEqual(waypoints(out), waypoints(attrs(ROUTE)))
        self.assertEqual(len(waypoints(out)), 8)

    def test_the_link_attr_survives(self):
        before = attrs(ROUTE).find("detail/link_attr").attrib
        after = attrs(codec.decode(codec.encode(ROUTE))).find("detail/link_attr").attrib
        self.assertEqual(before, after)

    def test_an_empty_navcues_round_trips(self):
        """ATAK writes <__navcues/> for every route whether or not the operator
        recorded any, so the ordinary case has to cost almost nothing."""
        out = attrs(codec.decode(codec.encode(ROUTE)))
        self.assertIsNotNone(out.find("detail/__routeinfo/__navcues"))
        self.assertEqual(len(out.find("detail/__routeinfo/__navcues")), 0)

    def test_navigation_cues_and_their_triggers_survive(self):
        raw = ROUTE.replace(
            b"<__navcues/>",
            b'<__navcues><__navcue id="aaaaaaaa-bbbb-cccc-dddd-000000000003" '
            b'voice="turn left" text="Turn left at the bridge">'
            b'<trigger mode="0" value="50"/><trigger mode="1" value="200"/>'
            b"</__navcue></__navcues>")
        out = attrs(codec.decode(codec.encode(raw)))
        cue = out.find("detail/__routeinfo/__navcues/__navcue")
        self.assertEqual(cue.get("voice"), "turn left")
        self.assertEqual(cue.get("text"), "Turn left at the bridge")
        self.assertEqual([(t.get("mode"), t.get("value")) for t in cue],
                         [("0", "50"), ("1", "200")])

    def test_waypoints_are_deltas_from_the_event_point(self):
        far = ROUTE.replace(b"20.9", b"70.9").replace(b"-140.5", b"-40.5")
        self.assertNotEqual(far, ROUTE)
        self.assertEqual(len(unsqueezed(codec.encode(far))),
                         len(unsqueezed(codec.encode(ROUTE))))

    def test_a_plain_control_point_costs_a_bit_not_a_uid(self):
        """A real ATAK route is mostly unnamed control points -- the 2026-08-12
        bench measured 52 of them against 3 named waypoints, and their uids
        alone were 884 of 2568 bytes.

        Safe to derive because ATAK never puts a control point in a map group
        (Route.java), so nothing can ever refer to one by uid. A WAYPOINT is a
        Marker that RouteImporter looks up with deepFindUID, so its uid stays
        on the wire.
        """
        # Random uids, because that is what ATAK generates
        # (UUID.randomUUID) and they do not compress. The shared fixture's
        # uids are sequential and squeeze away, which would hide the whole
        # cost this test exists to measure.
        rng = random.Random(7)
        def link(i, kind, callsign):
            uid = "%08x-%04x-%04x-%04x-%012x" % (
                rng.getrandbits(32), rng.getrandbits(16), rng.getrandbits(16),
                rng.getrandbits(16), rng.getrandbits(48))
            return (b'<link point="%s,%s,381.0" type="%s" uid="%s" '
                    b'relation="c" callsign="%s" remarks=""/>'
                    % (("%.7f" % (20.9089800 + 0.0009 * i)).encode(),
                       ("%.7f" % (-140.5829100 + 0.0007 * i)).encode(),
                       kind.encode(), uid.encode(), callsign.encode()))

        old = b"".join(_waypoint(i) for i in range(8))
        named = ROUTE.replace(old, b"".join(
            link(i, "b-m-p-w", "CP%d" % i) for i in range(20)))
        rng.seed(7)
        plain = ROUTE.replace(old, b"".join(
            link(i, "b-m-p-c", "") for i in range(20)))
        self.assertLess(len(codec.encode(plain)),
                        len(codec.encode(named)) / 2.0)
        # ...and the points still all come back.
        back = ET.fromstring(codec.decode(codec.encode(plain)))
        self.assertEqual(len(back.find("detail").findall("link")), 20)

    def test_a_derived_control_point_uid_is_stable(self):
        """It has to be, or re-sending a route accumulates a fresh set of
        control points on the receiver every time instead of moving the ones
        it already has."""
        first = codec.derive_waypoint_uid("route-1", 3)
        self.assertEqual(first, codec.derive_waypoint_uid("route-1", 3))
        self.assertNotEqual(first, codec.derive_waypoint_uid("route-1", 4))
        self.assertNotEqual(first, codec.derive_waypoint_uid("route-2", 3))

    def test_a_named_control_point_keeps_its_uid(self):
        """The operator put something there, so it is not ours to invent."""
        named_cp = (b'<link point="20.9175,-140.5133,381.0" type="b-m-p-c" '
                    b'uid="aaaaaaaa-bbbb-cccc-dddd-000000000001" relation="c" '
                    b'callsign="RALLY" remarks=""/>')
        xml = ROUTE.replace(_waypoint(0), named_cp)
        back = codec.decode(codec.encode(xml))
        link = [l for l in ET.fromstring(back).find("detail").findall("link")
                if l.get("callsign") == "RALLY"][0]
        self.assertEqual(link.get("uid"), "aaaaaaaa-bbbb-cccc-dddd-000000000001")

    def test_a_long_route_still_fragments_and_that_is_recorded(self):
        """Thirty NAMED waypoints fit one packet; thirty-one do not.

        The boundary was ten until 2026-08-12, when encode() stopped discarding
        a compact frame merely for exceeding one packet. A frame that big is
        going to be fragmented whichever form it takes, so the budget stops
        being a limit and the only question is which form is smaller -- and the
        compact one is, by nearly 3x on a real route.

        These are the expensive kind: every point named, so every one carries a
        uid, a callsign and a type. A real ATAK route is mostly unnamed control
        points, which cost a bit each -- see the plain-control-point test.

        Fragmenting is what makes a route expensive twice over, because an ARQ
        retry resends every fragment. Pinned here rather than wished away: a
        long route is still the worst traffic on this link."""
        def route_of(n):
            return ROUTE.replace(b"".join(_waypoint(i) for i in range(8)),
                                 b"".join(_waypoint(i) for i in range(n)))
        self.assertEqual(len(codec.build_packets(route_of(30))), 1)
        self.assertEqual(len(codec.build_packets(route_of(31))), 2)
        # ...and it still round-trips once fragmented.
        long_route = route_of(31)
        reassembled = codec.Reassembler()
        whole = [x for x in
                 (reassembled.offer(p) for p in codec.build_packets(long_route))
                 if x is not None]
        self.assertEqual(len(whole), 1)
        self.assertEqual(len(waypoints(attrs(codec.decode(whole[0])))), 31)

    def test_a_drawn_shape_still_goes_the_shape_route(self):
        # The regression that matters: a rectangle's bare point links must not
        # be swept into the route encoder, which is dispatched on route
        # furniture rather than on the link count.
        self.assertTrue(ext_of(codec.encode(RECTANGLE)) & codec.X_SHAPE)
        self.assertFalse(ext_of(codec.encode(RECTANGLE)) & codec.X_ROUTE)
        self.assertTrue(ext_of(codec.encode(ROUTE)) & codec.X_ROUTE)

    # -- what goes generic rather than typed ----------------------------------

    def test_an_unknown_link_attr_goes_generic(self):
        assert_carried_exactly(self, 
            ROUTE.replace(b'<link_attr color="-1"', b'<link_attr gradient="x" color="-1"'),
            "an attribute we would drop")

    def test_an_unknown_child_of_routeinfo_goes_generic(self):
        assert_carried_exactly(self, 
            ROUTE.replace(b"<__navcues/>", b"<__navcues/><__someotherplugin/>"),
            "a plugin's detail we would silently drop")

    def test_an_unknown_navcue_attribute_goes_generic(self):
        assert_carried_exactly(self, 
            ROUTE.replace(b"<__navcues/>",
                          b'<__navcues><__navcue id="x" chime="loud"/></__navcues>'),
            "an attribute we would drop")

    def test_a_waypoint_with_an_unexpected_relation_goes_generic(self):
        assert_carried_exactly(self, 
            ROUTE.replace(b'relation="c" callsign="CP0"',
                          b'relation="p-p" callsign="CP0"', 1),
            "a waypoint relation is always c")


class TestPositionClassification(unittest.TestCase):
    """Which events count as periodic position reports.

    One flag, three consequences: a position is throttled, sent
    unacknowledged, and encoded into the self-SA delta stream. Getting it
    wrong for a class of event therefore loses that event silently, which is
    exactly what happened -- the test below named for the 2026-08-15 range
    test is the regression, and the rest of this class is the boundary.
    """

    def test_a_marker_is_not_a_position(self):
        """The 2026-08-15 range test: six markers placed, two lost to the air
        with no retry and no report, because a-u-G starts with "a-" and so was
        read as a periodic report that a later one would supersede."""
        self.assertFalse(codec.summarize(MARKER).is_position)

    def test_the_devices_own_report_is_a_position(self):
        """The other half. If this ever goes false, every position report
        starts demanding an ack and the throttle stops applying -- which is
        the airtime budget the whole of milestone 3 is built on."""
        self.assertTrue(codec.summarize(PLI).is_position)

    def test_every_marker_affiliation_is_covered(self):
        """a-u-G is only the unknown one. Hostile, neutral and friendly
        markers have the identical shape and had the identical bug, so a fix
        that only recognised a-u-G would leave three quarters of it in place."""
        for affiliation in (b"a-h-G", b"a-n-G", b"a-f-G", b"a-u-G"):
            raw = MARKER.replace(b'type="a-u-G"', b'type="' + affiliation + b'"', 1)
            self.assertFalse(codec.summarize(raw).is_position, affiliation)

    def test_coordinates_alone_do_not_make_a_position(self):
        """The marker fixture has a perfectly good lat/lon. Position-ness is
        about whether a later event replaces this one, not about whether the
        event knows where it is."""
        summary = codec.summarize(MARKER)
        self.assertIsNotNone(summary.lat)
        self.assertIsNotNone(summary.lon)
        self.assertFalse(summary.is_position)

    def test_a_self_report_without_coordinates_is_not_a_position(self):
        """Nothing to supersede, so nothing to throttle. Guards the case where
        a self-SA arrives before a GPS fix."""
        raw = PLI.replace(b'lat="35.1234567" lon="-79.7654321"', b'lat="" lon=""', 1)
        self.assertFalse(codec.summarize(raw).is_position)

    def test_position_and_self_are_driven_by_the_same_signal(self):
        """Both key off <takv>, and deliberately so: "is this the sender
        describing itself?" is one question, and answering it twice is how the
        two answers drift apart."""
        for raw in (PLI, MARKER, GEOCHAT):
            summary = codec.summarize(raw)
            if summary.is_position:
                self.assertTrue(summary.is_self, raw[:40])

    def test_a_marker_quoting_a_self_type_is_still_not_a_position(self):
        """Markers carry <creator type="a-f-G-U-C"> and a <link> naming the
        placing device, so the string a-f-G-U-C appears inside a marker's XML.
        Only the event's own type attribute and its own takv may decide this."""
        self.assertIn(b'type="a-f-G-U-C"', MARKER)
        self.assertFalse(codec.summarize(MARKER).is_position)


# Events this codec had no typed encoding for, written by hand in the shapes
# ATAK emits them (the 2026-10-14 comparison used Meshtastic's fixtures, which
# are GPL-3.0 and so are not copied here).
EMERGENCY = (
    b'<event version="2.0" uid="ANDROID-352214111111111-9-1-1" type="b-a-o-tbl" '
    b'time="2026-08-07T15:32:35.000Z" start="2026-08-07T15:32:35.000Z" '
    b'stale="2026-08-07T15:42:35.000Z" how="h-e">'
    b'<point lat="35.1234567" lon="-79.7654321" hae="123.45" ce="9.5" le="9999999.0"/>'
    b'<detail><link uid="ANDROID-352214111111111" type="a-f-G-U-C" relation="p-p"/>'
    b'<contact callsign="BRAVO01-Alert"/>'
    b'<emergency type="911 Alert">BRAVO01</emergency></detail></event>'
)

RANGE_LINE = (
    b'<event version="2.0" uid="1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9" type="u-rb-a" '
    b'time="2026-08-07T15:32:35.000Z" start="2026-08-07T15:32:35.000Z" '
    b'stale="2026-08-08T15:32:35.000Z" how="h-e">'
    b'<point lat="35.1234567" lon="-79.7654321" hae="123.45" ce="9.5" le="9999999.0"/>'
    b'<detail><range value="1523.6672"/><bearing value="47.25"/>'
    b'<inclination value="0.0"/><rangeUnits value="1"/><bearingUnits value="0"/>'
    b'<northRef value="1"/><strokeColor value="-65536"/><strokeWeight value="3.0"/>'
    b'<contact callsign="R&amp;B 1"/><remarks/><labels_on value="true"/>'
    b'<link uid="ANDROID-352214111111111" type="a-f-G-U-C" '
    b'relation="p-p" production_time="2026-08-07T15:32:35.000Z"/></detail></event>'
)


class TestGenericElements(unittest.TestCase):
    """Detail children no typed encoding models ride as generic elements --
    exactly, and in place of the zlib'd-XML fallback that used to carry them."""

    CTX = {"uid": "ANDROID-352214111111111", "ts": 1786116755, "count": 0}

    def round_trip(self, text):
        raw = codec._encode_gvalue(text, self.CTX)
        back, end = codec._read_gvalue(raw, 0, self.CTX)
        self.assertEqual(back, text)
        self.assertEqual(end, len(raw))
        return raw

    # -- values ------------------------------------------------------------

    def test_each_typed_form_is_used_where_it_is_exact(self):
        for text, kind in (
                ("", codec.GV_EMPTY), ("true", codec.GV_TRUE),
                ("false", codec.GV_FALSE), ("-65536", codec.GV_INT),
                ("1523.6672", codec.GV_DEC), ("3.0", codec.GV_DEC),
                ("ANDROID-352214111111111", codec.GV_EVENT_UID),
                ("ANDROID-0123456789abcdef", codec.GV_ANDROID),
                ("1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9", codec.GV_UUID),
                ("2026-08-07T15:32:35.000Z", codec.GV_TIME),
                ("2026-08-07T15:32:35Z", codec.GV_TIME),
                ("21.44474,-141.25977", codec.GV_NUMS),
                ("20.9089800,-140.5829100,381.0", codec.GV_NUMS)):
            self.assertEqual(self.round_trip(text)[0] & 0x0F, kind, text)

    def test_what_only_looks_typed_stays_literal(self):
        # Each of these parses as the typed value and would rebuild as
        # different text, which is the one thing the codec must never do.
        for text in ("007", "+5", "-0", "-0.0", "1e5", " 5", "5 ", "0x10",
                     "1B2C3D4E-5F60-4718-8293-A4B5C6D7E8F9",
                     "2026-08-07T15:32:35+02:00", "2026-08-07t15:32:35z",
                     "33.1, -107.2", "1,,2", "True", "NaN", "inf",
                     "1234567890123456789012345"):
            raw = self.round_trip(text)
            self.assertEqual(raw[0] & 0x0F, codec.GV_STR, text)

    def test_a_typed_form_is_never_bigger_than_the_literal(self):
        for text in ("1", "a", "0.5", "1,2"):
            raw = self.round_trip(text)
            self.assertLessEqual(len(raw), 2 + len(text.encode("utf-8")))

    def test_long_and_unicode_text_survives(self):
        # Remarks and medline notes run past the 255 bytes an lstr holds.
        self.round_trip("x" * 1000)
        self.round_trip("ÅNGSTRÖM — 東京 ✓")

    def test_random_values_round_trip(self):
        rng = random.Random(20261014)
        alphabet = "0123456789.-,:TZ abcdefABCDEF+e"
        for _ in range(5000):
            text = "".join(rng.choice(alphabet)
                           for _ in range(rng.randint(0, 24)))
            self.round_trip(text)

    # -- events --------------------------------------------------------------

    def test_events_with_no_typed_encoding_take_the_compact_path(self):
        for name, src in (("emergency", EMERGENCY), ("range line", RANGE_LINE)):
            frame = codec.encode(src)
            self.assertIn(frame[0] & 0x0F,
                          (codec.T_COMPACT_X, codec.T_ZCOMPACT_X), name)
            # One Meshtastic packet: 231 B less the 12 B mesh envelope.
            self.assertLess(len(frame), 219 - 3, name)
            self.assertLess(len(frame), len(codec.encode_zxml(src)), name)

    def test_their_detail_comes_back_exactly(self):
        for src in (EMERGENCY, RANGE_LINE):
            out = codec.decode(codec.encode(src))
            want = {t for t in detail_tree(src) if t[0] not in ("contact", "link")}
            self.assertLessEqual(want, set(detail_tree(out)))
        out = attrs(codec.decode(codec.encode(EMERGENCY)))
        self.assertEqual(out.find("detail/emergency").text, "BRAVO01")
        self.assertEqual(out.find("detail/emergency").get("type"), "911 Alert")

    def test_a_known_tag_nothing_writes_here_is_not_lost(self):
        """KNOWN_DETAIL says a tag is modelled SOMEWHERE. <strokeColor> and
        <labels_on> are written only by the vertex-shape encoding, so a range
        line, a circle or a route carrying them lost them silently -- masked
        while an unknown sibling sent the event to zlib whole."""
        out = attrs(codec.decode(codec.encode(RANGE_LINE)))
        self.assertEqual(out.find("detail/strokeColor").get("value"), "-65536")
        self.assertEqual(out.find("detail/strokeWeight").get("value"), "3.0")
        self.assertEqual(out.find("detail/labels_on").get("value"), "true")
        for src in (CIRCLE, ROUTE):
            out = codec.decode(codec.encode(src))
            for tag in ("labels_on", "__shapeExtras"):
                if attrs(src).find("detail/" + tag) is not None:
                    self.assertEqual(attrs(out).find("detail/" + tag).attrib,
                                     attrs(src).find("detail/" + tag).attrib, tag)

    def test_a_rectangle_with_a_toggle_keeps_its_shape_encoding(self):
        # <tog enabled="0"/> is on every ATAK rectangle in Meshtastic's set and
        # sent each one to zlib. Now it rides beside the vertex list.
        src = RECTANGLE.replace(b"</detail>", b'<tog enabled="0"/></detail>')
        frame = codec.encode(src)
        self.assertTrue(ext_of(frame) & codec.X_SHAPE)
        self.assertLess(len(frame) - len(codec.encode(RECTANGLE)), 6)
        self.assertEqual(attrs(codec.decode(frame)).find("detail/tog").attrib,
                         {"enabled": "0"})

    def test_the_squeezed_form_keeps_its_own_type(self):
        frame = codec.encode(RANGE_LINE)
        if frame[0] & 0x0F == codec.T_ZCOMPACT_X:
            self.assertEqual(codec.decode(frame),
                             codec.decode(unsqueezed(frame)))
        flat = unsqueezed(frame)
        self.assertEqual(flat[0], codec.CTRL_COMPACT_X)
        self.assertEqual(codec._squeeze(flat)[0] & 0x0F,
                         codec.T_ZCOMPACT_X if len(codec._squeeze(flat)) < len(flat)
                         else codec.T_COMPACT_X)

    # -- refusing what is not ours to read ----------------------------------

    def test_every_truncation_is_refused_cleanly(self):
        for src in (EMERGENCY, RANGE_LINE):
            flat = unsqueezed(codec.encode(src))
            for cut in range(1, len(flat)):
                with self.assertRaises(codec.CotDecodeError, msg=cut):
                    codec.decode(flat[:cut])

    def test_trailing_bytes_after_the_block_are_refused(self):
        flat = unsqueezed(codec.encode(EMERGENCY))
        self.assertRaises(codec.CotDecodeError, codec.decode, flat + b"\x00")

    def test_a_compact_frame_relabelled_as_generic_is_refused(self):
        # The type promises a block; a frame without one must not decode as
        # if the promise were optional.
        plain = unsqueezed(codec.encode(PLI))
        self.assertRaises(codec.CotDecodeError, codec.decode,
                          bytes([codec.CTRL_COMPACT_X]) + plain[1:])

    def test_a_nesting_bomb_is_refused(self):
        deep = b"".join(b"<a%d>" % i for i in range(20)) + b"".join(
            b"</a%d>" % i for i in reversed(range(20)))
        src = PLI.replace(b"</detail>", deep + b"</detail>")
        self.assertEqual(codec.encode(src)[0], codec.CTRL_ZXML)
        # And a hand-built block claiming that depth is refused on decode:
        # one element, then each an escaped tag "x" with one child.
        ctx = {"uid": "u", "ts": 0, "count": 0}
        block = b"\x01" + b"\x00\x01x\x02\x01" * (codec._GENERIC_MAX_DEPTH + 2)
        with self.assertRaisesRegex(codec.CotDecodeError, "deeper"):
            codec._read_generic_block(block, 0, ctx, ET.Element("detail"))

    def test_an_absurd_element_count_is_refused_before_looping(self):
        ctx = {"uid": "u", "ts": 0, "count": 0}
        self.assertRaises(codec.CotDecodeError, codec._read_generic_block,
                          codec._uvarint(10 ** 9) + b"\x00", 0, ctx,
                          ET.Element("detail"))

    def test_the_new_types_are_compact_kinds(self):
        # rns_bridge decides "is this a bare position?" on the kind; a
        # position carrying a sensor cone must not stop counting as one.
        for kind in (codec.T_COMPACT, codec.T_ZCOMPACT,
                     codec.T_COMPACT_X, codec.T_ZCOMPACT_X):
            self.assertIn(kind, codec.COMPACT_KINDS)
        self.assertNotIn(codec.T_ZXML, codec.COMPACT_KINDS)

    def test_tables_keep_the_escape_slot_and_have_room(self):
        for table in (codec.GENERIC_TAG_TABLE, codec.GENERIC_ATTR_TABLE):
            self.assertEqual(table[0], "")
            self.assertEqual(len(set(table)), len(table))
            self.assertLessEqual(len(table), 256)

