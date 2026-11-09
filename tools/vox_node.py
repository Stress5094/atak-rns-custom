"""A laptop-side VOX node: keeps listening, and listens before it transmits.

WHY THIS EXISTS. The 2026-10-08 ten-minute two-way test sent traffic from a
laptop radio to a phone with NO listen-before-transmit, and about 17 of the ~18
lost transmissions were collisions -- both radios on the air at once, neither
able to hear the other (a half-duplex radio cannot receive while it transmits).
This is the laptop's half of the rule the app already follows: never key up into
a burst that is audible right now.

HOW IT LISTENS. The same detector the app uses (fm_carrier.CarrierDetector: in
4FSK exactly one of four tones is present at a time, so a burst concentrates
the tone energy and idle noise spreads it). Captured audio is cut to 8 kHz like
the phone's, fed to the detector continuously, and the node calls the channel
BUSY while the statistic has been over THRESHOLD at any point in the last
HOLD_S seconds.

THRESHOLD AND HOLD ARE CALIBRATED ON ONE CHAIN, from one recording. Replaying
the 660 s two-way capture (results of 2026-10-08, K1 PRO cable + Baofeng Mini,
Pixel 7 transmitting, laptop capture gain 100%) through the detector against
the decoded burst times:

    idle noise statistic    median 0.573   p99 0.619   max 0.669
    burst statistic         median 0.753   but it FLICKERS: 5% of mid-burst
                            samples read below 0.614

    threshold 0.70, no hold    flags 74% of burst time, 0.00% false alarms
    threshold 0.66, hold 1.0 s flags 99.3% of burst time, 0.77% false alarms
                               (mostly the tail after a burst, which is correct)

The numbers were chosen on the data they are judged on, so the live run is the
real test; and a different cable, radio or capture gain moves the idle floor.
Re-calibrate per chain.

A NODE THAT HAS WAITED OUT A BURST BACKS OFF A RANDOM 0..BACKOFF_MAX_S and then
listens AGAIN, the app's rule (fm_link.BACKOFF_MAX_S), so two nodes that waited
out the same burst do not key up together.

THE BLIND SPOT THIS CANNOT CLOSE. VOX takes 0.3-0.7 s to key the radio after
the audio starts, so a node that started playing a moment ago is not yet on the
air and another node's check reads "clear". That window is part of why VOX
collisions are more likely than PTT ones, and listening harder does not shrink
it.
"""

import os
import random
import struct
import subprocess
import sys
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "src", "main", "python"))

import fm_carrier  # noqa: E402

THRESHOLD = 0.66
HOLD_S = 1.0
BACKOFF_MAX_S = 2.0
CAPTURE_RATE = 48000
DECODE_RATE = 8000
DECIMATE = CAPTURE_RATE // DECODE_RATE


class LbtGate(object):
    """Busy for HOLD_S after the statistic last exceeded THRESHOLD.

    Pure (no audio, no sleeping, injectable clock) so the decision rule is
    testable without a radio.
    """

    def __init__(self, threshold=THRESHOLD, hold_s=HOLD_S, clock=time.monotonic):
        self.threshold = threshold
        self.hold_s = hold_s
        self._clock = clock
        self._last_hit = None

    def note(self, level):
        if level > self.threshold:
            self._last_hit = self._clock()

    def busy(self):
        return (self._last_hit is not None
                and self._clock() - self._last_hit < self.hold_s)

    def reset(self):
        self._last_hit = None


def wait_until_clear(gate, max_wait_s, sleep=time.sleep, clock=time.monotonic,
                     rng=random.uniform, poll_s=0.05):
    """Wait out any burst, back off a random time, listen again.

    Returns (waited_s, deferrals, expired). GIVES UP after max_wait_s rather
    than waiting forever (expired=True) -- the app's rule too: a channel that
    never clears must not make a node mute, and a collision in the gap is what
    resends absorb.
    """
    t0 = clock()
    deferrals = 0
    while True:
        saw_busy = False
        while gate.busy():
            saw_busy = True
            if clock() - t0 > max_wait_s:
                return clock() - t0, deferrals, True
            sleep(poll_s)
        if not saw_busy:
            return clock() - t0, deferrals, False
        deferrals += 1
        sleep(rng(0.0, BACKOFF_MAX_S))
        # Falls through and listens again: another node may have started
        # during the back-off.


class VoxNode(object):
    """Continuous capture + carrier detection + play-with-LBT on one card."""

    def __init__(self, profile, card="hw:3,0", rx_wav_path=None,
                 threshold=THRESHOLD, hold_s=HOLD_S):
        self.card = card
        self.profile = profile
        self.gate = LbtGate(threshold, hold_s)
        self.detector = fm_carrier.CarrierDetector(
            profile.tone_list(), profile.symbol_rate_bd, DECODE_RATE)
        self.rx_wav_path = rx_wav_path
        self.level = 0.0
        self._proc = None
        self._thread = None
        self._wav = None
        self._running = False

    def start(self):
        self._proc = subprocess.Popen(
            ["arecord", "-q", "-D", self.card, "-f", "S16_LE",
             "-r", str(CAPTURE_RATE), "-c", "2", "-t", "raw"],
            stdout=subprocess.PIPE)
        if self.rx_wav_path:
            self._wav = wave.open(self.rx_wav_path, "wb")
            self._wav.setnchannels(2)
            self._wav.setsampwidth(2)
            self._wav.setframerate(CAPTURE_RATE)
        self._running = True
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        chunk = CAPTURE_RATE * 4 // 10          # 0.1 s of stereo S16
        leftover = b""
        while self._running:
            data = self._proc.stdout.read(chunk)
            if not data:
                break
            if self._wav is not None:
                self._wav.writeframes(data)
            data = leftover + data
            usable = len(data) - len(data) % 4
            leftover = data[usable:]
            n = usable // 2
            v = struct.unpack("<%dh" % n, data[:usable])
            # Either channel may be the live one; the statistic is scale
            # invariant, so summing them costs nothing.
            mono = [(v[i] + v[i + 1]) / 65536.0 for i in range(0, n, 2)]
            m8 = [sum(mono[i:i + DECIMATE]) / DECIMATE
                  for i in range(0, len(mono) - DECIMATE + 1, DECIMATE)]
            self.detector.feed(m8)
            self.level = self.detector.level
            self.gate.note(self.level)

    def stop(self):
        self._running = False
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._wav is not None:
            self._wav.close()

    def play_wav(self, path, max_wait_s):
        """Listen first, then play. Returns (waited_s, deferrals, expired)."""
        waited, deferrals, expired = wait_until_clear(self.gate, max_wait_s)
        subprocess.run(["aplay", "-q", "-D", self.card, path], check=True)
        # Our own audio is not the channel; forget any reading that overlapped it.
        self.gate.reset()
        return waited, deferrals, expired
