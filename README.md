# ATAK over LoRa, with no network at all

[![tests](https://github.com/Stress5094/atak-rns-custom/actions/workflows/tests.yml/badge.svg)](https://github.com/Stress5094/atak-rns-custom/actions/workflows/tests.yml)

An Android app that carries ATAK's map traffic (positions, chat, markers,
drawings, routes) over a small LoRa radio instead of Wi-Fi or cellular. FM
handhelds can serve as a backup radio. ATAK itself is not modified: the app
runs next to it and looks to ATAK like an ordinary TAK server on the phone.

```
ATAK <-> this app <-> radio <-> [air] <-> radio <-> this app <-> ATAK
```

Everything else is in the **[User guide](docs/USER-GUIDE.md)**: every screen
and button, and the routing and repeating rules for RNodes and Meshtastic
boards.

## What you need

- **Two or more Android phones** (Android 8.0 or later) with ATAK-CIV.
- **One LoRa radio per phone**, either:
  - an **RNode** (tested on Heltec LoRa32 v3), by USB-C OTG cable or Bluetooth, or
  - a **Meshtastic board** (tested on RAK4631 and Seeed Tracker L1), by USB or Bluetooth.
- **Optional FM backup:** an FM handheld per phone, plus a USB sound card
  with a push-to-talk line (tested with a Digirig).
- **A frequency you are licensed or permitted to use** where you are.

## How to set it up

1. **Build and install** on each phone:

   ```
   git clone https://github.com/Stress5094/atak-rns-custom
   cd atak-rns-custom
   tools/build_libcodec2_android.sh        # FM modem library, once
   ./gradlew :app:assembleDebug
   adb install -r app/build/outputs/apk/debug/app-debug.apk
   ```

   You need the Android SDK (`local.properties` with `sdk.dir`; Android Studio
   writes it), plus the SDK's CMake 4.1.2 and NDK 28.2.13676358 for the FM
   library. Gradle fetches its own JDK 21. If the build can't find Python,
   install 3.9 (`uv python install 3.9`) and pass it with
   `-PbuildPython=/path/to/python3.9`. If you skip the FM library, the app
   still builds and works, just without FM.

2. **Connect the radio.** Plug it in and accept Android's USB prompt (tick
   "always open"). For Bluetooth or a Meshtastic board, see
   [Connecting the radio](docs/USER-GUIDE.md#3-connecting-the-radio).

3. **Set the same passphrase on every phone:** *Status → Passphrase*. There is
   no default. Check that the four-character **Network key** fingerprint on the
   Status tab matches on every phone.

4. **Set the same radio settings on every phone:** *Status → LoRa settings*. The default
   preset is Short Slow. Or set one phone up and share it: *Status → Share QR*
   on that phone, *Scan QR* on the others.

5. **Point ATAK at the app.** In ATAK, go to *Settings → Network Preferences*
   and add a server (streaming) connection: host `127.0.0.1`, port `7103`,
   TCP, no SSL.

6. **Check it works.** Move a marker. It should show up on the other phone in
   about a second, and the other node should appear under *Nodes* on the
   Status tab.

If a node doesn't show up, it is nearly always a passphrase or radio setting
that doesn't match. See [Troubleshooting](docs/USER-GUIDE.md#12-troubleshooting).

## Tests

`app/src/main/python/run_tests.sh` runs the desktop suite: 2,271 tests, and a
skipped test counts as a failure. It needs Python 3.9 and codec2 1.2.0
(`libcodec2-dev` on Debian/Ubuntu, `codec2` on Arch). CI runs the suite and
builds the APK on every push.

## Licence

The code is [MIT licensed](LICENSE). The built app bundles Reticulum, whose
licence adds two conditions: no use in systems designed to harm people, and no
use as AI training data. See [third-party notices](THIRD-PARTY-NOTICES.md).

Not affiliated with the TAK Product Center or the ATAK project. ATAK is
neither bundled nor modified.
