"""What the radio's own screen shows.

The RNode has a small OLED that normally shows the firmware's own pages. The
firmware will hand a 64x64 corner of it over to whatever is driving the radio
-- that is how Sideband puts its logo there -- and this module decides what
goes in it.

WHY BOTHER. Over a cable the radio's identity is obvious: it is the thing on
the end of the cable. Over Bluetooth it is not, and neither is which phone it
belongs to, or whether it is hearing anybody. Those three questions are
currently answerable only by taking a phone out and unlocking it, which is the
one thing you cannot do while carrying something in both hands.

WHAT THE FIRMWARE GIVES US. Exactly 64x64 pixels, one bit each, drawn 1:1 --
no scaling, no colour, no text. There is no "print this string" command, only
"here are 8 bytes, that is row N". So the font below is not decoration; it is
the only way to put a word on that screen.

That budget works out at 8 rows of 10 characters, which is why everything here
truncates rather than wraps: a name cut short is still recognisable, and a name
that steals the line below it costs a whole peer.

Pure and testable, like cot_codec and peers: nothing here talks to a radio.
rns_bridge owns the pushing, this owns the picture.
"""

WIDTH = 64
HEIGHT = 64
BYTES_PER_ROW = WIDTH // 8

# 5 pixels of glyph, one of gap. 7 of glyph, one of gap.
CHAR_WIDTH = 5
CHAR_PITCH = 6
LINE_HEIGHT = 7
LINE_PITCH = 8

COLUMNS = WIDTH // CHAR_PITCH    # 10 characters
ROWS = HEIGHT // LINE_PITCH      # 8 lines

# A 5x7 font, written as the pixels themselves rather than as hex, because the
# only way to check a font is to look at it. One line per glyph: the character,
# then its seven rows top to bottom.
#
# Uppercase only. Every string that reaches this screen is upper-cased first --
# callsigns and hex ids already are, device names look no worse for it, and a
# lowercase set would double the table for characters that are 5 pixels tall
# either way.
_FONT_ART = """
  00000 00000 00000 00000 00000 00000 00000
A 01110 10001 10001 11111 10001 10001 10001
B 11110 10001 11110 10001 10001 10001 11110
C 01110 10001 10000 10000 10000 10001 01110
D 11110 10001 10001 10001 10001 10001 11110
E 11111 10000 11110 10000 10000 10000 11111
F 11111 10000 11110 10000 10000 10000 10000
G 01110 10001 10000 10111 10001 10001 01111
H 10001 10001 11111 10001 10001 10001 10001
I 11111 00100 00100 00100 00100 00100 11111
J 00111 00010 00010 00010 00010 10010 01100
K 10001 10010 10100 11000 10100 10010 10001
L 10000 10000 10000 10000 10000 10000 11111
M 10001 11011 10101 10101 10001 10001 10001
N 10001 11001 10101 10011 10001 10001 10001
O 01110 10001 10001 10001 10001 10001 01110
P 11110 10001 10001 11110 10000 10000 10000
Q 01110 10001 10001 10001 10101 10010 01101
R 11110 10001 10001 11110 10100 10010 10001
S 01111 10000 10000 01110 00001 00001 11110
T 11111 00100 00100 00100 00100 00100 00100
U 10001 10001 10001 10001 10001 10001 01110
V 10001 10001 10001 10001 10001 01010 00100
W 10001 10001 10001 10101 10101 11011 10001
X 10001 10001 01010 00100 01010 10001 10001
Y 10001 10001 01010 00100 00100 00100 00100
Z 11111 00001 00010 00100 01000 10000 11111
0 01110 10001 10011 10101 11001 10001 01110
1 00100 01100 00100 00100 00100 00100 01110
2 01110 10001 00001 00010 00100 01000 11111
3 11111 00010 00100 00010 00001 10001 01110
4 00010 00110 01010 10010 11111 00010 00010
5 11111 10000 11110 00001 00001 10001 01110
6 00110 01000 10000 11110 10001 10001 01110
7 11111 00001 00010 00100 01000 01000 01000
8 01110 10001 10001 01110 10001 10001 01110
9 01110 10001 10001 01111 00001 00010 01100
- 00000 00000 00000 11111 00000 00000 00000
. 00000 00000 00000 00000 00000 01100 01100
: 00000 01100 01100 00000 01100 01100 00000
+ 00000 00100 00100 11111 00100 00100 00000
* 00000 10101 01110 11111 01110 10101 00000
> 10000 01000 00100 00010 00100 01000 10000
? 11111 10001 10001 10001 10001 10001 11111
"""


def _parse_font(art):
    font = {}
    for line in art.strip("\n").split("\n"):
        if not line.strip():
            continue
        # The space glyph's own key is a space, so split off exactly one
        # leading character rather than splitting on whitespace.
        char, rows = line[0], line[1:].split()
        if len(rows) != LINE_HEIGHT:
            raise ValueError("glyph %r has %d rows, expected %d"
                             % (char, len(rows), LINE_HEIGHT))
        for row in rows:
            if len(row) != CHAR_WIDTH or set(row) - set("01"):
                raise ValueError("glyph %r has a bad row %r" % (char, row))
        font[char] = rows
    return font


FONT = _parse_font(_FONT_ART)

# Anything the font does not have. A visible box rather than a blank, because a
# character silently becoming a space turns a wrong name into a plausible one.
FALLBACK = "?"


def blank():
    """An empty framebuffer: HEIGHT rows of BYTES_PER_ROW bytes."""
    return [bytearray(BYTES_PER_ROW) for _ in range(HEIGHT)]


def render(lines):
    """Draw up to ROWS lines of text into a 64x64 one-bit framebuffer.

    Returns a list of HEIGHT rows, each BYTES_PER_ROW bytes, most significant
    bit leftmost -- the order the firmware's own drawBitmap expects.

    Anything past the edges is dropped rather than wrapped. See the module
    docstring: on a screen this size, wrapping costs a whole line of something
    else.
    """
    frame = blank()
    for line_no, text in enumerate(lines[:ROWS]):
        top = line_no * LINE_PITCH
        for char_no, char in enumerate(str(text).upper()[:COLUMNS]):
            glyph = FONT.get(char, FONT[FALLBACK])
            left = char_no * CHAR_PITCH
            for row_no, row in enumerate(glyph):
                y = top + row_no
                if y >= HEIGHT:
                    break
                for col_no, pixel in enumerate(row):
                    if pixel == "1":
                        x = left + col_no
                        if x < WIDTH:
                            frame[y][x // 8] |= 0x80 >> (x % 8)
    return frame


def fit(text, width=COLUMNS):
    """Shorten to what the screen holds. No ellipsis -- it costs a character
    that is better spent on the name."""
    return str(text).upper()[:width]


def compose(node_id="", radio_name="", phone_name="", peers=(), rows=ROWS):
    """The lines to show, in the order the questions get asked.

    Identity first, because that is the one thing you cannot work out by
    looking at anything else; then who this radio belongs to; then who it can
    hear. Peers are named rather than counted -- a count tells you the mesh is
    alive, a name tells you whether the person you are waiting for is.

    `peers` is the peer-table snapshot: dicts with `name`, `callsign`,
    `node_id` and `connected`.
    """
    lines = []
    if node_id:
        lines.append(fit(node_id))
    if radio_name:
        lines.append(fit(radio_name))
    if phone_name:
        # The '>' marks the phone rather than another radio -- two bare names
        # in a row is the one thing on this screen that could be misread.
        lines.append(fit(">" + str(phone_name)))

    lines.append("-" * COLUMNS)

    connected = [p for p in peers if p.get("connected")]
    if not connected:
        lines.append("NO NODES")
        return lines[:rows]

    room = rows - len(lines)
    labels = [fit(p.get("callsign") or p.get("name") or p.get("node_id") or "?")
              for p in connected]
    if len(labels) > room:
        # Keep one line for the count of what did not fit. Saying "+3 MORE" is
        # worth more than a fourth name, because it is the difference between
        # a short list and a truncated one.
        shown = labels[:max(room - 1, 0)]
        lines.extend(shown)
        lines.append(fit("+%d MORE" % (len(labels) - len(shown))))
    else:
        lines.extend(labels)
    return lines[:rows]
