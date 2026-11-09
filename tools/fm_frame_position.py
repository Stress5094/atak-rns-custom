"""Which POSITION in a burst survives, when only some of the copies do.

    python3 tools/fm_frame_position.py --loopback                 # no radio
    python3 tools/fm_frame_position.py --tx 0 --rx 1 --trials 10 \\
            --note "volume max both, squelch open both"

WHY THIS EXISTS. The 2026-10-03 burst-duration A/B found a failure that is
systematic rather than random: on one direction of the desktop bench, EVERY
two-copy burst delivered exactly one of its two copies (10 of 10 at the 32 B
code, 8 of 10 at the 128 B), and every ONE-copy burst delivered nothing at all
(0 of 10). At the observed per-copy rate, independent copies would land
exactly-one-of-two in all ten bursts about once in a thousand times. So the
copies are not failing independently -- something about a copy's POSITION in
the burst decides whether it decodes.

`fm_burst.build` cannot answer which position, because it sends `[frame] *
repeats` -- the copies are byte-identical, so a decoded frame does not say
where it came from. This tool sends DISTINCT payloads, one per position, and
reports which positions arrived.

IT ALSO SETTLES A CONFOUND the A/B could not. `modulate_burst` calls
`freedv_set_frames_per_burst(len(frames))`, which the record describes as
"TX-side bookkeeping only" -- confirmed, but confirmed against RX-side
settings. If that call changes the transmitted waveform, then the one-copy
cell of the A/B differed from the two-copy cell by more than its copy count,
and the whole inference above is unsafe. Here every burst carries the same
number of frames, so that variable is held fixed while position varies.

THE PAYLOADS ROTATE, and that is not a nicety. Each position carries different
CONTENT by construction (it has to, or a decoded frame cannot name itself), so
a frame that always fails in position 0 might be failing because of what is in
it rather than where it is. Rotating which payload sits in which position
across trials separates those: the report tallies arrivals BY POSITION and BY
PAYLOAD, and only a position effect shows up in the first while staying flat
in the second. Not rotating is the same mistake as measuring two directions in
separate blocks, which cost this project a p = 0.00022 claim.

THREE FRAMES BY DEFAULT, not two. Two can only say "one of them failed"; three
separates "the FIRST frame is lost" from "only the LAST frame survives", and
those point at different mechanisms -- the first at a receiver still settling
when the data starts, the second at something that keeps degrading through the
burst.

Everything about the waveform comes from fm_config's operating point and the
same fm_bench_audio device/keying layer the A/B ran on, so a result here is
comparable with that CSV rather than being a separate experiment.
"""

import argparse
import csv
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "src", "main", "python"))

import fm_bench                      # make_profile/bench_chain, not duplicated
import fm_bench_audio as audio
import ptt_test

import fm_config
import fm_framing
import fm_modem


CSV_HEADER = ["utc", "direction", "trial", "position", "payload_id",
              "arrived", "frames_out", "rejected", "burst_s", "peak", "rms",
              "error", "note"]


def payload_for_id(ident, size):
    """A full-capacity payload that names itself in its first byte.

    The REST is a deterministic per-id filler rather than a constant, so two
    payloads differ across their whole length and not in one byte. Frames that
    differ in a single byte decode almost identically, which would leave a
    content effect indistinguishable from a position effect -- the very thing
    the rotation exists to separate.
    """
    body = bytes(((ident * 37 + j * 11) & 0xFF) for j in range(size - 1))
    return bytes([ident]) + body


def rotation(trial, frames):
    """Which payload id sits in each position for this trial.

    A plain cyclic shift: over `frames` trials every payload visits every
    position exactly once, so a tally by position and a tally by payload are
    balanced by construction rather than by luck.
    """
    return [(trial + pos) % frames for pos in range(frames)]


def decode_positions(modem, samples, sent_ids, size):
    """(set of payload ids that arrived, frames handed back, framing rejects).

    Matched by ID rather than by arrival order, deliberately. The demodulator
    returns what it recovered, not a slot per frame -- so if the first frame is
    missing, the second arrives FIRST in the list. Reading position off the
    list index is how "the first frame is lost" would be misread as "the first
    frame is fine and the last one is missing".
    """
    got, rejected = set(), 0
    frames = modem.demodulate(samples)
    for frame in frames:
        content = fm_framing.unpack(frame, modem.frame_payload_bytes)
        if content is None:
            rejected += 1
            continue
        ident = content[0]
        # Only count a frame whose whole content is the payload we sent for
        # that id: a correct first byte over corrupt filler is not an arrival.
        if ident in sent_ids and content == payload_for_id(ident, size):
            got.add(ident)
        else:
            rejected += 1
    return got, len(frames), rejected


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loopback", action="store_true",
                    help="modem only, no sound card and no radio. Run this "
                         "first: it proves the script before any of the "
                         "hardware's many ways to look like a software bug")
    ap.add_argument("--frames", type=int, default=3,
                    help="frames per burst, each a distinct payload "
                         "(default %(default)s -- see the module docstring "
                         "for why not 2)")
    ap.add_argument("--trials", type=int, default=10,
                    help="bursts per direction (default %(default)s). A "
                         "multiple of --frames keeps the rotation balanced")
    ap.add_argument("--gap", type=float, default=10.0,
                    help="seconds between bursts (default %(default)s). A "
                         "back-to-back batch is an upper bound, not a "
                         "measurement")
    ap.add_argument("--both-directions", action="store_true",
                    help="alternate the tx/rx roles INSIDE each trial, so the "
                         "two directions are interleaved rather than measured "
                         "as separate blocks")
    ap.add_argument("--code", default=fm_bench.DEFAULT_CODE,
                    help="LDPC code (default %s, the operating point's)"
                         % fm_bench.DEFAULT_CODE)
    ap.add_argument("--rs", type=int, default=fm_bench.DEFAULT_SYMBOL_RATE_BD)
    ap.add_argument("--first-tone", type=int,
                    default=fm_bench.DEFAULT_FIRST_TONE_HZ)
    ap.add_argument("--spacing", type=int, default=0)
    ap.add_argument("--tones", type=int, default=fm_bench.DEFAULT_TONES)
    ap.add_argument("--fs", type=int, default=audio.BENCH_FS)
    ap.add_argument("--lead-in", type=float, default=None,
                    help="lead-in tone seconds; defaults by keying method. "
                         "The knob to sweep if the FIRST position turns out "
                         "to be the one that fails")
    ap.add_argument("--tx", help="playback card (see fm_bench.py --devices)")
    ap.add_argument("--rx", help="capture card, the receiving end")
    ap.add_argument("--vox", action="store_true")
    ap.add_argument("--ptt-line", default=ptt_test.CM108_LINE)
    ap.add_argument("--ptt-dev", default=ptt_test.DEFAULT_DEV)
    ap.add_argument("--ptt-gpio", type=int, default=3)
    ap.add_argument("--cm108-path")
    ap.add_argument("--rx-deemphasis-us", type=float, default=0.0)
    ap.add_argument("--backend", default=audio.DEFAULT_BACKEND,
                    choices=list(audio.ALL_BACKENDS))
    ap.add_argument("--note", default="",
                    help="required over the air: knob positions and which "
                         "cable is on which end")
    ap.add_argument("--csv")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.frames < 2:
        ap.error("--frames must be at least 2; position is the whole question")
    over_the_air = not args.loopback
    if over_the_air and not args.note.strip():
        ap.error("--note is required for an over-the-air run: record the "
                 "volume-knob positions and which cable is on which end")
    if over_the_air and not (args.tx and args.rx):
        ap.error("--tx and --rx are required for an over-the-air run")
    if args.both_directions and args.vox:
        ap.error("--both-directions needs deterministic PTT on both ends")
    if not fm_modem.available():
        print("libcodec2 is not loadable here -- nothing can be measured.",
              file=sys.stderr)
        return 2
    if args.trials % args.frames:
        print("NOTE: --trials %d is not a multiple of --frames %d, so the "
              "payload rotation is not perfectly balanced; a payload effect "
              "and a position effect are only cleanly separable when it is.\n"
              % (args.trials, args.frames), file=sys.stderr)

    chain = fm_bench.bench_chain(args.note, args.tx, args.rx)
    ceiling = max(args.rs, fm_config.MAX_SYMBOL_RATE_BD)
    keying = fm_config.KEYING_VOX if args.vox else fm_config.KEYING_CM108
    prof = fm_bench.make_profile(args.rs, args.first_tone, args.spacing,
                                 args.tones, args.fs, ceiling, chain,
                                 slug="pos")
    # repeats=frames purely so the DURATION CEILING is checked against the
    # burst this actually sends. The frames differ in content, but there are
    # the same number of them, so the arithmetic is identical.
    try:
        cfg = fm_config.LinkConfig(
            name="frame-position", profile=prof, codename=args.code,
            repeats=args.frames, keying=keying, lead_in_s=args.lead_in)
        modem = fm_modem.Codec2Fsk(prof, args.code)
    except (fm_config.ProfileError, fm_modem.ModemError) as exc:
        print("configuration refused:\n  %s" % exc, file=sys.stderr)
        return 2

    scratch = os.environ.get("TMPDIR") or "/tmp"
    try:
        size = fm_framing.capacity_for(modem.frame_payload_bytes)
        print("%s  %d frames/burst  %d B each  lead-in %.2f s  burst %.2f s  "
              "Fs %d Hz" % (args.code, args.frames, size, cfg.lead_in_s,
                            cfg.burst_duration_s(), args.fs))
        print("payloads rotate across trials, so a POSITION effect and a "
              "PAYLOAD effect are separable\n")

        if args.both_directions:
            directions = [("AtoB", args.tx, args.rx),
                          ("BtoA", args.rx, args.tx)]
        else:
            directions = [("", args.tx, args.rx)]

        by_pos = {(d[0], p): 0 for d in directions for p in range(args.frames)}
        by_id = {(d[0], i): 0 for d in directions for i in range(args.frames)}
        sent = {(d[0], p): 0 for d in directions for p in range(args.frames)}
        sent_id = {(d[0], i): 0 for d in directions for i in range(args.frames)}

        fh = writer = None
        if args.csv:
            new = not os.path.exists(args.csv)
            fh = open(args.csv, "a")
            writer = csv.writer(fh)
            if new:
                writer.writerow(CSV_HEADER)
                fh.flush()

        try:
            for trial in range(args.trials):
                ids = rotation(trial, args.frames)
                payloads = [payload_for_id(i, size) for i in ids]
                frames = [fm_framing.pack(p, modem.frame_payload_bytes)
                          for p in payloads]
                for dname, dtx, drx in directions:
                    label = "trial %d/%d%s  positions->ids %s" % (
                        trial + 1, args.trials,
                        "  " + dname if dname else "", ids)
                    if not args.quiet:
                        print("%s ..." % label)
                    samples = modem.modulate_burst(frames,
                                                   lead_in_s=cfg.lead_in_s)
                    peak = rms = 0.0
                    error = ""
                    if args.loopback:
                        got, nframes, rejected = decode_positions(
                            modem, samples, set(ids), size)
                    else:
                        wav = os.path.join(scratch, "fm_pos_tx.wav")
                        rec = os.path.join(
                            scratch, "fm_pos_rx_%s.wav" % (dname or "one"))
                        try:
                            audio.transmit_and_record(
                                samples, modem.sample_rate_hz, wav, rec,
                                play_dev=dtx, rec_dev=drx,
                                line=args.ptt_line, dev=args.ptt_dev,
                                gpio=args.ptt_gpio, vox=args.vox,
                                backend=args.backend,
                                cm108_path=args.cm108_path, quiet=args.quiet)
                        except audio.BenchError as exc:
                            print("  ABORTED: %s" % exc, file=sys.stderr)
                            return 3
                        heard, peak, rms = audio.capture_to_samples(
                            rec, modem.sample_rate_hz,
                            deemphasis_us=args.rx_deemphasis_us)
                        got, nframes, rejected = decode_positions(
                            modem, heard, set(ids), size)

                    at = datetime.datetime.now(
                        datetime.timezone.utc).isoformat(timespec="seconds")
                    for pos, ident in enumerate(ids):
                        arrived = 1 if ident in got else 0
                        sent[(dname, pos)] += 1
                        sent_id[(dname, ident)] += 1
                        by_pos[(dname, pos)] += arrived
                        by_id[(dname, ident)] += arrived
                        if writer:
                            writer.writerow(
                                [at, dname, trial, pos, ident, arrived,
                                 nframes, rejected,
                                 "%.3f" % cfg.burst_duration_s(),
                                 "%.4f" % peak, "%.4f" % rms, error,
                                 args.note.replace(",", ";")])
                    if writer:
                        fh.flush()
                    if not args.quiet:
                        marks = "".join(
                            "1" if i in got else "." for i in ids)
                        print("  -> positions %s  (%d/%d)%s" % (
                            marks, len(got), args.frames,
                            "  rejected=%d" % rejected if rejected else ""))
                        if not args.loopback:
                            print("     level: %s"
                                  % fm_bench.level_warning(peak))
                    if args.gap > 0 and not args.loopback:
                        import time
                        time.sleep(args.gap)
        except KeyboardInterrupt:
            print("\n\nINTERRUPTED -- rows already on disk.", file=sys.stderr)
        finally:
            if fh:
                fh.close()

        print()
        for dname, _, _ in directions:
            tag = (" " + dname) if dname else ""
            print("BY POSITION%s  (this is the question)" % tag)
            for p in range(args.frames):
                n = sent[(dname, p)]
                if n:
                    print("   position %d: %2d/%-2d = %3.0f%%"
                          % (p, by_pos[(dname, p)], n,
                             100.0 * by_pos[(dname, p)] / n))
            print("BY PAYLOAD%s  (must stay FLAT, or the effect is content)"
                  % tag)
            for i in range(args.frames):
                n = sent_id[(dname, i)]
                if n:
                    print("   payload %d:  %2d/%-2d = %3.0f%%"
                          % (i, by_id[(dname, i)], n,
                             100.0 * by_id[(dname, i)] / n))
            print()
        if args.csv:
            print("rows appended to %s" % args.csv)
        return 0
    finally:
        modem.close()


if __name__ == "__main__":
    sys.exit(main())
