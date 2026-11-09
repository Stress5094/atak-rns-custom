# How it works

This is a tour of the whole app as it stands: how an ATAK event becomes a few
dozen bytes on a radio, how the app decides which radio to use and whether to
pass a frame along, how it gets one-shot messages through a lossy link, and what
each screen of the app is for.

It describes the current code. Where a number is quoted, it is the value in the
source, and the module is named so you can check it. Where something has only
been tested on a desktop, or not at all on real radios, this says so. Treat the
code as the final word if the two ever disagree.

Contents

1. [The big picture](#1-the-big-picture)
2. [Talking to ATAK](#2-talking-to-atak)
3. [Making events small: the codec](#3-making-events-small-the-codec)
4. [Deciding what is worth sending](#4-deciding-what-is-worth-sending)
5. [Security: the mesh envelope](#5-security-the-mesh-envelope)
6. [Routing](#6-routing)
7. [Getting one-shot messages through](#7-getting-one-shot-messages-through)
8. [Beacons and the peer table](#8-beacons-and-the-peer-table)
9. [The transmit queue](#9-the-transmit-queue)
10. [The FM link](#10-the-fm-link)
11. [A Meshtastic board as the radio](#11-a-meshtastic-board-as-the-radio)
12. [The app, screen by screen](#12-the-app-screen-by-screen)
13. [Settings sharing](#13-settings-sharing)
14. [Desktop tools and tests](#14-desktop-tools-and-tests)
15. [What is and is not proven](#15-what-is-and-is-not-proven)

---

## 1. The big picture

```
 ATAK ──TCP 127.0.0.1:7103──▶ CotTakServerBridgeService (Kotlin)
                                   │  length-prefixed CoT, 127.0.0.1:7101
                                   ▼
                         rns_bridge.py (Python, via Chaquopy)
          codec → transmit policy → mesh envelope → link choice → queue
                 │                    │                        │
         127.0.0.1:7100        127.0.0.1:7102            Wi-Fi (Reticulum
         byte pipe to the      audio pipe to the         AutoInterface)
         LoRa radio            FM handheld
                 │                    │
   UsbSerial / BleSerial      FmAudioBridgeService
   BridgeService (Kotlin)     (USB sound card + PTT)
                 │                    │
        RNode or Meshtastic      FM handheld radio
        board (LoRa)
```

The app is two halves joined by loopback sockets on the phone:

- **Kotlin** owns everything Android-specific: the foreground services, USB and
  Bluetooth, the sound card, the push-to-talk line, and the screens. It moves
  bytes and samples. It does not decide anything about the protocol.
- **Python** (run on the phone by Chaquopy) owns every decision: encoding,
  encryption, routing, retries, and the FM modem. Nearly all of it is written
  without Android or Reticulum imports and with the clock passed in, so the same
  files run in the desktop test suite.

Splitting it this way means Python can't tell how a radio is attached. A USB
cable and Bluetooth deliver the same bytes to port 7100, so the code above
that line is the same whichever is in use.

**Reticulum (RNS)** provides the LoRa interface (`RNodeInterface`, speaking KISS
to the RNode firmware) and the Wi-Fi interface. The app sends everything to a
single **PLAIN broadcast destination** (`atakrns.cot.broadcast`), so one
transmission reaches every node in earshot. It adds its own encryption, frame
identity, acknowledgements and relaying on top (sections 5 to 7). The reason is
cost: an RNS SINGLE destination is routed and encrypted per recipient, and costs
about 80 bytes of crypto for every recipient of every message, which a shared
LoRa channel can't afford.

The main Python module is `rns_bridge.py`. The policies it applies each live in
their own testable module, and this guide names them as it goes.

## 2. Talking to ATAK

ATAK isn't modified. The app presents itself as an ordinary **TAK server** on
the phone's own loopback address:

- In ATAK: *Settings → Network Preferences → add a server / streaming
  connection*, host `127.0.0.1`, port **7103**, TCP, no SSL.
- Port 7103 rather than the usual 8087, because ATAK's own mesh networking binds
  8087 on every interface.
- Loopback rather than ATAK's local multicast, because multicast only exists
  while there is an IP network (normally Wi-Fi). Loopback is always up, so the
  app works with every IP radio off.

`CotTakServerBridgeService.kt` finds event boundaries in ATAK's unframed XML
stream by watching for `</event>`. It hands each event to Python on port 7101
with a 4-byte length prefix. Events received from the air go the other way and
are written to ATAK as plain CoT XML.

## 3. Making events small: the codec

`cot_codec.py` (`PROTOCOL_VERSION = 15`).

An ATAK position report is roughly 600 to 900 bytes of XML. A LoRa packet carries
a few hundred at most, and every byte costs airtime on a shared channel. The
codec is what makes the link usable at all.

**The compact path.** Recognised event types are rebuilt from their meaning
instead of their text: position, callsign, team and role, marker,
GeoChat, drawn shapes (polygons, circles, ellipses), and routes. Fields go out
as packed binary, and the result is deflated against a trained dictionary
(`compact_zdict.py`, generated by `train_zdict.py`). A typical position report
leaves ATAK at about 705 bytes and goes on air at about 60.

**Sessions and deltas.** After a node's first full position report (a
*keyframe*), later reports from that node can carry only what changed. The
receiver holds the last keyframe and rebuilds the full event. The codec won't
split a keyframe across fragments. If one fragment were lost, every later delta
would be decoded against a base that never arrived.

**The fallback path.** An event the compact encoder can't express is sent as
deflated XML (`T_ZXML`). This works for anything but is much larger, so the
eligibility gate (section 6.5) keeps these off slow links.

**Fragmentation.** Anything larger than one packet is split. A message can be
at most `MAX_FRAGMENTS = 16` fragments. The receiver reassembles them, and
retries add their fragments to the partial message it already holds.

**Frame types.** The first byte's low nibble says what a frame is:

| Type | Value | What it is |
|---|---|---|
| `T_COMPACT` / `T_ZCOMPACT` | 0x0 / 0x7 | a compact event, plain or deflated |
| `T_ZXML` | 0x1 | deflated raw XML, the fallback |
| `T_FRAG` | 0x2 | one fragment of a larger message |
| `T_BEACON` | 0x3 | the periodic "I'm here" frame (section 8) |
| `T_RELIABLE` / `T_RELIABLE_TO` | 0x4 / 0x9 | a message that must be acknowledged; `_TO` names the one node that owes the ack |
| `T_ACK` | 0x5 | an acknowledgement |
| `T_PARTIAL` | 0x8 | "I have these fragments, send me the rest" |
| `T_BUNDLE` | 0xA | several small frames packed into one transmission |
| `T_COMPACT_X` / `T_ZCOMPACT_X` | 0xB / 0xC | extended compact frames |

**Bundling.** Each transmission carries about 19 bytes of Reticulum header no
matter how small the payload is. Packing several small frames into one
transmission therefore saves real airtime on every link (`airtime.py` has the
arithmetic).

## 4. Deciding what is worth sending

`tx_policy.py`.

ATAK sends position reports on its own timer whether or not anything has
changed. Forwarding all of them would fill a LoRa channel with a handful of
nodes. The policy throttles on **significance** rather than a flat rate:

- A position report is sent when the node has moved at least **25 m**, changed
  team, role or callsign, or when the **90 s** heartbeat is due.
- Sustained movement is capped to one report per **60 s**. The **first** move
  after standing still is sent immediately, so "they just started moving"
  isn't delayed. A marker already in motion can lag by up to one interval.
- A 0,0 fix isn't sent. ATAK emits one while the GPS is reacquiring, and it
  would put the marker in the ocean off Africa until the next update.
- **Echo suppression:** an event this node received from the air, and that
  ATAK then reflects back, isn't retransmitted.
- **Never throttled:** anything that isn't a periodic position report. Chat,
  markers, drawings and routes are one-shot. No later event replaces a lost one,
  so they always go, and they go through the acknowledged path (section 7).

On a real drive this policy suppressed 84% of position reports.

## 5. Security: the mesh envelope

`mesh.py`.

A PLAIN Reticulum destination is unencrypted and unauthenticated. Anyone on
the frequency could read positions and inject events that ATAK would draw as
real. So every frame the app sends is wrapped:

```
[ctrl 1][origin 3][epoch 1][seq 2][hops 1]   header, readable
[ciphertext, same length as the frame     ]
[tag 4                                    ]   authentication
```

- **AES-CCM** under a 32-byte key derived from the group **passphrase** with
  200,000 rounds of PBKDF2. Every node on a network must use the same passphrase.
  There is deliberately **no default**. A node with no passphrase set makes up
  its own and stays alone, rather than joining a network anyone could read.
- The status screen shows a **four-character fingerprint** of the key. A wrong
  passphrase and a dead radio look identical from one phone, so the fingerprint
  is how two people check they match.
- The **header is readable** so a node can throw away duplicates before
  decrypting anything. Everything except `hops` is covered by the tag, so
  `origin`, `epoch` and `seq` can't be forged. `hops` is left out because
  relays must decrement it. It is clamped on arrival, so a forged value buys
  little.
- **The nonce isn't transmitted.** It is derived from `origin`, `epoch` and
  `seq`, which are already in the header. That keeps the overhead to 12 bytes
  per frame instead of 24.
- **Replay and duplicate defence:** a cache of recently seen
  `(origin, epoch, seq)` (300 s, 512 entries). The same cache drives relay
  de-duplication.
- `passphrase_gen.py` can make a passphrase: six words from the EFF short word
  list, joined with dashes (about 62 bits).

## 6. Routing

Routing here means two decisions: **which radio** a frame goes out on, and
**whether a node passes on** a frame it heard.

### 6.1 The links

`rns_bridge.py`, `_LINK_PRIORITY = (lora, fm, wifi)`.

| Link | Hardware | Path in the app |
|---|---|---|
| **LoRa** | an RNode (USB or Bluetooth), or a Meshtastic board (section 11) | RNS `RNodeInterface`; the Meshtastic board uses its own interface |
| **FM** | an ordinary FM handheld, through a USB sound card with a push-to-talk line (e.g. a Digirig) | `fm_link.FmInterface` (section 10) |
| **Wi-Fi** | the phone's Wi-Fi | RNS `AutoInterface`, plus a small IPv4 discovery beacon |

Each link is built only if it is enabled and its hardware is present. A link
that isn't built is simply missing from the registry, and everything below
copes with any subset.

**Every built link always receives.** Only the **active link** transmits
broadcast traffic: beacons, positions and ordinary CoT. `_set_active_link()`
turns transmit on for that interface only, so a beacon isn't silently
duplicated onto every radio.

Wi-Fi is deliberately **last**. ATAK already shares SA over Wi-Fi by itself, so
the app should only fall back to Wi-Fi when no radio is working.

### 6.2 Failover and failback

`_check_auto_switch()`, run every tick of the reliability loop.

Each link has a health reading built from two signals:

- **Hard:** is the interface attached and online right now?
- **Soft** (LoRa and Wi-Fi, from `lora_health.py` / `wifi_health.py`): what share
  of recent one-shot messages ran out of retries with **no acknowledgement at
  all**. This uses a 300 s window and needs at least 3 messages before it says
  anything; above 50% the link is *degraded*. Only "nobody answered" counts.
  "The channel was too busy" doesn't, because congestion isn't a broken link
  and moving to a slower radio wouldn't help.
- FM has no acknowledgements, so its health is just "built and online". How
  well FM is *receiving* is graded separately (section 10).

The switching rule is asymmetric on purpose:

- **Failing over is instant:** if the active link is unhealthy, move to the
  next healthy link in priority order.
- **Failing back waits:** a higher-priority link must be continuously healthy
  for **30 s** (`_FAILBACK_DEBOUNCE_S`) before traffic moves back. That way one
  lucky acknowledgement after a bad patch can't cause a flap. This figure is a
  starting point, not a measured one.
- **Manual override:** the status screen can pin the active link (Force
  LoRa / FM / Wi-Fi) or return to automatic (`set_link_override`). Health keeps
  being computed underneath, so clearing the override resumes from a current
  reading. Every change is logged.

**Keeping a failed-over node visible.** When one node falls back to FM, its
beacons reach the others over FM, but theirs would still go out on LoRa, which
it can no longer hear. So when the latest sighting of a connected peer arrived
over FM, the node also sends a **reciprocal beacon on FM** (`_beacon_tick`).

### 6.3 Choosing a link per message

Broadcast traffic follows the active link. One-shot messages can be steered
individually:

- **Peers reachable only over FM.** A message addressed to a node this phone
  has heard on FM but never on LoRa goes straight to FM.
- **Last-resort escalation to FM.** When a one-shot message's acknowledged
  retries on LoRa (or Wi-Fi) run out with no acknowledgement at all,
  `_escalate_to_fm()` tries it once more over FM. It reuses the same message id,
  so a late copy is treated as the same message, not a new one. This is always
  FM, because FM's blind-repeat schedule (section 7.3) works without any
  acknowledgement.

### 6.4 Multi-hop relaying (LoRa)

`relay.py`. Every node *can* relay, but by default a frame is **not** relayed.
Each relay has to earn its airtime, because flooding would use about 14 times
what the channel can carry at 16 nodes.

1. **Hop budget.** The sender sets how far a frame may travel: beacons 0 hops,
   positions 2, one-shot messages 3 (`MAX_HOPS = 7`). A relay decrements it.
2. **Duplicates are never relayed** (the mesh cache from section 5). Hearing
   your own frame come back cancels your pending relay of it.
3. **Coverage test.** Every beacon carries an 8-byte Bloom filter of the
   neighbours its sender hears **directly**. A node relays only if it knows a
   neighbour missing from the sender's filter, in other words someone the
   sender couldn't reach. In a group standing together nobody passes this
   test, so nothing is relayed and relaying costs nothing until the group
   actually spreads out. A false positive in the filter means a missed relay,
   which the next position report repairs, so it errs on the safe side.
4. **Delay, then cancel.** A relay is scheduled, not sent. The node that heard
   the original **weakest** waits least, because it is probably farthest away
   and its copy reaches the most new ground. If anyone else's relay of the same
   frame is heard first, the pending one is cancelled.
5. **Budget.** Relaying is capped at **1.5 s of airtime per minute** per node
   (a token bucket), so stale filters or a bug can't flood the channel.

**FM never relays.** At FM's airtime cost, relaying would starve the
channel. **Wi-Fi isn't metered.**

**Reachable versus adjacent** (`peers.py`). Any authenticated frame, relayed or
not, proves a node is *reachable*. Only a frame heard first-hand proves it is
*adjacent*. Only adjacent nodes go into the coverage filter. Signal strength and
loss counts are only recorded from first-hand frames, because on a relayed frame
they describe the relay, not the originator. Hop count *does* survive relaying,
and it tells the retry logic how long to wait.

### 6.5 Per-link limits

`eligibility.py`. A frame is limited by how long it takes to **transmit**, not by
its size in bytes. Time is what collisions and channel-hogging are measured in.
The cap is 2.0 s per frame by default. On FM it is worked out from the actual
burst length, because every FM frame costs a whole burst no matter how full it
is. Frames that fell back to deflated XML are the ones a slow link should refuse.

### 6.6 Cross-link gateway (built, not switched on)

`gateway.py` describes forwarding a frame from the radio it arrived on to the
other radio, so an FM-only handheld could reach the LoRa mesh through a phone
that has both. The decision logic and its budget (5% duty) are built and
tested, but **nothing calls it from the receive path yet.** Today a node with
both radios uses them for its own traffic and for failover. It doesn't bridge
other nodes' traffic between them.

## 7. Getting one-shot messages through

### 7.1 Acknowledged delivery

`reliable.py`. Chat, markers, drawings and routes are wrapped as
`T_RELIABLE` and must be acknowledged:

- The payload is broadcast **once**, and receivers send small acks. That costs
  the payload once however many nodes are listening, which beats a separate
  Reticulum Link per peer.
- **Retries:** up to 4 attempts. The timeout is at least 2 s, grows ×1.5 per
  attempt with up to 1 s of jitter, and is stretched by the hop distance to the
  farthest addressee.
- **A direct chat** (`T_RELIABLE_TO`) names the node that owes the ack. Other
  nodes still deliver it to their ATAK but don't send an ack, so an
  addressed chat doesn't trigger a crowd of acks.
- **Selective retransmission:** a receiver missing some fragments sends
  `T_PARTIAL` saying which ones it has. The retry carries only the missing
  ones, under the same fragment id.
- **Acks are jittered** across slots (`_ACK_SLOT_S = 0.15`, up to 1 s), so a
  group's acks don't collide with each other. Up to 8 can share one bundled
  transmission.
- **Duplicates:** a retried message that already arrived is acknowledged
  again but not handed to ATAK a second time.
- **Giving up is loud.** A message that runs out of retries is logged as an
  error and shown on the status screen. A message that failed but *looks* sent is
  the specific failure this exists to prevent. Chat read receipts are dropped
  rather than sent, to save airtime. Delivery still shows on the app's status
  screen; ATAK's own "read" ticks don't appear.

### 7.2 Reassembly window

The receiver keeps partial messages long enough to outlast the sender's entire
retry schedule (`_reassembly_window_s`, the longest of the LoRa schedule and
FM's, plus 25%). If fragments collected across attempts were discarded just
before the attempt that would complete them, retrying would be wasted.

### 7.3 FM: blind repeats, cancelled by a heard-report

`fm_resend.py`. FM has no acknowledgements. A one-shot message on FM is repeated
on a schedule instead: up to 5 sends, the first repeat after 15 ± 5 s, then
about every 60 s. To stop it early, every FM beacon carries a **heard-report**:
short tags for up to 3 recent reliable messages that node received. When a
sender sees its message's tag in a peer's beacon, it cancels the remaining
repeats. Only reliable messages are tagged, so to see this work, send a chat.
Beacons alone never fill the heard-report.

## 8. Beacons and the peer table

Every node broadcasts a small **beacon** on a fixed cadence:

- Every **60 s** at present (`_BEACON_INTERVAL_S`). This is a temporary value;
  the intended normal is 90 s. While a range test runs, it drops to **30 s**,
  and the override ends when the test stops.
- Each node sends in a **fixed slot** within the interval, derived from its id,
  plus a little jitter. Two nodes whose timers drift into step would otherwise
  collide on every interval forever.
- A beacon carries a sequence number (so losses can be counted), a short node
  name and callsign, the neighbour filter (on LoRa), and on FM the heard-report.
  It can also carry a position delta. While a range test runs, it also carries
  the sender's current GPS fix.
- A beacon is also sent early when the neighbour filter changes, so a node that
  has just come into range is noticed quickly.

**Peer table** (`peers.py`). A node counts as **connected** if it has been heard
within three beacon intervals. It is forgotten after 600 s. The status screen's
node list is this table: "connected" means a frame from that phone actually
crossed the air recently, over the same path ATAK traffic uses. It isn't
guessed from configuration.

## 9. The transmit queue

`_rns_tx_loop` / `_next_outbound`.

- **Order:** acks first, then anything whose stagger delay has passed, then
  ordinary traffic.
- **Pacing:** multi-fragment messages are spaced to the radio's real bitrate
  (×1.2), which the RNode reports live, so the radio's own buffer never
  overflows.
- **Stagger:** when many nodes would transmit at once (the first report after
  starting to move, or an ack), each waits a random short delay.
- **Back-pressure:** a deep queue (16 entries) is a sign the channel is full.
  The status line also warns when the channel is busy more than 30% of the time.

## 10. The FM link

FM is the link for when there is no LoRa radio at all: ordinary FM handhelds
connected through their audio jack. The phone turns frames into audio tones,
and the far phone turns tones back into frames. It is the least mature part of
the app (section 15).

**The stack** (bottom to top; only the top module imports Reticulum):

| Module | Job |
|---|---|
| `fm_framing.py` | frame layout, length prefix, CRC, and a scrambler so frames are deterministic |
| `fm_config.py` | the operating point as one object (`LinkConfig`): tones, symbol rate, error-correcting code, copies, keying, lead-in; it refuses any burst over 11.5 s |
| `fm_modem.py` | the codec2 FSK modem with LDPC error correction (native `libcodec2`) |
| `fm_burst.py` | builds one transmit burst |
| `fm_rx.py` | receive: level measurement, then de-emphasis, then the modem, then unframing |
| `fm_carrier.py` | "is someone transmitting right now?" for listen-before-transmit |
| `fm_health.py` | grades the receive path: down / deaf / quiet / garbled / hearing, plus a level readout with which way to turn the volume knob |
| `fm_airlog.py` | one line per heard burst saying how it ended: OK, rejected, over our own transmission, cut short, or lost |
| `fm_resend.py` | blind repeats and the heard-report (section 7.3) |
| `fm_link.py` | the Reticulum interface, the MTU, and the audio plan the Android service asks for |

**Default operating point** (`fast`): 4-tone FSK at 400 symbols/s on tones
1000/1400/1800/2200 Hz, the `H_1024_2048_4f` code, one copy, push-to-talk keyed
over the sound card's CM108 GPIO pin with a 0.10 s lead-in. That makes a
**2.95 s burst** carrying up to 125 bytes (113 to the codec). Two other
measured tone placements (`narrow`, `wide`) suit radios whose audio path
doesn't pass the default band.

**Keying and half-duplex.** `FmAudioBridgeService.kt` keys the radio, waits the
measured 55 ms key-up delay, then plays the burst. A cable without a PTT pin
falls back to the radio's voice-operated switch (VOX), and the status screen
says so. The phone never decodes its own transmission. Anything sent while
this phone is transmitting is lost to it, and that is a large share of FM's
losses.

**Listen-before-transmit.** Before keying, the node checks the carrier detector.
In 4-tone FSK exactly one tone is present per symbol; in noise the energy is
spread across all four. If the channel is busy it waits, then backs off a random
0 to 2 s and listens again.

**Every timing number comes from Python.** The sample rate, keying, lead-in,
burst length and listen bound all come from one call (`fm_link.audio_plan()`).
That way Kotlin can't drift out of step with the waveform, which would fail
without any error.

**Custom settings** (`fm_custom.py`, *FM → Custom* in the app). Tone count,
placement, rate and code can be set by hand. The settings a whole net must agree
on travel as a short **net code** such as `4T-1000/400-400Bd-H1024.2048`, which
can be typed or shared. Per-phone settings (copies, keying, lead-in, filter)
stay local.

**Measure my radios** (`fm_tune.py`). One phone plays a sweep of 33 tones from
300 to 3500 Hz, and the other records how loudly each arrives. The app then
recommends tone placements and rates, ranking measured-safe ones first.

## 11. A Meshtastic board as the radio

`mt_link.py`, `mt_port.py`, `mt_config.py`, `MeshtasticBleFraming.kt`.

A board running Meshtastic firmware can stand in for an RNode as the LoRa radio.
The app passes its own sealed frames to the board inside a Meshtastic
"private app" packet, and the board puts them on the air. This is **not**
interoperability with other people's Meshtastic traffic. The two apps share a
radio, not a protocol.

- It talks to the board with the official `meshtastic` Python package, over the
  same loopback pipe used for an RNode (USB or Bluetooth). Over Bluetooth,
  `MeshtasticBleFraming.kt` rebuilds the serial stream from Bluetooth
  messages so Python can't tell the difference.
- Like FM, it skips Reticulum's 19-byte header, because the board is a single
  broadcast channel.
- **The board's own settings screen** (`MeshBoardDialog.kt`) sets region,
  modulation preset, frequency slot, transmit power, whether the board repeats
  other nodes' packets, and channel 0's name and key. Nothing is applied
  without being shown first. The key itself is never displayed. "Applied" means
  the board was read back and confirmed it, not just that the settings were sent.

## 12. The app, screen by screen

All screens only **show** what Python decides. Status is pulled from
`rns_bridge.get_status()`. The bridge runs as a foreground service and keeps
working whether or not a screen is open.

- **Status.** This node's name and key fingerprint, whether the radio and ATAK
  are connected, the active link and override buttons, channel busy-ness, FM
  receive health with level, and any one-shot message that failed.
- **Nodes.** The peer table: who is connected, how many hops away, signal
  strength (SNR) and loss on direct links.
- **Mesh.** A diagram of who is heard on which radio (`mesh_view.py`). One line
  per peer per radio, direct or "via mesh", graded good / weak / gone. The
  grading thresholds are first guesses, not measured.
- **Coverage.** A predicted coverage map for the chosen radio from where you
  stand (`coverage.py`, `overture_tiles.py`, `CoverageMapView.kt`). It uses
  building footprints and heights from Overture Maps (falling back to
  OpenStreetMap), SRTM terrain, and the radio's link budget. The model is
  line-of-sight with ground reflection up to the first obstruction, then
  diffraction over it. Other nodes' footprints can be drawn from their reported
  positions. This is a **prediction**; it hasn't been checked against
  measured range.
- **Range test.** Records one CSV row per beacon heard: both ends' positions,
  the distance, signal strength (on LoRa), and the age of each GPS fix
  (`rangetest.py`). Rows whose fixes are too old are marked unusable rather than
  trusted, because at driving speed an old fix is as wrong as the distance
  being measured. The CSV is exported to Downloads.
- **Radio** (RNode). Preset (eight, named the way Meshtastic names them; the
  default is **Short Slow**), frequency and transmit power. There is also a
  passive **band survey** that ranks channels by their worst noise reading
  (`spectrum.py`), switching the radio's Bluetooth on over the cable, pairing,
  a **blink-to-identify** command, and the radio's own small screen
  (`display.py` draws a 64×64 status page on it).
- **FM.** Backup on or off, tone profile, the custom editor, and *Measure my
  radios*.
- **Log.** `LogRecorder.kt` streams the app's own log to a file. The phone's
  built-in log buffer holds only minutes, and a field test usually happens
  away from a computer.

## 13. Settings sharing

`settings_share.py`, `settings_link.py`, `settings_net.py`, `settings_server.py`,
`settings_web.py`. The full design is in [settings-sharing.md](settings-sharing.md).

One **host** pushes radio settings to a group: LoRa preset, frequency and
power, the FM profile or net code, coverage settings, and optionally the
passphrase. Each phone approves the change.

- A bundle is **signed** (Ed25519), and its revision number only ever goes
  up. It travels as one string (`RTAKS1.…`), so it works as a QR code or over
  the network.
- A phone applies a bundle only with its own **PIN** (4 to 12 digits, 5 tries,
  then a 60 s lock), or automatically if the operator has chosen to trust that
  host.
- A passphrase is **sealed to one phone's key**, so it never travels in a form
  another device could read.
- Changes can be scheduled for a common moment (default 2 minutes ahead), so a
  whole group switches together and nobody is left on old settings. Each
  phone keeps an undo.
- Sharing runs over the local network or a server, **never over the radio**,
  because settings traffic must not cost airtime. `settings_server.py` runs the
  same host code as a Linux service, with an admin command line and a
  local-only web page.

## 14. Desktop tools and tests

- **App test suite:** `./app/src/main/python/run_tests.sh`. A skipped test
  counts as a failure. The phone's Python is 3.9 (Chaquopy), so the modules
  stay compatible with it.
- **Tools test suite:** `python3 -m unittest discover -s tools -p 'test_*.py'`.
- **CI** (`.github/workflows/tests.yml`) builds the debug APK and runs both
  suites on a clean machine. That makes it the real test of
  [building.md](building.md).
- **`tools/`** holds the bench instruments used to measure everything here: the
  FM bench (`fm_bench.py`, with the over-the-air flags it insists on), a
  channel model built from measured responses (`fm_channel.py`), a passband
  sweep, a desktop mesh node (`mesh_node.py`), Meshtastic sniffers and latency
  tools, and a pre-publication privacy check (`privacy_check.sh`).

## 15. What is and is not proven

**Measured on real radios:** the compact codec, the movement policy (84% of
reports suppressed on a drive), acknowledged delivery with retries and honest
failure reporting, the encrypted mesh, relaying across an intermediate node,
Bluetooth to the RNode, two phones forming a mesh over FM with the heard-report
cancelling repeats in both directions, Meshtastic boards as the radio, and FM
range tests on foot and by car ([results](../results)).

**Built and desktop-tested, not yet proven on radios:** the cross-link gateway
(not wired in, section 6.6), the newest frame-size savings, and FM
settings sharing between two radios.

**Known limits:**

- FM delivery isn't yet a stable number. On the 2026-11-07 walk, 69 to 77% of
  bursts were delivered depending on direction, out to 298 m. Losses were split
  between receive level and collisions. On the 2026-11-08 drives, one phone in a
  car decoded the parked one at **1384 m**. The other direction reached 846 m,
  and both directions worked together out to about 456 m. Which direction was
  stronger flipped between drives, and a receive volume set too high is the
  likely cause (inferred). See [results](../results).
- Two FM radios on one desk have shown a direction asymmetry that follows desk
  position, not the radio. It hasn't been explained.
- The failback delay, the mesh-diagram thresholds and the FM back-off are first
  guesses, not measurements.
- The beacon interval is temporarily 60 s; the intended value is 90 s.

The detailed design history behind each piece is in [routing.md](routing.md)
(airtime, acknowledgements, relaying), [protocol.md](protocol.md) (wire format
and codec versions), [architecture.md](architecture.md) and
[operations.md](operations.md) (running it on phones). Those files are written
as milestone records, so where they disagree with this guide or the code, the
code is current.
