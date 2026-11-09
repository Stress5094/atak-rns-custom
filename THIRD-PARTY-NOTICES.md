# Third-party components

This project's own code is under the MIT licence — see [LICENSE](LICENSE). The
built app also contains the following third-party components, each under its
own terms.

## Reticulum Network Stack (`rns`)

Copyright (c) 2016-2025 Mark Qvist — <https://github.com/markqvist/Reticulum>

Distributed under the **Reticulum License**, which is the MIT licence plus two
additional conditions. Paraphrased, they are:

- the software must not be used in any system that includes, among its
  functions, the ability to purposefully do harm to human beings; and
- the software must not be used, directly or indirectly, in creating a
  dataset for training artificial intelligence, machine learning or language
  models.

The authoritative text ships in the library's own source files and in the
upstream repository. **These conditions travel with any build of this app that
is handed to someone else**, because Chaquopy installs `rns` into the APK — the
app's MIT licence covers the code in this repository, not the bundled library.
Any binary release must carry the Reticulum License text alongside this notice.

Reticulum in turn pulls in `pyserial` (BSD 3-Clause) and `cryptography`
(Apache-2.0 / BSD 3-Clause), which are likewise bundled into the APK.

## Meshtastic Python client (`meshtastic` 2.7.11)

Copyright (c) Meshtastic LLC and contributors —
<https://github.com/meshtastic/python>

Distributed under the **GNU General Public License, version 3 only**
(GPL-3.0-only); the full text ships in the package's `LICENSE.md`. Chaquopy
installs the client into the APK because `mt_port.py` and `mt_config.py` import
its protobuf definitions and serial interface to talk to a Meshtastic board.
**This is bundled code, not only an idea.** Any binary handed to someone else
carries it and its GPL-3.0 obligations; the repository's own MIT licence does
not relicense it.

Its Python dependencies are installed with it and are likewise in the APK:
`protobuf` (BSD-3-Clause), `pypubsub` (BSD-3-Clause), `tabulate` (MIT),
`requests` (Apache-2.0), `bleak` (MIT), `pyyaml` (MIT), and `pyserial`
(BSD-3-Clause).

## codec2 (`libcodec2` 1.2.0)

Copyright (c) David Rowe and contributors —
<https://github.com/drowe67/codec2>

Distributed under the **GNU Lesser General Public License, version 2.1**
(LGPL-2.1). `tools/build_libcodec2_android.sh` cross-compiles it into
`app/src/main/jniLibs/arm64-v8a/libcodec2.so`; that binary is not committed to
this repository but is present in every build. `fm_modem.py` loads it at
runtime for the FM/FSK-LDPC link. Anyone redistributing a built APK must also
make the corresponding codec2 source available under the LGPL.

## Android libraries

| Component | Copyright | Licence |
|---|---|---|
| Chaquopy | Chaquo Ltd | MIT |
| CPython 3.8, bundled by Chaquopy | Python Software Foundation | PSF License |
| usb-serial-for-android | mik3y and contributors | MIT |
| AndroidX (`core-ktx`, `appcompat`) | The Android Open Source Project | Apache-2.0 |
| Kotlin standard library | JetBrains s.r.o. | Apache-2.0 |
| Material Components for Android | Google LLC | Apache-2.0 |
| Material Symbols (navigation icons, `res/drawable/ic_nav_*.xml`) | Google LLC | Apache-2.0 |
| ZXing and zxing-android-embedded (QR codes) | ZXing authors; JourneyApps | Apache-2.0 |

**EFF Short Wordlist 1** — the 1296 words `passphrase_gen.py` draws generated
passphrases from. © Electronic Frontier Foundation, <https://www.eff.org/dice>,
licensed CC BY 3.0 US. Used unmodified, embedded as a Python tuple.

## Borrowed as ideas, not as code

**Meshtastic protocol naming** — <https://meshtastic.org>

`radio.py` reproduces Meshtastic's eight LoRa preset names and the spreading
factor, bandwidth and coding rate behind each, and takes Long Fast as its
default, because those names are the vocabulary people in this space already
reason in. `rangetest.py` is modelled on their Range Test module, and one
relaying heuristic in `relay.py` follows their approach. None of that naming or
those heuristics is copied from the Meshtastic sources; the GPL-3.0 Python
client that *is* bundled is a separate dependency, covered above. Nor are
shared modem settings interoperability — the framing, addressing and encryption
above the modem are unrelated, and a Meshtastic node on the same preset will
not talk to this.

No licence obliges the paragraph above. It is here because the work deserves
naming.

## Not bundled

ATAK itself is not included, redistributed or modified by this project. The app
talks to ATAK over a local network socket on the same device; no ATAK code is
linked in, and this project is not affiliated with or endorsed by the TAK
Product Center.
