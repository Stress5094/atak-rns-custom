# How it is put together

The path a CoT event takes, why the app is split between Kotlin and Python, why
ATAK is reached over a TCP server connection rather than multicast, and what
each source file does.

> **Where things live**
> [README](../README.md) · [How it works](how-it-works.md) ·
> [How it is put together](architecture.md) · [What goes on the wire](protocol.md) ·
> [Routing and airtime](routing.md) · [Running it](operations.md) ·
> [Building it](building.md) · [Settings sharing](settings-sharing.md) ·
> [Plain-language guide](mesh-explained.html) · [Range test results](../results)

## The pipeline

  ATAK (local TCP server connection) --> Kotlin --> local socket --> Chaquopy Python (RNS)
        --> RNode (USB serial) --> [air] --> RNode --> RNS --> local socket
        --> Kotlin --> local TCP server connection --> ATAK

Milestone 1's goal was to prove that plumbing by relaying raw bytes with no
translation at all. Milestone 2 replaces the raw relay with a real codec,
because raw bytes turn out not to fit (see below).

(Originally this used ATAK's local SA multicast group instead of a TCP
server connection -- see "Why TCP loopback instead of multicast" below for
why that changed.)

## Why the architecture looks like this

One thing Android won't let plain Python (even under Chaquopy) touch
directly:

1. **USB host access to the RNode.** Android's USB host API
   (`UsbManager`/`UsbDeviceConnection`) is a Java/Kotlin API tied to
   Android's permission model (the user has to grant access via a system
   dialog, or you preauthorize via a device filter + intent). There's no
   POSIX serial device node Python's `pyserial` can just open.

So Kotlin owns that and exposes it to Python as a **local loopback TCP
socket** (127.0.0.1 only, never touches the network):

- `127.0.0.1:7100` – raw byte pipe to/from the RNode's USB serial port
- `127.0.0.1:7101` – length-prefixed CoT events to/from ATAK (see below)

Python (via Chaquopy, running RNS) connects to both as a client. On the
RNode side, `pyserial` can wrap a plain TCP socket via
`serial.serial_for_url("socket://127.0.0.1:7100")`, which gives back an
object with the same `.read()`/`.write()` interface a real `Serial` object
has — so RNS's own `RNodeInterface` (or a thin subclass of it) can use it
without knowing it's not a real device file.

## Why TCP loopback instead of multicast

The original version of this listened on ATAK's local SA multicast group
(`239.2.3.1:6969`) directly, matching how ATAK instances normally discover
each other on a shared LAN. That broke as soon as Wi-Fi was turned off
during bench testing: ATAK's local multicast only exists when there's an
active local IP network for the OS to route it over (normally Wi-Fi), and
without one, neither ATAK nor this bridge had any interface to send or
receive on -- entirely independent of the RNode/LoRa link's own state,
which never needed Wi-Fi for anything.

Loopback (`127.0.0.1`) is always up regardless of Wi-Fi/cellular radio
state -- it's a virtual interface, not tied to any physical radio. So
`CotTakServerBridgeService` instead runs a plain TCP server on
`127.0.0.1:7103` that ATAK connects to as a manually-configured "server"
(Settings -> Network Preferences -> add a server/streaming connection,
host `127.0.0.1`, port `7103`, non-SSL TCP -- the exact port number
doesn't matter as long as both sides agree on it, but it must NOT be
`8087`: ATAK's own native mesh/multicast networking uses `8087` and binds
it on every interface, so the bridge holding it on loopback made ATAK's
own listener fail). ATAK's streaming server
protocol sends/expects raw, unframed CoT XML documents back-to-back over
that TCP connection, so message boundaries are found by watching for the
closing `</event>` tag (CoT events don't nest, so this is unambiguous --
the same technique other CoT-over-TCP tools like PyTAK use). The internal
protocol to Python on `127.0.0.1:7101` (4-byte length prefix + payload) is
unchanged.

**Verified against real ATAK now, including a real problem this raised:**
`</event>`-boundary parsing works correctly against real ATAK traffic
(confirmed via live bench testing). But ATAK disconnected with a "Data
Reception Timeout" error and reconnected repeatedly during quiet stretches
with no real CoT to relay -- this is documented ATAK behavior, not a bug:
streaming server connections that go idle too long (commonly ~30s) get
dropped client-side. Since this service previously only wrote to ATAK's
socket when actual RNS traffic arrived, any quiet period looked idle to
ATAK. Fixed with a periodic keepalive (`sendKeepalive()`, every 10s) that
writes a minimal, meaningless CoT event (`type="t-x-c-t"`, CoT's generic
control/ping convention) whether or not there's real traffic, so the
connection never looks idle. Not independently verified that `t-x-c-t`
is exactly what ATAK expects for this -- if the timeout still occurs with
this in place, the keepalive interval or event shape are the first things
to adjust.

## Files

This is a real Android Studio / Gradle project (project root = repo root),
not a set of snippets to merge:

- `app/src/main/java/io/github/stress5094/customrnsbridge/UsbSerialBridgeService.kt` —
  owns the USB permission flow and the RNode connection (via
  `usb-serial-for-android`), serves it on `127.0.0.1:7100`.
- `app/src/main/java/io/github/stress5094/customrnsbridge/CotTakServerBridgeService.kt`
  — runs a local TCP server on `127.0.0.1:7103` that ATAK connects to as a
  configured server connection, and republishes that traffic to Python on
  `127.0.0.1:7101` with a 4-byte length prefix per event.
- `app/src/main/java/io/github/stress5094/customrnsbridge/PythonRnsService.kt` — starts
  Chaquopy's Python runtime and calls `rns_bridge.start()`.
- `app/src/main/java/io/github/stress5094/customrnsbridge/MainActivity.kt` — live status
  screen: local node, RNode/ATAK state, airtime, undelivered messages, and the
  connected/disconnected node list. See [routing and airtime](routing.md).
- `app/src/main/python/rns_bridge.py` — brings up an `RNS.Reticulum`
  instance, wires an RNode interface to the socket-backed serial port,
  opens a broadcast `RNS.Destination` for CoT, and pumps events between it
  and the CoT socket via `cot_codec`.
- `app/src/main/python/cot_codec.py` — milestone 2. CoT <-> compact binary
  codec, zlib fallback, and fragmentation. Deliberately free of RNS/Android
  imports so it can be tested on a desktop.
- `app/src/main/python/tx_policy.py` — milestone 3. Decides which captured
  events earn airtime (significance-based throttling, echo suppression). Also
  dependency-free and desktop-testable.
- `app/src/main/python/reliable.py` — milestone 4. Acknowledged delivery for
  one-shot traffic: which messages are still owed an ack, when to retry, and
  when to tell the user it never arrived. Same dependency-free shape.
- `app/src/main/python/test_cot_codec.py`, `test_tx_policy.py`,
  `test_reliable.py` — desktop test suites for the above. Run them with:

  ```
  python3 -m unittest discover -s app/src/main/python -p 'test_*.py' -v
  ```
- `app/src/main/res/xml/device_filter.xml` — USB VID/PID filter. Currently
  `4292`/`60000` (= `0x10c4`/`0xea60`, Silicon Labs CP210x), which is verified
  correct for the Heltec LoRa32 v3 boards used here. **On a different board,
  check this first** — a mismatch means Android never grants USB permission
  and the RNode is simply never seen. Find yours with `lsusb` and convert to
  decimal; CH340 boards are `0x1a86`, FTDI `0x0403`.
- `tools/bench_rnode.py`, `tools/bench_rns_link.py` — desktop LoRa bench
  harnesses (two RNodes plugged into the dev machine). See "Desktop LoRa bench
  results".
