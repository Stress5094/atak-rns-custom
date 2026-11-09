# Sample FM range test (2026-10-23 13:49:44, 900 s) -- NOT A RANGE RESULT

Both phones forced to FM, range test started on both (13:49:37 / 13:49:40,
beacon every 30 s). Build `cf4d00c`.

**The Pixel had no FM audio for the whole run**: "Still no USB audio input
device" x299 from 13:49:47 to the end, 0 PTT key-ups. Android on the Pixel
saw the Digirig's serial chip (10c4:ea60) but no USB audio device (no C-Media
CM108): the Digirig was half-enumerated. The OnePlus keyed 32 times with a
clear channel every time and heard nothing, because nothing was sent.

    Pixel -> OnePlus   0/22   (the Pixel logged them "sent", link=fm)
    OnePlus -> Pixel   0/20

Found: with no audio device the Pixel still reported `status(fm):
online=True` and logged "beacon sent ... link=fm" for 22 beacons; only 3 were
refused as "fm interface is offline". A cable that is not there must read as
FM down, not online. Not yet fixed.
