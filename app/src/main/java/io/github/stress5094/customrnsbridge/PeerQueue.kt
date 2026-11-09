package io.github.stress5094.customrnsbridge

/**
 * The order the Coverage tab works through other nodes' footprints in.
 *
 * Longest-waiting first. A footprint outside every saved area needs a
 * download, and coverage.compute_peer() allows one of those per
 * PEER_FETCH_MIN_INTERVAL_S across ALL peers -- so whichever peer asks first
 * when the window opens gets it. Taken newest report first (the order
 * rns_bridge.peer_positions() lists them), a peer could wait behind every
 * newcomer indefinitely; measured 2026-10-20, the peer added first was still
 * waiting after the one added after it had been drawn.
 */
object PeerQueue {

    /**
     * [listed] is every peer on the map, newest report first; [needs] the
     * ones without a current footprint. Returns those to work on, longest
     * waiting first, with peers new to the queue joining the back in the
     * order they reported. [waitingSince] carries the queue between passes
     * and is updated: a peer that no longer needs work leaves it, so if it
     * needs work again later it starts again at the back.
     */
    fun order(listed: List<String>, needs: Set<String>,
              waitingSince: MutableMap<String, Long>, now: Long): List<String> {
        waitingSince.keys.retainAll(needs)
        val oldestReportFirst = listed.asReversed().filter { it in needs }
        for (id in oldestReportFirst) waitingSince.getOrPut(id) { now }
        return oldestReportFirst.sortedBy { waitingSince.getValue(it) }   // stable
    }
}
