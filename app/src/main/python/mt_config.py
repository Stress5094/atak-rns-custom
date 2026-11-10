"""Reading and changing a Meshtastic board's own settings, so the Meshtastic app
is not needed to set one up.

WHAT "NECESSARY" MEANS HERE. The settings whose absence or mismatch stops two
boards hearing each other, plus the one that decides whether a board spends
airtime repeating traffic the app already manages:

  * region               a board with no region will not transmit at all
  * modulation preset    both ends must match
  * frequency slot and frequency override
  * transmit power
  * whether it repeats other nodes' packets
  * channel 0: name and key   both ends must match, or every packet is noise

This module is PURE LOGIC over a "node-like" object (anything with
`localConfig`, `channels` and the package's write calls), so it is tested
against fakes and not against a radio. mt_port supplies the real node;
rns_bridge sequences the apply, the reboot and the read-back.

THREE THINGS THAT ARE EASY TO GET WRONG, AND ARE DONE ON PURPOSE:

  * NOTHING IS WRITTEN UNLESS IT CHANGES. A write can cost the board its
    connection (the caller reconnects to read the result back), and a write of
    values it already holds buys nothing. Measured on a RAK4631 running 2.7.15:
    transmit power, repeat mode and channel name changed WITHOUT the board
    re-enumerating on USB, i.e. without a reboot. Whether a region or preset
    change reboots is checked separately and not assumed here.
  * ONE TRANSACTION. The package can open a settings transaction, send every
    change, and commit once. Without it each write is its own save and its own
    reboot.
  * THE KEY IS NEVER LOGGED OR RETURNED IN A SNAPSHOT. A snapshot says what KIND
    of key a channel has ("none", "default", "custom 256-bit") and nothing that
    could be used to join it. The key leaves this module only in a share link,
    which the operator asks for.

The library's own helpers call sys.exit() on bad input, which on a background
thread kills the thread silently. Links are therefore parsed here, and any
SystemExit that still escapes a library call is turned into a MeshConfigError.
"""

import base64
import collections
import secrets

from meshtastic.protobuf import apponly_pb2, channel_pb2, config_pb2

import radio

_LORA = config_pb2.Config.LoRaConfig
_DEVICE = config_pb2.Config.DeviceConfig
_POSITION = config_pb2.Config.PositionConfig
_BLUETOOTH = config_pb2.Config.BluetoothConfig

GPS_CHOICES = collections.OrderedDict([
    ("ENABLED", "On"),
    ("DISABLED", "Off"),
    ("NOT_PRESENT", "Not fitted"),
])

REGIONS = [n for n in _LORA.RegionCode.keys() if n != "UNSET"]

# Plain-English labels, in the order a person would think of them. The value
# sent to the board is the key; the label is only ever shown.
REBROADCAST_CHOICES = collections.OrderedDict([
    ("ALL", "Repeat everything it hears"),
    ("KNOWN_ONLY", "Repeat only for nodes it already knows"),
    ("LOCAL_ONLY", "Repeat only on this channel"),
    ("NONE", "Never repeat"),
])

TX_POWER_MAX_DBM = 30
CHANNEL_NAME_MAX = 11
CHANNEL_SLOT_MAX = 255

# A Meshtastic key is 0 bytes (no encryption), 1 byte (a numbered "default"
# key everyone knows), 16 (AES-128) or 32 (AES-256).
_KEY_SIZES = (0, 1, 16, 32)


class MeshConfigError(Exception):
    """Something the operator can act on; the message is written for them."""


# `changes`: what would differ, for showing. `problems`: why it cannot be done.
# `wants`: every value the request asked for, changed or not, for apply().
Plan = collections.namedtuple("Plan", "changes problems wants")


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def key_kind(psk):
    """What sort of key this is, never the key itself."""
    n = len(psk)
    if n == 0:
        return "none"
    if n == 1:
        return "default"
    return "custom"


def key_description(psk):
    kind = key_kind(psk)
    if kind == "none":
        return "No encryption"
    if kind == "default":
        return "The public default key (anyone can read this channel)"
    return "Custom %d-bit key" % (len(psk) * 8)


def parse_psk(text):
    """A key as the operator supplied it -> bytes.

    "none", "default", "random" / "random256", "random128", or base64 of a
    16- or 32-byte key (the form Meshtastic itself shows and shares).
    """
    t = (text or "").strip()
    if not t:
        raise MeshConfigError("The key is empty")
    low = t.lower()
    if low == "none":
        return b""
    if low == "default":
        return b"\x01"
    if low in ("random", "random256"):
        return secrets.token_bytes(32)
    if low == "random128":
        return secrets.token_bytes(16)
    try:
        padded = t + "=" * (-len(t) % 4)
        raw = base64.b64decode(padded.replace("-", "+").replace("_", "/"),
                               validate=True)
    except Exception:
        raise MeshConfigError("That key is not valid base64")
    if len(raw) not in (16, 32):
        raise MeshConfigError(
            "A custom key must be 16 or 32 bytes (%d given); use 'random' to "
            "make one" % len(raw))
    return raw


# ---------------------------------------------------------------------------
# The managed profile: the board settings the APP decides, from its own
# passphrase and radio settings, so every phone sets up every board alike --
# the way an RNode takes its frequency, preset and power from the app.
# ---------------------------------------------------------------------------

# Domain labels for the two values derived from the mesh key. Versioned so a
# later change of scheme is a new label, not a silent split of the network.
_CHANNEL_KEY_LABEL = b"atakrns-meshtastic-channel-key-v1"
_CHANNEL_NAME_LABEL = b"atakrns-meshtastic-channel-name-v1"
# v2 (2026-10-16) adds the board's node number: v1 gave every board on a mesh
# the same PIN.
_BT_PIN_LABEL = b"atakrns-meshtastic-bt-pin-v2"

# What Meshtastic firmware uses when nobody set a PIN: the code a board with
# no screen pairs with until the managed profile replaces it.
DEFAULT_BT_PIN = 123456
MANAGED_NAME_PREFIX = "rt-"

# Region for a frequency, when the board has none. Deliberately short: where
# two regions share a band (ANZ inside US's 902-928) the guess would be a
# legal claim about where the board is, so only unambiguous bands are here and
# anything else is the operator's to set.
_REGION_BANDS = (
    (902.0, 928.0, "US"),
    (863.0, 870.0, "EU_868"),
)


def derive_channel(mesh_key):
    """(channel name, 32-byte channel key) for a mesh key.

    FROM THE MESH KEY, NOT THE PASSPHRASE. The mesh key is already stretched
    (mesh.derive_key, PBKDF2), so a second slow derivation would only cost time
    at startup; and both values come out of a one-way HMAC under their own
    label, so a channel key read off a board -- it is in every share link --
    gives nothing towards the key that seals the app's messages.

    The NAME is derived too, because Meshtastic hashes name and key together to
    pick the channel: two networks with different passphrases then do not even
    try to decrypt each other's packets, and the name on the board says which
    network it is on without saying anything secret.
    """
    import hashlib
    import hmac
    if not mesh_key:
        raise MeshConfigError("There is no mesh passphrase to derive the "
                              "board's channel from")
    key = hmac.new(mesh_key, _CHANNEL_KEY_LABEL, hashlib.sha256).digest()
    tag = hmac.new(mesh_key, _CHANNEL_NAME_LABEL, hashlib.sha256).hexdigest()[:6]
    return MANAGED_NAME_PREFIX + tag, key


def derive_bt_pin(mesh_key, node_num):
    """A board's Bluetooth pairing PIN: six digits, never a leading zero (a PIN
    typed as 012345 and stored as 12345 is a pairing that fails for no visible
    reason).

    WHY DERIVED. A board with no screen cannot show a random PIN, and the
    firmware's fallback is the same 123456 on every board, which lets anyone in
    Bluetooth range pair and read the channel key. Derived like the channel, so
    every phone with the passphrase knows every board's PIN without it being
    typed, shown or stored anywhere. Its own label, so the PIN reveals nothing
    about the channel key.

    WHY PER BOARD. Over USB the board reports its node number; over Bluetooth
    the phone works it out from the address (node_num_from_ble). So each board
    gets its own PIN and the phone still needs nothing typed. 900000 PINs, so
    two boards CAN collide by chance -- about 1 in 18000 for a mesh of 10.
    """
    import hashlib
    import hmac
    if not mesh_key:
        raise MeshConfigError("There is no mesh passphrase to derive the "
                              "board's Bluetooth PIN from")
    if node_num is None or not 0 <= int(node_num) <= 0xFFFFFFFF:
        raise MeshConfigError("The board has not reported its node number, "
                              "which its Bluetooth PIN is derived from")
    digest = hmac.new(mesh_key, _BT_PIN_LABEL + int(node_num).to_bytes(4, "big"),
                      hashlib.sha256).digest()
    return 100000 + int.from_bytes(digest[:8], "big") % 900000


def node_num_from_ble(address, name=None):
    """A board's node number from what a phone sees before pairing: its
    Bluetooth address and advertised name. None if the address is not one.

    MEASURED (2026-10-15, two nRF52840 boards): the node number IS the low four
    bytes of the Bluetooth address -- RAK4631 02:00:00:0B:AD:02 is !000bad02,
    Tracker L1 02:00:00:0B:AD:03 is !000bad03.

    NOT MEASURED, from the firmware and ESP32 conventions: an ESP32 numbers
    itself from its base MAC and advertises at base + 2, so its address reads
    two above its node number. Its name ends "_xxxx", the node number's low two
    bytes (the L1's "1.2_ad03" agrees), so when the name says the address is
    off, the offset that makes them agree is taken. With no name, or no offset
    that agrees, the address is taken as it is.
    """
    import re
    hexes = re.sub(r"[^0-9A-Fa-f]", "", address or "")
    if len(hexes) != 12:
        return None
    low = int(hexes[4:], 16)
    m = re.search(r"_([0-9A-Fa-f]{4})$", (name or "").strip())
    if m:
        tail = int(m.group(1), 16)
        for offset in range(4):
            candidate = (low - offset) & 0xFFFFFFFF
            if candidate & 0xFFFF == tail:
                return candidate
    return low


def region_for_frequency(freq_mhz):
    for lo, hi, region in _REGION_BANDS:
        if lo <= freq_mhz <= hi:
            return region
    return None


def managed_request(mesh_key, preset, frequency_hz, txpower_dbm,
                    current_region="UNSET", current_gps="ENABLED", node_num=None):
    """The request (for plan/apply) that puts a board on the app's settings.

    Contains the channel key as bytes; never log it, never pass it through
    JSON. What the app owns:
      * preset, frequency (as an override, so the slot number stops mattering),
        transmit power -- the app's radio settings, as for an RNode
      * channel name and key -- derived from the passphrase
      * never repeat -- the app does its own relaying
      * the board's own GPS off. MEASURED (RAK4631, 2.7.15, 2026-10-14): with
        a client connected and its GPS on, a board broadcast its OWN position
        every 15-50 s, hop limit 7, although set to broadcast once a day -- in
        both the TAK and CLIENT roles. With GPS off: none in 3 minutes. Every
        repeating board then repeats each one, so three boards spent roughly a
        quarter of the channel on positions the app sends itself anyway. A
        board with no GPS fitted (NOT_PRESENT) is left alone.
      * Bluetooth on, with a FIXED PIN derived from the passphrase and the
        board's node number (derive_bt_pin), so a board with no screen can be paired from a phone
        that has never seen it. The PIN is bytes-level secret like the key:
        not in a snapshot, not in a change description.
    The REGION is kept when the board has one (it is a legal setting for where
    the board is, and the app does not know better); a board with none gets
    the region its frequency lies in, or a problem if there is no such region.
    """
    name, psk = derive_channel(mesh_key)
    freq_mhz = round(frequency_hz / 1e6, 4)
    request = {
        "preset": preset,
        "override_frequency_mhz": freq_mhz,
        "tx_power_dbm": min(int(txpower_dbm), TX_POWER_MAX_DBM),
        "rebroadcast": "NONE",
        "channel_name": name,
        "psk_bytes": psk,
    }
    # No node number, no PIN: profile_problems says so rather than this
    # raising, so the rest of the comparison still shows.
    if node_num is not None:
        request["bt_pin"] = derive_bt_pin(mesh_key, node_num)
    if current_region == "UNSET":
        region = region_for_frequency(freq_mhz)
        request["region"] = region or "UNSET"
    if current_gps != "NOT_PRESENT":
        request["gps_mode"] = "DISABLED"
    return request


def profile_problems(request):
    """Reasons a managed request cannot be applied as it stands, before any
    board is consulted (plan() checks the rest)."""
    if "bt_pin" not in request:
        return ["The board has not reported its node number yet, and its "
                "Bluetooth PIN is derived from it"]
    if request.get("region") == "UNSET":
        return ["The board has no region and the app's frequency (%.3f MHz) is "
                "not in a band this app knows -- set the region under Network settings > Meshtastic board"
                % request["override_frequency_mhz"]]
    return []


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------

def make_url(node):
    """The shareable link for this board's channel and radio settings. Contains
    the channel key. Same format as the Meshtastic app's, so a link made here
    opens there and the reverse."""
    chan_set = apponly_pb2.ChannelSet()
    for c in node.channels or []:
        if c.role in (channel_pb2.Channel.Role.PRIMARY,
                      channel_pb2.Channel.Role.SECONDARY):
            chan_set.settings.append(c.settings)
    chan_set.lora_config.CopyFrom(node.localConfig.lora)
    raw = base64.urlsafe_b64encode(chan_set.SerializeToString()).decode("ascii")
    return "https://meshtastic.org/e/#" + raw.rstrip("=")


def parse_url(url):
    """A share link -> apponly ChannelSet. Raises MeshConfigError, never exits."""
    u = (url or "").strip()
    if "/#" not in u:
        raise MeshConfigError("That is not a Meshtastic share link")
    b64 = u.split("/#")[-1].strip()
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        chan_set = apponly_pb2.ChannelSet()
        chan_set.ParseFromString(raw)
    except Exception:
        raise MeshConfigError("That share link is damaged")
    if len(chan_set.settings) == 0:
        raise MeshConfigError("That share link contains no channel")
    return chan_set


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _primary(node):
    chans = node.channels or []
    for c in chans:
        if c.role == channel_pb2.Channel.Role.PRIMARY:
            return c
    return chans[0] if chans else None


def preset_app_name(enum_name):
    """The app's name for a board preset ("Long Fast"), or None if the app has
    no name for it."""
    pretty = enum_name.replace("_", " ").title()
    return pretty if pretty in radio.PRESETS else None


def snapshot(node):
    """What the board is set to, safe to show and to log (no key)."""
    lora = node.localConfig.lora
    device = node.localConfig.device
    primary = _primary(node)
    preset = _LORA.ModemPreset.Name(lora.modem_preset)
    chan = {"name": "", "key_kind": "none", "key_description": key_description(b"")}
    if primary is not None:
        psk = bytes(primary.settings.psk)
        chan = {"name": primary.settings.name,
                "key_kind": key_kind(psk),
                "key_description": key_description(psk)}
    return {
        "region": _LORA.RegionCode.Name(lora.region),
        "use_preset": bool(lora.use_preset),
        "preset": preset,
        "preset_name": preset_app_name(preset) if lora.use_preset else None,
        "channel_num": lora.channel_num,
        "override_frequency_mhz": round(lora.override_frequency, 4),
        "tx_power_dbm": lora.tx_power,
        "tx_enabled": bool(lora.tx_enabled),
        "rebroadcast": _DEVICE.RebroadcastMode.Name(device.rebroadcast_mode),
        "role": _DEVICE.Role.Name(device.role),
        "gps_mode": _POSITION.GpsMode.Name(node.localConfig.position.gps_mode),
        # Mode and whether it is on; never the PIN itself.
        "bluetooth": {
            "enabled": bool(node.localConfig.bluetooth.enabled),
            "mode": _BLUETOOTH.PairingMode.Name(node.localConfig.bluetooth.mode),
        },
        "channel": chan,
    }


# ---------------------------------------------------------------------------
# Is the board set up so that it behaves like the radio this app expects?
# ---------------------------------------------------------------------------

# Roles whose whole job is to repeat other nodes' packets.
REPEATER_ROLES = ("ROUTER", "ROUTER_CLIENT", "REPEATER", "ROUTER_LATE")


def warnings(snap):
    """Plain-language reasons this board will not behave like an RNode, from a
    snapshot. Empty when it is set up as the app expects.

    The first is the one that matters most. The app does its own relaying (a
    coverage test, a weighted delay, a cancel if somebody else relayed first, a
    token bucket), and counts "my own frame came back relayed" as a free
    acknowledgement. A board that ALSO repeats packets could put a second flood on
    the channel and hide that signal, because the sending board treats a
    neighbour's repeat as its own acknowledgement and does not pass it up.

    WHAT WAS MEASURED (2026-10-14, three boards on one channel, 2.7.15): the
    app's own packets (hop limit 0) arrived at every receiving board with hop
    counts 0/0 and were never seen repeated, over ~1,000 packets. That is
    strong evidence, not proof: a board drops a duplicate itself before its
    client sees it, so a repeat of ours would be invisible from here, and the
    board counters that would show it are reported too rarely to separate in a
    short run. What a repeating board DOES do, measured: it repeats other
    boards' own Meshtastic traffic -- positions, node info, at hop limit 7 --
    5 relays in a 2-minute window with none of our traffic on the air.

    The others are silent failures: a board with no region, or with transmit
    off, looks exactly like a dead radio.
    """
    out = []
    if snap.get("rebroadcast") != "NONE":
        out.append("This board is set to repeat other nodes' packets (%s). It "
                   "repeats other boards' own Meshtastic traffic (their positions "
                   "and node info), which costs airtime. The app's own packets go "
                   "out with a hop limit of 0 and have not been seen repeated. "
                   "'Never repeat' under Network settings > Meshtastic board stops the rest."
                   % REBROADCAST_CHOICES.get(snap.get("rebroadcast"),
                                             snap.get("rebroadcast")))
    if snap.get("role") in REPEATER_ROLES:
        out.append("The board's role (%s) makes it a repeater. Use a client role."
                   % snap.get("role"))
    if snap.get("region") == "UNSET":
        out.append("No region is set on the board, so it will not transmit.")
    if not snap.get("tx_enabled", True):
        out.append("Transmit is turned off on the board.")
    if snap.get("gps_mode") == "ENABLED":
        out.append("The board's own GPS is on, so it broadcasts its own position "
                   "every 15-50 s whenever the app is connected, and other boards "
                   "repeat it -- airtime the app's own positions do not need. "
                   "'Use the app's settings' under Network settings > Meshtastic board turns it off.")
    bt = snap.get("bluetooth") or {}
    if bt.get("enabled") and bt.get("mode") == "NO_PIN":
        out.append("The board's Bluetooth needs no PIN: anyone in range can "
                   "connect to it and read the channel key. 'Use the app's "
                   "settings' under Network settings > Meshtastic board gives it the PIN from the passphrase.")
    return out


# ---------------------------------------------------------------------------
# Planning: validate a request and work out what would actually change.
# ---------------------------------------------------------------------------

def _preset_enum(value):
    """An app preset name ("Long Fast") or a board one ("LONG_FAST") -> enum
    name, or None."""
    v = str(value).strip()
    for app_name in radio.PRESETS:
        if v.lower() == app_name.lower():
            return app_name.upper().replace(" ", "_")
    up = v.upper().replace(" ", "_")
    return up if up in _LORA.ModemPreset.keys() else None


def plan(node, request):
    """Work out what `request` would change on `node`. Returns a Plan.

    `changes` is an ordered list of {"field", "label", "old", "new"} for ONLY the
    settings that differ; `problems` is a list of reasons the request cannot be
    applied. A request with any problem applies nothing at all: half of a
    channel change is how a node ends up on a network of one.
    """
    current = snapshot(node)
    problems = []
    changes = []
    wants = {}          # field -> value to write

    request = dict(request or {})
    url = request.pop("url", None)
    if url:
        try:
            chan_set = parse_url(url)
        except MeshConfigError as e:
            return Plan([], [str(e)], {})
        first = chan_set.settings[0]
        request.setdefault("channel_name", first.name)
        request["psk_bytes"] = bytes(first.psk)
        lc = chan_set.lora_config
        if lc.region != _LORA.RegionCode.UNSET:
            request.setdefault("region", _LORA.RegionCode.Name(lc.region))
        if lc.use_preset:
            request.setdefault("preset", _LORA.ModemPreset.Name(lc.modem_preset))
        if lc.channel_num:
            request.setdefault("channel_num", lc.channel_num)

    def note(field, label, old, new, show_old=None, show_new=None):
        wants[field] = new
        if old != new:
            changes.append({"field": field, "label": label,
                            "old": old if show_old is None else show_old,
                            "new": new if show_new is None else show_new})

    if "region" in request:
        r = str(request["region"]).strip().upper()
        if r not in REGIONS:
            problems.append("Unknown region %r" % request["region"])
        else:
            note("region", "Region", current["region"], r)

    if "preset" in request:
        e = _preset_enum(request["preset"])
        if e is None:
            problems.append("Unknown preset %r" % (request["preset"],))
        else:
            old = current["preset"] if current["use_preset"] else None
            note("preset", "Preset", old, e,
                 show_old=current["preset_name"] or old,
                 show_new=preset_app_name(e) or e)

    if "channel_num" in request:
        try:
            n = int(request["channel_num"])
            if not 0 <= n <= CHANNEL_SLOT_MAX:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("The frequency slot must be a whole number from 0 "
                            "to %d (0 picks one automatically)" % CHANNEL_SLOT_MAX)
        else:
            note("channel_num", "Frequency slot", current["channel_num"], n)

    if "override_frequency_mhz" in request:
        try:
            f = round(float(request["override_frequency_mhz"]), 4)
            lo, hi = radio.FREQ_MIN_HZ / 1e6, radio.FREQ_MAX_HZ / 1e6
            if f != 0 and not lo <= f <= hi:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("The frequency override must be 0 (none) or between "
                            "%g and %g MHz" % (radio.FREQ_MIN_HZ / 1e6,
                                               radio.FREQ_MAX_HZ / 1e6))
        else:
            note("override_frequency_mhz", "Frequency override (MHz)",
                 current["override_frequency_mhz"], f)

    if "tx_power_dbm" in request:
        try:
            p = int(request["tx_power_dbm"])
            if not 0 <= p <= TX_POWER_MAX_DBM:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("Transmit power must be a whole number from 0 to %d "
                            "dBm (0 uses the board's default)" % TX_POWER_MAX_DBM)
        else:
            note("tx_power_dbm", "Transmit power (dBm)", current["tx_power_dbm"], p)

    if "gps_mode" in request:
        g = str(request["gps_mode"]).strip().upper()
        if g not in GPS_CHOICES:
            problems.append("Unknown GPS setting %r" % request["gps_mode"])
        else:
            note("gps_mode", "The board's own GPS", current["gps_mode"], g,
                 show_old=GPS_CHOICES.get(current["gps_mode"], current["gps_mode"]),
                 show_new=GPS_CHOICES[g])

    if "rebroadcast" in request:
        m = str(request["rebroadcast"]).strip().upper()
        if m not in REBROADCAST_CHOICES:
            problems.append("Unknown repeat mode %r" % request["rebroadcast"])
        else:
            note("rebroadcast", "Repeating other nodes' packets",
                 current["rebroadcast"], m,
                 show_old=REBROADCAST_CHOICES.get(current["rebroadcast"],
                                                  current["rebroadcast"]),
                 show_new=REBROADCAST_CHOICES[m])

    if "bt_pin" in request:
        try:
            pin = int(request["bt_pin"])
            if not 100000 <= pin <= 999999:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("A Bluetooth PIN must be six digits, not starting with 0")
        else:
            bt = node.localConfig.bluetooth
            # One change line for all three fields: "Bluetooth: on, PIN from
            # the passphrase" is what the operator needs, and the PIN itself is
            # not shown -- the pairing screen is where it is read.
            wants["bt_pin"] = pin
            if not (bt.enabled and bt.mode == _BLUETOOTH.PairingMode.Value("FIXED_PIN")
                    and bt.fixed_pin == pin):
                if not bt.enabled:
                    old = "Off"
                elif bt.mode == _BLUETOOTH.PairingMode.Value("FIXED_PIN"):
                    old = ("On, the default PIN 123456" if bt.fixed_pin == DEFAULT_BT_PIN
                           else "On, a different fixed PIN")
                else:
                    old = "On, %s" % {"RANDOM_PIN": "a PIN shown on the board's screen",
                                      "NO_PIN": "NO PIN (anyone nearby can pair)"}.get(
                        _BLUETOOTH.PairingMode.Name(bt.mode), _BLUETOOTH.PairingMode.Name(bt.mode))
                changes.append({"field": "bt_pin", "label": "Bluetooth pairing",
                                "old": old,
                                "new": "On, this board's PIN from the mesh passphrase"})

    if "channel_name" in request:
        name = str(request["channel_name"]).strip()
        if len(name.encode("utf-8")) > CHANNEL_NAME_MAX:
            problems.append("A channel name can be at most %d characters"
                            % CHANNEL_NAME_MAX)
        else:
            note("channel_name", "Channel name", current["channel"]["name"], name)

    psk = None
    if "psk_bytes" in request:
        psk = request["psk_bytes"]
    elif request.get("psk"):
        try:
            psk = parse_psk(request["psk"])
        except MeshConfigError as e:
            problems.append(str(e))
    if psk is not None:
        primary = _primary(node)
        old_psk = bytes(primary.settings.psk) if primary is not None else b""
        # Compared as bytes, never shown: "changed" is all the operator needs.
        if old_psk != psk:
            wants["psk"] = psk
            old_text, new_text = key_description(old_psk), key_description(psk)
            # Two keys of the same KIND describe identically, and "Custom
            # 256-bit key -> Custom 256-bit key" reads as no change at all
            # (seen on the OnePlus 2026-10-14) while every board still on the
            # old key is about to stop hearing this one.
            if old_text == new_text:
                old_text = "%s (the current one)" % old_text
                new_text = "A different %s" % (new_text[0].lower() + new_text[1:])
            changes.append({"field": "psk", "label": "Channel key",
                            "old": old_text, "new": new_text})
        else:
            wants["psk"] = psk

    if problems:
        return Plan([], problems, {})
    return Plan(changes, [], wants)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def apply(node, request):
    """Write `request` to `node` in ONE settings transaction, only the parts
    that differ. Returns the list of changes made (empty if there was nothing
    to do). Raises MeshConfigError for anything the operator can act on."""
    the_plan = plan(node, request)
    if the_plan.problems:
        raise MeshConfigError("; ".join(the_plan.problems))
    changes = the_plan.changes
    if not changes:
        return []

    fields = {c["field"] for c in changes}
    wants = the_plan.wants
    lora = node.localConfig.lora
    device = node.localConfig.device

    # Mutate the node's local model first, then send. A half-built message is
    # never sent: nothing below this line can fail on validation.
    lora_changed = fields & {"region", "preset", "channel_num",
                             "override_frequency_mhz", "tx_power_dbm"}
    if "region" in fields:
        lora.region = _LORA.RegionCode.Value(wants["region"])
    if "preset" in fields:
        lora.use_preset = True
        lora.modem_preset = _LORA.ModemPreset.Value(wants["preset"])
    if "channel_num" in fields:
        lora.channel_num = wants["channel_num"]
    if "override_frequency_mhz" in fields:
        lora.override_frequency = wants["override_frequency_mhz"]
    if "tx_power_dbm" in fields:
        lora.tx_power = wants["tx_power_dbm"]
    if "rebroadcast" in fields:
        device.rebroadcast_mode = _DEVICE.RebroadcastMode.Value(wants["rebroadcast"])
    if "gps_mode" in fields:
        node.localConfig.position.gps_mode = _POSITION.GpsMode.Value(wants["gps_mode"])
    if "bt_pin" in fields:
        bt = node.localConfig.bluetooth
        bt.enabled = True
        bt.mode = _BLUETOOTH.PairingMode.Value("FIXED_PIN")
        bt.fixed_pin = wants["bt_pin"]

    primary = _primary(node)
    channel_changed = bool(fields & {"channel_name", "psk"})
    if channel_changed:
        if primary is None:
            raise MeshConfigError("The board has not reported its channels yet")
        if "channel_name" in fields:
            primary.settings.name = wants["channel_name"]
        if "psk" in fields:
            primary.settings.psk = wants["psk"]

    try:
        node.beginSettingsTransaction()
        if lora_changed:
            node.writeConfig("lora")
        if "rebroadcast" in fields:
            node.writeConfig("device")
        if "gps_mode" in fields:
            node.writeConfig("position")
        if "bt_pin" in fields:
            node.writeConfig("bluetooth")
        if channel_changed:
            node.writeChannel(primary.index)
        node.commitSettingsTransaction()
    except SystemExit as e:
        # The package exits the process-level way on some errors; on a worker
        # thread that would end the thread and report nothing.
        raise MeshConfigError("The board refused the change (%s)" % (e,))
    return changes
