# FM range test, three drives (2026-11-08, 20:41-21:43 local)

FM only. OnePlus 8T **stationary** (`aaaa01`); Pixel 7 **in a car**
(`aaaa02`). Each phone kept its own handheld and Digirig cable. Operating point
`4T-1000/400-400Bd-H1024.2048` (the `fast` preset): 4FSK, 400 Bd, one copy,
CM108 keying, 2.95 s burst. Range test running, so a beacon every 30 s, each
carrying the sender's GPS fix. Build `8f602a3` for drive 1 and `298da18` for
drives 2 and 3, on both phones.

**No positions are published.** The CSVs keep `distance_m` (the phone's own
figure, computed at the time) and the age of each fix. The latitude and longitude
columns were removed.

**Settings:** transmit power and receive volume weren't recorded for these drives.
Before drive 3 the operator was advised to turn the Pixel's volume down. The
Pixel's own receive level says it went **up** instead (below).

## Files

| File | What |
|---|---|
| `pixel/rangetest-*.csv`, `oneplus/rangetest-*.csv` | One row per beacon heard: signal, the age of both fixes, distance, `usable` |

The phone logs these figures were read from aren't published, because they
carry positions.

The `usable` column is the phone's own verdict. A row is unusable when either
fix was too old to trust at the speeds involved. The rule is provisional: a
fix more than 60 s old with no measured speed is rejected.

## Results

**Farthest decodes (measured, from the CSVs):**

| Direction | Farthest | Notes |
|---|---|---|
| OnePlus → Pixel (Pixel's CSV) | **1384 m**, usable | drive 2. Pixel also decoded at 1125 m and 1096 m on the same drive |
| Pixel → OnePlus (OnePlus's CSV) | **846 m** (drive 3), 778 m, 646 m (drive 2) | all marked unusable, because the OnePlus's *own* fix was 100-165 s old |
| Both directions within the same minutes | **~456 m** | drive 3 |

The OnePlus didn't move, so its old fix is probably still right, and the
846 m and 646 m decodes are probably real distances. That is **inferred**: the
phone's rule can't know the phone was parked, and the rows stay marked
unusable. A 1525 m row on drive 3 used a 128 s-old fix from the moving Pixel and
should be discarded.

**Per file** (rows / rows with a distance / usable / farthest usable):

| Drive | Pixel (mover) | OnePlus (stationary) |
|---|---|---|
| 1 (20:41-20:52) | 11 / 5 / 1 / 531 m | 14 / 13 / 3 / 80 m |
| 2 (21:18-21:26) | 10 / 8 / 7 / **1384 m** | 14 / 10 / 1 / 22 m |
| 3 (21:32-21:43, different route) | 6 / 5 / 3 / 220 m | 19 / 16 / 4 / 539 m |

## What limited it

- **Direction asymmetry, which flipped between drives.** On drive 2 the Pixel
  heard the OnePlus out to 1.1-1.4 km. Meanwhile the OnePlus decoded only 3 of
  the Pixel's 8 beacons and logged about 14 bursts it heard but couldn't decode
  ("lost" or "cut short"). On drive 3 it was the other way round: the Pixel →
  OnePlus direction was the stronger one. The loss in the car affects both
  directions equally, so the car isn't the cause.
- **The Pixel's receive level was too high (measured).** On drive 3 its receive
  level jumped about 2.3x (about +7 dB, rms 0.24 → 0.56-0.62, against a target
  of 0.18-0.32). During its longest deaf gap, it logged 2 full bursts lost
  clipped at full scale and about 25 cut short. That this caused the deafness
  is **inferred, not proven**.
- **The OnePlus's own GPS barely refreshed** while parked indoors (fix age
  30-165 s), so most of its rows are marked unusable.
- **The OnePlus's LoRa radio was offline** and retrying the whole time. This
  doesn't affect FM, but it is in the logs.
- On drive 1 the Pixel's sound-card cable dropped off USB at 20:52:27, taking FM
  offline. Whether the operator unplugged it isn't known.

## The fix that made this run possible

Before this build, a range test only learned the far end's position from its
position reports. On air those are rate-limited to about one a minute, so at
30 km/h the far end's position was 300-400 m stale. Since `8f602a3` a phone
running a range test puts its own fix on every beacon. On drive 2 the OnePlus
booked the Pixel's beacon fixes at 13.5 s and 9.1 s old, where earlier runs
had minutes-old ones.
