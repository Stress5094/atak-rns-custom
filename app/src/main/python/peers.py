"""Who else is on this mesh, how far off, and how well we hear them.

Extracted from rns_bridge.py after the 2026-08-12 three-node bench, where two
of the four bugs found were in this table and neither had -- or could have had
-- a unit test: it lived in the module that imports RNS and pyserial, which the
desktop suite cannot load. Both were caught on a radio instead, which is an
expensive way to find a dictionary being written to from the wrong place.

THE DISTINCTION THE WHOLE MODULE IS BUILT ON. Two different facts about a node
get conflated the moment they share a record, and conflating them was bug 1:

  * REACHABLE -- `last_seen`. Any authenticated frame proves it, relayed ones
    included. This is what the node list should show, what a chat should be
    addressable to, and who might reasonably answer an acknowledgement.

  * ADJACENT -- `last_direct`. Only a frame heard FIRST-HAND proves it. This is
    what the neighbour digest advertises, and the digest is a claim about what
    this node can do ALONE. A node that learns a two-hop neighbour from a
    relayed frame advertises covering it; the relay believes the claim and
    stops relaying; and that removes the very thing that made the claim true.
    On the bench a marker was refused `no new coverage` one second after being
    sent, on a path that had been working.

Two further fields must come only from a direct sighting, for the same reason
and with quieter consequences:

  * The SIGNAL reading on a relayed frame belongs to the RELAY, not to the
    originator. Filing it under the originator misreports link quality and
    scrambles relay ordering, which sends the node that heard a frame weakest
    first on the grounds that it is furthest away.

  * The LOSS count becomes meaningless: gaps in a stream of relayed copies
    count frames nobody relayed, not frames the radio missed. The bench had a
    node reporting `10/37 lost` on a link it was not listening to.

What a relayed frame CAN measure about its originator is DISTANCE, because the
hop count survives the trip by construction. That is the useful half of the
split, and it is what tells the ARQ how long to wait before retrying.

Deliberately free of RNS and Android imports, with the clock injected, so the
whole thing is testable on a desktop. Same shape as tx_policy.py, reliable.py,
relay.py and mesh.py.

IMPORTANT: the on-device interpreter is CPython 3.8 (Chaquopy ships
libpython3.8.so). Keep this module 3.8-compatible.
"""

import collections
import threading
import time

import relay

# A jump larger than this is read as a peer that restarted rather than as lost
# frames. The mesh sequence is 16-bit and restarts at zero under a fresh epoch,
# so without a ceiling one reboot would report tens of thousands of losses.
DEFAULT_MAX_CREDIBLE_GAP = 64

# What a sighting changed, for the caller that has to react to it. Returned
# rather than logged here because the reactions live in the bridge -- an
# identity keyframe, a log line -- and this module knows nothing about either.
Sighting = collections.namedtuple("Sighting", "fresh restarted")

_NOTHING_NEW = Sighting(False, False)


class PeerTable(object):
    """Every node this one has heard from, and what is known about each.

    note_frame() runs on the RNode serial read thread, so everything here is
    O(number of nodes) at worst and nothing raises: a bookkeeping failure must
    never be able to interrupt reception.

    `connected_s` and `forget_s` are passed in rather than defined here because
    they are tied to the beacon interval, which is the bridge's to choose --
    and because the relationship between the beacon's refresh timer and this
    window is load-bearing (see bug 3: a refresh longer than the window it
    feeds drops a quiet node out of every neighbour's list on a cycle).
    """

    def __init__(self, connected_s, forget_s, clock=time.time,
                 max_credible_gap=DEFAULT_MAX_CREDIBLE_GAP):
        self.connected_s = connected_s
        self.forget_s = forget_s
        self._clock = clock
        self._max_credible_gap = max_credible_gap
        self._peers = {}
        self._lock = threading.Lock()
        # Called as on_gap(node_id, previous_seq, seq, missed, since_s, name)
        # when a numbered frame was skipped. Set by the bridge, which owns the
        # logger; this module knows nothing about RNS. Always called OUTSIDE
        # the lock, and its failure is swallowed -- a log line must never be
        # able to interrupt reception.
        self.on_gap = None

    # -- sightings ----------------------------------------------------------

    def note_frame(self, node_id, rssi=None, snr=None, seq=None,
                   direct=True, hops_away=None, epoch=None, link=None,
                   radio=None):
        """Record that a frame arrived from `node_id`.

        `direct` is whether it was heard first-hand rather than forwarded --
        see the module docstring for everything that turns on it. `hops_away`
        is how many transmissions it took to arrive, which a relayed frame can
        still measure honestly. `epoch` is the sender's counter generation,
        which changes when it restarts. `link` is which of THIS node's own
        radios the frame arrived on -- stored unconditionally, direct or
        relayed, because it answers a REACHABLE question ("which of my links
        currently carries traffic involving this peer?"), not the stricter
        ADJACENT one rssi/snr/loss answer -- a relayed frame still arrived
        here over one real local radio, and that is real evidence for which
        link to try first when originating a message to this peer.

        `radio` is what KIND of radio `link` was at the moment the frame
        arrived -- "rnode" or "meshtastic" for "lora", otherwise the link name
        -- and keys a per-radio record (see _note_radio). Stamped here by the
        caller rather than looked up at display time, because the board in the
        LoRa slot can be swapped and that must not relabel what was heard on
        the previous one. Defaults to `link`.

        Returns a Sighting saying whether this node is new and whether it
        appears to have restarted since we last heard it.
        """
        try:
            with self._lock:
                peer = self._peers.get(node_id)
                fresh = peer is None
                if fresh:
                    peer = self._peers[node_id] = {}

                # A changed epoch means the sender restarted: it reset its
                # sequence counter and, with it, everything it knew about who
                # had heard what. Detected here rather than inferred from a
                # sequence going backwards, because the epoch is authenticated
                # and a sequence is not proof of anything on its own.
                restarted = False
                if epoch is not None:
                    previous_epoch = peer.get("epoch")
                    restarted = previous_epoch is not None and previous_epoch != epoch
                    peer["epoch"] = epoch

                since_s = (self._clock() - peer["last_seen"]
                           if "last_seen" in peer else None)
                peer["last_seen"] = self._clock()
                if hops_away is not None:
                    peer["hops_away"] = hops_away
                if link is not None:
                    peer["last_link"] = link
                    # Sticky, unlike last_link: a peer who moved off a link
                    # still has real history there, which last_link alone
                    # cannot say -- "most recently FM" and "never heard on
                    # LoRa at all" are different facts a caller may need to
                    # tell apart (see heard_on()).
                    peer.setdefault("links_seen", set()).add(link)
                if radio is not None or link is not None:
                    self._note_radio(peer, radio or link, link, direct,
                                     rssi, snr, seq, epoch)
                if not direct:
                    # Reachable, and worth showing and addressing -- but not a
                    # neighbour, and nothing else measured from this frame
                    # describes the sender's own link.
                    return Sighting(fresh, restarted)

                peer["last_direct"] = peer["last_seen"]
                # Guarded: the RNode interface clears its signal stats the
                # moment its callback returns, so a frame arriving without them
                # should leave the last real reading standing rather than blank
                # the node list.
                if rssi is not None:
                    peer["rssi"] = rssi
                if snr is not None:
                    peer["snr"] = snr
                gap = None
                if seq is not None:
                    gap = self._count_loss(peer, seq, restarted)
                name = peer.get("name")
                sighting = Sighting(fresh, restarted)
            if gap is not None and self.on_gap is not None:
                try:
                    self.on_gap(node_id, gap[0], seq, gap[1], since_s, name)
                except Exception:
                    pass
            return sighting
        except Exception:
            return _NOTHING_NEW

    def _count_loss(self, peer, seq, restarted):
        """Loss measured off the mesh sequence, which counts every frame rather
        than only the beacons -- and which had to take over once the beacon
        stopped firing whenever other traffic was already proving the link."""
        previous = peer.get("mesh_seq")
        peer["mesh_seq"] = seq
        peer["heard"] = peer.get("heard", 0) + 1
        if previous is None or restarted:
            # A restart resets the counter to zero, so the "gap" across one is
            # not loss and counting it would report a reboot as a dead link.
            return None
        gap = (seq - previous - 1) & 0xFFFF
        if 0 < gap <= self._max_credible_gap:
            peer["lost"] = peer.get("lost", 0) + gap
            return (previous, gap)
        return None

    def _note_radio(self, peer, radio, link, direct, rssi, snr, seq, epoch):
        """The same facts as the whole-node record, kept apart per radio.

        Exists for the mesh view, which draws one line per radio a peer is heard
        on: the whole-node rssi is whichever radio spoke last, and an FM frame
        (which carries no signal reading) next to a LoRa one would leave a LoRa
        number standing under an FM line. The same direct-only rule as the
        whole-node record applies, for the same reason.

        LOSS PER RADIO IS ONLY HALF A MEASUREMENT. The sender numbers its frames
        with one counter across all its radios, so a gap seen on one radio may be
        frames that went out on another. snapshot() therefore reports per-radio
        loss only for a peer heard on a single radio -- and even then a gap
        counts frames the sender put on a radio this node does not have.
        """
        now = self._clock()
        record = peer.setdefault("radios", {}).setdefault(radio, {})
        record["link"] = link
        record["last_heard"] = now
        if not direct:
            return
        record["last_direct"] = now
        if rssi is not None:
            record["rssi"] = rssi
        if snr is not None:
            record["snr"] = snr
        if seq is None:
            return
        # The epoch is compared per radio, not taken from note_frame's restart
        # flag: that fires once, on whichever radio carried the first frame
        # after the restart, and this radio's last sequence may predate it.
        restarted = (epoch is not None and record.get("epoch") is not None
                     and record["epoch"] != epoch)
        if epoch is not None:
            record["epoch"] = epoch
        previous = record.get("mesh_seq")
        record["mesh_seq"] = seq
        record["heard"] = record.get("heard", 0) + 1
        if previous is None or restarted:
            return
        gap = (seq - previous - 1) & 0xFFFF
        if 0 < gap <= self._max_credible_gap:
            record["lost"] = record.get("lost", 0) + gap

    def note_beacon(self, node_id, seq=None, name=None, callsign=None,
                    digest=None):
        """What a beacon adds on top of the frame that carried it.

        last_seen, the signal and the loss count come from note_frame, which
        has already run for this frame -- it runs for every frame, so repeating
        them here would only describe beacons.
        """
        try:
            with self._lock:
                # Seeded with a timestamp, because a record without one reads
                # as infinitely old and snapshot() prunes it on sight. In the
                # bridge note_frame() has always run first so this never
                # mattered, but a record that can exist in a state the rest of
                # the class treats as ancient is a trap for the next caller.
                # A beacon is never relayed -- the hop cap is zero and enforced
                # on receipt -- so hearing one is proof of adjacency.
                peer = self._peers.get(node_id)
                if peer is None:
                    now = self._clock()
                    peer = self._peers[node_id] = {"last_seen": now,
                                                   "last_direct": now}
                if seq is not None:
                    peer["seq"] = seq
                # Kept rather than blanked: an older peer or a padded
                # diagnostic beacon carries neither name, ATAK may not be
                # running yet, and forgetting one already learned would make
                # the node list flicker.
                if name:
                    peer["name"] = name
                if callsign:
                    peer["callsign"] = callsign
                # Replaced wholesale, not merged: this is who they can hear
                # NOW, and a neighbour they have lost is the whole reason the
                # answer changed.
                if digest is not None:
                    peer["digest"] = digest
        except Exception:
            pass

    # -- the coverage test --------------------------------------------------

    def neighbour_digest(self):
        """A Bloom filter of the nodes this one currently hears DIRECTLY.

        Only connected, and only direct: a node that fell off is no longer
        coverage this node can offer, and a node reached through somebody else
        never was. Advertising either tells the rest of the mesh not to relay
        for whoever it names.
        """
        return relay.digest_build(self.adjacent())

    def adjacent(self):
        """The one-hop neighbour set: heard first-hand, recently."""
        now = self._clock()
        with self._lock:
            return [n for n, p in self._peers.items()
                    if now - p.get("last_direct", 0) <= self.connected_s]

    def sender_covers_our_neighbours(self, sender):
        """Would relaying this sender's frame reach anybody?

        True means no -- everyone we can reach directly, the sender says it can
        reach too, so a relay would be pure cost. That is the ordinary answer
        for a squad standing together, and it is what keeps flooding free until
        the group actually splits.
        """
        now = self._clock()
        with self._lock:
            digest = (self._peers.get(sender) or {}).get("digest")
            # The sender is excluded because it obviously reaches itself, and
            # this node because the sender demonstrably reached us -- the frame
            # is in our hands. What is left is the coverage a relay would add.
            others = [n for n, p in self._peers.items()
                      if n != sender
                      and now - p.get("last_direct", 0) <= self.connected_s]
        return relay.covers(digest, others)

    def covers_target(self, sender, target):
        """Coverage for a frame addressed to one node -- see relay.covers_target.

        None means "cannot tell"; the caller falls back to the broadcast test.
        """
        if not target or target == sender:
            return None
        now = self._clock()
        with self._lock:
            digest = (self._peers.get(sender) or {}).get("digest")
            neighbours = {n for n, p in self._peers.items()
                          if now - p.get("last_direct", 0) <= self.connected_s}
        return relay.covers_target(digest, target, neighbours)

    def probable_pairs(self):
        """(a, b) for every known node b that peer a's digest claims to hear.

        For the mesh view's faint remote lines, and only that: a Bloom filter
        can say yes wrongly (never no wrongly), so each pair is a claim. Only
        from peers still connected -- a digest is "who I hear NOW" and an old
        one describes a mesh that has since moved. FM beacons carry no digest,
        so an FM-only neighbour contributes nothing here.
        """
        now = self._clock()
        with self._lock:
            known = list(self._peers)
            claims = [(n, p["digest"]) for n, p in self._peers.items()
                      if p.get("digest")
                      and now - p.get("last_seen", 0) <= self.connected_s]
        return [(a, b) for a, digest in claims for b in known
                if b != a and relay.digest_contains(digest, b)]

    # -- distance -----------------------------------------------------------

    def hops_away(self, node_id):
        """How many transmissions it takes to reach this peer. 1 is adjacent.

        A live direct link wins over any remembered distance: hearing a node
        first-hand is proof of adjacency, while `hops_away` is only the last
        thing observed and may describe a relayed copy that arrived while the
        direct one was lost to interference. Unknown nodes read as adjacent,
        which is the timing everything had before relaying existed.
        """
        now = self._clock()
        with self._lock:
            peer = self._peers.get(node_id) or {}
            if now - peer.get("last_direct", 0) <= self.connected_s:
                return 1
            try:
                return max(1, int(peer.get("hops_away", 1)))
            except (TypeError, ValueError):
                return 1

    def last_link(self, node_id):
        """Which of this node's own radios last carried traffic involving
        this peer, direct or relayed. None for a peer never seen at all --
        the caller decides what "unknown" should default to, since that
        default is a routing policy, not a fact this table can state."""
        with self._lock:
            peer = self._peers.get(node_id)
        return peer.get("last_link") if peer else None

    def heard_on(self, node_id, link):
        """Whether this peer has EVER been heard on `link`, not just most
        recently -- for a caller that needs "never heard on LoRa at all"
        rather than "most recently heard on FM", which last_link() alone
        cannot distinguish: a peer who moved off a link still has real
        history there. False for a peer never seen at all, same as
        last_link()'s None-defaults-to-caller's-choice convention."""
        with self._lock:
            peer = self._peers.get(node_id)
        return link in peer["links_seen"] if peer and "links_seen" in peer else False

    # -- reporting ----------------------------------------------------------

    def connected_count(self):
        """How many peers could plausibly answer right now. Sizes the ack
        jitter window, so it counts REACHABLE rather than adjacent -- a node
        two hops out can still receive a message and acknowledge it."""
        now = self._clock()
        with self._lock:
            return sum(1 for p in self._peers.values()
                       if now - p.get("last_seen", 0) <= self.connected_s)

    def connected(self):
        """Node ids that could plausibly answer, for the ack expectation."""
        now = self._clock()
        with self._lock:
            return [n for n, p in self._peers.items()
                    if now - p.get("last_seen", 0) <= self.connected_s]

    def label(self, node_id):
        """Name a peer the way the log, the status line and the UI all do: the
        device's own name, falling back to its id. Printing raw hex in one
        place and a name in another makes the same node look like two."""
        with self._lock:
            peer = self._peers.get(node_id) or {}
        return peer.get("name") or node_id

    def labels(self, node_ids):
        return ", ".join(self.label(n) for n in node_ids) or "nobody"

    def by_callsign(self, callsign):
        """Every node advertising this callsign. More than one is ambiguous and
        the caller is expected to refuse rather than guess -- two nodes
        answering to the same name would silently misroute an ack."""
        with self._lock:
            return [n for n, p in self._peers.items()
                    if (p.get("callsign") or "") == callsign]

    def snapshot(self):
        """Peers, freshest first, with connected/direct decided by age.

        Prunes only very old entries: a departed node stays in the list as
        disconnected for forget_s, because "who fell off?" is the question the
        node list exists to answer.
        """
        now = self._clock()
        out = []
        with self._lock:
            for node_id in [n for n, p in self._peers.items()
                            if now - p.get("last_seen", 0) > self.forget_s]:
                self._peers.pop(node_id, None)
            for node_id, peer in self._peers.items():
                age = now - peer.get("last_seen", 0)
                out.append({
                    "node_id": node_id,
                    "name": peer.get("name") or "",
                    "callsign": peer.get("callsign") or "",
                    "age_s": round(age, 1),
                    # `connected` means reachable, which is what the operator
                    # and the ack bookkeeping both want. `direct` is the
                    # stricter fact, and the only one the coverage test may
                    # use. See the module docstring.
                    "connected": age <= self.connected_s,
                    "direct": now - peer.get("last_direct", 0) <= self.connected_s,
                    "hops_away": peer.get("hops_away", 1),
                    "last_link": peer.get("last_link"),
                    "rssi": peer.get("rssi"),
                    "snr": peer.get("snr"),
                    "seq": peer.get("seq", 0),
                    "heard": peer.get("heard", 0),
                    "lost": peer.get("lost", 0),
                    "radios": self._radio_rows(peer, now),
                })
        out.sort(key=lambda p: p["age_s"])
        return out

    def _radio_rows(self, peer, now):
        """One row per radio this peer has been heard on, freshest first.

        `direct_age_s` is None for a radio that has only ever carried relayed
        copies, and so are its signal and loss -- nothing measured from those
        describes a link between this node and that one. `lost` is also None
        whenever the peer has been heard on more than one radio: see
        _note_radio for why a per-radio gap then means nothing.
        """
        radios = peer.get("radios") or {}
        single = len(radios) == 1
        rows = []
        for kind, record in radios.items():
            direct_at = record.get("last_direct")
            rows.append({
                "radio": kind,
                "link": record.get("link"),
                "age_s": round(now - record.get("last_heard", 0), 1),
                "direct_age_s": (None if direct_at is None
                                 else round(now - direct_at, 1)),
                "rssi": record.get("rssi"),
                "snr": record.get("snr"),
                "heard": record.get("heard", 0),
                "lost": record.get("lost", 0) if single and direct_at else None,
            })
        rows.sort(key=lambda r: r["age_s"])
        return rows

    def clear(self):
        """Forget everybody. For a passphrase change: what a peer bound under
        the old key says nothing about who holds the new one."""
        with self._lock:
            self._peers.clear()

    def __len__(self):
        return len(self._peers)
