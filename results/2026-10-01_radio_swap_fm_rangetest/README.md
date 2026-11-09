# 2026-10-01 afternoon -- FM run after exchanging the handhelds

Follow-up to `../2026-10-01_phone_phone_fm_rangetest/`, which found a 9-12
point delivery difference between the two directions. The handhelds were
swapped between the phones to see whether the weaker side moved with the
radio.

**It did not answer that, and it showed the morning run could not have
either.** With ~40 frames per direction the sampling noise on a direction
*difference* is +-11 points, so the morning's 8.9-point gap is 0.8 standard
errors from zero. Resolving a 10-point difference needs ~390 frames per
direction, about 3.3 hours of recording at the 30 s range-test beacon.

| | morning | this run |
|---|---|---|
| Pixel heard OnePlus | 79.3% | 50.0% |
| OnePlus heard Pixel | 70.4% | 47.7% |
| direction spread | 8.9 pts | 2.3 pts |

Both figures are CSV-sequence-gap scores, the one method available for both
directions here. The 23-29 point common-mode drop is much larger than the
noise floor and is the thing worth chasing; the radios were handled during the
swap and **the volume knob is the gain stage on an FM audio link**, but nothing
recorded its position.

Run `python3 analyze_run.py` from the repo root for the figures, the noise
floor and the run-length calculation. Its docstring lists what went wrong
during collection -- a lost logcat buffer and a mid-run bridge restart -- and
`log-filtering.txt` records what was stripped from the OnePlus log and why.

Full write-up: the 2026-10-01 afternoon entry in `docs/open-questions.md`.
