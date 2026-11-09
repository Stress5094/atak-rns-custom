# Settings sharing: one host pushes radio settings, every phone approves

Plan, 2026-10-20. **Steps 1-4 are built.**

- Step 1, the rules: `settings_share.py`, tests in `test_settings_share.py`
  (real crypto; also pass under the phone's Python 3.9 + cryptography 3.4.8;
  12 deliberately broken rules all caught).
- Step 2, QR: `settings_link.py` (where each setting lives; tested against
  the real bridge setters in `test_settings_link.py`), `SettingsShareUi.kt`
  (Status tab -> "Share settings"), ZXing for showing and scanning, and a
  10 s tick in `PythonRnsService` for scheduled switches and the automatic
  undo. On the OnePlus: approval screen, PIN creation, apply (radio took
  20 dBm live), replay refused, Undo, wrong PIN refused, own QR shown and
  decoded + verified on the laptop. NOT yet done with a real camera scan
  between two phones (the Pixel was off adb); the receive path was driven
  by a debug-only SETTINGS_SCAN broadcast instead.
- **The FM tone profile is not shared yet**: changing it needs an app
  restart, and so would undoing it. A phone drops it with a note.

- Step 3, a host phone on the same WiFi, 2026-10-21: `settings_net.py`
  (tests in `test_settings_net.py`, real sockets on this machine) and its
  wiring in `settings_link.py`; screens in `SettingsShareUi.kt` (host,
  roster, push, a notification for an offer that arrives while ATAK is in
  front). **Not over Reticulum**, see Routes. Verified on the OnePlus with
  two laptop stand-in phones over the real WiFi: the phone hosted, the push
  reached both, the passphrase was left out with the reason (not opted in),
  approve / reject came back to the roster live, and the late joiner got
  revision 1 with the ORIGINAL switch time. Found on the phone and fixed: a
  hosting phone heard its own beacon and offered itself its own settings.
  **Two phones, 2026-10-22** (OnePlus 8T Android 14, Pixel 7 Android 17,
  both with an RNode over Bluetooth, real WiFi): each hosted the other.
  Found by its beacon and connected with nothing typed, both ways; the
  Android 17 phone hosts and receives without any new permission. Accepted
  LATE (after the switch time): applied at once, the RNode took 21 dBm
  ("Radio ok"). Accepted EARLY: waited, then switched at the set time, 5 s
  and 11 s after it (the 10 s tick). Undo by hand reported back to the host's
  roster. The approval screen popped up by itself with the app in front, and
  the notification opened it with the app in the background.
  **Found and fixed: notifications were never shown** on Android 13+ -- the
  app had never asked for the permission, so Android dropped every one
  silently (both phones). `MainActivity` now asks on a fresh start, and
  `notify()` logs when notifications are off.
  **Seen once, not explained:** an offer that arrived while the Pixel's
  screen was off did not pop up when it was woken; a Share settings menu
  opened before the offer was showing instead. The offer was in the menu.
  **NOT verified:** a frequency or preset change, a
  passphrase push between phones, and a trusted host over WiFi.

Step 4 (server) built 2026-10-22, see below. Step 5 not started. Decisions marked **(operator)**
were made by the operator on 2026-10-20 and are not open.

## Why

Every phone keeps its own copy of the settings that must MATCH across the
network, typed by hand: the mesh passphrase, the LoRa preset / frequency /
power, the Meshtastic board's profile (derived from those), the FM tone
profile, and the Coverage tab's model. One typo strands a node, and both ends
look equally dead when keys or frequencies differ. Changing the network's
frequency means walking to every phone.

## What already exists and is reused

- `set_radio_config()` applies and READS BACK every radio parameter;
  `radio.validate()` and `_txpower_max_dbm()` guard it. A pushed setting goes
  through these, never around them.
- `apply_mesh_board_profile()`: the Meshtastic board already takes the app's
  settings only through a screen that shows the change and confirms it.
- `set_mesh_passphrase()` rebuilds the mesh live and returns the key
  fingerprint.
- The WiFi link (`AndroidAutoInterface`) already discovers phones on the same
  network. Each install has an RNS identity (Ed25519 signing, X25519
  encryption).

## The settings bundle

One format, carried by every route (host over WiFi, server, QR):

    {
      "v": 1,
      "host": "<host identity hash>", "host_name": "Host-OnePlus",
      "rev": 7,                       # monotonic per host; older is refused
      "switch_at": 1760990400,        # unix time; absent = on approval
      "settings": {
        "lora_preset": "Medium Slow", "lora_freq_mhz": "906.875",
        "lora_tx_dbm": 22,            # a REQUEST: each radio clamps to its max
        "fm_profile": "fast",
        "coverage": {"fm_mhz": "146.52", "fm_w": "5", "tx_height_m": "1.5"},
        "passphrase": <X25519-encrypted to ONE phone, only if allowed>
      },
      "sig": "<Ed25519 over everything above>"
    }

Only the settings present are changed. ~300-500 B signed, one QR code.

On receipt a phone: checks the signature, refuses `rev` <= the last applied
from that host (no replay or rollback), builds the before/after list, and
either asks (PIN) or applies (trusted host). Applying goes through the same
functions the Radio screen uses.

## Approval: a PIN on every phone (operator)

- Any phone may act as host. What protects a phone is ITS OWN PIN: accepting
  a change is Accept + this phone's PIN.
- The PIN is created on the phone the first time a change arrives. Stored as
  a salted slow hash (PBKDF2), never the PIN.
- 5 wrong tries lock it for 1 minute, doubling after each further run.
- Changing the PIN needs the old one. Resetting a forgotten PIN also clears
  every trusted host, or the reset would be a way round the PIN.

## Trusting a host (operator: optional, per host)

- Marking a host trusted needs the PIN. Trust is bound to the host's KEY, not
  its name, so a renamed phone does not inherit it.
- A trusted host's changes apply without a prompt; the phone then shows what
  changed with an Undo button.
- A trusted host can NEVER change: the PIN, the trust list, or this phone's
  "accept the passphrase over the network" choice. Trust must not be a way
  to lock the holder out.
- A QR code from the host is the quick way to trust it in person: scan,
  enter the PIN.

## The passphrase over the network (operator: an option)

- Host setting "Include the network passphrase in network pushes", OFF by
  default; and a phone-side setting to accept it, also OFF by default.
- When sent it is encrypted to each phone's own key (X25519), so another
  device on the same network cannot read it.
- QR always may carry it: that is in person.
- The approval screen shows a passphrase change in red: a wrong key cuts the
  phone off from everyone.

## Switching: automatic, with an undo by hand (operator)

Changing frequency or preset partitions the network -- phones that switch
cannot hear phones that did not.

- Every push carries `switch_at`, default 2 min ahead. Every phone that
  accepted by then switches at that moment; the host does not wait.
- Stragglers still catch up: the bundle travels over WiFi / the server, which
  a LoRa frequency change does not cut. A phone accepting late switches at
  once.
- **No automatic undo (operator, 2026-10-23).** An applied change stays
  until someone presses Undo on the phone. There was one (hearing no node
  for 300 s after a network-splitting change put the old settings back),
  removed because it counted a node heard on ANY link: a push that left LoRa
  deaf was never undone while FM failover or WiFi still heard someone. Phones
  built before then can still report "reverted"; the rosters still label it.
- The host's roster shows per phone: offered / approved / switched /
  rejected / failed / undone / not reached.
- A phone off WiFi during a change is left on the old settings. It catches up
  when it is next reachable or scans the QR. Expected, and shown on the
  roster as not reached.

## Routes

1. **QR** -- in person, no network. Library: ZXing (fully offline; Google's
   scanner needs Play services and may download on first use).
2. **Host phone on the same WiFi** -- BUILT AS THE FALLBACK, not over RNS:
   every phone runs Reticulum with `enable_transport = Yes`, so an announced
   settings destination would be re-announced onto LoRa by every phone that
   heard it. Instead (`settings_net.py`): a UDP beacon every 3 s to port
   47811 (every interface's broadcast address, so both an ordinary WiFi
   network and the host's own hotspot), and one TCP connection per phone to
   port 47812, JSON lines, phones connecting OUT. Nothing on the wire needs
   to be secret: bundles are signed, revisions only go up, the passphrase is
   sealed per phone. A fake host on the WiFi is an untrusted host (its offers
   need the PIN, and the approval screen shows the host key to compare). A
   phone's line on the roster CAN be forged by anyone on the WiFi: the
   roster is a report, never a decision. Sockets are pinned to the WiFi
   network through a duplicated descriptor (an off-grid router has no
   internet, so Android would otherwise route via mobile data).
3. **Server** -- the same host software running on a Linux machine. Below.

Bluetooth: dropped (operator), QR covers the in-person case.

## The server version

BUILT 2026-10-22 (step 4): `settings_server.py`, the SAME host code a phone
runs (`settings_net.HostServer`, `settings_share.Host`) as a Linux service.
How to run it is in `server/README.md`; `server/package.sh` builds the copy
(the six Python files in `settings_server.SERVER_FILES`, the README and a
systemd unit). Tests: `test_settings_server.py`, including the server run
from those six files alone.

**What changed from the plan above, and why:**

- **Not over Reticulum, here either.** A phone with an RNS TCP client
  interface to the server would re-announce the server onto LoRa, the reason
  step 3 left RNS. Phones reach a server over the step 3 protocol, by
  address, from the server's code (`card`: address, port, and the host key
  the phone then insists on) or typed. A TYPED address keeps the key of the
  first host to answer there and refuses any other afterwards (trust on
  first use; to accept a reinstalled server, remove it and add it again).
- **The connection is now a channel** (protocol v3, both routes, phone hosts
  too): the host signs its per-connection X25519 key; the phone sends its
  own per-connection X25519 key and proves it holds its permanent key (HMAC
  over a secret only that key can compute); the keys come from both
  per-connection keys AND the phone's permanent one, then AES-GCM per
  direction with counters. On the internet anyone on the path would
  otherwise read the network's frequency, and anyone could take a phone's
  line on the roster. v2 used only the phone's permanent key, so a phone key
  stolen later would have unlocked recorded sessions; v3 is forward secret
  (a test records a session and fails to open it with the stolen key).
  Still a protocol of our own on standard parts, not TLS, not externally
  reviewed.
- **An allow list** (default on a server, `--open` to turn it off): a phone
  that is not allowed waits on the roster with its id and is sent nothing.
  The phone shows the same id (Share settings -> Servers).
- **The passphrase is NEVER written to disk**, instead of encrypted at rest
  with an admin password: that password would be needed at every restart,
  which a systemd service cannot type. Kept in memory for stragglers; after
  a restart a phone still lacking such a push shows "push again" and gets
  nothing, rather than the push without it (it would decide that revision
  and could never be sent the passphrase at it).
- **Admin over a Unix socket in the state directory** (owner-only), no
  network admin port at all. No web page yet.
- **Phones**: a server is connected whether or not WiFi listening is on;
  pinned to WiFi only if its address is a private one (otherwise Android's
  default network, so mobile data works); retried 5 s doubling to 5 min; a
  slower keep-alive asked by the server (60 s against a phone host's 15 s).

**Verified 2026-10-22, both phones** against the packaged server on the
laptop, through `adb reverse` (the laptop's firewall blocks inbound, so this
is the path to 127.0.0.1, not a LAN or internet address):

- the Pixel added it from its code, the OnePlus by typed address; both
  "waiting for the admin" with the ids their screens showed;
- a mistyped preset refused before any phone saw it; a push with nobody
  allowed reached nobody; `allow` sent it at once to the Pixel, whose
  approval popped up; accepted early, switched at the set time;
- the server stopped through the switch and restarted: allow list and
  roster kept, both phones back within ~30 s, and the Pixel's "switched",
  made while the server was down, delivered on reconnect;
- allowing the OnePlus late got it the push (nothing to change), its
  reject reached the roster; `revoke` disconnected it, and it came back as
  waiting; both reconnected by themselves after an app reinstall; Undo
  reported as "undone".

**Verified 2026-10-22 on the real WiFi** (laptop 192.168.77.106, port
opened with ufw), packaged server with `--lan`: both phones found it by its
beacon within 1 s of start, nothing typed; allowed by id; a LoRa power push
(25 -> 22 dBm) accepted on both with the operator's PINs, switched at the
set time (OnePlus +4 s, Pixel ~+1 s), both "Radio ok", "Mesh 1". Then the
PASSPHRASE: a push while the OnePlus had not opted in was refused there with
the reason (the Pixel's copy was rejected by hand, or the two would have
split); after opting in, both accepted, both switched (11:05:08 / 11:05:13
against 11:05:07), both showed the same new network key (0b65, was 9424),
and heard each other again within ~77 s (one beacon interval). Undo on both
restored key 9424 and the mesh. The server's state files never contained
the passphrase. Found and fixed: the server crashed at start when its state
directory's path was long -- a Unix socket path must fit in 108 bytes; now
reached through /proc/self/fd (test added, fails without the fix).

**A phone approves every host it hears (operator, 2026-10-22).** Before,
a phone joined any host whose beacon it heard; only a server's allow list
stood in the way. Now a host heard on WiFi (a phone host or a `--lan`
server: their beacons look the same, and anyone can send one) is NOT
connected to until the operator taps Connect on that phone, after a
notification or a question on screen showing its name, address and key.
Approval is for the KEY: the connection insists on that host id, so another
machine beaconing the same id is refused ("a different host answers").
Ignore is remembered and not asked again; Remove disconnects and forgets. Ignore can be undone
(2026-10-23): Servers and hosts lists the ignored hosts, and Remove… offers
"Stop ignoring" for each (`host_unignore`), which only makes the phone ask
again -- at once if the host is heard now. Before this an ignored host was
listed nowhere and could not be brought back without clearing the app's data.
A server added from its code or by address is approved by that act.
Verified on both phones against the laptop server: both asked, nothing
connected; Connect on the Pixel joined it, Ignore on the OnePlus left it
away, and not asked again after an app restart.

**The admin page** (`settings_web.py`, `serve --web-port 8080`, address from
`web-url`): the roster live with Allow / Revoke, a push form, the server's QR
code. 127.0.0.1 only (an SSH tunnel from elsewhere); every request carries a
key from an owner-only file in a header, which another website cannot add;
the Host header must name this machine (DNS rebinding). Goes through the
same admin socket as the command line. The server now logs every allow,
revoke and push (never the passphrase).

**NOT verified:** the internet over mobile data, a server restart between
a passphrase push and a straggler (unit-tested only: "push again").

## Order of work

1. **Core, in Python, desktop-tested:** bundle format, sign/verify, revision
   rule, before/after diff, apply through the existing setters, PIN
   (create / check / lockout / reset), trusted hosts, `switch_at` with
   late catch-up, undo (the automatic undo was removed 2026-10-23).
2. **QR:** show and scan; trust a host by QR + PIN. First end-to-end path,
   no networking. On both phones.
3. **Host phone over WiFi:** announce, roster, push, approve / reject
   reported back.
4. **Server version:** the same host code as a Linux service; LAN first,
   then internet over an RNS TCP interface.
5. **Hardware:** two phones change frequency together via automatic switch;
   then one left behind deliberately, to see the late catch-up and Undo.

## Not yet known

- **Android 17 (the Pixel)**: step 3 ran there with no new permission
  (2026-10-22), hosting and receiving. The app targets SDK 33; a "local
  network access" permission may apply once it targets a newer SDK.
- **WiFi networks that isolate clients** (common on guest WiFi) block
  phone-to-phone traffic. The host phone's own hotspot or the internet
  server avoid that. Untested.
- **The WiFi link on an ordinary WLAN**, rather than the 802.11s mesh it was
  built for: two phones saw each other before (memory "WiFi multicast
  socket binding"), but verify before relying on it.
