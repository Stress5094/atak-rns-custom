# Range test results

FM range-test runs between two phones: a OnePlus 8T (Android 14) and a Pixel 7
(Android 17), each with its own FM handheld and Digirig sound-card cable. Each
folder holds the run's write-up (`RESULT.md` or `README.md`) and/or its
range-test CSVs, which record every beacon heard: signal, distance, and the age
of both GPS fixes.

How the range test works, and what the CSV columns mean, is in
[how it works](../docs/how-it-works.md#12-the-app-screen-by-screen) and
`app/src/main/python/rangetest.py`.

**No positions are published.** The latitude and longitude columns were removed
from every CSV. `distance_m` is the phone's own figure, computed at the time. Phone
identifiers are placeholders: `aaaa01` / `ANDROID-1111…` is the OnePlus and
`aaaa02` / `ANDROID-2222…` the Pixel. The phone logs and analysis scripts the
write-ups mention (`*.log`, `join.py`, `score.py`, `analyze_run.py`, `run.sh`)
were kept private, because the logs carry positions. Their outputs are quoted in
the write-ups, and `2026-11-07_fm_rangetest_walk/join_output.txt` is included.

| Run | What it was | Headline |
|---|---|---|
| [2026-10-01_phone_phone_fm_rangetest](2026-10-01_phone_phone_fm_rangetest) | First phone-to-phone FM run | ~9-point delivery difference between the two directions |
| [2026-10-01_radio_swap_fm_rangetest](2026-10-01_radio_swap_fm_rangetest) | Same, with the handhelds swapped between phones | Pixel heard OnePlus 79% → 50%, OnePlus heard Pixel 70% → 48%; too few frames to say whether the weak side follows the radio |
| [2026-10-23_fm_rangetest_sample](2026-10-23_fm_rangetest_sample) | Procedure sample | **Not a range result:** one phone's cable was half-detected and it sent nothing |
| [2026-10-23_fm_rangetest_sample2](2026-10-23_fm_rangetest_sample2) | Procedure sample, phones on the bench | Pixel → OnePlus 17/21, OnePlus → Pixel 13/21 |
| [2026-10-23_fm_rangetest_sample3](2026-10-23_fm_rangetest_sample3) | As run 2, handheld volume turned down | 18/21 (86%) and 11/21 (52%) |
| [2026-10-23_fm_rangetest_sample4](2026-10-23_fm_rangetest_sample4) | As run 3, phones swapped positions | 18/19 (95%) and 15/18 (83%): the weak direction followed the position, not the equipment |
| [2026-11-07_fm_rangetest_walk](2026-11-07_fm_rangetest_walk) | Walk: one phone stationary, one carried out and back | 24/31 (77%) and 18/26 (69%) of bursts delivered; farthest usable distance **298 m** |
| [2026-11-08_fm_drives](2026-11-08_fm_drives) | Three drives: one phone stationary, one in a car | Decoded at **1384 m** one way and 846 m the other; both directions within the same minutes out to ~456 m; strong direction flipped between drives |

All of these are FM at the default `fast` operating point unless the write-up
says otherwise. None is a LoRa range figure. The write-ups state what was
measured and what was only inferred. Read them before quoting a number.
