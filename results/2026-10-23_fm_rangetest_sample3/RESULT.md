# Sample FM range test, run 3 (2026-10-23 14:26:56, 900 s): handheld volume at 1

Same as run 2 (both phones forced to FM, range test on both, beacon every
30 s, build `cf4d00c`) with both handhelds' volume turned down to 1. Whether
the phones were moved apart is not recorded.

    Pixel -> OnePlus   18/21 (86%)   run 2: 17/21
    OnePlus -> Pixel   11/21 (52%)   run 2: 13/21

Receive level of decoded bursts: Pixel rx rms 0.19-0.21 (on target, was
~0.6); OnePlus 0.37-0.43 (still above 0.18-0.32, was ~0.6).

At the Pixel: 19 OK, 10 LOST, 2 over its own transmission. 8 of the 10 LOST
lines say "receive audio hit full scale (peak 1.00): turn the receiving volume
down", 2 say "level in band ... fading or interference". **Lost and decoded
bursts have the same level (0.65-0.98) and the same rms (~0.20).** A burst at
rms 0.20 reaching peak 1.00 is a crest factor ~5, far above an FSK tone's ~1.4:
short spikes on a normal signal, not an over-loud radio. The "turn the volume
down" advice is fm_airlog's own stated guess from the level and is probably
WRONG here -- turning down further would weaken the good signal and may not
touch the spikes.

The OnePlus -> Pixel direction is the weak one in both runs (62%, 52%) while
Pixel -> OnePlus held 81-86%. Untested hypotheses: spikes from the OnePlus's
transmit chain (Digirig / radio), or interference near the Pixel's radio. A
swap of the two handhelds (or Digirigs) between phones separates them.

At the OnePlus: 29 OK, 2 LOST (both "hit full scale").
