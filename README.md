# ATAK over LoRa, with no network at all

[![tests](https://github.com/Stress5094/atak-rns-custom/actions/workflows/tests.yml/badge.svg)](https://github.com/Stress5094/atak-rns-custom/actions/workflows/tests.yml)

An Android app that carries ATAK's situational awareness over a Reticulum/LoRa
radio link instead of an IP network. No Wi-Fi, no cellular, no TAK server: two
or more phones, one small radio each, and a map that stays shared.

ATAK is not modified and nothing is installed into it. The app runs alongside
it, presents itself as an ordinary TAK server on the phone's own loopback
address, and puts what ATAK sends over the air.

```
ATAK  <->  this app  <->  RNode (LoRa)  <->  [air]  <->  RNode  <->  this app  <->  ATAK
```

A position report that leaves ATAK as 705 bytes of XML crosses the air as
**63 bytes** and arrives in the other ATAK in about half a second, with the
callsign, team colour and role intact. Everything on the air is encrypted and
authenticated under a passphrase the group shares. Chat, markers, drawings and
routes are acknowledged and retried; positions are not, because the next one
repairs them.

## What you need

- **Two or more Android phones**, Android 8.0 or later, each with ATAK-CIV
  installed.
- **One RNode per phone.** These are hobby LoRa boards running RNode firmware —
  Heltec LoRa32 v3 is what every bench result here was measured on. Any RNode
  should work; if the board is not a Silicon Labs CP210x one, the USB device
  filter needs its vendor and product id, or Android will never offer the app
  permission to see it.
- **A USB-C OTG cable, or nothing at all** — the app talks to the radio over
  Bluetooth too, and can switch the radio's Bluetooth on for you over the cable
  the first time.
- **A frequency you are allowed to use.** See [before you
  transmit](#before-you-transmit).

## Quick start

1. **Build and install** on each phone:

   ```
   git clone https://github.com/Stress5094/atak-rns-custom
   cd atak-rns-custom
   ./gradlew :app:assembleDebug
   adb install -r app/build/outputs/apk/debug/app-debug.apk
   ```

   A fresh clone needs `local.properties` with `sdk.dir` pointing at your
   Android SDK; Android Studio writes it on first open. For the FM link, also
   build the modem library once first: `tools/build_libcodec2_android.sh`. Details and traps in
   [building it](docs/building.md).

2. **Connect the radio.** Plug the RNode in and grant the USB prompt, or use
   the app's radio picker to pair it over Bluetooth.

3. **Set the same passphrase on every phone.** There is deliberately no
   default: an unconfigured node makes up its own and stays alone, rather than
   joining a network anyone within earshot can read. The status screen shows a
   four-character fingerprint of the key — compare it across phones, because a
   wrong passphrase and a dead radio look identical from one device.

4. **Set the same radio preset on every phone.** Eight presets, named the way
   Meshtastic names them; the default is Short Slow. Mismatched radios also look
   exactly like dead ones.

5. **Point ATAK at the app.** In ATAK: Settings -> Network Preferences -> add a
   server / streaming connection, host `127.0.0.1`, port `7103`, TCP, non-SSL.
   Each phone's ATAK connects to itself; nothing leaves the phone except over
   the radio. (`7103`, not the conventional `8087`: ATAK's own native
   mesh/multicast networking also uses `8087` and binds it on every
   interface, so the bridge must stay off that port.)

6. **Move a marker.** It should appear on the other phone in about a second,
   and the status screen should show the other node connected.

## What works, and what it costs

Everything below was measured on real hardware — two phones and two RNodes,
plus a four-node bench for the load figures — not estimated.

**A second link exists for when there is no LoRa radio at all:** ordinary FM
handhelds, over the radio's own audio jack, no RNode required. Real ATAK CoT
has crossed it end-to-end on hardware, but unlike everything below its
reliability has not yet settled into a stable number. The range tests in
[results](results) are the evidence; see [how it works](docs/how-it-works.md#15-what-is-and-is-not-proven)
before relying on it.

**Working, and verified on the radio:** the compact codec (~11x smaller than
raw CoT), significance-based throttling of position reports, acknowledged
delivery with retries and honest failure reporting, an authenticated and
encrypted mesh under a shared group key, relaying across an intermediate node,
Bluetooth as an alternative to the cable, and the movement policy that
suppressed 84% of position reports on a real drive.

**Built and desktop-tested, not yet on the radio:** the milestone 7 frame
shrink — smaller headers, several messages bundled into one packet, and a
check-in cadence the receiver controls instead of ATAK's own timer.

**What it costs.** Capacity is not what the bitrate suggests, and it does not
even improve monotonically with it:

| | Short Turbo | Short Slow (default) | Medium Fast | Long Fast |
|---|---|---|---|---|
| Bitrate | 21,875 bps | 6,250 bps | 3,516 bps | 1,074 bps |
| Acknowledged messages a minute it keeps up with | 80 | **60** | 45 | **8** |
| The most it will carry before collapse | ~100 | **~130** | ~60 | ~10 |
| Latency floor | 0.6 s | 0.76 s | 1.0 s | 2.5 s |

Short Slow has been the default since 2026-10-15; it was Long Fast, which
carries roughly **eight acknowledged messages a minute across the whole mesh**
at four nodes — the entire budget for chat,
markers, drawings and routes combined. Positions are much cheaper, being
fire-and-forget. Faster presets are not automatically better: Short Turbo has
three and a half times Short Slow's bitrate and carries fewer messages, not
more, because its short symbols lose to collisions long before the air runs
out — it gives up with the channel 72% idle, where Short Slow fills it
honestly.

**Where it has not been proven:** every load figure comes from two to four
nodes, so sixteen-node behaviour is modelled rather than measured, and LoRa
range at distance is untested — all of it was measured with the radios close
together. FM has been range-tested on foot and by car (out to 1.4 km one way); those
runs are in [results](results).

2,271 app tests and 287 tool tests, no skips: `app/src/main/python/run_tests.sh` and
`python3 -m unittest discover -s tools -p 'test_*.py'`.


## Where the detail lives

This file is the entry point. The detail is split by the question you are
trying to answer rather than by when it was written.

| Document | Answers |
|---|---|
| [How it works](docs/how-it-works.md) | **Start here.** The whole app as it stands: routing, delivery, security, the FM and Meshtastic links, and every screen |
| [How it is put together](docs/architecture.md) | The pipeline, why Kotlin and Python split the work the way they do, and what each file is |
| [What goes on the wire](docs/protocol.md) | Frame formats, the compact encoding for every CoT type, fragmentation, and every protocol version bump |
| [Routing and airtime](docs/routing.md) | What gets sent at all, what gets acknowledged, what gets relayed, and what each costs |
| [Running it](docs/operations.md) | Radio settings, Bluetooth pairing, getting logs off a phone |
| [Building it](docs/building.md) | Which JDK, which Python, and the traps |
| [Settings sharing](docs/settings-sharing.md) | One host pushing radio settings to a group, approved per phone |
| [Plain-language guide](docs/mesh-explained.html) | How the network behaves, for people who will not read the code |
| [Range test results](results) | FM and desktop range-test runs: the CSVs, logs, and a write-up per run |

## Licence

This project's code is [MIT licensed](LICENSE).

The built app bundles the Reticulum stack, which carries two extra conditions
of its own — no use in a system able to purposefully harm people, and no use in
AI training data. Those travel with any build handed to someone else. See
[third-party notices](THIRD-PARTY-NOTICES.md) for the full list of components
and their terms.

Not affiliated with, endorsed by or connected to the TAK Product Center or the
ATAK project. ATAK is neither bundled nor modified here.
