# How traffic is routed and airtime is budgeted

What gets sent at all, what gets acknowledged, what gets relayed, and what each costs. Milestones 3 to 6.

> **Where things live**
> [README](../README.md) · [How it works](how-it-works.md) ·
> [How it is put together](architecture.md) · [What goes on the wire](protocol.md) ·
> [Routing and airtime](routing.md) · [Running it](operations.md) ·
> [Building it](building.md) · [Settings sharing](settings-sharing.md) ·
> [Plain-language guide](mesh-explained.html) · [Range test results](../results)

## Milestone 3: airtime and scale

### Why

Milestone 2 made events small (~63 bytes). It did not decide how *often* to
send them. At SF8/125 kHz the channel is 3125 bps — about **0.2 s of airtime
per position report** — and the milestone 2 bench run produced five events in
eight seconds across two phones, i.e. **~12% channel utilisation with two
nodes**. That scales linearly with node count, so five or six nodes saturates
the link and everything begins queueing behind everything else.

### The rule

A flat "one report per uid per N seconds" is the obvious fix and the wrong
one: it delays exactly the updates that matter, like a unit that has just
started moving. `tx_policy.py` throttles on **significance** instead. A
position report is sent when:

- the uid has not been seen before, or
- it moved **≥ 25 m** since the last report *that was actually sent*, or
- its type, callsign, team or role changed, or
- **30 s** have passed since the last one (heartbeat, so a stationary unit
  doesn't go stale on other maps)

...and is otherwise suppressed. This is the same shape as dynamic reporting in
other SA systems.

The 25 m threshold sits above consumer GPS jitter (~5–10 m), so a stationary
device does not retrigger on noise, and below the point where a marker
visibly jumps at tactical zoom. Distance is measured from the last **sent**
position, not the last seen one — otherwise a unit creeping just under the
threshold each time would never trigger, however far it actually travelled.
There is a test for exactly that.

Measured through the real bridge code paths: **20 stationary reports collapse
to 2 (90% suppressed)**, while a moving unit still sends 9 of 10.

### Chat receipts are dropped (added after the milestone 4 bench)

A **direct** chat provokes two further CoT events from the receiving ATAK,
each carrying the original message's guid as its uid: `b-t-f-d` (delivery) and
`b-t-f-r` (read). A room message provokes none — measured, not assumed.

They are expensive and nearly redundant. Airtime at SF8, from the bench:

| | direct chat | room chat |
|---|---|---|
| message | 0.74–0.86 s | 0.74 s |
| two receipts | 1.54 s | — |
| acks | 0.092 s × peers × 3 | 0.092 s × peers |
| **two nodes** | **2.56 s** | 0.83 s |
| eight nodes | 4.21 s | 1.38 s |

That 2.56 s is what produced the measured 15.3% duty for a single two-node
exchange, against a 0.9–2.2% idle baseline. The receipts are ~60% of it — and
they restate something the bridge already established for 17 bytes, since an
acknowledged message logs `DELIVERED … after N transmission(s)` and appears on
the node screen. Paying 281 bytes on air to repeat that is a bad trade on a
3125 bps channel shared by a squad.

So `tx_policy` drops them, which takes a direct chat from 2.56 s to ~0.83 s.

**The cost is real and visible**, and worth knowing before depending on this:
ATAK's own chat UI will not show delivered/read ticks, so a message that
arrived perfectly well keeps looking unconfirmed *in ATAK*. The delivery
information still exists, just on the bridge screen rather than in the chat
thread. `drop_chat_receipts=False` trades the airtime back for the ticks.

Matched by exact type rather than a `b-t-f-*` prefix: an unrecognised chat
subtype is relayed, because a bigger packet is a much cheaper mistake than
silently dropping an event nobody anticipated. Suppressions are counted as
`receipt=N` on the `tx policy:` status line.

### A direct chat is only owed an ack by its addressee

An ATAK chat uid carries its own routing —
`GeoChat.<sender-uid>.<destination>.<guid>` — where the destination is the
recipient's uid for a direct message and a room name for a broadcast. A direct
chat therefore now expects an acknowledgement from exactly the node it was
addressed to, instead of from every peer in earshot.

**The reason is that the old behaviour made `UNDELIVERED` mean the wrong
thing:** a chat to Alice was reported undelivered because Bob — an uninvolved
bystander — stayed quiet, even though Alice had it.

> **Correction (2026-08-17).** This section used to claim a second, smaller
> benefit: "it saves 0.55 s per direct chat at eight nodes, nothing at two."
> **That saving never happened.** Narrowing the expectation changed only which
> acks the *sender counted*; every node in earshot went on acking, because
> nothing on the wire told them not to — and it could not, since the wrapper
> carries an origin and a message id and the addressee is inside the frame,
> which is not decoded until after the ack has been queued. The airtime figure
> was written from the design intent and never measured. The saving is real now
> that the addressee is on the wire (`T_RELIABLE_TO`, see
> [protocol](protocol.md)), and it is larger than the figure claimed: those acks
> are the uncancellable kind, so a direct chat was paying **15 of them at 16
> nodes — about twice the airtime of the message itself.**

The ATAK-uid-to-node map is **learned from traffic, never advertised**:

- **From reliable frames**, exactly. Such a frame carries its sender's node id,
  and a chat uid carries its sender's ATAK uid.
- **From self-SA, by callsign**, as a fallback. Position reports are
  unacknowledged and so carry no sender node id, and correlating a device's own
  position report against the callsign in its beacon is the only handle
  available before that peer has ever chatted us. Ambiguous callsigns are
  skipped rather than guessed.

Advertising the uid in the beacon was the obvious alternative and is the wrong
trade: ~26 bytes in every beacon forever, roughly +0.4% duty per node, against
~7 bytes on the direct chats that actually need it.

**An unknown addressee falls back to expecting every peer**, which is exactly
the previous behaviour — so the map is an optimisation and never something
correctness depends on. Room messages fall into the same path, because a room
name never matches a node.

### Still open on airtime

Acks still cost 0.092 s per peer per broadcast, which is now the dominant
scaling term for room messages. And the chat message body itself is untouched:
959 bytes of XML compresses to 270 on air, where the actual information —
sender, recipient, timestamp and a line of text — is well under 100 bytes.
A compact encoding for `b-t-f` is the same trick milestone 2 played on
position reports, which went from 705 bytes to 63.

**The position side of this was addressed in milestone 5**, which capped
sustained movement and slowed the beacon. Both remaining items above — ack
cost per peer, and the uncompressed chat body — are untouched and are the
dominant terms once positions are no longer the biggest line.

> **Both items above have since been closed, and the paragraph is kept as
> written (2026-08-17).**
>
> - **The chat body** went onto the compact path at `PROTOCOL_VERSION 6` — see
>   [protocol](protocol.md). It is no longer the uncompressed 270 B described here.
> - **"0.092 s per peer" is no longer how a broadcast is priced.** A message
>   that any single node may answer carries `ANY_ACK_BIT`, and receivers cancel
>   their own ack on hearing somebody else's — so a room message costs about
>   **one** ack regardless of mesh size. The per-peer cost survived only on
>   direct chats, whose acks are deliberately uncancellable, and that is what
>   `T_RELIABLE_TO` closed.

### What is never throttled

Only CoT "atoms" (`a-*` types carrying a position) are eligible, because for
those a newer report always supersedes an older one. **Chat, drawings, routes
and waypoints pass through untouched** — they are user-initiated and one-shot,
so there is no later event to supersede a dropped one and suppressing one
loses information permanently. A position event with unparseable coordinates
is also passed through rather than suppressed, since the distance rule cannot
be evaluated.

### Echo suppression

Every event handed to local ATAK is remembered by `(uid, time)` for 120 s. If
ATAK ever reflects one back, the policy recognises it and refuses to put it
back on the air. No evidence has yet been seen of ATAK doing this, but the
failure mode is a feedback loop that amplifies with every node on the mesh,
and the guard costs one dictionary lookup. The bridge's own `t-x-c-t`
keepalive (uid `rns-bridge-ping`) is dropped for the same reason.

`decide()` **fails open**: a policy bug costs airtime, where failing closed
would silently take the bridge off the air.

### Reading it on the bench

```
[cot-bridge] status: rnode online=True txb=… rxb=… duty=3.2% | …
[cot-bridge] tx policy: sent=2 suppressed=18 (90%) [rate=18 echo=0 keepalive=0]
```

`duty` is measured from the interface's own byte counter over the status
interval — actual airtime consumed, not an estimate from the policy's own
bookkeeping. Above ~30% the channel starts queueing behind itself. Individual
decisions are logged at debug level with the uid and reason, which is also how
an echo loop would become visible.

Tunables are module constants at the top of `tx_policy.py`
(`DEFAULT_HEARTBEAT_S`, `DEFAULT_MIN_DISTANCE_M`, `DEFAULT_ECHO_TTL_S`).

### Bench result (2026-07-31)

Every branch of the policy was observed against real ATAK traffic, with duty
cycle at 0.9–2.2%:

```
sending   ANDROID-2222... (a-f-G-U-C): new contact
suppressed ANDROID-2222... (a-f-G-U-C): rate limited (5s ago, moved 0m)
sending   ANDROID-2222... (a-f-G-U-C): heartbeat
tx policy: sent=3 suppressed=2 (40%) [rate=2 echo=0 keepalive=0]
```

The run also produced `moved 12044711m` — a 12,000 km "move" between
consecutive reports, which is a GPS acquisition (a null or last-known fix
replaced by a real one), not travel. The policy handled it correctly by
sending, but the log gave no way to tell what had happened without guessing.
Implausible jumps (over 100 km) now print both coordinates and flag the likely
cause, so the next one explains itself.

**Confirmed 2026-07-31, and the improved logging is what confirmed it.** ATAK
does report exactly `0,0` before GPS lock. Both phones, independently:

```
Pixel  : moved 15144km (0.00000,0.00000 -> 20.90900,-140.58292 -- GPS acquisition?)
OnePlus: moved 15144km (0.00000,0.00000 -> 20.90898,-140.58292 -- GPS acquisition?)
```

So a device that starts without a fix relays a position at null island, and
peers show the contact there until the real fix arrives. **Decision still
open:** suppressing positions at exactly `(0, 0)` would cost a genuinely
unlocated device any presence on other maps at all, which is arguably the
honest outcome — an absent contact is a known unknown, whereas a contact in
the Gulf of Guinea is a confident lie.

## The node list screen

`MainActivity` shows live bridge state instead of the placeholder it used to:
this node's callsign and id, whether the RNode and ATAK are connected, current
airtime, any message that failed to get through (milestone 4), and a list of
peers marked connected or disconnected.

Node identity comes from the **device**, never from ATAK — see below for why
that matters.

**What "connected" actually means here matters.** It is not read from
configuration — it means a beacon from that device arrived within the last
180 s (three beacon intervals), over the same RNS destination real CoT uses. So a
green dot is positive evidence that a packet crossed the air from that node,
along exactly the path CoT depends on.

Disconnected peers stay listed (greyed, with an age) for 10 minutes rather than
vanishing, because "who fell off?" is the question the screen exists to answer.
Signal strength is shown only for connected peers — a last-known RSSI printed
next to a dead node reads as though the link were fine.

Two supporting changes were needed to make this useful:

- **Node ids are now persisted** (`~/.atakrns_node_id`, which on Android is the
  app's private directory). They used to be `os.urandom(3)` per process, so
  every app restart looked like a brand new node while the old id lingered as a
  ghost — exactly what was observed on the bench after a reinstall. Still
  random rather than derived from a device serial: it needs to be stable and
  distinguishable, not identifying.
- **Beacons carry a device-derived name** (`Build.MANUFACTURER`/`MODEL`, so
  "Pixel 7" or "OnePlus CPH2451"), passed from Kotlin via `set_node_name()`
  before `start()`. **This, not the ATAK callsign, is a node's identity.**

  That distinction is the point of the design. When ATAK is the thing that has
  gone wrong — not running, not connected, not emitting CoT — you still need to
  know which physical radio you are talking to in order to confirm the link is
  fine. An identity sourced from ATAK disappears exactly when you most need it.
  So the device name is always present, and the node list stays fully
  meaningful with ATAK absent from both ends.

- **The ATAK callsign is carried too, but strictly as secondary
  correlation** — shown in brackets after the device name, useful for matching
  a node against what's on the map, and simply absent when ATAK isn't running.
  The bridge learns its own callsign from outgoing self-SA, identified by the
  presence of `<takv>`: that element describes the *sending application*, so it
  appears on a device's own position reports and not on markers the user places
  for other things. Without that check a node would happily advertise somebody
  else's callsign.

  Once learned, both names are retained rather than blanked when a beacon
  arrives without them, so the list doesn't flicker when ATAK restarts.

  Both fields are appended after the sequence number, so an older build reads
  node id and sequence and ignores the rest — the same append-only-at-the-end
  discipline as `F_ACCESS` in the codec. A beacon carrying both is 36 bytes
  (55 on air), about 0.25% duty cycle per node at the 60 s interval.

Status is *pulled* from Python via Chaquopy (`rns_bridge.get_status()` returns
JSON) rather than pushed. The bridge has no reason to know a UI exists: it runs
identically whether or not the Activity is alive, which is the entire point of
it being a foreground service. Polling stops when the screen is not visible.

**Verified on hardware 2026-07-31**, with ATAK deliberately not running on
either phone:

```
Pixel  : LINK OK: heard peer OnePlus KB2007  seq=1 frame=28B rssi=-31 dBm snr=14.0 dB
OnePlus: LINK OK: heard peer Google Pixel 7  seq=4 frame=28B rssi=-33 dBm snr=12.75 dB
         status: ... duty=0.4% | peers: Google Pixel 7 UP (15s ago)
```

Both nodes identify each other by device, with no ATAK involved anywhere —
which was the point. Beacons cost 0.4–0.8% duty cycle.

**One-time id change on upgrade:** builds before this one generated the node id
per process, so the first run of a persisting build mints a new one and the old
id lingers as a disconnected peer until it ages out. That is the retention
behaviour working, not a bug. Ids are stable from that point on — though note
they live in the app's private data directory, so a full uninstall (as opposed
to `adb install -r`) resets them.

## Milestone 4: acknowledged delivery

### Why

Positions are safe on a lossy link because they self-heal: ATAK emits another
one seconds later, so a dropped packet costs nothing. Nothing else has that
property. **Chat, drawings, routes and waypoints are one-shot and
user-initiated** — no later event supersedes them, so a single lost packet lost
the message permanently and told neither user. A message that fails to arrive
is bad; a message that fails to arrive and *looks sent* is worse.

That "looks sent" part is the real defect, and it is what this milestone fixes
first.

### Why not `Link` + `Resource`

The earlier plan of record (see "Candidates for later milestones") was a second
`SINGLE` destination using RNS's own `Link` and `Resource`, which would supply
acknowledgement, sequencing and compression for free. Two findings moved it:

- **RNS's acknowledgement machinery is structurally unavailable on the current
  destination**, for exactly the reason `Resource` is: `Link.__init__`
  (`Link.py:231`) rejects any non-`SINGLE` destination, and `Packet.prove()`
  requires `destination.identity.prv`. A `PLAIN` destination cannot hold an
  identity, so there is nothing to sign a proof with.
- **A `Link` is the wrong shape for this radio.** It is point-to-point, so the
  same chat to N peers costs N times the payload airtime, plus handshakes,
  identity announces and per-link keepalives, on a shared 3125 bps channel.

Broadcast plus selective ARQ sends the payload **once** regardless of how many
peers are listening, and collects small acknowledgements:

| design | 284-byte chat, 2 peers | scaling |
|---|---|---|
| broadcast + ack | 0.78 s payload + 2 × 0.09 s ack ≈ **0.96 s** | +0.09 s per peer |
| `Link` per peer | ~1.8 s of payload alone, plus handshakes and announces | +0.78 s per peer |

It also reuses the peer table the beacon already maintains, introduces no new
RNS concepts, and keeps everything desktop-testable.

### What gets acknowledged

Exactly `not summary.is_position` — **the same predicate `tx_policy` uses to
exempt traffic from throttling.** That is deliberate: one property of an event,
"does a later event supersede this one?", drives both decisions, rather than
two lists that can quietly drift apart. Positions stay fire-and-forget;
acknowledging them would multiply airtime for no gain. Beacons stay
unacknowledged too — they *are* the liveness probe.

### Wire format

Two frame types added to the **existing** version-1 namespace:

```
0x14  reliable wrapper   ctrl | msg_id uint16 | lstr(origin) | <complete inner frame>
0x15  acknowledgement    ctrl | lstr(origin) | msg_id uint16 | lstr(acker)      = 17 bytes
```

`origin` is carried because every frame here is broadcast: an acknowledgement
has to name whose message it answers, and `msg_id` alone collides across
senders.

The wrapper sits **inside** fragmentation, so the receive path is
`Reassembler.offer()` → unwrap → ack → `decode()`, and `decode()` itself is
unchanged. The consequence is that a lost fragment costs a retransmission of
the whole message rather than the missing piece — accepted, because selective
retry needs a per-fragment bitmap and the traffic this protects fits in one
packet anyway.

**No `PROTOCOL_VERSION` bump**, on purpose. An older build hits `decode()`'s
existing `unsupported frame type` branch and drops the frame with a log line,
so a mixed-version bench degrades to "the old node never receives chat and
never acks, and the new node reports it undelivered" — visible and
diagnosable. Bumping the version would have broken positions across the same
gap, which is strictly worse.

### The rules

- **Four transmissions, then give up** (~16 s for a chat-sized message). The
  retry interval derives from the frame's own airtime rather than a constant —
  `max(2.0, airtime × 2 + 1.0)`, × 1.5 per attempt, plus jitter.
- **Expected acknowledgers are snapshotted at send time.** Peers that appear
  mid-flight are not added, or a message could never complete on a network
  anyone is joining.
- **With no peers heard from, nothing is tracked and nothing is retried.**
  Retrying into a room known to be empty is pure airtime waste, and running
  solo is the common case on a bench. The message still goes out once; it is
  just not reported as a failure, because "nobody acknowledged" is not news
  when nobody is there.
- **Duplicates are suppressed but still acknowledged.** A duplicate means our
  previous ack is what went missing, so staying silent guarantees another
  retransmission.
- **Acks jump the queue.** `_ack_queue` is drained ahead of `_tx_queue`; an ack
  stuck behind a multi-fragment message arrives after the sender's retry timer
  has already fired, which lengthens the queue, which delays the next ack.
  Receivers also jitter acks by up to 1 s so a squad does not key up in unison.
- **ARQ must not amplify congestion.** Retries pause entirely while the TX
  queue is at least half full. Skipping the whole tick rather than individual
  sends matters: `tick()` is what consumes an attempt, so a message must never
  burn one of its four tries on a transmission that never happened.

### What the user sees

`MainActivity` grows a MESSAGES section above the node list, hidden when there
is nothing to say, listing failures first:

```
✕ Not delivered
   no reply from OnePlus KB2007  ·  4 tries  ·  18s ago
   b-t-f GeoChat.ANDROID-352214.All Chat Rooms.a1b2
```

Peers are named by device, not by ATAK callsign, matching every other surface.
`rns_bridge` logs the same events at NOTICE (`DELIVERED`) and ERROR
(`UNDELIVERED`).

### Bench result (2026-07-31)

**Acknowledged delivery works end to end.** A GeoChat to All Chat Rooms, on
the sending phone:

```
CotTakServerBridge  Captured CoT event, 959 bytes
[cot-bridge] sending GeoChat.ANDROID-012345….All Chat Rooms.f49b9a5a… (b-t-f): not a position report
[cot-codec] fallback: unknown <detail> child
[cot-bridge] b-t-f GeoChat.…All Chat Rooms.f49… needs an ack from OnePlus KB2007 (msg 27446)
[cot-bridge] 959 bytes CoT -> 1 packet(s), 270 bytes on air
[cot-bridge] DELIVERED b-t-f GeoChat.…All Chat Rooms.f49… to OnePlus KB2007 after 1 transmission(s)
```

and on the receiving phone, the other half:

```
[cot-bridge] received 270 bytes over RNS -> 903 bytes of CoT for ATAK
CotTakServerBridge  Re-injected CoT event to ATAK, 903 bytes
[cot-bridge] transmitted 17 bytes (interface txb 592 -> 628)
```

That 17-byte transmission is the acknowledgement, at the size the design
predicted. 959 bytes of chat crossed as 270 (3.6x, via the zlib path and its
preset dictionary), delivered and confirmed in **1.7 s** — which includes up
to 1 s of deliberate ack jitter, so most of the elapsed time is a design
choice rather than the link.

**The USB failure path was also exercised, by accident and then on purpose.**
Mid-run the OnePlus's port failed with the same `USB get_status request
failed` seen before:

```
UsbSerialBridge  RNode link down (serial read failed: USB get_status request failed) — dropped Python connection so RNS reconnects
[Error] The interface RNodeInterface[rnode-bridge] experienced an unrecoverable error and is now offline.
[cot-bridge] status: rnode online=False txb=538 rxb=493 duty=0.0%
```

Compare against the old build's signature for the identical failure:
`online=True`, `txb` climbing 862 → 1889, `rxb` frozen. Here **`txb` stayed at
538 for the whole outage** — nothing was counted as sent because nothing was
accepted, which is the fix working at the counter level rather than just in
the log text. On replug it recovered unattended in ~30 s (`USB device attached`
→ `RNode serial port open` → `Reconnected port` → `LINK OK`) with no app
restart and no permission prompt.

### Second bench run, mesh disabled (2026-07-31 15:08)

With ATAK's mesh outputs turned off, the server connection is the only path CoT
can take — so this run measures the bridge and nothing else. Both addressing
modes, both directions, all delivered:

| direction | uid | CoT | on air | result |
|---|---|---|---|---|
| Pixel → OnePlus | `…ANDROID-fedcb…` (direct) | 1047 | 318 | delivered, 1 tx |
| Pixel → OnePlus | `…All Chat Rooms…` | 962 | 270 | delivered, 1 tx |
| OnePlus → Pixel | `…ANDROID-012345…` (direct) | 1047 | 315 | delivered, 1 tx |
| OnePlus → Pixel | `…All Chat Rooms…` | 960 | 270 | delivered, 1 tx |

**Direct chat reaches the bridge with mesh off**, which is what the endpoint
substitution was supposed to buy and had never been observed doing. Combined
with the room result, chat transport is fully settled.

**ATAK emits chat receipts as CoT, and they cross the bridge too.** Receiving a
`b-t-f` produced two further events from the receiving ATAK, both carrying the
*original message's* guid as their uid:

```
b-t-f-d 1b9c5f84-…   delivery receipt
b-t-f-r 1b9c5f84-…   read receipt
```

Neither is a position report, so both take the acknowledged path in turn. **One
chat message therefore costs three acknowledged exchanges, not one** — roughly
850 bytes of payload plus three acks. Duty cycle hit **15.3%** during the
exchange against a 0.9–2.2% idle baseline. Comfortable for two nodes; worth
knowing before assuming chat is cheap at squad scale.

**Retry and duplicate suppression both fired for real**, on both devices —
the first time either has been exercised outside the test suite:

```
OnePlus: retransmitting b-t-f-r 1b9c5f84… (msg 7851, attempt 2), still waiting on Google Pixel 7
OnePlus: retransmitting b-t-f-r 1b9c5f84… (msg 7851, attempt 3), still waiting on Google Pixel 7
OnePlus: DELIVERED b-t-f-r 1b9c5f84… to Google Pixel 7 after 3 transmission(s)
Pixel:   duplicate of message 7851 from OnePlus KB2007: acked again, not re-injected
```

That last line is the "acknowledge duplicates anyway" rule earning its place:
the Pixel already had the message, so it did not hand ATAK a second copy, but
it did ack again — and without that second ack the sender would have kept
retransmitting to a peer that already had it.

**But the retries were avoidable, and the cause was a bad constant of mine.**
The ack jitter was a flat 1 s — longer than the 0.78 s the message itself
occupies on air — and on a channel busy at ~15% the ack lost the race to the
sender's retry timer by about 0.2 s, twice. The comment on `_ACK_JITTER_S`
asserted it was "comfortably inside" the retry floor and "never provokes the
retry it exists to prevent"; the bench disproved both halves. The window is now
scaled by connected peer count (`_ACK_SLOT_S` per peer, capped at 1 s), so a
single peer waits ~0.15 s and the collision case it guards against — which
cannot occur with one peer at all — still gets its spread at squad scale.

Positions remained unacknowledged throughout: `received 62 bytes over RNS ->
529 bytes of CoT` at 15:10:09 with no ack following it, against every chat
frame producing one.

### Notes from the first run

Two things it exposed that are worth knowing:

- **`Python connected to RNode bridge` is logged twice per connect.** Not a
  fault: `_make_rnode_serial()` opens a probe connection first (so a cold
  start doesn't race Kotlin's `ServerSocket`) and then the real one. The probe
  closing is what produces `Python end of RNode bridge closed`, which is
  correctly *not* treated as a radio failure.
- **RNS retries its own reconnect every ~7 s during an outage**, and each
  attempt connects, fails its first write with `RNode port is not open`, and
  goes offline again. Noisy, but exactly the intended behaviour — the write
  failing is what keeps RNS from believing it is online.

### Still needs hardware verification

In particular:

1. ~~That a real ATAK GeoChat crosses at all.~~ **RESOLVED 2026-07-31 —
   including "All Chat Rooms", acknowledged end to end.** See "Bench result"
   below.

   One piece of speculation recorded here earlier deserves an explicit
   correction, since it would have sent someone chasing a bug that does not
   exist. When an earlier run showed no chat reaching the bridge, this section
   argued that room broadcasts probably do not take the server connection,
   while direct chat does because `cot_codec` substitutes `contact/@endpoint`
   with `*:-1:stcp`. **The first half is simply wrong** — a room broadcast
   (`GeoChat.<uid>.All Chat Rooms.<guid>`) reaches the bridge and crosses the
   link exactly like any other event. The earlier run just had no chat in it.
   The endpoint substitution is still a good thing and still makes contacts
   learned through the bridge addressable back through it; it is just not the
   dividing line it was made out to be.
2. ~~That the give-up path reports correctly.~~ **RESOLVED 2026-07-31 15:17.**
   With the far RNode unplugged and a chat sent inside the 45 s window, the
   sender reached `reliable: sent=7 delivered=6 undelivered=1 retries=4` — one
   message transmitted four times, unacknowledged, and given up on. Every
   branch of milestone 4 has now fired on hardware.

   Note the timing constraint when repeating this: a peer counts as connected
   for three beacon intervals, so the chat has to go out within that window of
   pulling the radio. Later than that and the sender correctly logs
   `no peers heard from` and does not retry at all — right behaviour, but it
   looks like a failed test. **That window was ~45 s when this run was made
   and is ~180 s since milestone 5 slowed the beacon**, so a repeat of this
   test has much more slack than the original did.
3. ~~That positions still take the unacknowledged path.~~ **RESOLVED** — see
   the second bench run above.
4. ~~Ack overhead in practice.~~ **Measured, and higher than predicted.** Not
   because of the acks — those are 17 bytes as designed — but because ATAK's
   own delivery and read receipts are themselves CoT events that take the
   acknowledged path, tripling the cost of a chat message. 15.3% duty during
   an exchange.
5. **Whether the scaled ack jitter actually removes the spurious retries.**
   The fix is derived from one bench observation; the check is that a chat
   exchange on an idle-ish channel shows `retries=0` rather than `retries=2`.
   **Needs a fresh install** — see below.
6. **Whether the beacon stays quiet while the radio is down.** Same caveat.

**Check the build before reading a bench run.** The 15:17 run was made by a
process that had been alive since before two of the fixes were written, so it
still logged `beacon sent` immediately after `rnode online=False` — which
looks like the beacon guard failing when it is simply absent from that binary.
The tell is cheap: a build with the guard logs `beacon NOT sent (seq=N): RNode
is offline` during an outage, and the Android process id stays constant in
logcat across a session unless the app was actually reinstalled.

## Milestone 5: airtime at squad scale

**Built and desktop-tested. NOT yet on the radio.**

### Why

Milestones 3 and 4 were both sized against a two-node bench, and two of the
constants they set do not survive contact with a squad. Estimating from the
bench figures — SF8/125 kHz is 3125 bps, so ~0.2 s of airtime per position
report and ~0.12 s per beacon — with 16 nodes, 14 of them walking:

| | Before | After |
|---|---|---|
| Positions | ~17% | ~5% |
| Beacons | ~13% | ~3% |
| **Total duty** | **~30%** | **~8%** |

The beacon number is the one that surprises. It looks negligible at two nodes
(~1.6%) because the interval is *per node*, so its cost is linear in mesh size
while its value is not. At 16 nodes a fixed 15 s probe was costing as much as
all the position traffic it exists to protect.

The working budget is ~10% duty. That is an engineering figure, not a
regulatory one: it sits below the point where uncoordinated transmissions start
losing more to collisions than they carry, and leaves headroom for chat, acks
and retries. **The radio is on 915 MHz. On EU 868 the legal ceiling may be 1%,
which would invalidate every number above.**

### The changes

**1. Movement is capped, but starting to move is not.** Milestone 3 sent on
every 25 m of travel, which at walking pace is a report every ~18 s. That
stream is now capped to one per minute — but the *first* qualifying move after
a node has been still is still sent immediately, unthrottled. This keeps the
property milestone 3 was built for (the module docstring calls a plain fixed
interval wrong because "it delays exactly the updates that matter — a unit that
just started moving") while dropping the part that did not scale.

Distinguishing the two needs state, and the state cannot be inferred from
distance: between two sends of a walking node, reports legitimately show under
25 m of travel simply because little time has passed. "Under the threshold"
means "not yet", not "stopped". So motion is remembered for a settle period
instead, refreshed by every qualifying report including the ones that get
dropped.

**`DEFAULT_SETTLE_S` must exceed `DEFAULT_MOVING_INTERVAL_S`, and this is a
trap.** Set equal, both expire in the same instant: a node held quiet for a
full interval has its motion lapse exactly as the cap releases, so the next
send is misread as a fresh onset — and then every capped report becomes an
onset forever after, silently undoing the whole change. Caught by a desktop
test, not by inspection. 90 s against 60 s.

**2. The beacon drops to 60 s.** Straightforward, except for what it drags
with it — see the cost below.

**3. Everything correlated is jittered.** Two cases, for different reasons:

- **The beacon had no jitter at all**, which is a defect independent of scale.
  It is the one thing on this link every node sends on a fixed timer, so two
  nodes whose beacons drift into alignment have nothing to push them apart and
  collide on *every* interval, indefinitely. Now re-randomised each period,
  applied around a fixed schedule rather than added to the sleep so the offsets
  cancel instead of accumulating into drift. A fixed schedule brings its own
  hazard on a phone: after the process is suspended (doze, screen-off
  governor) it would "catch up" by firing a burst of beacons for time that has
  already passed — and every node waking together would do it at once, which is
  precisely the correlated key-up being designed out. Falling more than one
  interval behind resyncs the schedule instead. Simulated: 500 beacons hold a
  mean gap of 60.00 s with no accumulating drift, and a 30-minute stall is
  followed by normal spacing rather than a burst.
- **Movement onsets are the one CoT event a whole squad emits at once** — a
  patrol stepping off is a single real-world event that keys up 16 radios
  together. They now go out at a random moment inside a peer-count-scaled
  window, through the same release-time lane the acks already use.

**The onset jitter is a mitigation, not a fix, and should not be mistaken for
one.** 16 nodes at ~0.2 s is 3.2 s of airtime; a window wide enough to fully
de-conflict that would be tens of seconds, which is longer than the report
stays useful. The 5 s cap turns a certain pile-up into roughly a third of the
channel busy during the burst. The RNode firmware's own CSMA backoff handles
the rest, and a collided onset is recovered by the next scheduled send anyway.

### What it costs

**Markers in motion lag by up to a minute** — ~85 m at walking pace, against
~25 m before. For a vehicle it would be ~900 m, which is likely too much; a
speed-aware interval is the obvious answer and is not built.

**Peer loss takes 180 s to detect, up from 45 s.** `_PEER_CONNECTED_S` is
three beacon intervals and *must* stay a multiple of at least two — any window
shorter than the interval plus its jitter expires before the next beacon is
due, so every peer would flap connected/disconnected on every cycle. Pinning it
to an absolute value is the tempting wrong answer here. The cost lands on
milestone 4: a node that walked away stays in the ack-expectation snapshot for
up to three minutes, so an UNDELIVERED verdict for it takes that much longer.
Correct, just slower.

**Milestone 3's significance logic is largely bypassed at these settings.**
With the cap and the heartbeat both at 60 s, walking and stationary nodes send
at identical rates and the 25 m threshold only detects onsets. Airtime becomes
very predictable — near enough 16 positions plus 16 beacons per minute — which
is worth having at this scale, but it is effectively fixed-rate reporting with
an onset exception. Deliberate, not accidental.

### Still needs hardware verification

1. **All of the above.** Every figure in the table is scaled arithmetic from
   two-node measurements. A two-node bench cannot produce a 16-node duty cycle;
   what it *can* confirm is that the per-node send rate matches the design,
   after which the duty figures follow.
2. **That the beacon jitter drops or duplicates nothing.** Sequence numbers
   make this directly checkable — gaps mean loss, repeats mean a scheduling
   bug.
3. **Whether onsets are actually rare in the field.** The whole design assumes
   real patrol movement produces occasional onsets, not constant ones. If
   stop-start walking re-triggers them every couple of minutes per node, the
   onset traffic is a larger share than estimated and the settle period needs
   to grow. The `[onset=N moving=N heartbeat=N]` counters on the `tx policy:`
   status line are there to answer exactly this.
4. **`airtime_limit_short` / `airtime_limit_long`.** `RNodeInterface` accepts
   both and pushes them to the firmware, and reports `r_airtime_short` /
   `r_airtime_long` back. `_build_rnode_interface()` sets neither. A
   firmware-enforced ceiling would be a backstop independent of any policy bug,
   and the reported values would make duty cycle *measured* rather than
   estimated — which would retire most of item 1.

## Milestone 6: an authenticated mesh (steps 1-4 of 5 done)

### Why

Everything through milestone 5 went out on one `PLAIN` destination, which is
unauthenticated and unencrypted **by design** — that is what `PLAIN` means.
Anyone with an RNode on 915 MHz and the destination hash could read every
position and inject events that ATAK would render as real. `_NODE_ID` is a
random value in a file with nothing binding a frame to it, so milestone 4's
acknowledgement accounting could be forged by anyone in earshot.

`PLAIN` is also single-hop and always will be: `Transport.py:1138` excludes it
from the path table and `Transport.py:1565` never injects plain broadcasts into
transport. The alternative RNS offers is a `SINGLE` destination, which *is*
routed multi-hop — but it costs a fixed ~80 bytes of crypto per message **per
recipient**, which for 16 nodes reporting once a minute is ~123% duty. It does
not fit in the channel.

So this milestone keeps the cheap `PLAIN` broadcast — one transmission reaches
every node in earshot — and puts our own encryption and our own frame identity
on top. Multi-hop follows in step 3, on the frame identity step 1 establishes.

### The relay budget, which is what shapes the rest

Accurate LoRa time-on-air for SF8/125 kHz/CR4-5 — preamble and symbol rounding
included, *not* payload÷bitrate, which is what the milestone 5 figures used and
which understates air by ~28%:

| 16 nodes, 1 position each per minute, SF8/125 kHz | s/min | duty |
|---|---|---|
| Milestone 5 as shipped, plaintext | 6.90 | 11.5% |
| **Step 1: encrypted, no relaying** | **8.21** | **13.7%** |
| …plus 4 relays/min | 9.40 | 15.7% |
| …plus 16 relays/min (one per position) | 12.97 | 21.6% |
| Naive flood, every node relays everything | 76.15 | **126.9%** |

Naive flooding misses the channel by a factor of thirteen, and even one relay
per position does not fit. **The design must default to zero relays and make
each one earn its airtime** — that constraint, not the crypto, is what dictates
everything in steps 2–5.

### Every duty figure here assumes SF8 / 125 kHz

Stated once, and it qualifies every percentage in the milestone 6 sections.
The radio setting moves these numbers more than any protocol work in this
repository does. The same traffic — step 2's measured mix of one 96 B keyframe
and four 58 B deltas per node per minute — across Meshtastic's own presets:

| preset | SF / BW | our 16-node traffic |
|---|---|---|
| Short Turbo | 7 / 500 kHz | 0.8% |
| Short Fast | 7 / 250 kHz | 1.6% |
| **Short Slow** | **8 / 250 kHz** | **2.9%** ← what step 5 proposes |
| Medium Fast | 9 / 250 kHz | 5.4% |
| **rtak today** | **8 / 125 kHz** | **5.7%** |
| Long Turbo | 11 / 500 kHz | 13.0% |
| **Long Fast** — Meshtastic's default | **11 / 250 kHz** | **18.0%** |
| Long Slow | 12 / 125 kHz | 112.8% |

Two things follow, and the second is easy to miss. **Step 5's "faster preset"
is not an exotic choice — it is precisely Meshtastic's Short Slow.** And
**comparing our 5.8% against a Meshtastic mesh running Long Fast compares
radio settings, not protocols**: our own traffic at their default would be
18.0%, well past any workable ceiling. They default slow because they optimise
for range, which is the trade step 5 has to measure before taking.

Meshtastic's "managed flood routing" is the same shape as what steps 3–5 will
build (dedup on sender+id, hop limit, randomized rebroadcast delay, overheard
rebroadcasts as implicit acks) and even derives its nonce the same way. It
relays *unconditionally*, then quiets the result by hand with `CLIENT_MUTE` /
`ROUTER` / `REPEATER` roles. It gets away with that because its default is one
position per 15 minutes; the same algorithm at our 1-per-minute rate is ~228%
duty. Worth stealing from it: rebroadcast delay weighted by SNR, so distant
nodes transmit first, which approximates a coverage test with no neighbour
table at all.

### Honest position against Meshtastic (checked 2026-08-05)

The comparison above was written when milestone 6 was designed and two of its
implications had gone stale by the time step 3 landed. Recorded here in full,
including the parts that do not flatter this project, because the README is the
project's memory and a comparison that quietly overstates its position is worse
than none — it will be believed later, when the context is gone.

**Their ATAK integration covers more CoT than this codec does.** The plugin's
`atak.proto` really does model only PLI and GeoChat, which is what the milestone
6 framing assumed. But [TAKPacket-SDK](https://github.com/meshtastic/TAKPacket-SDK)
— 203 commits, five language implementations, 1,000+ cross-platform tests —
converts markers, tactical graphics (circles, polygons, ellipses,
telestrations), routes, CASEVAC 9-lines, emergency alerts, ADS-B tracks, delete
events and chat with receipts. They are **ahead of us on coverage**, and drawings
and routes are exactly what we have not built.

**On size the result is split, and the split is instructive.** Payload to
payload, excluding both projects' packet headers:

| | TAKPacket-SDK | rtak | |
|---|---|---|---|
| **position** | 98 B | **~30 B** | **ours, 3.3x** |
| chat | 80 B | 114 B | theirs, 1.4x |
| marker | 81 B | 167 B | **theirs, 2.1x** |
| circle / rectangle / route | 80–101 B | not built | theirs |

**We win on positions because we keep state and they refuse to.** Their SDK is
explicitly stateless — "zero cross-message state, every message independently
decodable" — so a PLI carries the callsign and full coordinates every time.
Milestone 6 step 2's identity binding and delta coding only work *because* the
receiver remembers a keyframe, which is precisely the property they gave up.
That is the right call for a network sending one position per fifteen minutes
and the wrong one at our one per minute; the 3.3x is that trade, not cleverness.

**They win on markers and chat because of one technique we do not use: zstd
against a trained dictionary.** They serialise to protobuf and then compress
with dictionaries trained on real TAK traffic (512 KB for non-aircraft
payloads, level 19). We stop at the structured encoding. Our marker is 167 B of
hand-packed fields; theirs is a comparable structured form *then compressed
against a corpus*. That is the entire gap, and it is a technique we could adopt
on our own encoder independently of their code or their licence — see the
candidates list.

Two smaller ideas worth stealing: they **discard `time` and `start` entirely**
and rebuild them from the receiver's clock (we spend 4 bytes on an absolute
timestamp in every non-session frame), and shape vertices are **zigzag deltas
from the event anchor in two packed columns**, so field framing is paid once
rather than per vertex.

**We are not smaller on the wire, and the reason is instructive.** Our
per-packet overhead is **35 bytes** — 16 for the mesh envelope, 19 for
Reticulum's own packet header — against Meshtastic's 16. A position report is
~57 B on air for us against a likely ~45–60 B for a TAKPacket PLI. Milestone 6
step 2 got the payload from 61 B to 22 B; the transport overhead ate most of
that advantage back. The RNS header is the price of the Reticulum stack and is
not something this project can encode away.

**Their broadcast crypto is worse than recorded here, and is being fixed.**
"No MAC" is right, and it is worse than that: the IV derives from node number
plus packet ID, so an attacker who knows the plaintext can reuse that pair to
forge messages **without holding the PSK at all**. But
[XEdDSA packet signing lands in firmware 2.8.x](https://meshtastic.org/docs/about/overview/encryption/limitations/),
after which unsigned packets from a known node are rejected. **This project's
principal security advantage therefore has a shelf life**, and their per-node
PKI for direct messages (x25519 + AES-CCM, since 2.5) is already ahead of our
group key, which authenticates the group and not the member.

**What survives scrutiny:**

- **Authenticated broadcast today**, for 8 bytes — until 2.8.x ships.
- **Relay selection sized for 1 position/minute.** Their managed flood at our
  reporting rate is ~228% duty; our coverage test collapses to zero relays in a
  single cell. Still unbenched, so this is a design claim and not a result.
- **Honest undelivered reporting** for one-shot traffic (milestone 4).
- **Reticulum transport diversity** — the same mesh can bridge over TCP or
  packet radio, which Meshtastic does not do.

**What does not:** more CoT types, and "smaller frames" as a blanket claim. The
first was true against the plugin's protobuf and is false against their current
tooling. The second is only true where our session state applies — positions,
where we are 3.3x smaller — and false on markers, where we are 2.1x larger for
want of dictionary compression.

### Step 3: relaying

Steps 1 and 2 bought the airtime; this spends it. The whole design is set by
one number. With one position per node per minute at the 39 bytes step 2
measured:

| 16 nodes, one radio cell, SF8/125 kHz | duty |
|---|---|---|
| no relaying (step 2 as benched) | 5.8% |
| **one relay per frame** | **11.0%** |
| every node relays everything | 83.8% |

Naive flooding misses the channel by a factor of fourteen, which was expected.
The row that shaped the design is the middle one: **even a single relay per
frame does not fit** — and in a squad standing together every one of those
relays is pure waste, because everyone already heard the original. So the
default has to be zero relays and each one has to earn its airtime.

That is why the coverage test is not an optimisation to add later. It is what
makes step 3 fit at all.

**Four mechanisms**, in `relay.py`, in the order they matter:

**1. Duplicate suppression.** A frame seen before is never relayed. This
already existed as `mesh.SeenCache`, built for replay defence in step 1 and
keyed on exactly the `(origin, epoch, seq)` that flood identity needs.

**2. Coverage.** Every node advertises an 8-byte Bloom filter of the neighbours
it hears; a receiver relays only when it knows a neighbour missing from the
sender's filter. In one cell nobody can extend coverage, so nothing relays and
the cost collapses to the first row of the table — **flooding costs nothing
until the squad actually splits.** This is MPR selection, as OLSR does it.

A Bloom false positive reads as "you already cover that node", i.e. a *missed*
relay, which the next position repairs. The error direction is the safe one,
which is what makes a filter worth its false positives here.

**3. Delay with passive cancellation, weighted by signal.** A relay is
scheduled rather than sent, and cancelled if the same frame is heard from
anyone else first. The delay is shortest for whoever heard the original
*weakest*, because that node is furthest away and its relay reaches the most
new ground — Meshtastic's trick, and it approximates the coverage test on its
own when digests are stale.

**4. A token bucket**, 1.5 s of relay airtime per node per minute against the
~2.5 s of headroom step 2 left. Deliberately short of it, so relaying cannot
itself be what pushes the channel over.

**A relay does not re-wrap.** `mesh.forward()` passes the ciphertext, the tag
and every authenticated header byte through untouched and changes only the hop
count — which is the entire reason that byte sits outside the AAD. Re-wrapping
would re-encrypt under the relay's own origin and sequence, which would erase
the sender the receiver needs *and* give the frame a new flood identity at
every hop, so duplicate suppression would never fire and it would circulate
forever. A test pins each of those.

It also buys something for free: the original sender hears its own frame come
back, recognises its own origin and drops it. **That is passive acknowledgement
of the hop at zero airtime.**

**Where the neighbour digest rides.** On the beacon, not on a position report.
It is bridge metadata rather than anything ATAK said, and the beacon is the
bridge's own frame — the same reasoning that kept the device name out of the
compact codec in step 2. It also means the coverage test costs nothing on the
frames that actually carry traffic.

But step 2 had quietened the beacon precisely when traffic was flowing, which
would have starved the coverage test of its input. So the beacon now fires for
**three** reasons rather than one:

- **quiet** — nothing else proved the link this interval, the original rule
- **changed** — the set of nodes this one hears is different, and until that is
  advertised nobody will relay for whoever just arrived
- **stale** — at least every 5 minutes regardless, so the digest cannot age out
  in a busy mesh just because something else keeps being on the air

**Hop caps by traffic class**, stamped at the single choke point every
transmission already passes through:

| | cap | |
|---|---|---|
| beacon | **0** | defines the one-hop neighbour set; flooding it would make every node look adjacent to every other and destroy the coverage test |
| ack | 0 | addressed to a node that just transmitted to us, so by definition one hop away |
| position | 2 | replaced within the minute, so it is what the bucket sacrifices first |
| chat, markers, drawings | 3 | one-shot; there is no later event to supersede a dropped one |

> **Two corrections, 2026-10-14.** The ack cap is **3**, not 0, since
> 2026-08-12 (`relay.HOPS_ONESHOT`; the reasoning is in `relay.py`). And "what
> the bucket sacrifices first" was never implemented: `RelayPolicy.decide()`
> charges first-come-first-served, so a position can take the room a chat
> needed. That, and the bucket being too small to ever hold a large message
> (~152 B on Meshtastic Long Fast, ~182 B on an RNode at Long Fast), is an
> Still an open issue.

The beacon cap is enforced **on receipt** as well as when sending, because a
hop count is not covered by the authentication tag — it is the one field an
attacker can edit, which is the price of making it decrementable.

**Fragments are charged as a whole message.** A relay never reassembles — it
forwards fragments blind — but it decides for the whole message at once and
charges the bucket the full estimated airtime on the first fragment. Half a
relayed drawing cannot be reassembled by anyone, so the airtime is spent for
nothing; the refusal has to happen before the first fragment rather than
during the fourth.

**PROTOCOL_VERSION 5.** Not really for the trailing digest field, which an old
decoder ignores, but for what its *absence* would mean: a node with no digest
reads as covering nobody, so a v4 node in a v5 mesh would have every frame it
sends relayed by everyone that hears it — the one thing the budget cannot
absorb.

**A note on the desktop test environment.** The 47 tests covering
authentication, forgery rejection and nonce uniqueness had been *skipping* on
this machine for want of the `cryptography` package, since step 1. They now run
(under a venv) and pass, along with the new relay tests: **335 tests, no
skips.** Worth knowing that "all tests pass" had been quietly weaker than it
sounded for two milestones.

## Milestone 7: what gets sent at all, revisited

Built 2026-08-17. **Desktop-tested only — none of this has been on a radio.**

### The check-in cadence, which is where the airtime actually was

Every node transmitted something every 60 seconds whether or not anything had
happened. At 16 nodes standing still that is **the entire at-rest airtime bill**
— 6.3% of the channel spent saying nothing.

It was 60 seconds because ATAK drops a marker roughly two minutes after the last
position it heard. **So the cadence of a radio protocol was set by a countdown
in somebody else's software**, and every transmission bought nothing but a timer
reset.

That countdown is now the receiver's to set. Three pieces:

- **The receiving bridge extends `stale` on arrival** (`cot_codec._stale_at`),
  telling its own ATAK to hold the marker for `_MARKER_STALE_S`. It costs no
  bytes, needs no agreement between the two ends, and the receiver is the side
  that actually knows whether it can still hear the sender. It never *shortens*
  a longer stale: a sender that deliberately set one keeps it.
- **The bridge withdraws the marker itself** when its own peer table gives up on
  a node (`_withdraw_departed`), re-sending the last position already expired.
  Deliberately not a `t-x-d-d` delete — that is what ATAK sends when a *user*
  deletes something and it removes the item for good, whereas a peer who walks
  back into range should reappear on their next report.
- **`_MARKER_STALE_S` is deliberately longer than `_PEER_CONNECTED_S`**, so the
  two timers cannot race. A marker comes off the map because this bridge decided
  the node is gone, never because a timer in ATAK ran out — only one of those is
  looking at whether the radio can still hear anything.

The beacon and the position heartbeat both move to **90 s** (matched, so a
stationary node makes one transmission per interval rather than two).

| | dead node lingers | at rest, 16 nodes |
|---|---|---|
| before | 1–2 min | 6.8% |
| **after** | **3–4.5 min** | **3.1%** |

**90 s and not the 150 s this first shipped with (2026-08-17).** The stress
bench measured what ATAK's own timer was worth: under load ~5% of frames
between the two phones were lost to collisions, and ATAK gives up after roughly
*one* missed check-in, so it would blink a healthy node off the map about one
time in twenty. 150 s bought 1.9% but cost 5–7.5 minutes to notice a death.
90 s keeps most of the saving and lands inside the old system's **honest**
detection time rather than its optimistic one.

| interval | at rest, 16 nodes | dead node noticed in |
|---|---|---|
| 60 s (the old value) | 4.7% | 2–3 min |
| **90 s** | **3.1%** | **3–4.5 min** |
| 120 s | 2.3% | 4–6 min |
| 150 s | 1.9% | 5–7.5 min |

**The heartbeat did not go away, and the reason is worth recording.** Nothing on
the map depends on it any more, so it looks removable. What still depends on it
is a node that arrives *late*: a phone joining after a stationary peer's last
report has no way to learn where that peer is, and there is no catch-up
mechanism and deliberately none planned. So `DEFAULT_HEARTBEAT_S` is not the
map's refresh rate — it is the worst case for how long a newcomer sees an
incomplete map, and that is what it should be chosen against.

**Today's faster answer is also a lying answer, which makes the trade better
than the table suggests.** Two minutes of tolerance against a check-in every
minute means ATAK gave up after roughly *one* missed transmission. Every bench
so far has been at rssi ~-28 dBm with the radios inches apart, where nothing is
ever lost; at real range, every missed packet would blink a healthy marker off
the map. Deciding this from the peer table instead means waiting for a *run* of
missed check-ins. **This is the knob the range test should retune** — if real
loss is low, one missed check-in is enough and detection halves.

### An ack asks a narrower question than a broadcast

An acknowledgement is the only traffic here with a single destination, and it
already names it: the origin of the message being answered *is* the node the
answer has to reach.

The coverage test asks "do I reach anyone the sender does not?", which for an
ack is the wrong question — it will forward a receipt down a branch of the mesh
nobody in the conversation is on. `relay.covers_target()` asks the narrower one:

- the sender's digest already contains the target → **suppress**, they can reach
  it themselves
- we have the target as a direct neighbour → **relay**, this is the hop that
  delivers it
- the target is a stranger → **no opinion**, fall back to the broadcast test

That last branch is what keeps a three-hop ack moving: the node in the middle
has never heard of either end.

**This is the cheap realisation of what MeshCore buys with source-routed paths,
and it was chosen over building those.** Recording a real path means relays
appending to the frame, which means the path sits outside the authentication tag
(the same reasoning that put `hops` there) — an attacker-editable field added to
the crypto envelope, for a benefit that only appears in a mesh with three or
more cells. The destination was already on the wire; nothing needed adding.

**Worth almost nothing at squad scale, and that was known before it was built.**
In a single radio cell the coverage test already suppresses every relay, so
there is nothing to narrow. It only pays once the group splits across three or
more cells — which this project has never had, and cannot bench with the
hardware on hand.

### What the whole milestone is worth

| | before | after |
|---|---|---|
| 16 nodes standing still | 6.8% | **3.1%** (54% less) |
| 16 nodes moving and talking | 12.4% | **6.6%** (47% less) |

Computed, not measured — but computed with the airtime model milestone 6
checked against real radios to within 2% (8.37 s/min measured against 8.21
predicted), using frame sizes read off the real encoder rather than estimated.

**Do not expect it to raise the ceiling.** The stress bench already ran this
experiment by accident: reverting the beacon from 15 s to 60 s halved background
airtime and *did not move the collapse point at all*. Everything here except
bundling is background load. What this buys is headroom and battery at rest, not
a mesh that survives heavier traffic.
