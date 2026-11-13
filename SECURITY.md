# Reporting a security problem

**Please do not open a public issue for a security flaw.** Use GitHub's
private vulnerability reporting instead: the **Security** tab of this
repository, then **Report a vulnerability**. That opens a thread only you and
the maintainer can read.

This is a small project maintained by one person in their own time. Expect an
acknowledgement within a week or so rather than within hours, and do not treat
this as a service with an SLA behind it.

Useful things to include: what you did, what happened, what you expected, and
whether you were on a real radio or a desktop. A test that fails is worth more
than a description.

## What this app is trying to protect

Traffic between the phones of a group that already trusts each other and
shares a passphrase. Specifically:

- Somebody listening on the same frequency should not be able to read
  positions, chat or markers.
- Somebody transmitting on the same frequency should not be able to put false
  positions or markers onto the group's maps.

Everything is encrypted and authenticated with AES-CCM under a key derived
from the passphrase. A frame that fails authentication is dropped.

## What it is not trying to protect

These are known and deliberate. They are not vulnerabilities, though a way to
make any of them *worse* than described would be:

- **Anyone with the passphrase is a full member.** There are no per-node keys
  and no revocation. Removing somebody means changing the passphrase on every
  phone.
- **"Send FM unencrypted" turns authentication off as well**, because for
  amateur use the content must be readable. In that mode anyone in range can
  inject traffic on FM. LoRa is unaffected. The user guide says so plainly.
- **Traffic analysis is possible.** The app does not hide that a transmission
  happened, how long it was, or how often a node beacons.
- **A phone in someone else's hands.** Auto-erase is a timer, not a defence
  against an attacker who can stop the app: the countdown only runs while the
  bridge is running. It also leaves some files behind; the user guide lists
  what a wipe does and does not take.
- **The phone itself.** If Android is compromised, nothing here helps.

## Things worth knowing before you look

- **Counters, not timestamps, make each frame's encryption unique.** Each
  passphrase allows a bounded number of app starts (256) before the phone
  stops transmitting, rather than risk repeating one. That stop is deliberate.
- **Replay is handled in two layers**: a short-horizon duplicate cache, and a
  per-sender window of recent counters for anything older.
- **The passphrase is the whole key.** It is stretched with PBKDF2, and the
  app refuses to invent a default: a phone with no passphrase makes up its own
  and stays alone rather than joining something anyone could read.

## Out of scope

- The released APK is signed with a key held only by the maintainer. Reports
  that you can modify and re-sign the APK with your own key describe how
  Android works.
- Anything requiring physical access to an unlocked phone that already has the
  passphrase.
- Denial of service by transmitting noise on the frequency. It is a radio.
