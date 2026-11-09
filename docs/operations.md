# Running it

Getting logs off a phone, radio settings, and the RNS version notes.

> **Where things live**
> [README](../README.md) · [How it works](how-it-works.md) ·
> [How it is put together](architecture.md) · [What goes on the wire](protocol.md) ·
> [Routing and airtime](routing.md) · [Running it](operations.md) ·
> [Building it](building.md) · [Settings sharing](settings-sharing.md) ·
> [Plain-language guide](mesh-explained.html) · [Range test results](../results)

## Verified against RNS 1.4.2 source (update if you're on a different version)

The original draft of `rns_bridge.py` hand-built the `RNodeInterface` object
field-by-field via `__new__()`, skipping its real `__init__`. Installing RNS
1.4.2 locally and reading `Interfaces/RNodeInterface.py` / `Reticulum.py` /
`Transport.py` turned up three bugs that would have stopped milestone 1 cold,
now fixed in `_build_rnode_interface()`:

1. **`Reticulum` instances have no `.interfaces` attribute.** The old
   `_reticulum.interfaces.append(rnode_iface)` throws `AttributeError`
   immediately on `start()`. The real interface list lives on
   `RNS.Transport.interfaces`, populated via `RNS.Transport.add_interface()`
   — which is what the fixed code does now.
2. **Wrong `owner`.** `process_incoming()` calls `self.owner.inbound(data,
   self)`, and `inbound()` only exists as a `@staticmethod` on
   `RNS.Transport`, not on a `Reticulum` instance. The old code passed the
   `Reticulum` instance as owner, which would have crashed on the first byte
   received from the RNode. Fixed: `owner` is now `RNS.Transport`.
3. **Hand-built interface skips required state and the radio handshake.**
   Bypassing `__init__` meant `interface_ready`, `rxb`/`txb`, `flow_control`,
   and `id_callsign` were never set (crashing `process_outgoing`/
   `process_incoming` on the first packet either direction), and — more
   importantly — `initRadio()` was never called, so frequency/bandwidth/TX
   power/spreading factor were never pushed to the physical RNode. The radio
   would have stayed powered off even without the crashes. Fixed: the new
   code subclasses `RNodeInterface`, overrides only `open_port()` to attach
   the bridged socket, and lets the real `__init__()`/`configure_device()`
   run against it — so the actual KISS detect/radio-config handshake happens
   against your hardware. This does mean the interface constructor now
   requires real LoRa radio parameters (`RNODE_FREQUENCY_HZ` etc. at the top
   of `rns_bridge.py`) — set these to values your board supports and that
   are legal to transmit on in your region, and make sure both phones agree.

One caveat that turned out to be a non-issue: the `.serial` attribute name
concern from the original draft was correct as written — it really is
`.serial` in RNS 1.4.2. That wasn't the actual blocker; the three bugs above
were.

**Worth knowing about, not currently used:** RNS ships a native Android
interface (`RNS.Interfaces.Android.RNodeInterface`), built on `jnius`/
`usb4a`/`usbserial4a` for direct USB access from Python — this is the path
Sideband itself uses, via python-for-android rather than Chaquopy. It's not
compatible with Chaquopy's Java-interop model, so adopting it would mean
switching the whole toolchain away from Chaquopy. The fix above keeps the
current Chaquopy + Kotlin-USB-bridge architecture and makes it correct
instead — but if you ever hit a wall this is the "how upstream actually does
it" alternative.

## Getting the log off the phone

**Android's logcat is a ring buffer, and on these phones it is minutes deep.**
Both bench devices report 256 KiB. The 2026-08-14 range test lost its entire
record to that: the walk ran 17:38–18:09, and by 18:12 the oldest surviving
line from this app was 18:08:31. What saved the result was luck — the tx policy
counters are cumulative since process start, so `[onset=2 moving=8]` could
still be read off the *current* status line long after the lines describing it
had aged out.

So the app now records its own log to a file. It starts automatically with the
bridge, and the status screen shows the filename and its size — **in red if
recording is off**, because a test with no log is otherwise discovered hours
later, back at a laptop.

**Start log / Stop log** bounds a run: stopping and starting begins a fresh
file, so a test can be exactly the file rather than the file plus everything
either side of it.

**Export** stitches the whole run — the rotated pieces and the live file, in
order — into `Downloads/bridge-<date>-<time>.txt`. That is the one to use when
the phone was never on Wi-Fi, since Downloads is readable by the Files app and
by any share target, and `adb pull`s without `run-as`:

```
adb pull /sdcard/Download/bridge-20260814-183852.txt
```

Without exporting, the raw files are in app-private storage and need `run-as`
(so, a debug build):

```
adb shell run-as io.github.stress5094.customrnsbridge ls -la files/logs/
adb shell run-as io.github.stress5094.customrnsbridge cat files/logs/bridge-20260814-183852.log > bench.log
```

Internal storage rather than `getExternalFilesDir()` on purpose: adb's access
to `/sdcard/Android/data` was restricted in Android 11 and varies by version,
and a `run-as` shell gets no mount view of it at all — confirmed on the OnePlus
while building this. Export exists precisely because that path is unreliable.

Two things to know. Writes lag by up to ~30 s, because logcat buffers before
flushing — stop the log before exporting for the most complete file. And on a
long run the rotated siblings (`….log.1`, `.2`) are separate files; Export
already concatenates them, `cat` does not.

Only this app's own lines are captured. An app reading logcat without
`READ_LOGS` still sees its own entries and only its own, which is both what we
want and, usefully, free of the device noise that caused the rotation.

## Radio settings are set in the app, not in the source

There is no longer a radio config to edit or revert. **Radio → preset,
frequency, TX power**, applied to the live radio and persisted in app-private
storage the way the mesh passphrase is. The presets are Meshtastic's own eight,
by their own names, because those are the names people in this space already
reason in — see `radio.py`.

The default is **Long Fast**, which is within half a dB and ten percent of
throughput of the SF10/125 kHz setting this app carried before presets, so an
upgrade lands on effectively the same radio.

Two things follow from that, and both bite silently:

- **Every node must match.** Mismatched radio settings and a dead radio are
  indistinguishable from one device, which is why the status screen prints the
  settings on every node — read them side by side, the same way you compare the
  key fingerprint.
- **The duty-cycle figures in this README were measured at SF8 / 125 kHz**, and
  the preset changes them. Long Fast is 1074 bps against that setting's 3125,
  so the same traffic costs roughly three times the airtime. Fine at two nodes;
  it does not fit at sixteen. The preset comparison table above is the map.

TX power accepts up to 37 dBm, which is RNS's own hard limit — above it
`RNodeInterface` fails its config check and the interface never comes up. If
the board cannot deliver what you asked for it clamps and reports the real
figure, and **that reported figure is what gets saved**. This is not cosmetic:
RNS aborts RNode startup outright when the reported parameters disagree with
the configured ones, so storing the requested value would leave a radio that
worked until the next launch and then presented as dead.

**Boards clamp at the bottom too.** Measured 2026-08-18: a Heltec v3 asked for
2 dBm reports 13 and will go no lower, so the same reconcile-and-save path runs
in reverse. That matters for bench setups, where the tool's own default is
2 dBm precisely because radios inches apart desensitize each other — on this
hardware 13 dBm is the quietest you can get, and the 2026-08-18 preset bench
logged interference at +70 dB over the noise floor because of it.

The reassembly window follows the radio automatically and needs no edit — see
`_resize_reassembly()`, and `TestRetryOutlivesReassembly` for why it has to.

## FM tone placement, and mixed radio models

> **The FM transport was removed on 2026-10-01 to be rebuilt from the ground
> up, so there is no tone-placement setting in the app today.** This section
> is kept because it is the *operating procedure* the bench work produced, and
> it is what a rebuilt FM link needs on day one. See
> FM lessons for the measurements behind every number here.

**Radio link → FM tone placement** was a named profile, visible whenever FM
was selected as the transport or enabled as the backup link. Saved per node,
applied at the next bridge start, and printed on the status screen as the
first line of the FM block.

This setting exists because an FM handheld passes only a narrow slice of
audio — built for voice, it quietly discards anything too low or too high —
and **where that slice sits differs by radio.** Measured on this bench's two
chains: 7.3 dB of difference at 3000 Hz alone. The four FSK tones have to
land inside it. They did not until 2026-09-30, which is why phone-to-phone
FM delivered nothing at all: the top tone sat past the edge of the band,
13–21 dB down, and a quarter of a four-symbol alphabet mostly absent is not
something LDPC recovers.

**It is one setting for the whole net, not a per-person adjustment.** Two
nodes on different profiles recover nothing from each other — total silence,
not reduced range — and the link **cannot report the mismatch**, because a
disagreeing receiver never gets a frame intact enough to carry a complaint.
So:

- **Match it by eye before splitting up.** The status screen line is the only
  check that exists. Compare it across phones the same way you compare radio
  presets and the key fingerprint.
- **Pick for the weakest radio in the party.** If one model passes a narrower
  band than the rest, everyone moves to a placement that fits it. You do not
  give that person their own profile — there is no such thing here.
- **A non-default profile is tinted as a warning** on the status screen. Not
  because it is wrong, but because "someone changed the tones" is worth ruling
  out before concluding a radio is broken.

**Qualifying a new radio model is bench work, not field work.** Run
`tools/passband_sweep.py` one tone at a time, with the radio on the machine
running it, and test the model **both transmitting and receiving** — this
project has already found one cable that was a weak transmitter and another
that was a weak receiver, each as a specific unit rather than as a category.
The bar: all four tones arriving within roughly 5 dB. For scale, 4.7 dB
delivered 7 of 9 beacons; 13–21 dB delivered nothing.

Two limits worth knowing before you plan around this:

- **Four tones need 750 Hz of usable band and cannot be squeezed tighter.**
  `narrow` sat at the tightest legal spacing: tone spacing must be at least
  the symbol rate for the tones to stay orthogonal, and 250 Bd is itself a
  hard floor on this link (an analog limit in the receiving cable, not a
  software choice). A radio whose usable band is narrower than that cannot be
  accommodated by moving tones; it needs fewer tones or a slower symbol rate.
- **A bench button must send the shape the link actually sends.** The cheap
  TX/RX test buttons ran a smaller, faster code at the library's own default
  tones, so a pass there said nothing about the placement production was on.
  That is why a separate beacon-shaped probe existed at all.

Profiles are deliberately few. An entry in that table is a placement
something has actually been measured on; an unmeasured one is a way to select
a dead link from a hillside.

## Reaching the radio over Bluetooth instead of a cable

**Verified on hardware 2026-08-18.** Both phones carried the whole preset
bench over Bluetooth — ~40 minutes of load, three presets, zero link drops —
see hardware results. What has *not* been tested is a link that
fades rather than one that works: see open questions.

The RNode speaks the same bytes over Bluetooth as it does down the cable, so
nothing above the wire changes: same codec, same mesh, same acknowledgements,
same airtime figures. Only `UsbSerialBridgeService` is swapped for
`BleSerialBridgeService`, and both feed the identical loopback socket Python
connects to.

**It is BLE, not the old wireless-serial kind of Bluetooth.** The Heltec
LoRa32 v3 is an ESP32-S3 and that chip has no classic Bluetooth radio at all,
so RFCOMM/SPP — which would have been ten lines — is not available on this
hardware. The transport is the Nordic UART service, the same one Reticulum's
own Android interface talks to.

### Switching a phone over

1. **Radio link → Turn on**, with the cable still plugged in. The radio ships
   with Bluetooth off, and the command to switch it on has to travel over a
   link you already have. It is stored in the radio, so this is once per
   radio, not once per session. The firmware does not acknowledge it, so the
   app reports only that the command was sent.
2. **Scan**, and tap the radio when it appears.

   Android will ask for a six-digit code that only the radio knows. The radio
   shows it on its own screen — but only while it is in pairing mode, which is
   what the **Pairing mode** button is for, or a five-second hold of the
   radio's own button. It also *reports the code back over the link*, as KISS
   command 0x62, and RNS's `RNodeInterface` has no case for that command at
   all: the frame arrives and is discarded. `_PinSniffingSerial` watches for it
   so the app can show the code on the phone that is asking for it, which is
   the difference between pairing in the field and pairing next to a laptop.

   Note that **BLE devices do not show up in Android's own Bluetooth settings
   screen** — that is why the scan lives in the app rather than in a line of
   documentation pointing at system settings. Sideband carries the same screen
   for the same reason.
3. **Save**, then restart the bridge when asked. Changing the wire under a
   running bridge would leave two services fighting over one loopback port.
4. Unplug.

The button on the status screen names the link that is actually carrying
traffic, and says `(restart to apply)` while a saved change has not landed
yet.

### Which radio is this phone using

Over USB the cable answers it. Over Bluetooth nothing does, so two things say
so explicitly:

- The status screen names the radio in the identity block, under the node id,
  whenever the link is Bluetooth.
- **Radio link → Identify** flashes the light on the radio *this* phone is
  connected to, which is the only way to match a name like `RNode 3f2a` to a
  box on the table. It sends KISS command 0x30 with a blink count —
  `led_indicate_info()` in the firmware. Nothing in RNS or `rnodeconf` sends
  that command, they only name it, so this is the one command here without an
  upstream copy to check against. The firmware does not acknowledge it, and
  what lights up varies by board.

### Putting this node on the radio's screen

**Radio link → Show this node on the radio's screen.** Off by default: the
screen belongs to the radio, and taking it over is a choice. The same button
gives it back.

What it shows, in the order the questions get asked:

```
4F8A2C          this node's id, as other phones list it
RNODE 3F2A      the radio's own name (Bluetooth only — see below)
>PIXEL 7        the phone this radio is attached to
----------
BRAVO           who it can hear, by callsign, or device name
CHARLIE         when ATAK has not supplied one
SAMSUNG S2
+2 MORE
```

The firmware hands over a 64×64 corner of the OLED and nothing else: one bit
per pixel, drawn 1:1, with no text command of any kind. `display.py` therefore
carries its own 5×7 font, written out as pixels rather than hex because the
only way to check a font is to look at it. That budget is **8 lines of 10
characters**, which is why every line truncates rather than wraps — a name cut
short is still recognisable, a name that steals the row below it costs a whole
peer.

Three things worth knowing:

- **The radio's own name is only known over Bluetooth.** Nothing in the KISS
  protocol carries it; the phone's Bluetooth stack is the only thing that knows
  it. Over USB that line is left out rather than left blank, so the peer list
  gets the row instead.
- **Only changed rows are sent**, and the transmit lock is taken per row rather
  than per frame. Held across a whole redraw, the push would block CoT for as
  long as it takes — a second or two over Bluetooth, where every write waits
  for the radio to acknowledge it.
- **The pairing code still wins.** The firmware draws its own pairing screen
  over the external framebuffer whenever a code is active, so taking the screen
  does not cost you the ability to pair.

### What can go wrong

- **Two paired RNodes and no explicit choice** — the service refuses to guess
  and waits, saying so in the log. One paired radio needs no configuration at
  all.
- **Permission denied** — the picker still opens and USB still works. The
  service asks for nothing but `BLUETOOTH_CONNECT`; scanning is the picker's
  business, not the background service's.
- **Bluetooth switched off on the phone** is treated exactly like the cable
  being pulled: the Python connection is dropped so RNS reconnects rather than
  writing into a socket nobody is draining.
- **The radio needs its own battery now.** Over USB the phone was feeding it.
