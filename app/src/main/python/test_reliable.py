"""Desktop tests for reliable.py.

Run the whole suite (from this directory):

    python3 -m unittest discover -p 'test_*.py' -v

The clock is injected throughout, so retry and expiry tests are instant rather
than sleeping for the real timeouts. The rng is injected too, and pinned to
zero in most tests so retry timing is exactly predictable -- jitter has its own
test.
"""

import unittest

import cot_codec as codec
import relay
import reliable

A_PLI = (
    b'<event version="2.0" uid="ANDROID-1" type="a-f-G-U-C" how="m-g" '
    b'time="2026-07-31T18:00:00Z" start="2026-07-31T18:00:00Z" '
    b'stale="2026-07-31T18:02:00Z">'
    b'<point lat="37.4219" lon="-122.0840" hae="12.0" ce="9.5" le="9999999.0"/>'
    b'<detail><contact callsign="ALPHA"/></detail></event>'
)

ME = "aa11bb"
PEER_A = "cc22dd"
PEER_B = "ee33ff"


class FakeClock(object):
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_tx(clock=None, jitter=0.0, **kw):
    clock = clock or FakeClock()
    tx = reliable.ReliableTx(clock=clock, rng=lambda: jitter, **kw)
    return tx, clock


def a_frame():
    return codec.encode(A_PLI)


class TestRegister(unittest.TestCase):
    def test_returns_a_wrapped_frame_that_round_trips(self):
        tx, _ = make_tx()
        msg_id, wrapped = tx.register(a_frame(), ME, [PEER_A], label="chat")
        got = codec.unwrap_reliable(wrapped, ME)
        self.assertEqual(got.msg_id, msg_id)
        self.assertEqual(got.origin, ME)
        self.assertEqual(got.frame, a_frame())

    def test_a_named_addressee_reaches_the_wire(self):
        """Without this on the wire a receiver cannot tell it is a bystander,
        so every node in earshot acks a message one node was asked about."""
        tx, _ = make_tx()
        _, wrapped = tx.register(a_frame(), ME, [PEER_A], addressee=PEER_A)
        got = codec.unwrap_reliable(wrapped)
        self.assertEqual(got.addressee, PEER_A)
        # And it must not also claim any-ack, or a bystander could silence the
        # one node that owes the answer.
        self.assertFalse(got.any_ack)

    def test_a_message_to_everyone_names_nobody(self):
        """A room message is genuinely owed an ack by whoever is nearest, so
        naming one node would throw away the passive cancellation instead."""
        tx, _ = make_tx()
        _, wrapped = tx.register(a_frame(), ME, [PEER_A, PEER_B])
        got = codec.unwrap_reliable(wrapped)
        self.assertIsNone(got.addressee)
        self.assertTrue(got.any_ack)

    def test_an_addressee_we_do_not_expect_an_ack_from_is_dropped(self):
        """It would silence every receiver AND not count when it answered, so
        the message would be reported undelivered every single time."""
        tx, _ = make_tx()
        _, wrapped = tx.register(a_frame(), ME, [PEER_A], addressee=PEER_B)
        self.assertIsNone(codec.unwrap_reliable(wrapped).addressee)

    def test_naming_the_addressee_does_not_change_who_is_expected(self):
        tx, _ = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A], addressee=PEER_A)
        tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        self.assertEqual(tx.stats()["pending"], 0)

    def test_no_peers_means_no_tracking_and_no_retries(self):
        # Running solo is the common case on a bench. Retrying into a room
        # known to be empty is pure airtime waste, and reporting "nobody
        # acknowledged" is not news when nobody is there.
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [])
        self.assertEqual(tx.stats()["pending"], 0)
        clock.advance(600)
        retries, gave_up = tx.tick()
        self.assertEqual(retries, [])
        self.assertEqual(gave_up, [])
        self.assertEqual(tx.stats()["sent_unwitnessed"], 1)

    def test_self_is_never_expected_to_ack(self):
        tx, _ = make_tx()
        tx.register(a_frame(), ME, [ME])
        self.assertEqual(tx.stats()["pending"], 0)

    def test_msg_ids_are_unique_while_in_flight(self):
        tx, _ = make_tx(max_inflight=64)
        ids = {tx.register(a_frame(), ME, [PEER_A])[0] for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_forget_drops_without_reporting_a_failure(self):
        # For a message that was registered but never actually transmitted.
        # Reporting it undelivered would announce a delivery failure for
        # something that never went out.
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.forget(msg_id)
        self.assertEqual(tx.stats()["pending"], 0)
        self.assertEqual(tx.snapshot()["undelivered"], [])
        clock.advance(600)
        self.assertEqual(tx.tick(), ([], []))

    def test_forget_of_an_unknown_id_is_harmless(self):
        tx, _ = make_tx()
        tx.forget(999)
        self.assertEqual(tx.stats().get("forgotten", 0), 0)

    def test_inflight_is_bounded(self):
        tx, _ = make_tx(max_inflight=4, undelivered_kept=16)
        for _ in range(10):
            tx.register(a_frame(), ME, [PEER_A])
        self.assertEqual(tx.stats()["pending"], 4)
        # The shed ones are reported rather than silently forgotten.
        self.assertEqual(len(tx.snapshot()["undelivered"]), 6)

    def test_displaced_messages_reach_the_caller(self):
        """The snapshot is not enough on its own.

        It keeps only the last `undelivered_kept`, and nothing logs it. Under
        congestion displacement is the common way a message dies, so it has to
        come out of tick() like any other give-up or the loudest failure mode
        stays the quietest.
        """
        tx, _ = make_tx(max_inflight=2, undelivered_kept=1)
        for _ in range(5):
            tx.register(a_frame(), ME, [PEER_A])
        _retries, gave_up = tx.tick()
        self.assertEqual(len(gave_up), 3)
        self.assertTrue(all(r.reason == reliable.DISPLACED for r in gave_up))
        # Handed over exactly once.
        self.assertEqual(tx.tick()[1], [])

    def test_retry_exhaustion_still_says_no_acknowledgement(self):
        tx, clock = make_tx(max_attempts=1)
        tx.register(a_frame(), ME, [PEER_A])
        clock.advance(600)
        _retries, gave_up = tx.tick()
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(gave_up[0].reason, reliable.NO_ACK)

    def test_retry_exhaustion_carries_the_frame_for_escalation(self):
        """A give-up on NO_ACK has to carry the actual wrapped frame, not
        just describe it, so a caller can re-send it on another link
        instead of only reporting the failure."""
        tx, clock = make_tx(max_attempts=1)
        _msg_id, wrapped = tx.register(a_frame(), ME, [PEER_A])
        clock.advance(600)
        _retries, gave_up = tx.tick()
        self.assertEqual(gave_up[0].frame, wrapped)


class TestWhoOwesAnAck(unittest.TestCase):
    """The receive-side half of the addressee rule. Its failure modes are a
    silent chat reported undelivered (too strict) and N-1 wasted acks per
    direct message (too loose)."""

    def test_an_unnamed_frame_is_owed_an_ack_by_everyone(self):
        self.assertTrue(reliable.owes_ack(None, ME))
        self.assertTrue(reliable.owes_ack(None, PEER_A))

    def test_a_named_frame_is_owed_an_ack_only_by_that_node(self):
        self.assertTrue(reliable.owes_ack(PEER_A, PEER_A))
        self.assertFalse(reliable.owes_ack(PEER_A, PEER_B))
        self.assertFalse(reliable.owes_ack(PEER_A, ME))

    def test_the_addressee_always_answers_its_own_message(self):
        """The one case that must never be silenced: the sender counts this
        ack and nothing else, so getting it wrong means every direct chat is
        reported undelivered."""
        _, wrapped = make_tx()[0].register(a_frame(), ME, [PEER_A],
                                           addressee=PEER_A)
        got = codec.unwrap_reliable(wrapped)
        self.assertTrue(reliable.owes_ack(got.addressee, PEER_A))
        self.assertFalse(reliable.owes_ack(got.addressee, PEER_B))


class TestCongestionBackoff(unittest.TestCase):
    """Retries stop while too many messages are outstanding.

    The 2026-08-16 stress bench: every healthy load step peaked at ONE message
    in flight, every collapsed one sat at 21-32, and above the knee ARQ spent
    ~70% of the channel to deliver nothing at all.
    """

    def test_retries_continue_below_the_threshold(self):
        tx, clock = make_tx(congestion_inflight=4)
        for _ in range(4):
            tx.register(a_frame(), ME, [PEER_A])
        clock.advance(5)
        retries, _gave_up = tx.tick()
        self.assertEqual(len(retries), 4)
        self.assertEqual(tx.stats().get("retry_suppressed", 0), 0)

    def test_retries_stop_above_the_threshold(self):
        tx, clock = make_tx(congestion_inflight=4)
        for _ in range(5):
            tx.register(a_frame(), ME, [PEER_A])
        clock.advance(5)
        retries, _gave_up = tx.tick()
        self.assertEqual(retries, [])
        self.assertEqual(tx.stats()["retry_suppressed"], 5)

    def test_a_suppressed_retry_does_not_burn_an_attempt(self):
        """Otherwise congestion alone would exhaust a message's four tries
        without a single transmission having gone out."""
        tx, clock = make_tx(congestion_inflight=1, max_attempts=4)
        ids = [tx.register(a_frame(), ME, [PEER_A])[0] for _ in range(3)]
        for _ in range(10):
            clock.advance(1)
            tx.tick()
        self.assertEqual(tx.stats().get("undelivered", 0), 0)
        for msg_id in ids:
            entry = [e for e in tx.snapshot()["inflight"]
                     if e["msg_id"] == msg_id]
            self.assertEqual(entry[0]["attempts"], 1)

    def test_congestion_ends_a_message_instead_of_stalling_it(self):
        """Suspended retries stop consuming attempts, so max_attempts can no
        longer end anything. Without the deadline these would wait forever and
        then be shed silently by the in-flight cap."""
        tx, clock = make_tx(congestion_inflight=1)
        for _ in range(3):
            tx.register(a_frame(), ME, [PEER_A])
        gave_up = []
        for _ in range(200):
            clock.advance(5)
            gave_up += tx.tick()[1]
        self.assertEqual(len(gave_up), 3)
        self.assertTrue(all(r.reason == reliable.CONGESTED for r in gave_up))
        self.assertEqual(tx.stats()["pending"], 0)

    def test_the_deadline_never_fires_before_the_retries_would_have(self):
        """A quiet channel must behave exactly as it did before this existed:
        four transmissions, then no acknowledgement -- never a deadline."""
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [PEER_A])
        gave_up = []
        for _ in range(400):
            clock.advance(0.5)
            gave_up += tx.tick()[1]
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(gave_up[0].reason, reliable.NO_ACK)
        self.assertEqual(gave_up[0].attempts, 4)

    def test_relief_lets_delivery_resume(self):
        tx, clock = make_tx(congestion_inflight=2)
        ids = [tx.register(a_frame(), ME, [PEER_A])[0] for _ in range(4)]
        clock.advance(5)
        self.assertEqual(tx.tick()[0], [])
        for msg_id in ids[:3]:
            tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        clock.advance(1)
        retries, _ = tx.tick()
        self.assertEqual([r[0] for r in retries], [ids[3]])


class TestAcknowledgement(unittest.TestCase):
    def test_ack_completes_a_single_peer_message(self):
        tx, _ = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        done = tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        self.assertIsNotNone(done)
        self.assertEqual(tx.stats()["pending"], 0)
        self.assertEqual(tx.stats()["delivered"], 1)

    def test_one_ack_completes_a_broadcast(self):
        """The default is DEFAULT_REQUIRED_ACKS = 1, and this is the whole
        reason: a broadcast goes out once and everybody hears it, but the
        confirmations come back one at a time, so at 16 nodes one marker costs
        15 acks. "Did anybody get it?" is the answer worth paying for."""
        tx, _ = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A, PEER_B])
        self.assertIsNotNone(tx.on_ack(codec.Ack(ME, msg_id, PEER_A)))
        self.assertEqual(tx.stats()["pending"], 0)
        self.assertEqual(tx.stats()["delivered"], 1)

    def test_requiring_every_peer_is_still_available(self):
        """Milestone 4's stronger promise, kept as a setting rather than
        deleted -- the trade is airtime for certainty, and which one matters
        depends on what is being sent."""
        tx, _ = make_tx(required_acks=99)
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A, PEER_B])
        self.assertIsNone(tx.on_ack(codec.Ack(ME, msg_id, PEER_A)))
        self.assertEqual(tx.snapshot()["inflight"][0]["waiting_on"], [PEER_B])
        self.assertIsNotNone(tx.on_ack(codec.Ack(ME, msg_id, PEER_B)))
        self.assertEqual(tx.stats()["pending"], 0)

    def test_the_requirement_never_exceeds_the_peers_there_are(self):
        """Otherwise a two-node network could never deliver anything: asking
        for three acks from one peer is unsatisfiable, and it would present as
        every message failing."""
        tx, _ = make_tx(required_acks=99)
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        self.assertIsNotNone(tx.on_ack(codec.Ack(ME, msg_id, PEER_A)))

    def test_ack_for_unknown_msg_id_is_ignored(self):
        tx, _ = make_tx()
        self.assertIsNone(tx.on_ack(codec.Ack(ME, 1234, PEER_A)))
        self.assertEqual(tx.stats()["ack_unknown"], 1)

    def test_ack_naming_another_origin_is_ignored(self):
        # Every frame is broadcast, so we hear acks meant for other senders.
        # Matching on msg_id alone would let one of those complete our message.
        tx, _ = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        self.assertIsNone(tx.on_ack(codec.Ack(PEER_B, msg_id, PEER_A)))
        self.assertEqual(tx.stats()["pending"], 1)

    def test_duplicate_ack_is_harmless(self):
        tx, _ = make_tx(required_acks=99)
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A, PEER_B])
        tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        self.assertEqual(tx.snapshot()["inflight"][0]["waiting_on"], [PEER_B])


class TestRetry(unittest.TestCase):
    def test_nothing_is_due_before_the_timeout(self):
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [PEER_A])
        clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S - 0.01)
        self.assertEqual(tx.tick(), ([], []))

    def test_retry_fires_after_the_timeout(self):
        tx, clock = make_tx()
        msg_id, wrapped = tx.register(a_frame(), ME, [PEER_A])
        clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S)
        retries, gave_up = tx.tick()
        self.assertEqual(gave_up, [])
        self.assertEqual(len(retries), 1)
        # The retransmitted bytes are identical, so the receiver recognises it
        # as the same message and suppresses the duplicate delivery.
        self.assertEqual(retries[0][0], msg_id)
        self.assertEqual(retries[0][1], wrapped)
        self.assertEqual(retries[0][3], 2)         # attempt number
        self.assertEqual(retries[0][4], [PEER_A])  # still waiting on

    def test_an_acked_message_is_never_retried(self):
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        clock.advance(600)
        self.assertEqual(tx.tick(), ([], []))

    def test_backoff_grows_the_interval(self):
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [PEER_A])
        fired = []
        for _ in range(600):
            clock.advance(0.1)
            retries, gave_up = tx.tick()
            if retries:
                fired.append(round(clock.t, 1))
            if gave_up:
                break
        gaps = [round(b - a, 1) for a, b in zip(fired, fired[1:])]
        self.assertEqual(len(fired), reliable.DEFAULT_MAX_ATTEMPTS - 1)
        self.assertTrue(all(b > a for a, b in zip(gaps, gaps[1:])),
                        "intervals should grow, got %r" % (gaps,))

    def test_gives_up_after_max_attempts(self):
        # Strict mode: one peer answers and the other never does, so the
        # message stays in flight to the attempt cap. Under the default
        # one-ack rule the first ack would complete it and there would be
        # nothing left to give up on.
        tx, clock = make_tx(required_acks=99)
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A, PEER_B], label="chat 1")
        tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        sends = 1
        gave_up = []
        for _ in range(1000):
            clock.advance(0.1)
            retries, up = tx.tick()
            sends += len(retries)
            gave_up += up
            if gave_up:
                break
        self.assertEqual(sends, reliable.DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(len(gave_up), 1)
        record = gave_up[0]
        self.assertEqual(record.msg_id, msg_id)
        self.assertEqual(record.label, "chat 1")
        # Only the peer that never answered is named -- this is the line the
        # user reads, so it must not accuse a peer that did acknowledge.
        self.assertEqual(record.missing, [PEER_B])
        self.assertEqual(record.attempts, reliable.DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(tx.stats()["pending"], 0)

    def test_gives_up_within_about_twenty_seconds(self):
        # The user has to learn about the failure while they still remember
        # sending the message. Guard the actual elapsed time, not the constants.
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [PEER_A])
        while clock.t < 120:
            clock.advance(0.1)
            if tx.tick()[1]:
                break
        self.assertLess(clock.t, 20.0)
        self.assertGreater(clock.t, 10.0)

    def test_jitter_shifts_the_retry(self):
        early, clock_a = make_tx(jitter=0.0)
        late, clock_b = make_tx(jitter=1.0)
        early.register(a_frame(), ME, [PEER_A])
        late.register(a_frame(), ME, [PEER_A])
        clock_a.advance(reliable.DEFAULT_MIN_TIMEOUT_S)
        clock_b.advance(reliable.DEFAULT_MIN_TIMEOUT_S)
        self.assertEqual(len(early.tick()[0]), 1)
        self.assertEqual(late.tick()[0], [])

    def test_undelivered_list_is_bounded(self):
        tx, clock = make_tx(undelivered_kept=3, max_inflight=64)
        for i in range(10):
            tx.register(a_frame(), ME, [PEER_A], label="msg %d" % i)
        for _ in range(1000):
            clock.advance(0.1)
            tx.tick()
        self.assertEqual(tx.stats()["pending"], 0)
        self.assertEqual(len(tx.snapshot()["undelivered"]), 3)


class TestMultiHopTiming(unittest.TestCase):
    """A relay does not forward on arrival -- it waits out its cancellation
    window first -- so a peer two hops away is slower by a fixed amount per
    relayed leg, not merely further off.

    The 2026-08-12 bench ran a 2.0 s retry floor against a 2.0 s relay window on
    each leg, so the first retry fired before the message had even reached the
    far node: every multi-hop chat took three transmissions to deliver once, and
    the wasted relays put the relay budget under strain for the first time.
    """

    def test_a_one_hop_peer_is_timed_exactly_as_before(self):
        """The single-cell case must not pay for multi-hop being possible."""
        tx, clock = make_tx(hops_away=lambda n: 1)
        tx.register(a_frame(), ME, [PEER_A])
        clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S - 0.01)
        self.assertEqual(tx.tick(), ([], []))
        clock.advance(0.01)
        self.assertEqual(len(tx.tick()[0]), 1)

    def test_a_two_hop_peer_is_not_retried_on_the_one_hop_schedule(self):
        """The exact regression the bench caught."""
        tx, clock = make_tx(hops_away=lambda n: 2)
        tx.register(a_frame(), ME, [PEER_A])
        clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S)
        self.assertEqual(tx.tick(), ([], []),
                         "retried before the message could even arrive")

    def test_the_allowance_covers_the_relay_delay_both_ways(self):
        """Out and back: the message is relayed going, the ack coming."""
        tx, clock = make_tx(hops_away=lambda n: 2)
        tx.register(a_frame(), ME, [PEER_A])
        clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S
                      + 2.0 * reliable.DEFAULT_RELAY_DELAY_S)
        self.assertEqual(len(tx.tick()[0]), 1)

    def test_a_further_peer_waits_longer_still(self):
        near, _ = make_tx(hops_away=lambda n: 2)
        far, _ = make_tx(hops_away=lambda n: 3)
        near.register(a_frame(), ME, [PEER_A])
        far.register(a_frame(), ME, [PEER_A])
        self.assertLess(near._pending[list(near._pending)[0]].next_try,
                        far._pending[list(far._pending)[0]].next_try)

    def test_the_farthest_peer_sets_the_schedule_not_the_nearest(self):
        """A message is not delivered until the LAST peer answers, so timing on
        the nearest retransmits to everybody to chase the slowest one."""
        both, _ = make_tx(hops_away=lambda n: 1 if n == PEER_A else 3)
        only_near, _ = make_tx(hops_away=lambda n: 1)
        both.register(a_frame(), ME, [PEER_A, PEER_B])
        only_near.register(a_frame(), ME, [PEER_A, PEER_B])
        self.assertGreater(both._pending[list(both._pending)[0]].next_try,
                           only_near._pending[list(only_near._pending)[0]].next_try)

    def test_a_broken_distance_lookup_never_stops_a_retry(self):
        """Bookkeeping must not be able to strand a message in flight."""
        def boom(node_id):
            raise RuntimeError("peer table exploded")
        tx, clock = make_tx(hops_away=boom)
        tx.register(a_frame(), ME, [PEER_A])
        clock.advance(600)
        self.assertEqual(len(tx.tick()[0]), 1)

    def test_the_allowance_is_taken_from_the_relay_module(self):
        """Restating the window here is what let the two drift into collision."""
        self.assertEqual(reliable.DEFAULT_RELAY_DELAY_S,
                         relay.DEFAULT_WINDOW_S + relay.TIE_JITTER_S)
        self.assertGreater(reliable.DEFAULT_RELAY_DELAY_S,
                           reliable.DEFAULT_MIN_TIMEOUT_S / 2.0)


class TestSelectiveRetransmission(unittest.TestCase):
    """Milestone 6 step 4. A retry used to resend every fragment, so a
    five-fragment drawing that lost one piece had to land all five again."""

    def partial(self, msg_id, acker, count, have):
        # `acker` is passed to decode, not carried by the frame: since v15 it
        # comes from the mesh envelope's authenticated sender.
        return codec.decode_partial(
            codec.encode_partial(ME, msg_id, acker, count, have), acker)

    def test_without_a_report_everything_is_resent(self):
        """The honest fallback, and what always happened before the bitmap."""
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [PEER_A])
        clock.advance(600)
        self.assertIsNone(tx.tick()[0][0][5])

    def test_a_report_narrows_the_retry_to_what_is_missing(self):
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.note_fragment_count(msg_id, 5)
        tx.on_partial(self.partial(msg_id, PEER_A, 5, {0, 1, 3, 4}))
        clock.advance(600)
        self.assertEqual(tx.tick()[0][0][5], {2})

    def test_the_union_is_sent_when_peers_are_missing_different_pieces(self):
        """One broadcast serves everybody, so a fragment only one peer lacks
        still has to go out. Intersecting would strand whoever lost the most."""
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A, PEER_B])
        tx.note_fragment_count(msg_id, 4)
        tx.on_partial(self.partial(msg_id, PEER_A, 4, {0, 1, 2}))
        tx.on_partial(self.partial(msg_id, PEER_B, 4, {1, 2, 3}))
        clock.advance(600)
        self.assertEqual(tx.tick()[0][0][5], {0, 3})

    def test_a_peer_that_has_said_nothing_forces_a_full_resend(self):
        """Narrowing on partial information would strand the silent peer."""
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A, PEER_B])
        tx.note_fragment_count(msg_id, 4)
        tx.on_partial(self.partial(msg_id, PEER_A, 4, {0, 1, 2}))
        clock.advance(600)
        self.assertIsNone(tx.tick()[0][0][5])

    def test_a_fully_acked_peer_stops_constraining_the_retry(self):
        # Strict mode: narrowing a retry to the fragments some peer still
        # lacks is only meaningful while the message is still waiting on a
        # peer at all.
        tx, clock = make_tx(required_acks=99)
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A, PEER_B])
        tx.note_fragment_count(msg_id, 4)
        tx.on_partial(self.partial(msg_id, PEER_A, 4, {0, 1, 2}))
        tx.on_ack(codec.Ack(ME, msg_id, PEER_B))
        clock.advance(600)
        self.assertEqual(tx.tick()[0][0][5], {3})

    def test_a_partial_never_completes_a_message(self):
        """It says the opposite of an ack: these arrived, the rest did not."""
        tx, _ = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.note_fragment_count(msg_id, 4)
        tx.on_partial(self.partial(msg_id, PEER_A, 4, {0, 1, 2, 3}))
        self.assertEqual(tx.stats()["pending"], 1)
        self.assertEqual(tx.stats().get("delivered", 0), 0)

    def test_a_partial_does_not_postpone_the_retry(self):
        """A stalled peer reporting in is news about WHAT to send, not
        evidence anything is moving. Treating it as liveness would let one
        stuck peer hold a message in flight indefinitely."""
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.note_fragment_count(msg_id, 4)
        due = tx._pending[msg_id].next_try
        clock.advance(1.0)
        tx.on_partial(self.partial(msg_id, PEER_A, 4, {0}))
        self.assertEqual(tx._pending[msg_id].next_try, due)

    def test_a_report_against_a_different_split_is_ignored(self):
        """Indexed against a fragmentation we did not use, so trusting it would
        resend the wrong pieces."""
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.note_fragment_count(msg_id, 5)
        tx.on_partial(self.partial(msg_id, PEER_A, 3, {0, 1}))
        clock.advance(600)
        self.assertIsNone(tx.tick()[0][0][5])
        self.assertEqual(tx.stats()["partial_mismatch"], 1)

    def test_a_report_for_somebody_elses_message_is_ignored(self):
        tx, _ = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.note_fragment_count(msg_id, 4)
        stray = codec.decode_partial(
            codec.encode_partial(PEER_B, msg_id, PEER_A, 4, {0}))
        tx.on_partial(stray)
        self.assertEqual(tx.stats()["partial_unknown"], 1)

    def test_an_unfragmented_message_is_never_narrowed(self):
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.note_fragment_count(msg_id, 1)
        clock.advance(600)
        self.assertIsNone(tx.tick()[0][0][5])

    def test_a_partial_naming_nothing_held_asks_for_everything(self):
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.note_fragment_count(msg_id, 3)
        tx.on_partial(self.partial(msg_id, PEER_A, 3, set()))
        clock.advance(600)
        self.assertEqual(tx.tick()[0][0][5], {0, 1, 2})


class TestRetryOutlivesReassembly(unittest.TestCase):
    """The receiver's reassembly buffer has to outlive the sender's retry
    schedule, or fragments accumulated across attempts are discarded just
    before the attempt that would have completed them.

    Broken on 2026-08-12 by the multi-hop timeout fix earlier the same day:
    every base had been 2.0 s, finishing at 9.5 s inside a fixed 30 s buffer;
    the relay allowance took two hops to 30.9 s and three to 52 s.
    """

    def test_the_span_grows_with_distance(self):
        near = reliable.max_retry_span_s(hops=1)
        far = reliable.max_retry_span_s(hops=3)
        self.assertGreater(far, near)

    def test_the_span_covers_every_attempt(self):
        """Not just the last interval -- the buffer has to survive the whole
        schedule, so this is the sum rather than the final gap."""
        span = reliable.max_retry_span_s(hops=1)
        base = reliable.DEFAULT_MIN_TIMEOUT_S
        last_gap = base * (reliable.DEFAULT_BACKOFF **
                           (reliable.DEFAULT_MAX_ATTEMPTS - 2))
        self.assertGreater(span, last_gap)

    def test_the_old_fixed_buffer_would_not_have_covered_two_hops(self):
        """Pins the regression itself, so the numbers cannot drift back."""
        self.assertGreater(reliable.max_retry_span_s(hops=2, frame_len=200),
                           30.0)

    def test_a_bigger_frame_needs_a_longer_span(self):
        self.assertGreater(reliable.max_retry_span_s(frame_len=464),
                           reliable.max_retry_span_s(frame_len=0))

    @staticmethod
    def _airtime_at(bitrate):
        """Mirrors rns_bridge._airtime_s: +19 bytes for the RNS HEADER_1."""
        return lambda payload_len: ((payload_len + 19) * 8.0) / bitrate

    # SF8 and SF10 at 125 kHz / CR 4-5, which is what the radio reports back
    # and what RNodeInterface.updateBitrate() derives .bitrate from.
    _SF8_BPS = 3125.0
    _SF10_BPS = 977.0

    def test_a_slower_radio_stretches_the_span(self):
        """The same schedule takes longer on a slower link, because the base
        interval is derived from airtime rather than fixed."""
        self.assertGreater(
            reliable.max_retry_span_s(frame_len=464,
                                      airtime_s=self._airtime_at(self._SF10_BPS)),
            reliable.max_retry_span_s(frame_len=464,
                                      airtime_s=self._airtime_at(self._SF8_BPS)))

    def test_an_sf8_sized_buffer_does_not_cover_an_sf10_schedule(self):
        """Pins the second way these two can drift apart.

        2026-08-12 was the schedule growing past a fixed buffer. This is the
        mirror image: the buffer is derived correctly, but from a bitrate
        constant, so slowing the RADIO down moves the schedule underneath it.
        rns_bridge computes the buffer at import, when no interface exists to
        ask, and therefore at reliable._FALLBACK_BITRATE -- which is SF8. Hence
        _build_rnode_interface() re-sizing it once the radio reports in.
        """
        kwargs = dict(hops=4, frame_len=464)
        sf8_buffer = 1.25 * reliable.max_retry_span_s(
            airtime_s=self._airtime_at(self._SF8_BPS), **kwargs)
        sf10_span = reliable.max_retry_span_s(
            airtime_s=self._airtime_at(self._SF10_BPS), **kwargs)
        self.assertGreater(sf10_span, sf8_buffer)

        # ...and that re-sizing against the live bitrate is what fixes it.
        sf10_buffer = 1.25 * sf10_span
        self.assertGreater(sf10_buffer, sf10_span)

    def test_the_fallback_bitrate_is_still_the_one_sf8_produces(self):
        """The fallback is only correct while the shipping radio config is
        SF8/125kHz/CR5. If that changes, the import-time buffer changes with
        it -- so this fails rather than letting the constant quietly rot."""
        self.assertAlmostEqual(reliable._FALLBACK_BITRATE, self._SF8_BPS,
                               places=0)


class TestReceiverDedup(unittest.TestCase):
    def test_first_delivery_allowed_duplicate_suppressed(self):
        rx = reliable.ReliableRx(clock=FakeClock())
        self.assertTrue(rx.should_deliver(ME, 7))
        self.assertFalse(rx.should_deliver(ME, 7))
        self.assertFalse(rx.should_deliver(ME, 7))
        self.assertEqual(rx.stats()["duplicate"], 2)

    def test_same_msg_id_from_different_origins_are_distinct(self):
        rx = reliable.ReliableRx(clock=FakeClock())
        self.assertTrue(rx.should_deliver(PEER_A, 7))
        self.assertTrue(rx.should_deliver(PEER_B, 7))

    def test_entries_expire(self):
        clock = FakeClock()
        rx = reliable.ReliableRx(ttl_s=10.0, clock=clock)
        rx.should_deliver(ME, 7)
        clock.advance(11)
        # Past the TTL the sender has long since given up, so a frame with this
        # id is a new message rather than a retransmission.
        self.assertTrue(rx.should_deliver(ME, 7))
        self.assertEqual(rx.stats()["seen"], 1)

    def test_seen_cache_is_bounded(self):
        rx = reliable.ReliableRx(max_seen=16, clock=FakeClock())
        for i in range(500):
            rx.should_deliver(ME, i)
        self.assertEqual(rx.stats()["seen"], 16)


class TestNeverRaises(unittest.TestCase):
    """Matches the contract the rest of the bridge is built on: a bug in this
    module must not take the link down. TxPolicy.decide and Reassembler.offer
    make the same promise."""

    def test_on_ack_survives_junk(self):
        tx, _ = make_tx()
        for junk in (None, object(), codec.Ack(None, None, None),
                     codec.Ack(ME, "not an int", PEER_A)):
            self.assertIsNone(tx.on_ack(junk))

    def test_tick_survives_a_corrupted_entry(self):
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [PEER_A])
        for pending in tx._pending.values():
            pending.next_try = "not a number"
        clock.advance(60)
        self.assertEqual(tx.tick(), ([], []))
        self.assertEqual(tx.stats()["error"], 1)

    def test_should_deliver_fails_open(self):
        # Failing open costs a duplicate marker on a map; failing closed would
        # drop a real message, which is the whole thing this milestone exists
        # to prevent.
        def broken_clock():
            raise RuntimeError("clock exploded")

        rx = reliable.ReliableRx(clock=broken_clock)
        self.assertTrue(rx.should_deliver(ME, 1))
        self.assertEqual(rx.stats()["error"], 1)

    def test_snapshot_survives_a_corrupted_entry(self):
        tx, _ = make_tx()
        tx.register(a_frame(), ME, [PEER_A])
        for pending in tx._pending.values():
            pending.created = "not a number"
        self.assertEqual(tx.snapshot(), {"inflight": [], "undelivered": []})


class TestReporting(unittest.TestCase):
    def test_summary_line_counts(self):
        tx, clock = make_tx()
        msg_id, _ = tx.register(a_frame(), ME, [PEER_A])
        tx.on_ack(codec.Ack(ME, msg_id, PEER_A))
        tx.register(a_frame(), ME, [])
        line = tx.summary_line()
        self.assertIn("sent=1", line)
        self.assertIn("delivered=1", line)
        self.assertIn("solo=1", line)

    def test_snapshot_shape(self):
        tx, clock = make_tx()
        tx.register(a_frame(), ME, [PEER_A], label="chat 1")
        clock.advance(3)
        snap = tx.snapshot()
        self.assertEqual(len(snap["inflight"]), 1)
        entry = snap["inflight"][0]
        self.assertEqual(entry["label"], "chat 1")
        self.assertEqual(entry["waiting_on"], [PEER_A])
        self.assertEqual(entry["age_s"], 3.0)

    def test_undelivered_is_newest_first(self):
        tx, clock = make_tx(max_inflight=64, undelivered_kept=8)
        for i in range(3):
            tx.register(a_frame(), ME, [PEER_A], label="msg %d" % i)
            clock.advance(0.5)
        for _ in range(1000):
            clock.advance(0.1)
            tx.tick()
        labels = [u["label"] for u in tx.snapshot()["undelivered"]]
        self.assertEqual(labels, ["msg 2", "msg 1", "msg 0"])


class TestTwoNodeExchange(unittest.TestCase):
    """The exact sequence rns_bridge performs, without RNS in the way.

    Sender: encode -> register -> fragment -> radio.
    Receiver: reassemble -> unwrap -> ack -> should_deliver -> decode.
    Sender: decode_ack -> on_ack.

    Worth having as one test because each piece is correct in isolation and
    the composition is where a layering mistake would actually live.
    """

    def setUp(self):
        self.clock = FakeClock()
        self.tx = reliable.ReliableTx(clock=self.clock, rng=lambda: 0.0)
        self.rx = reliable.ReliableRx(clock=self.clock)
        self.reassembler = codec.Reassembler(clock=self.clock)

    def receive(self, packets, drop=()):
        """Returns (delivered_xml_or_None, ack_frame_or_None)."""
        whole = None
        for i, packet in enumerate(packets):
            if i in drop:
                continue
            got = self.reassembler.offer(packet)
            if got is not None:
                whole = got
        if whole is None:
            return None, None
        # ME is what the mesh envelope would have carried: since v15 the
        # wrapper no longer repeats the sender, so the receive path supplies it.
        wrapper = codec.unwrap_reliable(whole, ME)
        ack = codec.encode_ack(wrapper.origin, wrapper.msg_id, PEER_A)
        if not self.rx.should_deliver(wrapper.origin, wrapper.msg_id):
            return None, ack
        return codec.decode(wrapper.frame), ack

    def test_clean_delivery_in_one_pass(self):
        msg_id, wrapped = self.tx.register(a_frame(), ME, [PEER_A], "chat")
        xml, ack_frame = self.receive(codec.fragment(wrapped))
        self.assertIn(b"ALPHA", xml)
        self.assertIsNotNone(self.tx.on_ack(codec.decode_ack(ack_frame, PEER_A)))
        self.assertEqual(self.tx.stats()["pending"], 0)

    def test_lost_ack_causes_a_retry_that_is_acked_but_not_redelivered(self):
        # The failure the "acknowledge duplicates anyway" rule exists for: the
        # message arrived, our ack did not. Staying silent on the retry would
        # leave the sender retransmitting until it gave up, and reporting a
        # message undelivered that the user is already reading.
        _, wrapped = self.tx.register(a_frame(), ME, [PEER_A], "chat")
        xml, _lost_ack = self.receive(codec.fragment(wrapped))
        self.assertIsNotNone(xml)

        self.clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S)
        retries, _ = self.tx.tick()
        self.assertEqual(len(retries), 1)

        xml2, ack_frame = self.receive(codec.fragment(retries[0][1]))
        self.assertIsNone(xml2, "a duplicate must not reach ATAK twice")
        self.assertIsNotNone(ack_frame, "a duplicate must still be acked")
        self.assertIsNotNone(self.tx.on_ack(codec.decode_ack(ack_frame, PEER_A)))

    def test_lost_fragment_retransmits_the_whole_message(self):
        # No selective retry: the receiver acknowledges reassembled messages,
        # so an incomplete one is simply never acknowledged.
        _, wrapped = self.tx.register(b"\x11" + b"x" * 900, ME, [PEER_A], "big")
        packets = codec.fragment(wrapped, 300)
        self.assertGreater(len(packets), 1)
        xml, ack = self.receive(packets, drop={1})
        self.assertIsNone(xml)
        self.assertIsNone(ack)

        self.clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S * 3)
        retries, _ = self.tx.tick()
        self.assertEqual(len(retries), 1)
        whole = None
        for packet in codec.fragment(retries[0][1], 300):
            got = self.reassembler.offer(packet)
            if got is not None:
                whole = got
        self.assertIsNotNone(whole, "the retransmission must reassemble")
        self.assertEqual(codec.unwrap_reliable(whole).frame, b"\x11" + b"x" * 900)


class TestAirtime(unittest.TestCase):
    def test_default_estimate_matches_the_bridge_formula(self):
        # reliable.py cannot import RNS, so it carries a fallback copy of
        # rns_bridge._airtime_s. If the two drift apart, retry timing on the
        # phone stops matching what these tests exercise.
        self.assertAlmostEqual(reliable.default_airtime_s(284),
                               (284 + 19) * 8.0 / 3125)

    def test_a_large_frame_gets_a_longer_timeout(self):
        clock = FakeClock()
        tx = reliable.ReliableTx(clock=clock, rng=lambda: 0.0)
        tx.register(b"\x10" + b"x" * 4000, ME, [PEER_A])
        clock.advance(reliable.DEFAULT_MIN_TIMEOUT_S)
        # 4 KB is ~10 s of airtime, so the floor must not apply.
        self.assertEqual(tx.tick()[0], [])


class TestLinkLatency(unittest.TestCase):
    """A radio's fixed per-leg delay (a Meshtastic board's queue, back-off and
    decrypt) is charged on top of airtime, twice -- message out, ack back --
    and is 0 for an RNode, which must time exactly as it always did."""

    FLAT = staticmethod(lambda n: 1.0)      # 1 s of airtime for any frame

    def first_retry_at(self, **kw):
        tx, clock = make_tx(airtime_s=self.FLAT, **kw)
        tx.register(a_frame(), ME, [PEER_A])
        for step in range(1, 400):
            clock.t = step * 0.05          # set, not summed: no float drift
            if tx.tick()[0]:
                return round(clock.t, 2)
        return None

    def test_no_latency_is_the_rnode_timing_unchanged(self):
        self.assertEqual(self.first_retry_at(), 3.0)            # 2 x 1 + 1
        self.assertEqual(self.first_retry_at(link_latency_s=0.0), 3.0)

    def test_latency_is_charged_once_per_leg(self):
        self.assertEqual(self.first_retry_at(link_latency_s=0.9), 4.8)  # 3 + 2 x 0.9

    def test_a_callable_follows_the_live_radio(self):
        self.assertEqual(self.first_retry_at(link_latency_s=lambda: 0.5), 4.0)
        self.assertEqual(self.first_retry_at(link_latency_s=lambda: 0.0), 3.0)

    def test_a_broken_latency_source_never_stops_a_retry(self):
        def boom():
            raise RuntimeError("no radio")
        self.assertEqual(self.first_retry_at(link_latency_s=boom), 3.0)
        self.assertEqual(self.first_retry_at(link_latency_s=-5), 3.0)

    def test_the_span_includes_it_so_reassembly_outlives_the_retries(self):
        without = reliable.max_retry_span_s(airtime_s=self.FLAT)
        with_it = reliable.max_retry_span_s(airtime_s=self.FLAT, link_latency_s=1.0)
        # Three retries' bases each grow by 2 s, scaled by the backoff.
        grow = sum(2.0 * reliable.DEFAULT_BACKOFF ** k
                   for k in range(reliable.DEFAULT_MAX_ATTEMPTS - 1))
        self.assertAlmostEqual(with_it - without, grow)


if __name__ == "__main__":
    unittest.main()
