"""Sound cards, keying and capture, with no idea what a waveform is.

RECOVERED, NOT WRITTEN. Every function here was working code in
`tools/otair_test.py`, which went out with the FM teardown (`0355f92`) and
took `tools/passband_sweep.py` and `tools/rx_compare.py` down with it -- both
of which the teardown commit says it kept on purpose, one of them because
"without [it] the rebuild's first checklist item cannot be done". They were
kept and broken in the same commit. What is recovered is the half that is
about hardware; the half that was about the old modem is deliberately left
behind.

WHY IT IS SPLIT OUT THIS WAY. otair_test took `(modem, payload)` and did
everything from framing to decoding, so measuring a different waveform meant
either editing it or writing a second copy of the device handling. Here the
boundary is float samples: a caller hands over audio and gets audio back, and
every lesson below about cards, channels, keying and staleness applies
whatever produced it. That is what lets the rebuilt transport be measured by
the same instrument that measured the one it replaces.

THE SAMPLE RATE IS A PARAMETER AND THE DEFAULT IS 48000. This is the one
thing to get right before reading any number out of this file, because the
record is split on it and that split is not written down anywhere:

  * The BENCH ran at 48000 Hz. otair_test: `FS = 48000  # what the sound
    cards run at; no resampling anywhere`. Both K1 PRO cards report capture
    locked at 48000 via --dump-hw-params, so raw `hw:` at any other rate
    either fails or quietly resamples.
  * The PHONE ran at 8000 Hz, which is what `docs/fm-lessons.md` section 2
    records as the shipped operating point -- with "fixed by" left as a dash,
    because nothing fixed it.

So the symbol rates in the record (375, 480, 500 Bd) are rates that divide
48000, and `fm_config.SAMPLE_RATE_HZ = 8000` cannot even construct two of
them. Anything comparing against a historical figure must run at the rate
that figure was taken at.

ALSA BY DEFAULT, PIPEWIRE KEPT. PipeWire is a sound SERVER: it owns the card
and whatever it does to the stream on the way through -- resampling, channel
remixing, its own volume curve -- sits between this file and the radio,
unlogged and unversioned. On a link whose failures are already marginal and
already hard to attribute, that is a variable nobody chose. It was also
observed holding these two cards with repeated "Device or resource busy".
The pipewire path stays because every number in the older bench record was
taken through it, and reproducing an old measurement means reproducing its
path.
"""

import math
import os
import re
import subprocess
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ptt_test


# The bench's sample rate, and the default here. See the module docstring --
# this is NOT the phone's 8000 Hz, and the two are not interchangeable when
# comparing against a recorded figure.
BENCH_FS = 48000

# Silence around the burst inside the played file. The radio needs the front
# one: PTT is asserted first but the carrier is not up for another 55 ms
# (measured), and anything transmitted before then is simply gone.
LEAD_SILENCE_S = 0.30
TAIL_SILENCE_S = 0.30

# How loud the recorded burst is made before decoding. Not 1.0: the
# demodulator multiplies by its own peak constant and clamps at int16, so a
# burst normalised to full scale is a burst clipped inside the decoder.
DECODE_PEAK = 0.5

# Energy either side of the burst, kept when trimming. Generous, because
# cutting into a preamble costs the whole frame and keeping noise costs
# nothing but time.
TRIM_PAD_S = 0.25

# Measured on this bench: MD-UV390 keyed by a Digirig Lite. airtime.fm()'s
# FM_PTT_KEYUP_S must agree with it.
PTT_KEYUP_S = 0.055

# What VOX holds the channel for after the audio stops, same radio, same
# Digirig cable. Through an APRS-K1 it was 3.2 s, which is a property of that
# cable rather than of the radio.
VOX_HANG_S = 0.125

# ALSA buffer time, microseconds, for both directions on the bench.
#
# HALF A SECOND, WHICH IS ABSURD FOR AUDIO AND CORRECT HERE. Nothing on this
# bench is interactive: a burst is built, written to disk, played, recorded to
# disk and decoded afterwards, so latency buys nothing and slack buys
# everything. Both dongles are 12 Mbps FULL-SPEED devices sharing one root
# hub, and a playback stream on one was measured starving the capture on the
# other -- a 0.7 s run of digital silence (peak 0.001, against a 0.07 hiss
# floor) in the MIDDLE of a 3.70 s transmission that PTT held continuously.
# A default-sized buffer has no margin for that; this does.
ALSA_BUFFER_US = 500000

BACKEND_ALSA = "alsa"
BACKEND_PIPEWIRE = "pipewire"
ALL_BACKENDS = (BACKEND_ALSA, BACKEND_PIPEWIRE)
DEFAULT_BACKEND = BACKEND_ALSA


class BenchError(RuntimeError):
    """The bench hardware could not be driven, or produced nothing to trust.

    Its own class so a sweep can tell "this run did not happen" apart from
    "this configuration did not deliver" -- the second is a result worth
    recording and the first is not.
    """


# -- device discovery -------------------------------------------------------

def alsa_devices(kind):
    """The sound cards ALSA can see, as [(index, id, name)].

    `kind` is "capture" or "playback" -- a card can do one and not the other,
    and this bench uses two cards that each do both.
    """
    cmd = {"capture": "arecord", "playback": "aplay"}[kind]
    try:
        out = subprocess.run([cmd, "-l"], capture_output=True, text=True,
                             check=False).stdout
    except FileNotFoundError:
        raise BenchError("%s is not installed -- the %s backend needs "
                         "alsa-utils" % (cmd, BACKEND_ALSA))
    found = []
    for line in out.splitlines():
        # "card 5: Audio [USB Audio], device 0: USB Audio [USB Audio]"
        m = re.match(r"card (\d+): (\S+) \[([^]]*)\], device (\d+):", line)
        if m and m.group(4) == "0":
            found.append((int(m.group(1)), m.group(2), m.group(3)))
    return found


def resolve_alsa_device(name, kind):
    """An ALSA device string for `name`, or raise with what IS available.

    Accepts what ALSA itself accepts ("hw:5,0", "plughw:5,0", "default")
    verbatim, a bare card index ("5"), or a substring of the card's id or
    name ("Audio_1"). The substring form is there because a card's INDEX
    moves when another USB device is plugged in, so a bench script that
    hardcodes an index is one replug away from recording the wrong radio.

    A WRONG DEVICE MUST FAIL LOUDLY HERE. arecord against a missing card does
    error out, but a wrong card records a silent file that looks exactly like
    a radio that never transmitted.
    """
    if name is None:
        return None
    if re.match(r"^(hw|plughw|sysdefault|default|pulse)\b", name):
        return name
    available = alsa_devices(kind)
    if name.isdigit():
        matches = [d for d in available if d[0] == int(name)]
    else:
        low = name.lower()
        matches = [d for d in available
                   if low in d[1].lower() or low in d[2].lower()]
    if len(matches) == 1:
        return "hw:%d,0" % matches[0][0]
    listing = "\n  ".join("hw:%d,0  %s [%s]" % d for d in available) or "(none)"
    if not matches:
        raise BenchError("no %s card matches %r. Available:\n  %s"
                         % (kind, name, listing))
    raise BenchError(
        "%r matches %d %s cards, which is ambiguous -- both K1 PRO cables "
        "enumerate under nearly the same name, so say which by index "
        "(hw:N,0). Available:\n  %s" % (name, len(matches), kind, listing))


def alsa_usb_device(hw_dev):
    """The physical USB device behind an ALSA `hw:N,0` string, or None.

    Returns `ptt_test.usb_device_id`'s value for the card's kernel node --
    the same identity a CM108 hidraw node is matched against in
    `ptt_test.find_cm108`, so a sound card and the PTT GPIO on the SAME
    physical Digirig Lite can be tied together even though the CM108 protocol
    has no notion of which card it belongs to. None for anything that is not
    a plain `hw:N,0`, since there is then no card index to look up.
    """
    m = re.match(r"^hw:(\d+),", hw_dev or "")
    if not m:
        return None
    device_dir = "/sys/class/sound/card%s/device" % m.group(1)
    if not os.path.exists(device_dir):
        return None
    return ptt_test.usb_device_id(device_dir)


#: USB vendor ids that key PTT by a CM108-style HID GPIO report.
#:
#: `ptt_test.find_cm108` defaults to C-Media's 0x0D8C alone, which is correct
#: for a Digirig Lite and silently returns None for a K1 PRO -- it reports as
#: a generic USB audio device under its OWN vendor id (0x001F, enumerating
#: here as "Walmart USB Audio") while speaking the same HID GPIO protocol.
#: tools/99-digirig.rules already grants hidraw access to both, so without
#: this list the permissions are right, the node exists, and keying still
#: looks unavailable. Which is the failure mode that rule's comment was
#: written about.
PTT_HID_VENDORS = (0x0D8C, 0x001F)


def hid_output_report_bytes(hidraw_path):
    """Size of this HID device's largest OUTPUT report, or 0 if it has none.

    THE CHEAP CHECK THAT ANSWERS "WHY DOES KEYING DO NOTHING". CM108-style
    PTT works by writing an output report to drive a GPIO pin, so a device
    with no OUTPUT item in its report descriptor has no GPIO to write to and
    cannot key anything, at any pin number. Without this, that case surfaces
    as `BrokenPipeError: [Errno 32] Broken pipe` out of os.write -- which
    reads like a transient fault and sent this bench hunting for the right
    --ptt-gpio value for a pin that does not exist.

    Measured on the two cables here:

      * Digirig Lite (0d8c:0012) ends `09 00 95 04 91 02` -- Report Count 4,
        Output. Four bytes, no report ID, hence the five-byte write
        ([report id 0] + 4). That is the whole story behind ptt_test's
        "the datasheet describes four and four fails with EPIPE".
      * K1 PRO (001f:1613) has Input items only -- consumer-control buttons
        and one telephony bit, then End Collection. NO output report at all,
        which makes it VOX-only. tools/rx_compare.py already said so ("the
        K1 PRO has none"); tools/99-digirig.rules claims the opposite and is
        wrong.

    Walks the descriptor as HID items rather than scanning for the 0x91
    byte, because a 0x91 can perfectly well occur inside another item's
    DATA and a byte scan would report PTT on a device that has none.
    """
    try:
        with open("/sys/class/hidraw/%s/device/report_descriptor"
                  % os.path.basename(hidraw_path), "rb") as fh:
            desc = fh.read()
    except OSError:
        return 0

    size = 0            # Report Size, bits
    count = 0           # Report Count
    best = 0
    i = 0
    n = len(desc)
    while i < n:
        prefix = desc[i]
        if prefix == 0xFE:      # long item: [0xFE, dataLen, tag, ...data]
            if i + 2 >= n:
                break
            i += 3 + desc[i + 1]
            continue
        length = prefix & 0x03
        if length == 3:         # 0b11 encodes four data bytes, not three
            length = 4
        data = desc[i + 1:i + 1 + length]
        value = int.from_bytes(data, "little") if data else 0
        tag_type = prefix & 0xFC
        if tag_type == 0x74:        # Global, Report Size
            size = value
        elif tag_type == 0x94:      # Global, Report Count
            count = value
        elif tag_type == 0x90:      # Main, Output
            best = max(best, (size * count + 7) // 8)
        i += 1 + length
    return best


def find_ptt_node(usb_device):
    """The hidraw node on the SAME physical device as `usb_device`, or None.

    Tries every vendor in PTT_HID_VENDORS rather than C-Media's alone, so a
    two-cable bench resolves both ends. Narrowing by `usb_device` first is
    what makes that safe: with two dongles plugged in, a vendor match is not
    unique and keying the wrong cable's PTT transmits on the wrong radio.

    A node with no OUTPUT report is NOT returned, because it cannot key --
    see hid_output_report_bytes. Returning it anyway is what produced a
    BrokenPipeError traceback in place of "this cable is VOX-only".
    """
    if usb_device is None:
        return None
    for vendor in PTT_HID_VENDORS:
        node = ptt_test.find_cm108(vendor=vendor, usb_device=usb_device)
        if node and hid_output_report_bytes(node) > 0:
            return node
    return None


def no_ptt_reason(usb_device, play_dev):
    """Why `play_dev` cannot be keyed, as a line that names the remedy.

    Two causes with the same symptom and opposite fixes: a cable that HAS a
    GPIO whose hidraw node is unreachable (a permissions problem, fixable),
    and a cable with no output report at all (a hardware fact, not fixable).
    Telling them apart is the difference between installing a udev rule and
    switching that end to VOX.
    """
    nodes = []
    for vendor in PTT_HID_VENDORS:
        node = ptt_test.find_cm108(vendor=vendor, usb_device=usb_device)
        if node:
            nodes.append((node, hid_output_report_bytes(node)))
    if nodes and all(size == 0 for _, size in nodes):
        return (
            "%s has a HID node (%s) but its report descriptor contains NO "
            "OUTPUT report, so there is no GPIO to drive and no --ptt-gpio "
            "value can key it. This cable is VOX-ONLY: re-run with --vox. "
            "Note what that costs on a K1 PRO -- a 0.30 s lead-in for VOX to "
            "attack, and a measured 3.2 s VOX hang afterwards against the "
            "Digirig's ~0.13 s."
            % (play_dev, ", ".join(n for n, _ in nodes)))
    return (
        "no HID PTT node on the same USB device as %s (checked vendors %s). "
        "Either this cable has no GPIO PTT -- use --vox -- or hidraw access "
        "is missing: install tools/99-digirig.rules, then REPLUG, since udev "
        "only applies rules on connect."
        % (play_dev, ", ".join("0x%04X" % v for v in PTT_HID_VENDORS)))


def capture_channels(dev):
    """How many channels this card's capture is fixed at.

    NOT THE SAME FOR EVERY CARD ON THIS BENCH. Both K1 PRO cards are fixed
    stereo capture, but the Digirig Lite's C-Media codec captures MONO only
    (its stereo side is playback-only, which is what the right-channel VOX
    trick needs) -- so a hardcoded `-c 2` works until the Digirig Lite is the
    RECEIVING end, where it fails outright ("Channels count non available")
    rather than silently misrecording. Which matters here specifically,
    because the Digirig-as-receiver cell is the one Phase 0 is about.
    """
    proc = subprocess.run(["arecord", "-D", dev, "--dump-hw-params", "-d", "1"],
                          capture_output=True, text=True, check=False)
    # arecord writes the param dump to stderr, not stdout.
    out = proc.stdout + proc.stderr
    for line in out.splitlines():
        if line.strip().startswith("CHANNELS:"):
            return int(line.split(":", 1)[1].strip())
    return 2


def describe_devices():
    """Every card ALSA can see, for a log header and for a bench with no
    hardware plugged in yet -- which is its own common failure, and one whose
    symptom (a silent recording) is indistinguishable from a dead radio."""
    lines = []
    for kind in ("playback", "capture"):
        found = alsa_devices(kind)
        lines.append("%s:" % kind)
        for idx, ident, name in found:
            lines.append("  hw:%d,0  %s [%s]" % (idx, ident, name))
        if not found:
            lines.append("  (none)")
    return "\n".join(lines)


# -- payloads ---------------------------------------------------------------

def payload_for(n, seed=0x5A):
    """A deterministic payload, so a wrong byte is visible as a wrong byte.

    Deterministic and not random so a failing trial can be re-run against the
    same bytes, and high-entropy rather than a pattern because a long run of
    any single value is itself a measured decode failure (the zero-run bug --
    see fm_framing.py).
    """
    out = bytearray(n)
    x = seed
    for i in range(n):
        x = (x * 1103515245 + 12345) & 0xFF_FF_FF_FF
        out[i] = (x >> 16) & 0xFF
    return bytes(out)


# -- wav ---------------------------------------------------------------------

def write_wav(path, samples, fs=BENCH_FS, channels=1):
    """Write mono `samples`, duplicated across `channels` if asked.

    `channels=2` is for the ALSA backend: both K1 PRO cards' playback is fixed
    at 2 channels (--dump-hw-params), and raw `hw:` has no remixer to spread a
    mono file across them. DUPLICATING rather than putting the payload on one
    side and silence on the other is deliberate -- it is what the phone side
    does (Android's mixer duplicates a mono AudioTrack onto both USB
    channels, which is what every successful on-phone transmission in
    docs/open-questions.md actually sent), it guarantees the payload reaches
    whichever conductor a given cable listens on, and on a Digirig Lite it is
    specifically the right channel that keys VOX.
    """
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(b"".join(
            int(max(-32768, min(32767, round(s * 32767.0)))).to_bytes(
                2, "little", signed=True) * channels for s in samples))


def read_wav(path):
    """A mono sample list (int16 scale) and the rate, picking a channel if
    there are two.

    THE CHANNEL PICK IS LOAD-BEARING, not tidying. The ALSA backend records
    stereo because these cards cannot capture mono at all, and a K1 PRO feeds
    its receive audio down ONE conductor -- so the other channel is the
    card's own idle noise. Averaging the two (the obvious way) would halve
    the signal and add that noise to it: about 6 dB of SNR thrown away,
    showing up as marginal decodes indistinguishable from a genuinely
    marginal radio link, which is the exact class of failure this bench has
    already lost days to. Taking channel 0 unconditionally is the same trap
    with a 50% chance of reading the silent side.

    So: measure both, keep the louder. Where the signal really is on both
    channels this picks either one, which is correct.
    """
    with wave.open(path, "rb") as w:
        fs = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    step = 2 * ch
    lanes = []
    for c in range(ch):
        off = 2 * c
        lanes.append([int.from_bytes(raw[i + off:i + off + 2], "little",
                                     signed=True)
                      for i in range(0, len(raw) - step + 1, step)])
    if not lanes:
        return [], fs
    if len(lanes) == 1:
        return lanes[0], fs
    energies = [sum(abs(s) for s in lane) for lane in lanes]
    return lanes[energies.index(max(energies))], fs


# -- conditioning -----------------------------------------------------------

def normalise(samples, peak=DECODE_PEAK):
    """Scale a recorded burst into the range the demodulator expects.

    Its own peak, not a fixed divisor: the recording level depends on the
    radio's volume and the sound card's gain, neither of which the modem
    knows or should care about.
    """
    top = max((abs(s) for s in samples), default=0)
    if top == 0:
        return [0.0] * len(samples)
    scale = peak / float(top)
    return [s * scale for s in samples]


def deemphasis(samples, tau_us, fs):
    """A one-pole low-pass, correcting a TX/RX pre-emphasis mismatch.

    Found on the UV-5R-transmits/TYT-receives pairing: the tones were exactly
    on frequency (goertzel confirmed 0 Hz shift on all four), but the top tone
    carried ~70% more energy than the bottom one -- the signature of FM
    pre-emphasis applied on one side without matching de-emphasis on the
    other. codec2's FSK_LDPC estimator would not even reach TRIAL_SYNC
    against that tilt, at any normalisation peak from 0.1 to 0.99; this
    filter, at anywhere from 150-400 us, made it decode byte-perfect. The
    other pairing (TYT transmits, UV-5R receives) worked with no filtering at
    all, so this is OFF BY DEFAULT -- apply it per pairing, not globally.
    """
    if tau_us <= 0:
        return samples
    a = (1.0 / fs) / (tau_us * 1e-6 + 1.0 / fs)
    y = 0.0
    out = []
    for x in samples:
        y = y + a * (x - y)
        out.append(y)
    return out


def find_burst(samples, fs, pad_s=TRIM_PAD_S, window_s=0.01):
    """Where the loud part is, padded. (start, end) sample indices.

    Trimming is for speed and for keeping a long quiet recording from being
    normalised against its own noise floor. Feeding the whole file would also
    work -- codec2 syncs on the preamble -- it is just slower and less
    robust.
    """
    step = max(1, int(window_s * fs))
    levels = []
    for i in range(0, len(samples) - step + 1, step):
        block = samples[i:i + step]
        levels.append(math.sqrt(sum(x * x for x in block) / len(block)))
    if not levels:
        return 0, len(samples)
    peak = max(levels)
    if peak <= 0:
        return 0, len(samples)
    floor = sorted(levels)[len(levels) // 10] if len(levels) > 10 else 0.0
    thresh = max(floor * 3.0, peak * 0.2)
    hot = [i for i, v in enumerate(levels) if v >= thresh]
    if not hot:
        return 0, len(samples)
    pad = int(pad_s * fs)
    start = max(0, hot[0] * step - pad)
    end = min(len(samples), (hot[-1] + 1) * step + pad)
    return start, end


def levels(samples):
    """(peak, rms) of a recording, both in 0..1, for the run log.

    BOTH, not one. Peak is what the saturation threshold is written against
    (fm_config.SATURATED_PEAK), and rms is what the receive-level readout
    shows an operator (fm_config's target band). A sweep that logs only one
    of them cannot be compared against half the record.
    """
    if not samples:
        return 0.0, 0.0
    peak = max(abs(s) for s in samples) / 32768.0
    rms = math.sqrt(sum(float(s) * s for s in samples) / len(samples)) / 32768.0
    return peak, rms


# -- the air ----------------------------------------------------------------

def transmit_and_record(samples, fs, wav_path, rec_path, play_dev, rec_dev,
                        line=ptt_test.CM108_LINE, dev=ptt_test.DEFAULT_DEV,
                        gpio=3, quiet=False, vox=False, tx_gain=1.0,
                        backend=DEFAULT_BACKEND, cm108_path=None):
    """One burst of float `samples` over the air, recorded on the far side.

    Returns the air time in seconds. The caller owns the waveform: this adds
    only the silence either side, the stereo duplication the cards need, and
    the keying.

    `cm108_path` pins PTT to one specific hidraw node (e.g. "/dev/hidraw8")
    when `line == "cm108"`, for a bench with more than one Digirig Lite
    plugged in. Left None, PTT is DERIVED FROM `play_dev` itself --
    alsa_usb_device() finds which physical USB device it resolves to and
    find_cm108 matches a hidraw node against that same device, so keying
    always follows whichever cable is about to play the burst. That matters
    on a two-dongle bench where the roles get swapped between rounds.

    With `vox`, nothing keys the radio -- the audio does, which is the whole
    difference. PTT is deterministic and the transmitter is up before
    anything plays; VOX has to notice the sound first, and the front of the
    burst pays for that.

    `tx_gain` scales the played audio before it reaches the radio's mic
    input.

    IT DOES NOT MEANINGFULLY WEAKEN THE LINK, measured 2026-10-02, and the
    docstring here used to claim it did ("a real, over-the-air way to weaken
    what the far radio receives"). The radio's microphone LIMITER pins
    deviation at maximum across an enormous range: 36 dB of hardware
    attenuation on the codec's own Speaker control changed per-copy not at
    all, digital scaling held 6/6 down to 0.004, and the received peak never
    moved off 0.25 anywhere in either sweep. Only 0.001 degrades anything, by
    which point the played samples have ~32 quantization levels left, so that
    is measuring digital quantization rather than the channel.

    So this is useful for probing the limiter and useless for grading a
    channel. Graded degradation needs an RF change -- the radio's own power
    setting, real distance, or an inline attenuator.

    Still use PTT, not `vox`, when driving it down: VOX's trip threshold is
    itself level-dependent, and a gain low enough to matter can stop VOX
    keying at all, which is a different failure (never transmitted) from the
    one this would be for (transmitted and not decoded).
    """
    padded = ([0.0] * int(LEAD_SILENCE_S * fs) + list(samples)
              + [0.0] * int(TAIL_SILENCE_S * fs))
    if tx_gain != 1.0:
        padded = [s * tx_gain for s in padded]

    # Stereo on the ALSA backend, mono on PipeWire: see write_wav() and the
    # module docstring. ALSA's hw: device has no remixer, so the file itself
    # has to match what the card accepts.
    alsa = backend == BACKEND_ALSA
    write_wav(wav_path, padded, fs, channels=2 if alsa else 1)
    air_s = len(padded) / float(fs)

    # Resolved up front rather than inside make_ptt, because it needs
    # play_dev in its already-resolved "hw:N,0" form -- and because a wrong
    # guess should fail BEFORE a radio gets keyed, not after.
    #
    # Resolved to a NODE here, not left as a usb_device for make_ptt to look
    # up, because make_ptt's lookup only knows C-Media's vendor id and would
    # come back empty for a K1 PRO. See find_ptt_node.
    # `not vox` is load-bearing: under VOX the audio does the keying and no
    # PTT node is used at all, so resolving one would refuse a run that is
    # about to work perfectly well -- which is exactly what it did to the
    # first VOX run on a cable that has no GPIO, the case VOX is FOR.
    if (alsa and not vox and line == ptt_test.CM108_LINE
            and cm108_path is None and play_dev):
        usb_device = alsa_usb_device(resolve_alsa_device(play_dev, "playback"))
        cm108_path = find_ptt_node(usb_device)
        if usb_device and cm108_path is None:
            raise BenchError(no_ptt_reason(usb_device, play_dev))

    if alsa:
        rec_alsa_dev = resolve_alsa_device(rec_dev, "capture") if rec_dev else None
        # Channel count is ASKED of the card, not assumed -- see
        # capture_channels(). read_wav() picks the live channel back out.
        ch = capture_channels(rec_alsa_dev) if rec_alsa_dev else 2
        rec_cmd = ["arecord", "-f", "S16_LE", "-r", str(fs), "-c", str(ch),
                   "--buffer-time=%d" % ALSA_BUFFER_US]
        if rec_dev:
            rec_cmd += ["-D", rec_alsa_dev]
        rec_cmd += [rec_path]
        # No --duration: the recorder is terminated around the playback
        # below, so a burst is never cut off by a timer that guessed short.
        # aplay reads rate/format/channels from the WAV header and errors
        # rather than silently converting if the card cannot take them, which
        # is the behaviour this backend exists for.
        play_cmd = ["aplay", "-q", "--buffer-time=%d" % ALSA_BUFFER_US]
        if play_dev:
            play_cmd += ["-D", resolve_alsa_device(play_dev, "playback")]
        play_cmd += [wav_path]
    else:
        rec_cmd = ["pw-record", "--rate", str(fs), "--channels", "1",
                   "--format", "s16"]
        if rec_dev:
            rec_cmd += ["--target", tone_test_target(rec_dev)]
        rec_cmd += [rec_path]
        play_cmd = ["pw-play", wav_path]
        if play_dev:
            play_cmd[1:1] = ["--target", tone_test_target(play_dev)]

    # Remember what was here before. A failed recorder leaves the PREVIOUS
    # run's file on disk, and analysing that reports an earlier measurement
    # as this one's -- which on this bench would have passed off a loopback
    # as a radio result. Staleness is checked, not assumed.
    before = os.path.getmtime(rec_path) if os.path.exists(rec_path) else None
    # Recorder stderr goes to a FILE, not a pipe. arecord reports every xrun
    # on stderr, and a pipe nobody drains during the burst fills up and blocks
    # the recorder -- turning the diagnostic into the fault it exists to
    # report. A file also survives the run, for the log.
    rec_log = rec_path + ".log"
    log_fh = open(rec_log, "w")
    rec = subprocess.Popen(rec_cmd, stderr=log_fh, text=True)
    try:
        # Let the recorder settle before any audio exists, so the head of the
        # file is genuine noise floor for the threshold to key off.
        subprocess.run(["sleep", "0.4"], check=False)
        if rec.poll() is not None:
            log_fh.flush()
            raise BenchError("recorder exited immediately: %s"
                             % (read_log(rec_log) or "no message"))
        # Keyed around the WHOLE playback rather than for a fixed duration:
        # the transmitter must already be up when the preamble starts and
        # must not drop before the postamble ends.
        if vox:
            if not quiet:
                print("    playing %.2f s, VOX to key it ..." % air_s)
            play_or_raise(play_cmd)
        else:
            # No usb_device: cm108_path is already the resolved node above.
            with ptt_test.make_ptt(line, dev=dev, gpio=gpio,
                                   path=cm108_path) as ptt:
                ptt.key()
                try:
                    if not quiet:
                        print("    keyed, playing %.2f s ..." % air_s)
                    play_or_raise(play_cmd)
                finally:
                    ptt.unkey()
        subprocess.run(["sleep", "0.4"], check=False)
        rec.terminate()
        rec.wait(timeout=30)
    finally:
        if rec.poll() is None:
            rec.terminate()
        log_fh.close()

    if not os.path.exists(rec_path):
        raise BenchError("no recording was produced")
    after = os.path.getmtime(rec_path)
    if before is not None and after <= before:
        raise BenchError(
            "the recording file was not updated -- the recorder failed and an "
            "OLDER file is still on disk. Refusing to analyse it: it would "
            "report a previous run's numbers as this one's.")
    check_recording_length(rec_path, fs, air_s)
    check_no_xrun(rec_log)
    return air_s


def read_log(path):
    """A recorder's stderr log, or "" -- never an exception. A missing log is
    not itself a fault, and raising here would mask whatever is being
    diagnosed."""
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def check_no_xrun(rec_log):
    """Refuse a capture that dropped samples.

    AN XRUN IS AN INSTRUMENT FAULT, NOT A CHANNEL MEASUREMENT, and this is
    the check that keeps the two apart. arecord prints "overrun!!!" and
    carries on, so a capture that drops samples mid-burst still produces a
    plausible file, still decodes to zero frames, and still scores as a lost
    message. That is exactly how a 0.7 s hole in a continuously-keyed 3.70 s
    transmission came to be read as 0/5 delivered at a symbol rate already
    known to work.

    Both dongles on this bench are 12 Mbps FULL-SPEED devices sharing one
    root hub, and playback on one was measured starving capture on the other.
    ALSA_BUFFER_US is the first line of defence; this is what stops a failure
    of it being written down as data.
    """
    text = read_log(rec_log)
    low = text.lower()
    if "overrun" in low or "xrun" in low or "underrun" in low:
        detail = " / ".join(l.strip() for l in text.splitlines() if l.strip())
        raise BenchError(
            "the recorder dropped samples (%s). The capture has a hole in it, "
            "so this burst cannot be scored.\n  Both USB dongles are "
            "full-speed devices on one root hub, and playback on one starves "
            "capture on the other. Move one dongle to a port on a different "
            "bus (see `lsusb -t`), or raise ALSA_BUFFER_US above its current "
            "%d us." % (detail[:200], ALSA_BUFFER_US))


def key_and_record(rec_path, fs, play_dev, rec_dev, key_s=1.0, lead_s=2.0,
                   tail_s=2.5, line=ptt_test.CM108_LINE,
                   dev=ptt_test.DEFAULT_DEV, gpio=3, cm108_path=None):
    """Key `play_dev`'s radio for `key_s` while recording on `rec_dev`.

    No audio is played: this asks only whether asserting PTT puts a CARRIER
    on the air, which is the one thing a data run cannot tell you apart from
    a dozen other faults. "The HID write succeeds and keys nothing" is a
    recorded failure on this bench, and the GPIO pin number is per-cable (a
    Digirig wires PTT to 3; nothing has ever confirmed what a K1 PRO wires).

    Returns (rec_path, held_s) -- the MEASURED hold, not the requested one,
    so a scheduler hiccup between key and unkey is not charged to the radio.

    THE RECEIVING RADIO'S SQUELCH MUST BE OPEN. ptt_test.analyze_carrier
    finds the carrier as the QUIET stretch: with squelch open, no carrier is
    loud hiss and an unmodulated carrier is quiet. With squelch closed both
    are silence and there is nothing to measure.
    """
    rec_alsa_dev = resolve_alsa_device(rec_dev, "capture") if rec_dev else None
    ch = capture_channels(rec_alsa_dev) if rec_alsa_dev else 2
    rec_cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(fs), "-c", str(ch),
               "--buffer-time=%d" % ALSA_BUFFER_US]
    if rec_alsa_dev:
        rec_cmd += ["-D", rec_alsa_dev]
    rec_cmd += [rec_path]

    if line == ptt_test.CM108_LINE and cm108_path is None and play_dev:
        usb_device = alsa_usb_device(resolve_alsa_device(play_dev, "playback"))
        cm108_path = find_ptt_node(usb_device)
        if cm108_path is None:
            raise BenchError(no_ptt_reason(usb_device, play_dev))

    before = os.path.getmtime(rec_path) if os.path.exists(rec_path) else None
    rec = subprocess.Popen(rec_cmd, stderr=subprocess.PIPE, text=True)
    try:
        subprocess.run(["sleep", str(lead_s)], check=False)
        if rec.poll() is not None:
            raise BenchError("recorder exited immediately: %s"
                             % (rec.stderr.read().strip() or "no message"))
        with ptt_test.make_ptt(line, dev=dev, gpio=gpio,
                               path=cm108_path) as ptt:
            held = ptt.keyed_for(key_s)
        subprocess.run(["sleep", str(tail_s)], check=False)
        rec.terminate()
        rec.wait(timeout=30)
    finally:
        if rec.poll() is None:
            rec.terminate()

    if not os.path.exists(rec_path):
        raise BenchError("no recording was produced")
    after = os.path.getmtime(rec_path)
    if before is not None and after <= before:
        raise BenchError(
            "the recording file was not updated -- the recorder failed and an "
            "OLDER file is still on disk. Refusing to analyse it.")
    return rec_path, held


def play_or_raise(play_cmd):
    """Play, and REFUSE TO BE QUIET ABOUT A FAILURE.

    This existed as `subprocess.run(play_cmd, check=False)` and cost a whole
    20-trial batch. PipeWire was holding the playback card, aplay died with
    "audio open error: Device or resource busy" before a single sample left
    the laptop, and because the call ignored both the exit status and stderr
    (and ran with -q), the run completed, keyed the radio twenty times,
    recorded twenty files of pure hiss and reported 0/20 delivered at every
    symbol rate -- including the rate that was already known to work. A
    silent transmitter is indistinguishable from a dead channel in the
    results, and it is the more likely of the two.

    Checks stderr as well as the exit status because aplay's open failure
    does not reliably set a non-zero status, so status alone would still
    have missed this.
    """
    proc = subprocess.run(play_cmd, capture_output=True, text=True,
                          check=False)
    # aplay announces itself on STDERR, not stdout ("Playing WAVE '...' :
    # Signed 16 bit ..."), so a bare "any stderr means failure" rule rejects
    # every successful playback. `-q` suppresses it, and this filter means the
    # check does not silently depend on that flag still being there.
    noise = "\n".join(
        line for line in (proc.stderr or "").splitlines()
        if line.strip() and not line.startswith("Playing ")).strip()
    if proc.returncode != 0 or noise:
        hint = ""
        if "busy" in noise.lower():
            hint = (
                "\n  A sound SERVER is holding the card. Check with "
                "`fuser -v /dev/snd/pcmC<N>D0p`; if it is pipewire, free it "
                "with `pactl suspend-sink <sink> 1` or set that device's "
                "profile off in wpctl. Do NOT work around it by switching to "
                "--backend pipewire: that puts the server's resampling and "
                "remixing back between this file and the radio, which is the "
                "variable the alsa backend exists to remove.")
        raise BenchError(
            "playback FAILED, so nothing was transmitted: %s (exit %d)%s"
            % (noise or "no message", proc.returncode, hint))


def check_recording_length(rec_path, fs, expected_s, tolerance=0.6):
    """Refuse a recording far shorter than the transmission it should hold.

    The second half of the lesson above. The staleness check already catches
    "the recorder failed and an older file is on disk", but not "the recorder
    was torn down early", which is what a failed playback produces: the file
    is new, non-empty, full of real audio, and holds nothing but the silence
    either side of a burst that never happened. It decodes to zero frames,
    which reads as a result.
    """
    try:
        with wave.open(rec_path, "rb") as w:
            got_s = w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error) as exc:
        raise BenchError("cannot read back %s: %s" % (rec_path, exc))
    if got_s < expected_s * tolerance:
        raise BenchError(
            "the recording is %.2f s but the transmission was %.2f s -- the "
            "recorder was torn down early, so this file cannot contain the "
            "burst. Refusing to score it as a lost frame: an instrument "
            "fault is not a channel measurement."
            % (got_s, expected_s))
    return got_s


def tone_test_target(name):
    """PipeWire node name for `name`, deferred to tone_test.

    Imported lazily so the ALSA path -- the default, and the only one Phase 0
    should use -- does not depend on the pipewire half of tone_test at all.
    """
    import tone_test
    return tone_test.resolve_pw_target(name)


def capture_to_samples(rec_path, fs, deemphasis_us=0.0, trim=True):
    """A recording -> (float samples ready for a demodulator, peak, rms).

    The order is load-bearing: trim, measure, filter, normalise. Measuring
    AFTER normalising would report 0.5 every time -- which is the peak the
    normaliser was asked for, not the one the radio delivered, and the
    receive level is the single most common fixable fault on this link.
    """
    raw, got_fs = read_wav(rec_path)
    if got_fs != fs:
        raise BenchError("recording is %d Hz, the configuration is %d Hz -- "
                         "nothing may resample between the card and the "
                         "demodulator" % (got_fs, fs))
    if not raw:
        return [], 0.0, 0.0
    if trim:
        start, end = find_burst(raw, fs)
        raw = raw[start:end]
    peak, rms = levels(raw)
    return normalise(deemphasis(raw, deemphasis_us, fs)), peak, rms
