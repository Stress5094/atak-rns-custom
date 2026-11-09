# What goes on the wire

Frame formats, the compact encodings for every CoT type, fragmentation, the mesh envelope, and every PROTOCOL_VERSION bump and what forced it.

> **Where things live**
> [README](../README.md) · [How it works](how-it-works.md) ·
> [How it is put together](architecture.md) · [What goes on the wire](protocol.md) ·
> [Routing and airtime](routing.md) · [Running it](operations.md) ·
> [Building it](building.md) · [Settings sharing](settings-sharing.md) ·
> [Plain-language guide](mesh-explained.html) · [Range test results](../results)

## Milestone 2: CoT codec, compression and fragmentation

### Why this is a correctness fix, not an optimization

Milestone 1 relayed raw CoT bytes. That cannot work for real ATAK traffic:

- `RNS.Packet.PLAIN_MDU` is **464 bytes** (`Reticulum.MTU` 500 −
  `HEADER_MAXSIZE` 35 − `IFAC_MIN_SIZE` 1), and `Packet.pack()` **raises
  `IOError`** above the MTU.
- A realistic ATAK position report is **600–900 bytes** of XML once `takv`,
  `contact`, `__group`, `status`, `track` and `precisionlocation` detail is
  included.

So `RNS.Packet(_destination, payload).send()` was raising on essentially every
real position report. It was caught by a broad `except Exception` and logged,
so the bridge *looked* alive while silently dropping all meaningful traffic.

There is no built-in way out. `Resource`, `Channel` and `Buffer` all provide
multi-packet transfer and bz2 compression, but all require a `Link`, and
`Link.py:231` refuses any destination that is not `SINGLE` — and a `PLAIN`
destination cannot hold an identity, so it can never be `SINGLE`. Compression
and fragmentation have to be built at the app layer.

Measured on the test suite's PLI fixture:

| encoding | bytes | airtime @3125 bps (SF8/125 kHz/CR5) |
|---|---|---|
| raw XML (milestone 1) | 617 | 1.63 s — and unsendable, raises `IOError` |
| zlib, no dictionary | 411 | 1.10 s |
| zlib + preset dictionary | 197 | 0.55 s |
| **compact field codec** | **61** | **0.20 s** |

### Wire format

Every RNS payload starts with one control byte: high nibble = protocol
version, low nibble = frame type. `0x10` compact, `0x11` zlib'd XML, `0x12`
fragment. An unknown version *or* type is dropped with a log line rather than
guessed at, so a mixed-firmware bench (one phone updated, one not) fails
loudly instead of corrupting the map. Versioning costs zero extra bytes.

**Compact frames** (`0x10`) carry a 16-bit presence bitmap, then a mandatory
core (type, time, stale delta, lat/lon, uid, how) and whichever optional
fields are present (start delta, hae, ce, le, callsign, team/role, battery,
track, precisionlocation, `<uid Droid>`, takv). lat/lon are `int32` scaled
1e7 (1.1 cm); timestamps are `uint32` unix seconds with start/stale as
signed varint deltas. Repeated strings — CoT type, `how`, team colour, role,
altsrc, geopointsrc, platform — are looked up in small tables, with an escape
that carries any unrecognized value verbatim.

Note that `stale` is mandatory while `start` is optional. If `stale` were
flagged, a clear flag would have to mean `stale == time`, i.e. already
expired, and the receiving ATAK would drop the track the instant it arrived.
`start == time` is both safe and the common case, so that is what gets flagged.

**Tables are append-only.** A decoder rejects an index past the end of its own
table, so adding entries is safe, but reordering or removing them silently
remaps every value. A wrong or missing table entry only costs bytes (via the
escape path), never correctness.

**Fallback frames** (`0x11`) are the raw XML deflated with a fixed preset
dictionary of common CoT boilerplate. This is worth ~2.3x, and it is what
takes a typical GeoChat message from 468 bytes — over the 464 budget, so it
would have to be split into two unacknowledged packets — down to ~270, i.e.
one packet. `COT_ZDICT` is part of the wire format: **changing it requires
bumping `PROTOCOL_VERSION`.** zlib embeds the dictionary's Adler-32 in the
stream header, so a mismatch between builds fails loudly at `decompress()`
rather than silently corrupting.

**Fragment frames** (`0x12`) add `msg_id`/`index`/`count` and carry a slice of
the complete layer-A frame, control byte included — so reassembly yields a
`0x10`/`0x11` frame that feeds straight back into `decode()`.

### Which path an event takes

The compact path is used only when the codec fully understands the event:
root tag `event`, parseable uid/type/time, exactly one `<point>` with
parseable lat/lon, no unknown `<event>` attributes, and every `<detail>` child
either known or on the drop allowlist.

**Any unknown `<detail>` child sends the whole event down the lossless
fallback path** rather than being dropped. This is deliberate: losing `<link>`
on a route, `<__chat>` on a message or `<emergency>` on a 911 event would turn
a meaningful event into a confidently wrong one, and silent semantic
corruption on a tactical link is far worse than a larger packet.

Two exceptions are dropped rather than forcing fallback: `<_flow-tags_>`
(TAK-server provenance ATAK ignores, which *grows on every hop*) and
`<archive/>`. Consequence of the latter: markers arriving over the bridge are
not auto-archived on the receiving device. `<status>` is a third, narrower
exception — only `@battery` is carried, since forcing fallback on
`readiness="true"` would defeat the compact path on nearly every PLI.

### What the compact path loses

Compact round-trips are **not** byte-identical. Deliberately discarded:
sub-second timestamp precision (`.123Z` arrives as `.000Z`); `hae`/`ce`/`le`
quantized to 1 m, with `ce`/`le` above 100 km treated as unknown; course to
0.01°, speed to 0.01 m/s; `<takv>` entirely (~100 bytes of cosmetic device
strings — `INCLUDE_TAKV` flips this without a version bump); `contact/@endpoint`
(replaced with `*:-1:stcp`, since the real value is an address on the
*sender's* LAN that the receiving phone cannot reach by construction) and
`@phone`; `<status>` attributes other than `@battery`; attribute order and
whitespace. The fallback path loses only the XML declaration and surrounding
whitespace.

### Packet loss is unrecoverable at this layer

**Superseded by milestone 4 for one-shot traffic — see "Milestone 4:
acknowledged delivery" below. Kept because it describes what the codec layer
does and does not do on its own.**

RNS applies no acknowledgement of any kind on a `PLAIN` broadcast destination,
so at this layer a lost packet means the message is gone — and a lost
*fragment* means the rest is gone too, after the 30-second reassembly timeout.

- **Position updates self-heal.** ATAK re-emits them every few seconds, and
  they are single-packet compact frames that never fragment anyway.
- **Chat, drawings and routes do not self-heal.** There is no later event to
  supersede a dropped one.

One correction to how this was originally written, found while planning
milestone 4: the risk was described as *fragment* loss, which understates
where it actually lives. A GeoChat compresses to ~284 bytes against a 464-byte
budget, so it fits in **one** packet and never fragments. Ordinary
single-packet loss was always the dominant exposure; fragmentation makes it
worse rather than causing it.

Relatedly, a full replay of every fragment of a message will re-emit that
event (interleaved duplicates *are* suppressed). Harmless for CoT, which is
idempotent state keyed on uid, and milestone 4's receiver suppresses it
outright for acknowledged messages.

### Two traps worth knowing about

**The on-device Python is 3.8** (Chaquopy ships `libpython3.8.so`), while any
modern desktop is 3.11+. `datetime.fromisoformat` cannot parse ATAK's
`...31.123Z` on 3.8 but silently succeeds on 3.11+, so a timestamp parser
built on it passes every desktop test and then fails on the phone.
`cot_codec` uses an explicit regex plus `calendar.timegm` instead, and a test
asserts the module source never regains a `fromisoformat(` call. The suite is
run against a real 3.8 via `uv python install 3.8`.

**RNS applies no transmit pacing on this interface.** `RNodeInterface` only
clears `interface_ready` when `flow_control` is set, and it defaults to
`False` — so `queue()`/`process_queue()` are dead code and `process_outgoing`
writes every packet to the USB pipe as fast as Python can loop. `rns_bridge.py`
therefore drains a queue on its own `_rns_tx_loop` thread.

The original justification for that was fear of overrunning the RNode's
firmware TX buffer. **Bench testing showed that fear was unfounded** (see
"Desktop LoRa bench results") — the firmware handles an instant 8-frame burst
fine. The pacing is retained for backpressure control instead, and the thread
is worth having regardless, since it keeps a multi-fragment send from stalling
reads from ATAK.

Note the trap if you try the obvious alternative: adding `"flow_control":
"true"` to the config dict in `_build_rnode_interface()` raises
`AttributeError`, because RNS calls `c.as_bool()` on it and we pass a plain
dict — it works today only because the key is absent. Setting
`iface.flow_control = True` after construction is the correct form, but it is
a *separate experiment* to run deliberately: if the firmware does not actually
emit `KISS.CMD_READY`, `interface_ready` latches `False` after the first
packet and all transmission stops permanently and silently.

### Still needs hardware verification

The desktop suite proves the codec is self-consistent. It cannot prove any of
this, in rough priority order:

1. ~~That real ATAK renders the decoder's output.~~ **RESOLVED 2026-07-31 —
   markers and callsigns render correctly on the receiving map.** So the
   decoder's output is accepted and understood by real ATAK despite `.000Z`
   timestamps, an absent `<takv>`, the substituted `contact/@endpoint` and
   1-metre-quantized `hae` — every one of which was a guess when written.

   Worth a deliberate spot-check if it has not had one: **team colour and
   role**. These come from nibble tables built from educated guesses, and a
   wrong entry is silent — the marker still appears and the callsign is still
   right, so nothing looks broken unless you compare the colour against the
   sending device.
2. **Whether the string tables match this ATAK build. PARTLY REOPENED
   2026-08-04.** This previously read "zero `fallback:` lines on the bench run
   means `KNOWN_DETAIL` matches what this build emits", which was too broad a
   conclusion from too narrow a run: that bench exercised **position reports**,
   and positions do match. The milestone 6 run put *markers* on the air for the
   first time and every one of them fell back, costing ~3.2x the airtime a
   compact marker would (see "Markers cost 3x more airtime than they need to").
   So `KNOWN_DETAIL` matches this build for `a-f-G-U-C` self-SA and not for
   placed markers. Still worth grepping logcat for `[cot-codec] .*_escape`
   to find table entries that round-trip correctly but cost extra bytes —
   that part is a size optimization, not a correctness question.
3. ~~Whether escaped bytes survive the USB/KISS path.~~ **RESOLVED, on both
   paths.** On the desktop LoRa bench (see "Desktop LoRa bench results"
   below), all-`0xC0` and all-`0xDB` payloads — the maximum-escaping cases,
   which double in size on the wire — crossed byte-exact, as did real
   compressed codec output. That exercised the *Linux* serial path.

   The Android path is closed by the chat result in milestone 4 item 1:
   **direct callsign-to-callsign GeoChat crosses between the phones with no
   Wi-Fi and no cellular.** Chat takes the zlib fallback path, so those are
   genuine high-entropy compressed frames traversing the USB driver, KISS
   framing and LoRa on real devices — which position reports, being
   low-entropy compact-path output, never demonstrated.
4. ~~Whether a 464-byte RNS payload transmits over this radio at all.~~
   **RESOLVED — a full 483-byte RNS packet crosses byte-exact.** RNode
   firmware does split oversize frames across on-air transmissions, so
   `RNS_MAX_PAYLOAD = 464` stands and needs no lowering.
5. ~~Whether `_PACING_FACTOR` is right, or even necessary.~~ **Measured: not
   necessary for correctness on this firmware.** Kept for backpressure
   control — see the bench results.
6. ~~Whether ATAK re-broadcasts received CoT back into its server
   connection.~~ **RESOLVED — it does not.** On the 2026-07-31 run,
   `Re-injected CoT event to ATAK, 903 bytes` is never followed by a
   `Captured CoT event`, and `tx policy: … echo=0` holds throughout. Worth
   noting the test was strong rather than lucky: the phones were on Wi-Fi with
   ATAK's mesh outputs live, so ATAK had every opportunity to reflect
   multicast-received traffic into the server connection and did not.
7. **The real loss rate** — `Reassembler.stats()["timeouts"]` versus
   `["completed"]`, and now also the retry and undelivered counts on the
   `reliable:` status line. Milestone 4 makes chat survive loss; these numbers
   are what say how much loss it is actually surviving, which is the input to
   any range decision.

### Desktop LoRa bench results

Reproduce with `tools/bench_rnode.py` (needs two RNodes on the dev machine and
RNS importable — see the docstring for the serial-permissions note):

```
sg uucp -c "~/.local/share/pipx/venvs/rns/bin/python tools/bench_rnode.py"
```

Both RNodes attached directly to the development laptop (`/dev/ttyUSB0` and
`/dev/ttyUSB1`), driving RNS's real `RNodeInterface` over a real air gap at
915 MHz / 125 kHz / SF8 / CR5, 2 dBm (low, since the radios are inches apart
and high power desensitizes the receiver at that range). Hardware: **Heltec
LoRa32 v3, firmware 1.86, SX1262**, measured bitrate 3125 bps.

**Important scope caveat.** On the laptop, Python talks to the RNode through
pyserial and the Linux kernel's CDC driver. The entire Kotlin bridge — socket
7100, `UsbSerialBridgeService`, `BulkOnlyCdcDriver` — is bypassed. So this
clears the codec, the KISS layer and the radio, but says nothing about the
Android USB path. That is useful precisely because it isolates variables: if
something works here and fails on the phones, the Android driver is the
remaining suspect.

Every payload crossed **byte-exact**:

| case | bytes | result |
|---|---|---|
| compact PLI (real codec output) | 61 | OK |
| zlib GeoChat (real codec output) | 270 | OK |
| ASCII | 201 | OK |
| high-entropy | 200 / 400 | OK |
| full MDU payload | 464 | OK |
| max RNS packet | 483 | OK |
| escape-heavy, all `0xC0` | 300 | OK |
| escape-heavy, all `0xDB` | 300 | OK |

The two escape-heavy cases are the worst case for KISS: every single byte
needs substituting, so 300 bytes becomes 600 on the wire. They survive.

**The pacing hypothesis was wrong, and that is worth recording.** The concern
was that `flow_control=False` plus a fragment burst would overrun the RNode's
firmware TX buffer, since RNS applies no backpressure. Measured: **8 full-MTU
frames submitted in 0.26 s — demanding 9.9 s of airtime — all arrived
byte-exact and in order.** The firmware buffers and serializes correctly.
Paced and unpaced runs were identical (10/10 fragments, 5/5 messages, both
modes).

`_PACING_FACTOR` is kept anyway, on better grounds than the original ones: it
decides *where* a backlog accumulates. Unpaced, `_rns_tx_loop` shovels the
whole queue into an opaque firmware buffer and any overflow is dropped on the
firmware's terms; paced, a backlog stays in `_tx_queue` where the drop-oldest
policy applies, and dropping the stalest position report is a far better loss
than an arbitrary one. The cost is modest — the sleep overlaps the radio's
actual transmission, adding roughly 20% idle time between fragments.

What this bench still does not cover: everything ATAK-dependent (items 1, 2
and 6 above), the Android USB path, and loss rate at realistic distance —
these radios were inches apart, so the 100% delivery figure says nothing about
range.

### End-to-end bench result (2026-07-31) — the milestone's actual goal

Two phones, two RNodes, real ATAK on both, real LoRa air gap. One event traced
the whole way:

```
OnePlus 10:28:23.322  Captured CoT event, 705 bytes
OnePlus 10:28:23.323  705 bytes CoT -> 1 packet(s), 63 bytes on air
OnePlus 10:28:23.326  transmitted 63 bytes (interface txb 187 -> 269)
Pixel   10:28:23.832  received 63 bytes over RNS -> 530 bytes of CoT for ATAK
Pixel   10:28:23.834  Re-injected CoT event to ATAK, 530 bytes
```

**Half a second ATAK-to-ATAK**, and the same in reverse simultaneously.

| | milestone 1 | milestone 2 measured |
|---|---|---|
| real ATAK PLI | 653–705 B | 653–705 B |
| on air | **unsendable — `IOError`** | **58–63 B** |
| ratio | — | **~11x** |
| airtime @3125 bps | 1.8 s (if it could send) | **0.20 s** |

Two things worth noting beyond the headline:

- **Zero `[cot-codec] fallback:` lines.** The compact path engaged on 100% of
  real ATAK traffic, which also means no unexpected `<detail>` children were
  hiding behind the `@access` problem — the codec's `KNOWN_DETAIL` set matches
  what this ATAK build actually emits.
- The decoded XML is smaller than the original (530 B from 705 B) because
  `<takv>`, `contact/@endpoint` and `@phone` are deliberately dropped. See
  "What the compact path loses".

This exercised every layer for the first time together: ATAK capture,
`</event>` framing, the compact codec, RNS, KISS escaping, the Android USB
driver, the radio, and the Kotlin re-injection path including `writeToAtak`.

### Bugs found on the first real-ATAK bench run (2026-07-31)

Both found from logcat within minutes of installing, and neither was findable
any other way:

1. **Every position report was taking the zlib fallback path.** Real ATAK
   stamps an `@access` attribute on every event, which the codec did not know,
   and the unknown-attribute check disqualifies an event before it ever looks
   at `<detail>`. Result: a 741-byte PLI went out as 268 bytes of zlib instead
   of ~62 bytes of compact — **4x the airtime on every single position
   update**, with the compact path never once engaging in the field.

   Fixed by carrying `@access` as a table-coded optional field rather than
   dropping it: in some deployments it holds a real handling marking, and
   silently discarding that is harmless on a bench and not harmless
   elsewhere. `"Undefined"` costs one byte.

   Note the flag (`F_ACCESS`) is deliberately the **highest** bit and is read
   last, which is why this needed no `PROTOCOL_VERSION` bump: `decode_compact`
   reads in flag order and never checks for trailing bytes, so an older build
   ignores both the flag and the bytes it leaves unread. Any future field must
   preserve that property — append a higher bit, never insert a lower one — or
   bump the version. There is a test asserting `F_ACCESS` stays the maximum.

2. **One broken pipe to ATAK permanently killed inbound CoT.**
   `pumpPythonToAtak`'s `try/catch` sat outside its `while` loop, so an
   `IOException` from writing to ATAK's socket exited the whole pump thread.
   Nothing restarted it: the accept loop only spawns a new pump when a new
   *Python* client connects, and Python's socket was still perfectly healthy.

   This is not a rare path. ATAK drops and re-establishes the streaming
   connection routinely on its own idle timeout, and the bench log caught it
   doing exactly that (`Broken pipe` from `sendKeepalive()`, ATAK reconnecting
   nine seconds later). So on any session long enough to see one reconnect,
   the bridge would keep transmitting perfectly while silently never
   delivering anything again — the asymmetry that makes a bridge look
   half-alive and is miserable to diagnose from the outside.

   Fixed by moving the ATAK write into `writeToAtak()`, which catches
   per-event, retires the dead socket and lets the accept loop install the
   replacement. Only a read failure from Python ends the pump now. This is the
   exact mirror of the `_cot_listen_loop` ECONNRESET fix on the Python side —
   the same lesson, one layer over.

   `atakSocket`/`pythonSocket` are now `@Volatile`, since they are written by
   the accept loop and read by both the pump and the scheduled keepalive.

### The link beacon

`rns_bridge.py` broadcasts a small probe every 60 s (jittered — see milestone
5) on the **same** destination real CoT uses, and logs any it hears:

```
[cot-bridge] LINK OK: heard peer 9e0e95 seq=1 frame=12B rssi=-28 dBm snr=14.5 dB
[cot-bridge] status: rnode online=True txb=31 rxb=31 | peers: 9e0e95 (9s ago) | beacon sizes heard: [12]
```

Riding the same destination is the point: a beacon arriving proves the entire
path CoT depends on — radio parameters matching, range, KISS framing, the
Android USB path, and the destination hash agreeing between devices. And it
works with **ATAK not running at all**, which removes the worst property of the
old workflow, where "no link" and "no traffic" looked identical and you had to
wait minutes for a position report to tell them apart.

Beacons cycle through a **size ladder** (12, 64, 128, 256, 384, 464 bytes;
31–483 on the wire with the RNS header). This exists because a link proven at
31 bytes is not proven at 464 — see the bench result below. Padding is
high-entropy, so it exercises KISS escaping too. `beacon sizes heard` in the
status line is the readout: if it plateaus at the small end while larger
beacons keep being sent, the link has a size ceiling.

**Verified on hardware 2026-07-31, and this retired the last of the
transport-layer unknowns.** Both phones reported the complete ladder:

```
beacon sizes heard: [12, 64, 128, 256, 384, 464]
```

Bidirectional, consecutive sequence numbers with no gaps at any size, rssi
-28/-30 dBm at close range. That covers RNS PLAIN broadcast delivery, the
destination hashes agreeing, KISS escaping of high-entropy data, and the
Android USB path at the full MDU — none of which the desktop LoRa bench could
test, since it bypasses Android entirely.

It also confirms the `BulkOnlyCdcDriver` partial-write fix: 464-byte beacons
are precisely the regime that was silently failing beforehand, when 264-byte
CoT packets never arrived while everything looked healthy.

**What beacons still do not cover:** `_handle_beacon()` returns before touching
the CoT socket, so the Kotlin delivery path (`pumpPythonToAtak` /
`writeToAtak`) is deliberately not exercised by them. Nor is the codec against
real ATAK events, nor whether ATAK accepts the decoder's output. Those need
ATAK running — see the hardware verification list.

### Deliberately not in this milestone

No transmit-side filtering of any kind: no per-uid rate limiting, no
echo/loop suppression, no dropping of the bridge's own keepalives. No uid
hash caching (every frame carries the full uid string, ~24 of a PLI's 61
bytes, so that broadcast packet loss can never orphan anything). No multi-hop
routing.

(Filtering arrived in milestone 3, acknowledgement in milestone 4. Hash
caching and multi-hop are still open.)

### Wire format

```
[ctrl 1][origin 3][epoch 1][seq 2][hops 1]   header, sent in clear
[ ciphertext, same length as the plaintext frame          ]
[ tag 8                                                   ]
```

16 bytes on a 61-byte position report, which is 77 bytes on the air and
**exactly the same time-on-air as before** — both land in the same LoRa symbol
count, so step 1 is free in practice.

The mesh envelope is the *outermost* layer and wraps every existing frame type
unchanged: compact, zlib'd XML, fragments, beacons, reliable wrappers, acks.
The whole frame vocabulary keeps working and all of it gets encrypted.

The header is readable **on purpose** — duplicate suppression has to work
before anything is decrypted, or a relay would have to trial-decrypt everything
it hears — but every field except `hops` is covered by the authentication tag.
`hops` is excluded because a relay must be able to decrement it, exactly like
an IP TTL, which makes it the one field an attacker can rewrite; it sits last
so the authenticated part is a contiguous prefix, and it is clamped on the way
in, so a forged value buys nothing the duplicate cache does not already bound.

**The nonce is derived, never transmitted:** `origin || epoch || seq || zeros`.
That is what keeps crypto overhead at 8 bytes rather than 20, and it means
nonce uniqueness reduces to "can one node ever reuse an (epoch, seq) pair".
Sequence numbers are monotonic within a process; `epoch` is persisted and
incremented at every start, and a `seq` wrap bumps it too. Reusing a nonce
under CCM leaks the XOR of two plaintexts and opens the door to forgery, so
this is the one thing in `mesh.py` that must not be changed casually.

`AESCCM` with an 8-byte tag, from the `cryptography` package RNS already pulls
in (native OpenSSL on arm64-v8a, not the pure-Python path). Not RNS's own
`Token`, which spends 48 bytes plus PKCS7 padding — 79% overhead on a position
report. Not AES-GCM either, because pyca will not truncate a GCM tag. An
8-byte tag is what 802.15.4 and LoRaWAN use, for the same reason.

### Trust model, stated plainly

One key, shared by the whole group, stretched from a passphrase with PBKDF2.
Holding it **is** membership: a node that cannot decrypt is not a peer.

That authenticates the **group**, not the **member**. Any node holding the key
can put another node's id in `origin` and be believed. This is the same model
as WPA2-PSK and Meshtastic's channels, and it is the right default for a squad
whose radios are all accounted for — but it is a choice, not an oversight.
Per-node Ed25519 signatures would close it at 64 bytes a frame, more than
doubling a position report; there is room in the frame-type space to add that
later without disturbing this format.

Two further consequences: a lost radio compromises the group until the
passphrase changes, and the readable header leaks who transmits and how often —
traffic analysis that IPsec and WireGuard also accept.

**There is deliberately no default passphrase.** A node with nothing configured
generates its own, logs it loudly, and is then *isolated* until that passphrase
reaches the other radios. Isolated and obvious beats joined and exposed, which
is exactly what a shipped default key would produce.

### Setting the passphrase

On the status screen, under the local node: **Network key 3f9a · 42 heard**,
with a **Passphrase** button beside it. Setting one takes effect immediately —
the mesh is rebuilt in place and the peer list cleared, because peers heard
under the old key are on a different network now and leaving them listed would
show unreachable nodes as perfectly healthy. No restart, so fixing a typo takes
five seconds instead of a support call.

**The dialog shows the passphrase rather than masking it.** Masking is the
reflex and it is wrong here: this is a *group* secret whose entire purpose is
to be copied onto the other radios, so a field you cannot read is a field you
cannot provision from. It is already stored in plaintext in app-private
storage, so hiding it on screen protects nothing.

**The four-character key fingerprint is the point of the whole section.** A
wrong passphrase and a dead radio produce exactly the same symptom — an empty
node list — so comparing fingerprints across two devices is the only quick way
to tell them apart. It is a hash of the *derived* key, not the passphrase, so
it also catches a mismatch in derivation itself. Sixteen bits is far too short
to attack a key with and quite enough to spot a typo, which is the entire job.

The line also reports frames heard but rejected, and turns red when frames are
being rejected while none are getting through — the signature of a mismatch.

Bench provisioning is therefore: install on both, open the dialog on one and
type a passphrase, type the same on the other, confirm both show the same four
characters. `adb` and `run-as` are no longer needed for this.

### What this also fixed

The envelope carries an authenticated sender, so `_reject_impostor()` now
requires the node id inside a beacon, an ack or a reliable wrapper to agree
with it. Those ids were in the frame bodies from back when nothing was
authenticated and there was nowhere else to put them. This does not make ids
unforgeable — a key holder can lie in both places — but it closes the case
where a frame's body contradicts its envelope, and it keeps outsiders out
entirely.

### Markers cost 3x more airtime than they need to

The one real finding. Every marker logged `[cot-codec] fallback` and went out
on the zlib path at ~310 B rather than the compact path:

| | as XML | on air | ratio |
|---|---|---|---|
| marker `a-u-G` | 860 B | 329 B | 2.6x |
| marker `a-h-G` | 861 B | 327 B | 2.6x |
| position report | 706 B | 79 B | 8.9x |

Markers are position-shaped events — one `<point>`, simple detail — so they
*should* be compact-encodable. A compact marker would be ~77 B on air against
329 B, which is **3.2x less airtime on the traffic milestone 6 exists to carry
across hops**. (GeoChat falling back is expected and unavoidable: its entire
payload lives in `<detail>` children the compact codec has no slots for.)

The bench could not say *which* child caused it, because the log line only said
"unknown `<detail>` child". That is now fixed — it names the tag, and
distinguishes an unknown tag from a repeated one, which need different fixes: a
new `KNOWN_DETAIL` entry versus a wire format that has one slot per tag.

**But the answer came from the ATAK source rather than another bench run.**
`PlacePointTool.createMarker` sets two pieces of metadata unconditionally on
every placed marker, and each has a detail handler that always fires:

| set by | handler | emits | on every marker? |
|---|---|---|---|
| `setMetaInteger("color", color)` | `ColorDetailHandler` | `<color argb="…"/>` | **yes** |
| `ATAKUtilities.setAuthorInformation` | `CreatorDetailHandler` | `<creator uid= type= callsign= time=/>` | **yes** |
| `setMetaBoolean("archive", true)` | `ArchiveDetailHandler` | `<archive/>` | usually — already droppable |
| `setMetaBoolean("readiness", …)` | `StatusDetailHandler` | `<status readiness="…"/>` | yes — already known |
| iconset chosen | `UserIconHandler` | `<usericon iconsetpath="…"/>` | only for custom icon sets |
| remarks entered | `RemarksDetailHandler` | `<remarks>…</remarks>` | only if typed |

So it is `<color>` and `<creator>`, not the `<usericon>` first guessed. Both are
unconditional, which is why *every* marker fell back and not just some.

**Confirmed on hardware 2026-08-04:** placing an `a-u-G` marker logged
`fallback: unhandled <detail> child: <color>`, matching the source reading.
Chat logged `<__chat>`, which is expected and unavoidable.

That run also exposed a limit in the diagnostic itself: it named only `<color>`,
because it returned at the *first* unknown child and `<color>` precedes
`<creator>` in ATAK's element order. Acting on that log would have meant adding
one tag, rebuilding, going back to the radio, and finding the next. It now
collects every offender in one pass — `<color>, <creator>` — bounded so a broken
event cannot write an unbounded line on the listener thread.

Worth noting what this is not: a gap in `KNOWN_DETAIL` relative to TAK itself.
`detail.proto`'s strongly-typed set is contact, `__group`, precisionlocation,
status, takv and track — exactly what `KNOWN_DETAIL` already holds. TAK puts
`<color>` and `<creator>` in its `xmlDetail` escape hatch for the same reason we
fall back. The difference is that TAK's fallback still carries the rest as
protobuf, while ours drops the whole event onto zlib.

### Markers on the compact path — PROTOCOL_VERSION 2

`<color>` and `<creator>` are now encoded, and markers take the compact path:

| | frame | on air | airtime |
|---|---|---|---|
| measured on the bench, zlib | 309 B | 325 B | 0.932 s |
| compact, same shape of marker | 109 B | 125 B | **0.420 s** |

**~2.2x less airtime**, on exactly the traffic multi-hop relaying exists to
carry. `<color>` costs 5 bytes (one attribute-name byte plus an int32);
`<creator>` costs ~40, dominated by its uid string.

**That measurement did not survive the bench, and the caveat was the reason.**
The fixture was a *reconstruction* built from `PlacePointTool` and the detail
handlers — 532 B against the 859 B the bench produced — and the warning that
"the missing ~330 B may contain another unhandled child" was correct. It
contained four:

```
[cot-codec] fallback: unhandled <detail> child: <remarks>, <usericon>, <link>, <marti>
```

`<color>` and `<creator>` are gone from that list, so version 2 did its job —
but real markers still fall back at ~310 B, because reading `PlacePointTool`
found what marker *placement* sets and missed what the marker *detail pipeline*
adds afterwards. Source reading narrowed the problem; it did not finish it.
The whole-list diagnostic is what made one run enough to see the rest.

### Version 3 finished the job

`<remarks>`, `<usericon>` and `<link>` are encoded; `<marti>` is dropped.

| | frame | on air | airtime |
|---|---|---|---|
| zlib fallback | 300 B | 316 B | 0.912 s |
| compact | 172 B | 188 B | **0.584 s** |

**1.6x less airtime.** Two things are worth knowing about how it got there.

**The flags field ran out.** Sixteen bits, all spent. Rather than widen it for
every frame — including position reports, the high-volume traffic that carries
none of these — the last bit became `F_EXT`, an escape meaning "a second flags
word follows". Markers pay two bytes; positions pay nothing.

**A marker names its parent twice.** ATAK writes both `<creator>` and a parent
`<link>`, in different spellings of the same four values:

```
creator uid= type= callsign=        time=
link    uid= type= parent_callsign= production_time= relation=
```

So the link is the creator plus a relation. `L_LIKE_CREATOR` says exactly that,
and takes the link from ~35 bytes to **five** — a subflag byte and an lstr'd
`"p-p"`. Set only when every shared field genuinely matches; a link that differs,
or one with no creator to lean on, is still sent in full, and a URL link
(`relation="r-u"`, with `url`/`mime`) falls back rather than losing an
attachment's address.

### Markers cannot be sent to specific people

**Not being built (2026-08-04 decision).** The operator can already address a
marker to one person in ATAK's own UI, which is what prompted this being set
aside. Worth being precise about what that does and does not mean, because the
two are easy to confuse: **choosing a recipient in ATAK changes nothing about
who receives the marker on this mesh.** The bridge broadcasts every event to
every node in earshot, and `<marti>` — the only place ATAK writes the choice
down — is dropped before transmission. Every node still gets it and every node
still renders it. The rest of this section stands as the record of what
building it would actually involve.

**Originally deferred, not declined.** Dropping `<marti>` is the right call on the bytes and
on ATAK's own classification of it — but it removes the only place ATAK writes
down "this marker is for Bob, not everyone", so the gap should be recorded rather
than quietly closed.

That intent was **already** not being honoured: the bridge broadcasts every event
to every node in earshot, and did so long before `<marti>` was dropped. What
changed is that the request is no longer even carried.

Doing it properly means separating two things that sound alike:

- **Targeting** — only the named node acts on the event. Everyone still hears the
  bytes; the rest ignore them. Cheap, and most of the machinery exists:
  `_atak_uid_to_node` already maps an ATAK uid to the node it lives on, and
  `_chat_addressee_node` already uses it to narrow a direct chat's ack
  expectation to one peer.
- **Confidentiality** — only the named node *can read* it. This is not the same
  problem. Every node shares one group key, so anything one member can read,
  all can. It needs a second key layer (per-recipient, or per-subgroup), and it
  interacts with the flood-relay design: a relay has to forward what it cannot
  read.

Targeting is a small piece of work on top of what milestone 4 already built.
Confidentiality is a milestone. **Do not conflate them** — shipping targeting and
calling it "private markers" would be exactly the kind of security theatre the
group-key section warns about.

**A regression this nearly introduced.** `<status readiness="true"/>` rides on
every placed marker. The encoder only ever modelled `status/@battery`, and
`status` is in `KNOWN_DETAIL` — so moving markers onto the compact path would
have silently dropped `readiness` from traffic that had been carrying it
verbatim on the zlib path. It is now encoded, and `<status>` rejects attributes
it cannot carry rather than discarding them. **The wider gap is still open:**
every other known child still drops unmodelled attributes. `status` was fixed
because this change is what made it bite.

**Still worth doing, and step 2 unblocked it:** `<creator>`'s uid and callsign
are the *sender's own*, which the receiver already learns from self-SA and the
uid→node map. A flag meaning "the sender" plus a timestamp would cost ~5 bytes
instead of ~40, taking the marker to ~85 B on air and ~3x. The objection
recorded here was that it needs the codec to know the local identity, costing
the statelessness that makes it desktop-testable — but that is exactly what
`SaTx`/`SaRx` now carry, injected by the caller and testable on a desktop. The
binding a self-SA establishes is the same identity a marker's `<creator>`
restates, so the mechanism is already built and this is now a small change
rather than a design question.

### Step 2: the position report stops repeating itself

Where the 61 bytes of a compact position report actually went, measured rather
than estimated:

| field | bytes | |
|---|---|---|
| **uid** `ANDROID-352214111111111` | **24** | constant for the session |
| **callsign** `BRAVO01` | **8** | constant for the session |
| lat + lon | 8 | |
| time | 4 | absolute |
| track | 4 | |
| stale−time | 2 | constant in practice |
| hae | 2 | |
| ctrl + flags | 3 | |
| type, how, ce, group, battery, precisionloc | 6 | |

**Identity is 32 of 61 bytes — 52% — and it is the same 32 bytes every minute
for as long as the device is switched on.** The mesh envelope already carries an
authenticated `origin` identifying the sender, so all of it is redundant.

Three changes, in descending order of what they were worth:

**The beacon goes quiet when something else already proved the link.** It cost
44 B on air per node per minute and, whenever ATAK is running, demonstrated
nothing the position report underneath it had not just demonstrated. It now
fires only when nothing has been transmitted in the last interval — which is
the case it was built for, ATAK down or no traffic flowing, where the
alternative is not knowing whether the radio works. The first one always goes
out, because until it does no peer knows this node's device name. **This was the
largest single win in step 2 and needed no wire-format change at all**, which is
why it replaced the planned "merge the beacon into the position report": a merge
would have folded bridge metadata into a CoT frame to save the same airtime.

**Identity is bound, not repeated.** A keyframe carries uid and callsign and
says "these are mine"; the frames after it set `X_BOUND` and carry neither. The
binding is per origin and the origin is authenticated, so a node can only ever
bind its own identity.

**Position is delta-coded** against the keyframe: time, lat and lon as varint
offsets, and the stale offset inherited outright.

What a five-frame cycle looks like on the air, walking pace:

| | inner | on air | |
|---|---|---|---|
| keyframe | 63 B | 79 B | `X_BIND` |
| delta | 21 B | 37 B | `X_BOUND X_DELTA` |
| delta | 23 B | 39 B | |
| delta | 23 B | 39 B | |
| delta | 23 B | 39 B | |

| 16 nodes, SF8/125 kHz | s/min | duty |
|---|---|---|
| step 1, as benched | 8.21 | 13.7% |
| **step 2** | **3.45** | **5.7%** |

That is **2.4x**, and it turns a 10% budget from 2.2 s/min overdrawn into
2.5 s/min spare — roughly 11 relay transmissions per minute network-wide, which
is what step 3 has to spend.

**A delta references the keyframe, not the frame before it.** This is the one
design decision here worth arguing about, and the obvious answer is wrong.
Referencing the previous frame gives marginally smaller offsets, but then a
single lost frame desyncs the receiver until the *next keyframe* — every
delta after the loss is measured from a base it never reconstructed. Measured
over a 5-minute interval at walking pace:

| | 16 nodes | duty | one lost frame costs |
|---|---|---|---|
| delta vs previous frame | 3.45 s/min | 5.7% | up to 5 minutes of positions |
| delta vs last keyframe | 3.55 s/min | 5.9% | that one frame |

Two tenths of a point is not a price worth paying for a scheme that breaks on
the first lost packet, so the robust form was taken. (The measured run above
lands at 5.7% anyway — the real delta frames came out at 21–23 B against the
25 B the estimate assumed.) Varint boundaries are coarse enough that the extra
magnitude usually does not cross one; a **half-world jump still deltas**, because
five bytes per coordinate is the varint ceiling and the fixed form costs four
plus the two-byte stale offset a delta does not carry.

**What goes wrong, and how it recovers.** A receiver that missed the keyframe
refuses the frames that follow it rather than guessing — a CoT event without a
uid is not something ATAK can render, and inventing one would put a marker at a
made-up place under a made-up name. Recovery is the next keyframe, so the
exposure is bounded by the interval. Two things narrow it further:

- **A new peer forces a keyframe.** A node joining just after one would
  otherwise wait most of an interval, which is the case a fixed cadence serves
  worst. Any first frame from an unknown origin queues one — and crucially that
  happens *before* the decode is attempted, so a frame this node cannot yet read
  still triggers discovery.
- **Re-keying clears every binding and forces a keyframe**, because what a peer
  bound under the old key says nothing about who holds the new one.

That first point makes joining resolve itself without any request traffic, and
it does so in both directions. A node arriving hears an existing node's delta,
cannot read it, but registers the origin and forces its own keyframe; the
existing node hears *that* keyframe, registers the newcomer and forces one back.
Each direction takes at most one position interval, so a join settles in about
two minutes with nothing sent that would not have been sent anyway.

**The plan called for the receiver to request a binding instead, and that was
deliberately not built.** It costs a new frame type, a new transmit path and
herd control — fifteen nodes that all missed one keyframe would all ask at once,
which is the shape of problem the whole relay budget exists to avoid. What it
buys over the above is faster recovery in one case only: a keyframe lost on the
air, where the wait is up to a full interval. If bench runs show that happening
often enough to matter, the cheap version is to shorten the keyframe interval,
which is one constant and costs 0.5 points of duty to halve.

Five minutes was picked off the measured curve rather than guessed: 3 minutes
costs 6.2% and 10 minutes buys only 5.7%, so the knee is flat and the shorter
recovery is worth more than the byte.

**Only the device's own self-SA is compressed.** The signal is `<takv>`, which
describes the TAK application that produced the event and so appears on events a
device emits about itself. A marker's uid belongs to the marker and its callsign
names the marker, so there is nothing constant to bind; chat, drawings and
routes go out through the reliable path, which is never handed a session at all.
A device that omits `<takv>` simply never binds and pays what it pays today.

**Encoding is not transmitting.** The session is staged and committed only when
a frame is actually emitted, because a session that advanced for a frame that
fell back to zlib — or that the encoder refused — would leave every receiver
decoding deltas against a base they were never given. A frame dropped from the
TX queue under backlog is the one case still unhandled, and deliberately: it is
indistinguishable from a frame lost on the air, and the recovery is the same.

**Loss measurement moved to the mesh sequence.** Beacon sequence gaps used to be
how packet loss was read off the logs, and quietening the beacon would have
taken that away. The mesh envelope's per-origin sequence is strictly better —
authenticated, and incremented on *every* frame rather than only the probes — so
peers now carry `heard` and `lost` counts derived from it. A jump larger than 64
is read as a peer restart rather than as loss, since the sequence is 16-bit and
restarts at zero under a fresh epoch.

**A related saving fell out of the version bump:** the extended flags word is
now a uvarint. Every flag defined so far is below 0x80, so it costs one byte
rather than two, and markers get that back as well — 168 B to 167 B.

### Step 4: selective retransmission (PROTOCOL_VERSION 12)

A retry resent every fragment. A 5-fragment drawing at 3 relays and 4 attempts
is 80 transmissions — 44.9 s of channel from one person sketching a polygon.
It is now 32, and **the bitmap was the second half of the fix, not the first.**

**A retry was not even the same message.** `fragment()` picks a random id per
call and the reassembler keys on `(msg_id, count)`, so every retransmission
began a *second* reassembly and orphaned the first. Fragments that had already
arrived were discarded; the abandoned partial held one of sixteen slots until
it timed out. A five-fragment drawing that lost one piece had to land all five
again — and a bitmap describing fragments nobody could correlate across
attempts would have meant nothing.

So an acknowledged message now fragments under **its own message id**, the one
the acknowledgement already names. Retries became cumulative on their own, with
no protocol change and no extra bytes.

**Then the bitmap.** A new frame type, `T_PARTIAL`, deliberately separate from
`T_ACK` rather than an optional field on it: the two say opposite things about
whether a message arrived, and the cost of confusing them is a delivery
reported that never happened. ~20 bytes against the ~0.4 s a redundant fragment
costs, so it pays for itself the first time it prevents one.

**When to speak up** is the part with a real choice in it. Reporting on the
last fragment of a burst never fires when the last fragment is the one lost,
which is as likely as any other. Reporting on a duplicate only fires once the
sender has already begun resending everything, which is the airtime this
exists to save. So the receiver reports on a **stall** — a reassembly that has
gone quiet for 1.5 s — which catches both, and is rate-limited to once per
stall so a message stuck behind a lost fragment does not report every tick.

**The sender sends the union**, not the intersection: one broadcast serves
every peer, so a fragment only one peer is missing still has to go out. Any
peer that has not reported forces a full resend, because narrowing on partial
information would strand whoever stayed silent.

#### A timer this broke, hours after it was introduced

The receiver's reassembly buffer has to outlive the sender's retry schedule —
fragments accumulated across attempts are worthless if they are discarded just
before the attempt that would have completed them. That held at a fixed 30 s
for as long as every retry base was 2.0 s and the schedule finished at 9.5 s.

The multi-hop retry allowance added earlier the same day took a two-hop
schedule to **30.9 s** and a three-hop one to **52 s**. So a receiver began
throwing away partial messages moments before the last attempt to complete
them, and the feature that depends on accumulating fragments would have been
quietly undermined by a fix made that morning.

Nothing connected the two numbers, which is what let one move past the other.
They are connected now: `reliable.max_retry_span_s()` computes the worst case
and the reassembly timeout is derived from it with margin — 104.5 s for a
full-MTU frame to the furthest peer a one-shot hop cap allows. The same shape
as the digest-refresh bug the previous bench found, and the same fix: derive
one from the other rather than pick both.

#### Verified on hardware, 2026-08-12

The whole loop, on three real nodes:

```
laptop:  DROPPING fragment 1/2 of message 11851 from aaaa08
laptop:  asking aaaa08 for fragments [1] of message 11851 (1/2 held)
Pixel:   laptop has 1/2 fragments of message 11851; missing [1]
Pixel:   retransmitting b-m-r ... (attempt 2, fragments [1] only), still waiting on laptop
Pixel:   DELIVERED ... after 2 transmission(s)
```

**The loss had to be manufactured.** Two full route sends across three radios
on a desk delivered every fragment first time, so `T_PARTIAL` had never once
been emitted on a radio — built, desktop-tested, installed, and completely
unexercised. `tools/mesh_node.py --lose-fragment N` drops a given fragment
once, which is the same fixture pattern as simulated deafness and exists for
the same reason: the behaviour under test only happens on loss, and a good link
never provides any.

#### The bench then found something a single receiver could not

Seven seconds after the OnePlus had passed the whole route to ATAK, it asked
the Pixel for fragment 0 of that same message. It had completed on the first
attempt — and then heard the retry, which is **broadcast**, aimed at the
laptop. The reassembler had already popped the finished message, so a lone
fragment arriving under that id looked like the start of a new one: it opened a
reassembly, stalled, and asked for the rest of something it had already read.

On a two-node bench that is one wasted report. At 16 nodes it is fifteen, plus
fifteen reassembly buffers held for the full timeout — the airtime selective
retransmission exists to save, spent somewhere else. The reliable layer dedups
by message id, but the fragment layer beneath it had no memory of what it had
already finished. It does now, expiring once no retry could still be carrying a
fragment of it.

This is the kind of defect that only appears with more than one receiver, which
is the argument for benching a broadcast protocol on more than two nodes.

#### Still to check on the radio

The loop above was exercised with a **manufactured** loss and only two
receivers. What it has not seen:

1. **Natural loss**, rather than a fixture dropping a chosen fragment. That
   needs a marginal link — distance, or a relay in the path.
2. **More than one peer missing DIFFERENT fragments**, which is the case
   `needed()` computes a union for. Two receivers on a desk both get
   everything, so the union has never been anything but a single peer's gap.
3. **A route long enough to still fragment after the v13 shrink**, since a
   2-packet route only ever exercises "resend 1 of 2".

**Both phones must be updated together**, as always — `PROTOCOL_VERSION` is 12
and 11 refuses it by design.

### Step 5: a direct chat names the node that owes the ack (`T_RELIABLE_TO`)

Milestone 3 narrowed a direct chat's ack *expectation* to the one node it was
addressed to, and [routing](routing.md) recorded that as saving 0.55 s per chat at eight
nodes. **It saved nothing.** The narrowing changed only which acks the sender
counted — which was its real purpose, and it worked — but every node in earshot
went on acking, because nothing on the wire told them not to.

Nor could they work it out. `_queue_ack()` runs on the reliable wrapper alone,
which carries an origin and a message id; the addressee is inside the chat uid,
in the frame, which is not decoded until after the ack has been queued.
Deliberately so: acking before decoding is what makes a duplicate still get
answered, and answering a duplicate is what stops the sender retransmitting
forever when it was our ack that went missing.

And those acks are the expensive kind. A room message sets `ANY_ACK_BIT`, so
receivers cancel on hearing each other and the mesh pays for **one** ack no
matter how large it is. A direct chat cannot use that — a bystander answering
first would silence the actual recipient and the message would be reported
undelivered while sitting on their screen — so every one of those acks is
uncancellable. At 16 nodes that is 15 acks of ~0.09 s each against a chat of
~0.34 s: **roughly twice the airtime of the message they confirm**, spent
saying something the sender discards.

So the addressee goes on the wire:

```
0x19  addressed reliable   ctrl | msg_id uint16 | lstr(origin) | lstr(addressee) | <inner frame>
```

Identical to `0x14` with one field inserted, and a receiver whose node id is
not that field stays silent.

**A new frame type rather than a flag bit**, for the reason `T_PARTIAL` is
separate from `T_ACK`: an old decoder reading an unknown *flag* misparses
everything after it, while an unknown *type* hits the existing `unsupported
frame type` branch and is dropped with a log line. So this needs **no
`PROTOCOL_VERSION` bump** — the same trade milestone 4 made when it added
`T_RELIABLE` and `T_ACK` to the existing namespace, and it matters more than
usual here: the version is a nibble, and 14 of its 15 usable values are spent.
An un-updated phone loses direct chats visibly and keeps everything else.

**The name is only paid for when somebody could otherwise answer wrongly.**
It costs ~7 bytes, and with a single peer there are no bystanders to silence —
which is every bench this project has ever run. Below two peers the expectation
is still narrowed and the name is simply left off.

**Two counters, because they are different savings.** `acks suppressed=` is
"we would have said the same thing and somebody beat us to it"; `not-ours=` is
"we were never being asked". Only the second scales with the size of the mesh.

**Unverified on hardware.** The airtime above is computed, and the saving is
zero at two nodes by construction, so the bench that would show it needs at
least three.

### GeoChat on the compact path (PROTOCOL_VERSION 6)

Chat was the largest frame left on this link — **286 B on air against 39 for a
position report**, seven times the size — and all of it took the zlib route
because `<__chat>` and `<__serverdestination>` were unmodelled.

It is large because it says the same few things over and over. In 914 bytes of
XML:

| | appears | where |
|---|---|---|
| the sender's uid | **4×** | event `uid`, `<chatgrp uid0>`, `<link uid>`, `<remarks source>` |
| the addressee | **5×** | event `uid`, `chatroom`, `id`, `<chatgrp uid1>`, `<remarks to>` |
| the message id | **2×** | event `uid`, `<__chat messageId>` |
| the TAK server's IP | 1× | `<__serverdestination>` — meaningless here |
| the message text | 1× | the only field carrying information once |

So each distinct value is sent once and the repeats are rebuilt:

| | XML | on air | | |
|---|---|---|---|---|
| room message | 914 B | **130 B** | 0.431 s | was 286 B, **2.20x** |
| direct message | 939 B | **166 B** | 0.523 s | was 335 B, **2.02x** |

`<__serverdestination>` joins `<marti>` and `<archive>` on the droppable list.
It carries a TAK server's address and port, and this bridge exists precisely
because there is no server — 83 bytes of XML on every message describing a host
no node here can route to.

The 36-character message UUID goes as its 16 raw bytes, and `"All Chat Rooms"`
became a table entry, so the default broadcast room costs one byte instead of
fifteen. A direct message's addressee is a plain uid, which escapes to a string
and pays one byte more than raw — a good trade against fourteen saved on every
message to the whole group.

**Chat deliberately does not use the step 2 identity binding**, which would
save another 24 bytes by dropping the sender's uid. A receiver that lacks the
binding *refuses* the frame — and would acknowledge it anyway, because the ack
is queued before decoding — so the message would be reported DELIVERED having
never been rendered. That is exactly the silent loss milestone 4 exists to
prevent. A position can afford to be refused because another arrives within the
minute; a chat message cannot. The 24 bytes stay.

**What falls back, and why that is the point.** The encoder rebuilds every
repeated field on the far side, so anything it does not recognise has to fall
back rather than be dropped — an unknown attribute on `<__chat>`, a `messageId`
that disagrees with the uid, a `<remarks source>` naming someone other than the
sender, an extra child, or a room name containing a dot (which would make the
uid impossible to split back apart unambiguously). Each has a test. The direct
message above is a *reconstruction* of what ATAK sends, not a capture, so if a
real one differs it will fall back exactly as it does today and the log will
name the reason — which is how the marker encoding got finished.

### Dictionary compression on the compact path (PROTOCOL_VERSION 7)

Adopted from the prior art, and it is the one place their encoding genuinely
beat ours: their 81 B marker against our 167 B was not a better field layout,
it was that they compress the structured form against a dictionary trained on
real traffic and we did not.

The structured encoding removes what a *schema* can remove — tag names,
repeated identity, type strings. What it cannot touch is redundancy **across**
frames: that every marker in the world carries a similar-shaped uid, a similar
iconset path, a similar creator block. A dictionary takes those.

Measured on frames the dictionary had **never seen** — different callsigns,
different message texts, different iconset paths, a different random seed from
the one that trained it:

| | before | after | |
|---|---|---|---|
| position | 30.5 B | **22.9 B** | 1.33x |
| marker | 164.4 B | **111.6 B** | 1.47x |
| chat | 119.7 B | **78.0 B** | 1.53x |

At 16 nodes that takes step 2's benched 5.8% to **5.3%**, and the relay
headroom under a 10% ceiling from 2.52 to 2.80 s/min.

**Two findings worth keeping, because both were counter-intuitive.**

**Without a dictionary this does not work at all.** Plain zlib makes a compact
frame *bigger* — 61 B to 70 B on a position — because the frames are already
dense and all compression can add is a stream header. The dictionary is the
entire mechanism, not a tuning of it.

**Measuring it is easy to get wrong twice.** A first pass built the dictionary
from the tail of a concatenated corpus, which happened to be all chat frames,
and reported markers at 141 B instead of 75 — the dictionary has to be
*representative*, and a zlib dictionary is only its last 32 KB. A second pass
used a held-out half of the corpus and reported 2.0–2.1x; the honest figure is
1.33–1.53x, because both halves came from the same four fixtures. **Held-out is
not the same as unseen.** The numbers above are from frames built with
vocabulary the dictionary has never met.

**zlib rather than zstd**, which is what the prior art uses. Two reasons: it is
in the standard library, so there is no question of whether a native wheel
exists for Chaquopy's arm64 Python — and on this corpus it *won* on chat (61 B
against zstd's 69) while losing only modestly on markers (78 against 66). A
native dependency is not worth 15% on one frame type.

**Safety.** `encode()` emits the compressed form only when it is actually
smaller, so a dictionary that suits the traffic badly costs nothing beyond the
attempt — which is what makes it safe to ship one trained on synthetic frames.
And zlib carries the dictionary's Adler-32 in the stream header, so a build with
a different dictionary **fails loudly at decompress rather than decoding
garbage**. Both have tests.

**The dictionary is trained on synthetic variations of the test fixtures**, and
that is its weakness. `train_zdict.py` regenerates it and is meant to be re-run
against captured frames once there are some; that is worth doing and costs a
`PROTOCOL_VERSION` bump, because the dictionary is part of the wire format.

### Drawn shapes on the compact path (PROTOCOL_VERSION 8)

The 2026-08-05 bench drew a rectangle and it cost **452 B on air, 1.26 s** —
the most expensive frame this project had sent. Every part of it was
unmodelled, and one part could not be modelled at all without a structural
change.

**Detail children can now repeat.** `_detail_children` returned a plain dict of
tag → element, so a rectangle's four `<link point="lat,lon,hae"/>` corners
became one corner and three silent losses. It now returns a `_Children` — a
dict of tag → *first* element, so every existing `children.get(tag)` reads
exactly as it did, plus `all(tag)` where repetition is the point.
`REPEATABLE_DETAIL` is deliberately just `link`: anything else that repeats is
still a fallback, because quietly keeping one of them is the bug this fixes.

**A shape's `<link>` and a marker's `<link>` share only the tag.** A marker's
carries provenance — uid, type, relation, parent callsign. A shape's carries a
point and nothing else. The encoder decides which it has from the count, and
the marker path now asserts there is only one of its kind.

**Vertices go as zigzag deltas from the event's own point** — which for a
rectangle is its centre — in two packed columns, every latitude then every
longitude, rather than interleaved pairs. Packing by column keeps
same-magnitude numbers adjacent. A test pins the consequence: the same shape
costs the same wherever on earth it is drawn.

| | before | after |
|---|---|---|
| rectangle, 747 B of XML | 452 B on air, 1.26 s | **122 B on air, 0.410 s** |

**3.1x**, and every corner survives to seven decimal places with the stroke
colour, weight, style, fill colour, `__shapeExtras` and `labels_on` intact.
A 40-vertex polygon still fits in one packet.

**Where it came from.** `u-d-r` was first written up here as a route on the
strength of the `-r`. It is a rectangle — `CotMapAdapter.java:283` comments it
so — and the fixture is built from `DrawingRectangle.toCot()` in the ATAK
source rather than from a guess. Reading the source is what turned "+2 more"
into a known list; the `_MAX_REJECTED_REPORTED` cap has been raised from 6 to
12 so the next bench does not truncate one again.

**What this does not cover**, and each is a different shape of problem:

- ~~**Circles and ellipses**~~ **Done — PROTOCOL_VERSION 10, see above.**
- ~~**Real routes**~~ **Done — PROTOCOL_VERSION 11, see above.** A route of
  eleven waypoints or more still fragments, which is now the largest remaining
  airtime problem on this link.

### Circles and ellipses (PROTOCOL_VERSION 10)

A circle has no vertices — a centre, which is the event's own point, and radii.
`DrawingCircle.toCot()` wraps that in what its own comment calls "this dusty
old format":

```xml
<shape>
  <ellipse major= minor= angle= [swapAxis=]/>          one per ring
  <link type="b-x-KmlStyle" uid="{event uid}.Style" relation="p-c">
    <Style>
      <LineStyle><color>ffffffff</color><width>4.0</width></LineStyle>
      <PolyStyle><color>00000000</color></PolyStyle>
    </Style>
  </link>
  <link relation="p-p-CenterAnchor" type= uid=/>       optional
  <link relation="p-p-RadiusAnchor" type= uid=/>       optional
</shape>
```

**Almost none of it carries information.** The style link's uid is the event's
own with `.Style` appended; its type and relation never vary; and the whole KML
nest is three colours and a width inside ~150 bytes of ceremony. What is left
is the radii and two anchor uids.

| | before | after |
|---|---|---|
| circle | 460 B, **2 packets**, 1.281 s | **131 B, 1 packet, 0.431 s** |
| rectangle (v9 fix) | 452 B, 1.260 s | 116 B, 0.400 s |

**3.0x**, and the packet count is the part that matters: a circle no longer
fragments, so it is no longer four transmissions when ARQ retries it.

**Radii go in centimetres.** ATAK writes a Java double, so a 152.4 m radius can
arrive as `"152.4000244140625"`. A centimetre is far below what anyone can draw
or see, and it is the same trade already made for `hae` and `speed` — but it
does mean the re-emitted XML differs from what was sent, in the last decimal
places of a number nobody reads. A test pins it rather than leaving it to be
discovered.

### Routes (PROTOCOL_VERSION 11)

The 2026-08-07 bench answered the open question in one line, which is what the
nesting diagnostic was added for:

```
fallback: unhandled <detail> child: <link_attr>, <__routeinfo> containing <__navcues>
```

**`<__navcues>` is empty unless the operator recorded voice cues.** ATAK writes
the element for every route regardless (`NavigationCueHandler.toCotDetail`
always returns the node), so the ordinary case costs one byte and the
structure is still there when someone does record cues.

Three parts, all now modelled:

- **Waypoints** get the same treatment as a drawn shape's vertices — zigzag
  deltas from the event's own point, in packed columns. What a route adds is
  that a waypoint may *also* be a marker, so a bitmap says which ones are, and
  their uid, callsign, type and remarks follow in a second pass.
- **`<link_attr>`** — ten flat attributes. `color`, `stroke` and `style` are
  plain integers (`Route.toCot` casts stroke to `int`, unlike the double a
  drawn shape's `strokeWeight` carries — the first round-trip caught that), and
  `method`, `direction`, `routetype` and `order` come from short enumerations
  that go in tables and escape safely for anything unrecognised.
- **`<__routeinfo>`/`<__navcues>`** with `<__navcue>` and its `<trigger>`
  children.

| | before | after |
|---|---|---|
| 8-waypoint route | 651 B, **2 packets**, 1.947 s | **230 B, 1 packet, 0.687 s** |
| ...as the bench actually sent it, with its ARQ retry | **3.893 s** | 0.687 s |

**2.8x on size, and 5.7x on what that route actually cost the channel**, because
fragmenting is expensive twice: the bench run needed a retransmission, and an
ARQ retry resends *every* fragment. One route took a two-node channel to
**12.3% duty**.

**A long route still fragments, and that is recorded rather than wished away.**
Twenty-nine *named* waypoints fit one packet; thirty do not. A test pins both
sides of that boundary and that a longer route still round-trips once split.

That boundary was **ten** until 2026-08-12, and the two things that moved it
are below. Both were found by capturing a real ATAK route off the air rather
than reasoning about the fixture — which had quietly been measuring the wrong
thing, because its uids are sequential and squeeze away while ATAK's are
random and do not.

#### The compact encoder was being thrown away on the traffic it was built for

`encode()` kept a compact frame only if it fitted a **single packet**, and fell
back to zlib'd XML otherwise. A real 55-point route encoded to 911 B compact —
and was discarded in favour of 2568 B of zlib, 6 packets against 3. Every route
long enough to fragment had been paying that, which is to say every route the
work on routes was aimed at, and the fixture never showed it because a fixture
route is small.

A frame that big is going to be fragmented whichever form it takes, so the
caller's budget stops being a limit and the only question left is which form is
smaller. It now compares and takes the smaller.

**Not for a frame carrying session state.** A staged session means a self-SA
report binding identity or delta-coding against a base, and those are
fire-and-forget — no ARQ, nothing to notice a loss. Fragmenting one multiplies
the chance the receiver never sees it while the sender has already advanced,
stranding every later delta against a base that never arrived. That is the
`delta frame references generation N, last base is None` failure the same
day's bench spent an hour on, and the guard is why a position still refuses to
take the win a route can.

#### A real route is mostly control points, and their uids are pure overhead

The captured route was **55 points: 3 named waypoints and 52 unnamed control
points**. The control points' uids alone were 884 of 2568 bytes.

ATAK draws the distinction itself. A `b-m-p-w` waypoint becomes a Marker that
`RouteImporter` looks up with `deepFindUID`, so another message can reference
it and its uid stays on the wire. A `b-m-p-c` control point is, in ATAK's own
comment, *"never added to a mapgroup"* — nothing can refer to it, so its uid is
a random number generated for an object that is never addressed by it.

A plain control point — type `b-m-p-c`, no callsign, no remarks — now costs
**one bit** in a second bitmap. Its uid is derived on receipt from the route's
own uid and the point's index, which is stable, so re-sending a route moves the
control points ATAK already has instead of accumulating a fresh set. A *named*
control point is not plain: the operator put something there, and it goes on
the wire.

| the captured 55-point route | size | packets | airtime |
|---|---|---|---|
| before | 2568 B | 6 | 6.9 s |
| after | **785 B** | **2** | **2.1 s** |

**3.3x smaller.** Coordinates round-trip bit-exact; altitude is quantised to
the metre, as it always was. Still above the 1.5 s relay budget, so a route
this long remains single-hop — but a route of ~20 points now fits one packet
and can cross a relay, where before it could not.

### What routes needed, before the bench answered it

Kept as the record of how the unknown was closed. `b-m-r` needed three things,
of which only two were understood at the time:

1. **Waypoint links.** A route's `<link>` is a superset of a shape's: `point`
   always, plus `type`, `uid`, `relation="c"`, `callsign` and `remarks` when
   that vertex has a marker (`EditablePolyline.toCot()`). The repeated-children
   work is already in place; this is an extension of the vertex encoding.
2. **`<link_attr>`.** Ten flat attributes — colour, stroke, style, transport
   type, method, direction, routetype, order, planning method, prefix. Every
   one is a small string or number, and most come from short enumerations.
   Straightforward.
3. **`<__routeinfo>`.** **Not understood.** It is a container that ATAK's
   handlers write into, and `NavigationCueHandler` alone can nest
   `__navcues` → `__navcue` → `trigger` inside it. Whether a plain drawn route
   carries anything there, and what, is not something to guess at — this
   section has already had to correct one guess (a rectangle called a route)
   and one bug from an unverified table.

So the fallback diagnostic now **names what is inside a container**, one level
deep:

```
fallback: unhandled <detail> child: <link_attr>, <__routeinfo> containing <__navcues>
```

`<__routeinfo>` on its own says nothing about whether the work is an afternoon
or a fortnight; the answer is entirely in its children. One more bench line
settles it.

**The alternative worth considering instead** is a generic nested-detail
encoder — carry any unmodelled subtree compactly rather than failing the whole
event back to zlib. That would cover `<__routeinfo>`, every future ATAK detail,
and the long tail this codec keeps meeting one tag at a time. It is a larger
change and a different kind of change, and it should be a deliberate choice
rather than something arrived at by accident while chasing routes.

### Updating one phone before the other

The version bump changes the mesh envelope's control byte, so a half-finished
rollout gives two radios that transmit perfectly and ignore each other — the
worst failure shape there is. A frame carrying our envelope *type* at a
different version is therefore counted as `wrong_version`, not lumped in with
noise as `foreign`, and reported as:

```
mesh: ... -- HEARD 3 FRAMES FROM PROTOCOL v10 WHILE ON v11, UPDATE THE OTHER NODES
```

The status screen says the same, and this check runs **before** the passphrase
warning: both can be true at once, but only one names the fix.

That warning only fires once a mismatched frame has actually been heard, so the
status screen also states this node's own version outright, next to the key
fingerprint and used the same way — read it off both phones and compare:

```
protocol v11   ·   build 06bf622
```

**The build stamp is the git commit, not `versionName`.** `versionName` has read
`"1.0"` since the first commit and has never been bumped, so two phones showing
it would prove nothing and give false confidence — the exact opposite of what
the line is for. The stamp comes from `git describe --always --dirty`, so it
changes on every commit and gains a `+` when the APK was built from a tree with
uncommitted changes. It catches the failure one step earlier than the protocol
version does: not "these two disagree about the format" but "only one of these
phones got the new APK".

### Null fixes are no longer transmitted

Both phones logged this twice on 2026-08-04:

```
started moving 15144km (20.90893,-140.58291 -> 0.00000,0.00000 -- GPS acquisition?)
started moving 15144km (0.00000,0.00000 -> 20.90898,-140.58291 -- GPS acquisition?)
```

ATAK reports latitude and longitude both zero while GPS is reacquiring. That is
a real place — open ocean in the Gulf of Guinea, about 600 km south of Ghana,
which mapmakers call Null Island — so the policy read it as a 12,000 km move,
which is emphatically a movement onset, the one category never rate limited.
Then GPS recovered and it was another onset coming back.

Two of the most expensive sends available, to say something false, and in
between the marker sat in the Atlantic on every other device. The policy already
recognised the pattern well enough to print `GPS acquisition?` — it labelled it
and transmitted it anyway.

Now dropped, counted as `nofix=` on the policy line. **The subtle half is that
it is dropped *before* the last-sent lookup and never recorded**: leave the
previous real fix in place and the position arriving when GPS recovers is metres
away, so it passes as an ordinary heartbeat. Record the 0,0 instead and the
return trip still looks like a fresh 12,000 km onset — fixing one spurious send
and keeping the other.

The tolerance is about a centimetre. It exists to catch the exact zeros ATAK
emits, not to carve out a region of the ocean.

**Verified on hardware 2026-08-04.** Both phones sat through a GPS dropout:

```
suppressed ANDROID-0123456789abcdef (a-f-G-U-C): no GPS fix
tx policy: sent=0 suppressed=4 (100%) [... nofix=4] [onset=0 moving=0 heartbeat=0]
```

**`onset=0` is the result that matters.** Four null fixes dropped per phone and
not one spurious transmission, where the same dropout previously produced two
unthrottled onsets per round trip. When GPS returned, the real position went out
normally at 62 bytes — logged as `new contact` rather than a movement onset,
which is the proof that the 0,0 was never recorded as a previous position.

## Milestone 7: the frames stop repeating themselves (PROTOCOL_VERSION 15)

Built 2026-08-17. **Desktop-tested only — none of this has been on a radio.**

### Why, and the measurement that reframed everything

Milestone 6 step 2 took a position report's payload from 61 bytes to 22 and the
airtime barely moved. Measuring where the bytes actually went explains that:

| frame | payload | on air | overhead share |
|---|---|---|---|
| bare beacon | 15 B | 50 B | **70%** |
| ack | 17 B | 52 B | **67%** |
| position | 39 B | 74 B | 47% |
| chat | 114 B | 149 B | 23% |

Every packet paid 35 bytes it could not avoid — 19 for Reticulum's header, 16
for the mesh envelope. **The payload work had already hit diminishing returns
and nobody had noticed, because the frames that dominate the traffic are the
small ones where the header is most of the cost.**

### What v15 changes

**Node ids go as their 3 raw bytes**, not a length-prefixed hex string. Seven
bytes to say three, on every beacon, twice on every ack and once on every
acknowledged message. The hex spelling is for humans reading logs.

**The frames stop repeating what the envelope already proves.** The beacon's
`node_id`, the ack's `acker`, the partial ack's `acker` and the reliable
wrapper's `origin` are gone. Each existed from before anything on this link was
authenticated; once the mesh envelope started carrying an authenticated sender,
they could only ever be *checked against a better copy of themselves* — which
is what `_reject_impostor()` did, and why that function no longer exists. A body
can no longer contradict its envelope because there is no second claim to
contradict it with, which is strictly stronger than checking. The limit is
unchanged and worth restating: this was never what makes a node id unforgeable.
Anyone holding the group key can put what they like in the envelope.

**Absolute timestamps carry their low 24 bits**, rebuilt against the receiver's
clock. **Three bytes and not two, and the reason is the interesting part.** Two
bytes spans 18 hours, which covers everything this link realistically carries —
and fails *silently* when it does not, decoding a day-old event to a date in
the wrong week with no way for the receiver to notice, since any 16-bit value
is a valid time in the current window. ATAK does emit events stamped hours or
days earlier. Three bytes spans 194 days and still saves a byte. This is also
deliberately *not* TAKPacket-SDK's trick of dropping the timestamp and stamping
arrival: a retry can land 16 seconds late and a relayed copy later still, so
stamping arrival would quietly relabel when things happened.

**The AES-CCM tag halves to 4 bytes.** A forgery succeeds with probability
2⁻³² per attempt instead of 2⁻⁶⁴. That is defensible *here* rather than in
general, because the channel bounds the attempts: a forged frame costs ~0.13 s
of airtime, so an attacker transmitting flat out manages ~460 tries an hour and
needs ~10⁶ years for an even chance — against a group key a compromised phone
hands over instantly. Replay is untouched; `SeenCache` rejects a repeat whatever
its tag says.

Result, measured on the frames themselves:

| | v14 | v15 |
|---|---|---|
| ack | 17 B | **6 B** |
| beacon | 15 B | **8 B** |
| reliable wrapper | 10 B | **3 B** |
| mesh envelope | 16 B | **12 B** |

### Why one bump and not four

Each of those changes the layout of an existing frame, so each alone would need
its own `PROTOCOL_VERSION`. **THIS IS THE LAST VERSION THE NIBBLE HOLDS** — the
high nibble of the control byte is 4 bits and 15 is its largest value — so
spending four of them on one milestone's worth of byte-trimming was not
available. They land together.

Version 16 costs nothing to plan for and nothing to build today: version 0 has
never been used, so a future build can spell `0` in the nibble and put the real
version in a following byte. A v15 node reading that sees version 0, reports
`wrong_version` and refuses the frame — exactly right, since a version bump is
supposed to stop mixed builds talking. **Do not spend version 0 on anything
else.**

### Bundling: several frames in one packet (`T_BUNDLE`, 0x1A)

```
0x1A  bundle   ctrl | [len uint8 | frame]*
```

A new frame type, so no version bump. One byte of length per frame, which caps
a bundled frame at 255 bytes — not a limitation worth engineering around, since
a frame that size already amortises its own header and the point is the frames
that do not.

| | separate | bundled |
|---|---|---|
| 2 acks | 370 ms | 236 ms |
| 3 acks | 554 ms | 277 ms |
| 4 acks | 739 ms | **328 ms** |

Wired into the ack lane, which is where several small frames genuinely come due
at once. Measured through the real encoder: **four acks are 60 bytes bundled
against 148 separate.**

**The hop count is why this is not simply "pack whatever is queued."** One
envelope carries one hop count, so everything inside a bundle is relayed
together and must be entitled to the same reach. A beacon (cap 0, because the
coverage test depends on beacons never being forwarded) sharing a packet with a
chat message (cap 3) would either flood the beacon and destroy the neighbour
digest, or strand the chat. `bundle()` refuses to mix, and `_hops_for()` reads
the cap off the first frame inside.

**This is the only change in this milestone that helps a saturated radio.** The
2026-08-16 stress bench found that halving the *background* load did not move
the collapse point at all, because collapse is about how many packets are in
the air rather than how big they are. Bundling is the only one that reduces the
count.
