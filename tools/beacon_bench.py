"""Bidirectional over-the-air test using a REAL beacon frame -- the exact
bytes rns_bridge.py's beacon loop builds (cot_codec.encode_beacon() ->
mesh.wrap()) -- through the REAL fm_interface.py build_tx_burst()/
feed_audio() functions, instead of otair_test.py's synthetic fixed payload
or a hand-rolled workaround. This is the actual production Python code path
(everything except the Kotlin/Android audio plumbing), run against this
desktop bench's real radios.

HISTORY: originally written 2026-09-20 with a local CRC/padding-trim
workaround, because fm_interface.py's real feed_audio() could not
authenticate ANY real frame at the time (see docs/open-questions.md,
"BUG FOUND, real-beacon test"). That bug is now FIXED in fm_interface.py
itself (_LEN_PREFIX -- see its own comment there) and this script was
rewritten to call the real, fixed functions directly rather than
reimplementing the workaround -- a genuine end-to-end validation of the fix,
not a parallel path that could drift from what production actually does.

Requires the project .venv (mesh.py needs `cryptography` for AEAD):
    .venv/bin/python3 tools/beacon_bench.py --rounds 15 --repeats 2 \
        --card-a hw:5,0 --card-b hw:6,0
"""

import argparse
import datetime
import os
import struct
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))


def _install_rns_stub():
    """Same minimal stub test_fm_interface.py uses -- RNS isn't on the
    desktop, and fm_interface.py imports it at module level."""
    if "RNS" in sys.modules:
        return
    rns = types.ModuleType("RNS")
    for name in ("LOG_ERROR", "LOG_WARNING", "LOG_DEBUG", "LOG_INFO",
                 "LOG_NOTICE", "LOG_CRITICAL", "LOG_EXTREME", "LOG_VERBOSE"):
        setattr(rns, name, 0)
    rns.log = lambda *a, **k: None

    class _StubInterface(object):
        def __init__(self):
            self.rxb = 0
            self.txb = 0
            self.online = False

    interface_mod = types.ModuleType("RNS.Interfaces.Interface")
    interface_mod.Interface = _StubInterface
    interfaces_pkg = types.ModuleType("RNS.Interfaces")
    interfaces_pkg.Interface = interface_mod
    rns.Interfaces = interfaces_pkg
    sys.modules["RNS"] = rns
    sys.modules["RNS.Interfaces"] = interfaces_pkg
    sys.modules["RNS.Interfaces.Interface"] = interface_mod


_install_rns_stub()

import otair_test as ot
import cot_codec
import mesh
import fm_interface

NODE_A = bytes.fromhex("a1a1a1")
NODE_B = bytes.fromhex("b2b2b2")
TEST_KEY = mesh.derive_key("bidir-beacon-bench-test-only-2026-09-20")

# Mirrors rns_bridge.py's real caps for fm_interface.CODENAME (H_256_512_4):
# _BEACON_NAME_MAX_B=2, _BEACON_CALLSIGN_MAX_B=0.
NAME = "N1"

_NATIVE_LIB_DIR = "/unused"  # see test_fm_interface.py's own convention --
# fsk_modem's library loader falls back to the system libcodec2 when this
# path does not exist, which is all a desktop bench needs.


def build_beacon_sealed(sender_mesh, seq):
    """The real bytes rns_bridge.py's beacon loop sends -- unpadded, the
    ordinary case (most real beacons are NOT padded to the code's full
    capacity), now that fm_interface.py's own length prefix means a
    shorter-than-hw_mtu frame is no longer a problem for reception."""
    frame = cot_codec.encode_beacon(sender_mesh.origin, seq, size=0,
                                    callsign=None, name=NAME, digest=None,
                                    heard=None)
    return sender_mesh.wrap(frame, hops=0)


def _pcm16(samples):
    clamped = [max(-32768, min(32767, int(round(s * 32767.0))))
              for s in samples]
    return struct.pack("<%dh" % len(clamped), *clamped)


def run_one(sealed, play_dev, rec_dev, tag, round_i, repeats, log):
    """Sends `sealed` through the REAL fm_interface.build_tx_burst() over
    the actual radios, then decodes with the REAL fm_interface.feed_audio()
    against a recorded WAV -- otair_test.py supplies the ALSA/PTT plumbing
    (record/play/key), fm_interface.py supplies the real framing/modem
    logic under test.
    """
    # build_tx_burst() honours fm_interface.REPEATS, not a parameter -- so
    # this script's own --repeats flag is applied the same way otair_test.py
    # tools apply theirs, temporarily, restored after.
    old_repeats = fm_interface.REPEATS
    fm_interface.REPEATS = repeats
    try:
        pcm = fm_interface.build_tx_burst(_NATIVE_LIB_DIR, sealed)
    finally:
        fm_interface.REPEATS = old_repeats

    n = len(pcm) // 2
    samples = [s / 32768.0 for s in struct.unpack("<%dh" % n, pcm)]

    wav = "beacon_tx_%s.wav" % tag
    rec = "beacon_rx_%s_round%d.wav" % (tag, round_i)
    # fm_interface builds at 8000 Hz mono (see its own module docstring
    # history) -- otair_test.py's own transmit_and_record() assumes it owns
    # modulation itself, so this script's _play_and_record() plays the
    # ALREADY-BUILT burst at fm_interface's own rate directly, matching
    # what the real Kotlin AudioTrack path does.
    ot.write_wav(wav, samples, 8000, channels=1)
    air = _play_and_record(wav, rec, play_dev, rec_dev)

    raw, fs = ot.read_wav(rec)
    start, end = ot.find_burst(raw, fs)
    window = raw[start:end]
    peak = max((abs(s) for s in window), default=0) / 32768.0
    rec_samples = ot.normalise(window)
    rec_pcm = _pcm16(rec_samples + [0.0] * 8000)

    fm_interface._RX_MODEM[0] = None  # clean sync state per trial, as the
    # real long-lived receive loop would have BEFORE this burst if nothing
    # else had been decoding -- avoids leftover state from a prior trial's
    # tail corrupting this one's sync acquisition.
    got_frames = fm_interface.feed_audio(_NATIVE_LIB_DIR, rec_pcm)
    contents = fm_interface.FrameParser().feed(got_frames)

    recv_mesh = mesh.Mesh(NODE_B if tag == "AtoB" else NODE_A, TEST_KEY, epoch=0)
    decoded_ok = False
    beacon = None
    for content in contents:
        inbound = recv_mesh.unwrap(content)
        if inbound is not None:
            try:
                beacon = cot_codec.decode_beacon(inbound.frame, node_id=inbound.origin)
                decoded_ok = True
                break
            except cot_codec.CotDecodeError:
                pass

    now = datetime.datetime.now().strftime("%H:%M:%S")
    line_txt = ("%s dir=%s round=%2d unwrap_ok=%-5s frames=%d air=%5.2fs "
                "peak=%.3f beacon=%s"
                % (now, tag, round_i, decoded_ok, len(contents), air, peak, beacon))
    print(line_txt)
    log.write(line_txt + "\n")
    log.flush()
    return decoded_ok


def _plughw(hw_dev):
    """hw:N,0 -> plughw:N,0 -- this bench's raw ALSA cards do not do 8000 Hz
    natively (otair_test.py's own FS=48000 exists for exactly this reason:
    "no resampling anywhere" on the bare hw: device), but
    fm_interface.build_tx_burst() builds PCM at the modem's own 8000 Hz
    (matching what a real phone's AudioRecord/AudioTrack presents to it).
    plughw's software resampler bridges the two -- a real, honest stand-in
    for the phone's native 8000 Hz audio path, not a workaround for a bug.
    """
    if hw_dev.startswith("hw:"):
        return "plug" + hw_dev
    return hw_dev


def _play_and_record(wav, rec, play_dev, rec_dev, line="cm108"):
    """Minimal record/play/PTT wrapper at fm_interface's own 8000 Hz rate --
    otair_test.transmit_and_record() assumes it owns modulation itself
    (builds the WAV from a payload+modem), which this script cannot use
    since fm_interface.build_tx_burst() already produced the final PCM.
    """
    import subprocess
    rec_alsa = _plughw(ot.resolve_alsa_device(rec_dev, "capture"))
    play_alsa_raw = ot.resolve_alsa_device(play_dev, "playback")
    play_alsa = _plughw(play_alsa_raw)
    ch = 1
    rec_cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "8000", "-c", str(ch),
              "-D", rec_alsa, rec]
    play_cmd = ["aplay", "-q", "-D", play_alsa, wav]
    # PTT disambiguation is keyed off the RAW hw: device (its USB physical
    # device), not the plughw: alias -- alsa_usb_device() only parses
    # "hw:N,..." (see its own docstring).
    usb_device = ot.alsa_usb_device(play_alsa_raw)

    import ptt_test
    proc = subprocess.Popen(rec_cmd, stderr=subprocess.PIPE, text=True)
    try:
        import time
        time.sleep(0.3)
        if proc.poll() is not None:
            raise RuntimeError("recorder exited immediately: %s"
                               % (proc.stderr.read().strip() or "no message"))
        with ptt_test.make_ptt(line, usb_device=usb_device) as ptt:
            ptt.key()
            try:
                subprocess.run(play_cmd, check=False)
            finally:
                ptt.unkey()
        time.sleep(0.4)
        proc.terminate()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    # NOT wave.open(...).getnframes(): arecord killed via SIGTERM sometimes
    # leaves the RIFF header's chunk-size field at its placeholder value
    # (seen directly: 2**30 frames for a ~2 s recording) rather than
    # patching it in before exit. ot.read_wav() doesn't trust that field
    # either (readframes() is bounded by actual file content regardless of
    # what the header claims), so reusing it here gives a real sample count.
    samples, fs = ot.read_wav(rec)
    return len(samples) / float(fs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--card-a", required=True)
    ap.add_argument("--card-b", required=True)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--log", default="beacon_bench.log")
    args = ap.parse_args(argv)

    mesh_a = mesh.Mesh(NODE_A, TEST_KEY, epoch=0)
    mesh_b = mesh.Mesh(NODE_B, TEST_KEY, epoch=0)

    print("config: codename=%s Rs=%d repeats=%d hw_mtu=%d"
          % (fm_interface.CODENAME, fm_interface.RS, args.repeats,
             fm_interface.hw_mtu()))

    results = {"AtoB": [], "BtoA": []}
    seq = 0
    with open(args.log, "a") as log:
        log.write("=== %s (via REAL fm_interface.py) codename=%s rs=%d "
                  "repeats=%d ===\n"
                  % (datetime.datetime.now().isoformat(), fm_interface.CODENAME,
                     fm_interface.RS, args.repeats))
        for round_i in range(1, args.rounds + 1):
            seq += 1
            sealed = build_beacon_sealed(mesh_a, seq)
            ok = run_one(sealed, args.card_a, args.card_b, "AtoB", round_i,
                        args.repeats, log)
            results["AtoB"].append(ok)

            seq += 1
            sealed = build_beacon_sealed(mesh_b, seq)
            ok = run_one(sealed, args.card_b, args.card_a, "BtoA", round_i,
                        args.repeats, log)
            results["BtoA"].append(ok)

    print()
    for tag in ("AtoB", "BtoA"):
        r = results[tag]
        print("%s: %d/%d = %.0f%%" % (tag, sum(r), len(r), 100.0 * sum(r) / len(r)))
    print("BEACON_BENCH_DONE")


if __name__ == "__main__":
    sys.exit(main())
