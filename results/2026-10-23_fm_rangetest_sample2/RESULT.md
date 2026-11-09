# Sample FM range test, run 2 (2026-10-23 14:08:08, 900 s)

Both phones forced to FM, range test on both (beacon every 30 s), Pixel's
Digirig reseated after run 1 (where it was half-enumerated and sent nothing).
Build `cf4d00c`. Phones on the bench, not moved apart as far as the logs show
-- a sample of the procedure, not a range figure.

    score.py pixel.log oneplus.log $(cat start_host_epoch.txt) $(cat end_host_epoch.txt)

(`score.py` here is the Stage 2 scorer with the beacon-heard pattern widened:
FM beacons log `rssi=? snr=?`, which the original's number-only pattern
silently skipped -- it first reported 0/21 both ways for this run.)

    Pixel -> OnePlus   17/21 beacons (81%)
    OnePlus -> Pixel   13/21 beacons (62%)

Bursts decoded / lost at each receiver (all frames, not only beacons):
Pixel 22 OK, 6 LOST, 1 cut short, 1 over its own transmission;
OnePlus 26 OK, 4 LOST.

**Every LOST burst was a full-length carrier flagged "receive audio hit full
scale (peak 1.00): turn the receiving volume down".** Decoded bursts ran rx rms
0.56-0.74 (OnePlus mean 0.62) against the 0.18-0.32 target. Both handhelds'
volume is too high; on this evidence clipping is the main loss on the bench,
ahead of half-duplex (the receiver keying over a burst).

The laptop's three RNodes were unplugged 14:11:18-14:11:23 (kernel log); the
phones logged their LoRa radios offline -- irrelevant with FM forced.

Beacons every 30 s on two nodes, 2.95 s bursts: ~20% of the time one node is
on the air, so overlaps are expected (1 "OVER OUR TX" seen at the Pixel).
