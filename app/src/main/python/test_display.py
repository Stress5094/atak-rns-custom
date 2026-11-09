import unittest

import display


class TestFont(unittest.TestCase):
    def test_covers_what_a_name_or_an_id_can_contain(self):
        for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.:+*>?":
            self.assertIn(char, display.FONT, "font is missing %r" % char)

    def test_every_glyph_is_the_declared_size(self):
        # _parse_font enforces this at import, so a failure here means the
        # check itself was weakened rather than a glyph being wrong.
        for char, rows in display.FONT.items():
            self.assertEqual(len(rows), display.LINE_HEIGHT, char)
            for row in rows:
                self.assertEqual(len(row), display.CHAR_WIDTH, char)


class TestRender(unittest.TestCase):
    def test_frame_is_the_shape_the_firmware_expects(self):
        frame = display.render([])
        self.assertEqual(len(frame), 64)
        for row in frame:
            self.assertEqual(len(row), 8)
            self.assertEqual(bytes(row), b"\x00" * 8)

    def test_pixels_land_where_the_glyph_says(self):
        # "-" is one horizontal bar on the fourth row of the glyph, five wide,
        # in the leftmost character cell -- so row 3, top five bits.
        frame = display.render(["-"])
        self.assertEqual(frame[3][0], 0b11111000)
        for other in (0, 1, 2, 4, 5, 6):
            self.assertEqual(frame[other][0], 0)

    def test_second_line_starts_one_line_pitch_down(self):
        frame = display.render(["", "-"])
        self.assertEqual(frame[3][0], 0)
        self.assertEqual(frame[display.LINE_PITCH + 3][0], 0b11111000)

    def test_second_character_starts_one_char_pitch_right(self):
        frame = display.render([" -"])
        # Six pixels in: the top of byte 0 is clear, and the bar starts at bit 6.
        self.assertEqual(frame[3][0], 0b00000011)
        self.assertEqual(frame[3][1], 0b11100000)

    def test_unknown_characters_are_visible_rather_than_silent(self):
        # A character quietly becoming a space turns a wrong name into a
        # plausible one, which is the failure this screen exists to prevent.
        self.assertNotEqual(display.render(["~"])[0][0], 0)
        self.assertEqual(display.render(["~"])[0][0], display.render(["?"])[0][0])

    def test_overlong_text_is_dropped_not_wrapped(self):
        wide = display.render(["ABCDEFGHIJKLMNOP"])
        exact = display.render(["ABCDEFGHIJ"])
        self.assertEqual([bytes(r) for r in wide], [bytes(r) for r in exact])

    def test_extra_lines_are_dropped(self):
        many = display.render(["-"] * 20)
        self.assertEqual(len(many), 64)
        # Nothing past the last line that fits.
        self.assertEqual(bytes(many[display.ROWS * display.LINE_PITCH - 1]), b"\x00" * 8)


class TestCompose(unittest.TestCase):
    def peer(self, name, connected=True, callsign=""):
        return {"node_id": "aa11", "name": name, "callsign": callsign,
                "connected": connected}

    def test_identity_comes_first(self):
        lines = display.compose(node_id="4f8a2c", radio_name="RNode 3f2a",
                                phone_name="Pixel 7", peers=[])
        self.assertEqual(lines[0], "4F8A2C")
        self.assertEqual(lines[1], "RNODE 3F2A")
        self.assertEqual(lines[2], ">PIXEL 7")

    def test_missing_radio_name_costs_no_line(self):
        # Over USB the radio never tells us its name, and a blank line there
        # would waste a peer slot for nothing.
        lines = display.compose(node_id="4f8a2c", phone_name="Pixel 7", peers=[])
        self.assertEqual(lines[1], ">PIXEL 7")

    def test_says_so_when_nobody_is_out_there(self):
        lines = display.compose(node_id="4f8a2c", peers=[])
        self.assertIn("NO NODES", lines)

    def test_disconnected_peers_are_not_listed(self):
        lines = display.compose(node_id="4f8a2c",
                                peers=[self.peer("BRAVO", connected=False)])
        self.assertIn("NO NODES", lines)

    def test_callsign_wins_over_device_name(self):
        lines = display.compose(node_id="4f8a2c",
                                peers=[self.peer("Pixel 7", callsign="BRAVO")])
        self.assertIn("BRAVO", lines)

    def test_long_peer_lists_end_with_a_count(self):
        peers = [self.peer("NODE%d" % n) for n in range(9)]
        lines = display.compose(node_id="4f8a2c", radio_name="RNODE 3F2A",
                                phone_name="Pixel 7", peers=peers)
        self.assertTrue(lines[-1].startswith("+"))
        self.assertTrue(lines[-1].endswith("MORE"))
        # Every peer is accounted for: those shown, plus those counted.
        shown = [line for line in lines if line.startswith("NODE")]
        counted = int(lines[-1].split()[0].lstrip("+"))
        self.assertEqual(len(shown) + counted, len(peers))

    def test_never_more_lines_than_the_screen_holds(self):
        peers = [self.peer("NODE%d" % n) for n in range(30)]
        lines = display.compose(node_id="4f8a2c", radio_name="RNODE 3F2A",
                                phone_name="Pixel 7", peers=peers)
        self.assertLessEqual(len(lines), display.ROWS)

    def test_composed_lines_all_fit_the_width(self):
        peers = [self.peer("A VERY LONG DEVICE NAME INDEED")]
        lines = display.compose(node_id="4f8a2c",
                                radio_name="RNode with a long name",
                                phone_name="A phone with a long name",
                                peers=peers)
        for line in lines:
            self.assertLessEqual(len(line), display.COLUMNS, line)


if __name__ == "__main__":
    unittest.main()
