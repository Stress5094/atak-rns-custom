# FM range test walk (2026-11-07 14:26:11-14:37:39, ~11.5 min)

FM only (no RNode attached on either phone, by design; `transport=ble
fm_backup=True`, the RNode-not-found errors in both logs are expected). Build
`dde76cb` on both. Operating point `fast`: 4FSK 400 Bd, 1000-2200 Hz,
`H_1024_2048_4f`, 1 copy, CM108 keying, 2.95 s burst. Range test started on both
(beacon every 30 s while it runs).

**Who was where.** OnePlus 8T stationary at the start point. Pixel 7 carried out
roughly due west and back. Farthest usable distance **298 m** (Pixel's own CSV,
14:33:14). Each phone kept its own handheld and Digirig.

**Settings:** `rx_volume=not recorded` on both handhelds -- declined, not
forgotten. The OnePlus's receive level says its knob was too high (below).

## Delivery, every keyed transmission (join.py -> join_output.txt)

Each phone's PTT key-up/key-down is lined up against the other phone's `[fm-air]`
verdict 0.5-7.5 s after it ended. Counts every burst sent (beacons, positions,
keyframe resends), not beacons only.

| | delivered | channel loss | lost to a collision |
|---|---|---|---|
| Pixel -> OnePlus | **24/31 (77%)** | 4 (all "hit full scale") | 3 |
| OnePlus -> Pixel | **18/26 (69%)** | 4 in band + 1 cut short | 3 |

Excluding collisions: **24/28 (86%)** and **18/23 (78%)**; Fisher p = 0.71, no
detectable direction difference. OnePlus -> Pixel against run 3's weak direction
(11/21): p = 0.11 -- suggestive only, and a different setup (walk, not desk).

Beacons only, by sequence number: Pixel -> OnePlus 8/10 in the window (lost seq
5 and 11, both in collisions); OnePlus -> Pixel 10/14 (lost 5, 7, 13, 15; 5, 13
and 15 overlapped the Pixel's own transmissions).

## What the losses were

1. **Three mutual collisions (measured):** 14:29:25 (keyed 0.5 s apart -- the
   Pixel's back-off ended as the OnePlus keyed; its carrier detector read 0.541
   against threshold 0.568 ~0.4 s into the OnePlus's burst), 14:35:48 (0.9 s),
   14:36:46 (0.0 s, both range-test beacons). Six bursts, both ends lost each
   time. **Guess:** two fixed 30 s beacon timers can lock in step; no jitter seen.
2. **OnePlus receive too loud (measured):** all 4 of its channel losses logged
   "receive audio hit full scale"; median OK-burst rx rms **0.60** (n=30) against
   the 0.18-0.32 aim. Pixel median **0.21** (n=22). Run 4 (2026-10-23) had the
   OnePlus at ~0.42. The losses were at 0-170 m, unrelated to distance.
3. **Pixel losses with level in band (app's guess: fading/interference):**
   14:27:21 (before the Pixel's first fix, at the start point), 14:28:24 (3 m),
   14:32:20 (170 m), 14:34:38 (240 m). OnePlus losses for comparison: start
   point, 3 m, 71 m, 170 m.
4. One delta frame at the OnePlus was decoded and then dropped: "delta frame
   references generation 2, last base is 1" (14:31:06) -- its keyframe never
   arrived.

**Distance did not show:** beyond 240 m, Pixel -> OnePlus 6/6, OnePlus -> Pixel
5/6. The range limit was not reached. FM gives no RSSI/SNR.

## The OnePlus's CSV distances are wrong -- fixed in `rangetest.py`

The stationary phone's CSV booked the walker as **1.0 m away, usable** at
14:31:11 and 14:31:42, when the Pixel's own fixes put it 143-170 m out; and 73 m
usable at 14:36:15-58 against a real 7-46 m. Cause, from the Pixel log: it
re-sent its start-up keyframe blind (msg 29598, attempts 2-4 at 14:28:23,
14:29:24, 14:30:23) and re-sent a position once (msg 2067, 14:29:49). The
OnePlus booked each copy by ARRIVAL time, so the old start position became a
fresh fix, and an unchanged resend measured speed 0, making `fix_error_m` tiny.

Fixed on the branch that filed this: a report is booked by its own CoT time,
one no newer than the fix held is refused, and fixes are aged from when they
were taken. **Use `rangetest-pixel.csv` for distance from this run**; the
OnePlus's CSV is kept as the evidence.

## Positions

No positions are published for this run. The CSVs keep `distance_m`, the
phone's own figure computed at the time, and the ages of both fixes. The
latitude and longitude columns were removed.

## Desk check of the fix (same evening, 17:12-17:25)

Both phones on the fixed build, side by side, range test started and stopped
over the new adb hook (`FM_TEST cmd=rangetest`). Both summaries:
`21 row(s), ... 4 stale report(s) ignored`. At the OnePlus, the Pixel's keyframe
resends #2-#4 (17:18:46, 17:19:55, 17:20:59) and one heartbeat resend (17:25:20)
were refused, and the Pixel's fix age kept counting from 17:18:21 through them
(25 -> 94 -> 158 s) where the old code reset it to ~0; the two new heartbeats
(stamped 17:21:21 and 17:24:21) were booked. Distances read 2-15 m (indoor GPS).
Not a walk -- the distance error itself needs one. Desk CSVs not kept in the repo.

Noticed: a frame's own row is written before the position it carries is booked,
so that row shows the previous fix (e.g. 17:22:33, age 251 s). Pre-existing,
small; not changed.

**Building the fix exposed a break:** the app's `rns` was unpinned, a rebuild
pulled 1.6.0, and FM would not come up ("FM backup interface could not be
built: 'FmInterface' object has no attribute 'announce_rate_target'"). Pinned
to 1.5.7 in `app/build.gradle.kts`; FM came up on both.
