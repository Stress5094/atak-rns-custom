package io.github.stress5094.customrnsbridge

import org.junit.Assert.assertEquals
import org.junit.Test

class PeerQueueTest {

    private val waiting = HashMap<String, Long>()

    private fun pass(listed: List<String>, needs: Set<String>, now: Long) =
        PeerQueue.order(listed, needs, waiting, now)

    @Test
    fun newPeersAreTakenInTheOrderTheyReported() {
        // peer_positions lists newest first: C reported last.
        assertEquals(listOf("A", "B", "C"),
            pass(listOf("C", "B", "A"), setOf("A", "B", "C"), 0))
    }

    @Test
    fun aPeerStillWaitingGoesBeforeANewerOne() {
        // The 2026-10-20 case: H waited, I arrived, I was served first.
        pass(listOf("H"), setOf("H"), 0)
        assertEquals(listOf("H", "I"), pass(listOf("I", "H"), setOf("H", "I"), 15))
        // And it keeps its place pass after pass while the limit holds it.
        assertEquals(listOf("H", "I", "J"),
            pass(listOf("J", "I", "H"), setOf("H", "I", "J"), 30))
    }

    @Test
    fun aServedPeerLeavesTheQueueAndRejoinsAtTheBack() {
        pass(listOf("B", "A"), setOf("A", "B"), 0)
        assertEquals(listOf("B"), pass(listOf("B", "A"), setOf("B"), 15))   // A drawn
        // A moves 100 m and needs work again: it is behind B, not ahead.
        assertEquals(listOf("B", "A"), pass(listOf("B", "A"), setOf("A", "B"), 30))
    }

    @Test
    fun peersNeedingNothingAreNotReturned() {
        assertEquals(listOf("B"), pass(listOf("C", "B", "A"), setOf("B"), 0))
        assertEquals(setOf("B"), waiting.keys)
    }

    @Test
    fun aPeerThatLeftTheMapIsForgotten() {
        pass(listOf("A"), setOf("A"), 0)
        assertEquals(emptyList<String>(), pass(emptyList(), emptySet(), 15))
        assertEquals(emptySet<String>(), waiting.keys)
    }
}
