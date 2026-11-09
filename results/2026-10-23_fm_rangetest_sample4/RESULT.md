# Sample FM range test, run 4 (2026-10-23 14:45:57, 900 s): phones swapped places

As run 3 (FM forced, range test on both, volume at 1, build `cf4d00c`), but the
two phones swapped POSITIONS, each keeping its own handheld and Digirig.

    Pixel -> OnePlus   18/19 (95%)   run 3: 18/21, run 2: 17/21
    OnePlus -> Pixel   15/18 (83%)   run 3: 11/21, run 2: 13/21

The weak direction recovered when the positions swapped and the equipment did
not: points at POSITION (something near where the Pixel's radio sat, or the
path), not the OnePlus's transmit chain. Fisher exact, OnePlus->Pixel run 3 vs
run 4: p = 0.051; runs 2+3 pooled (24/42) vs run 4: p = 0.076. Suggestive, not
settled -- a run swapped back would test it.

Bursts: at the Pixel 27 OK / 3 LOST (2 "hit full scale") / 2 over its own TX;
at the OnePlus 29 OK / 2 LOST / 1 over its own TX. The spike-type loss at the
Pixel fell from 8 (run 3) to 2. Receive levels: Pixel ~0.18, OnePlus ~0.42.
