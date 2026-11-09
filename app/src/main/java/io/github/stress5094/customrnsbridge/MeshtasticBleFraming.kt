package io.github.stress5094.customrnsbridge

import java.io.ByteArrayOutputStream

/**
 * Translates between the Meshtastic serial stream and its Bluetooth mailboxes.
 *
 * Down a cable a Meshtastic board speaks a byte stream: every protobuf goes
 * out as `0x94 0xC3 <len hi> <len lo> <payload>`, and the official Python
 * package (mt_port.py's `_StreamBoard`) reads and writes exactly that. Over
 * Bluetooth the same protobufs travel one per GATT operation instead -- each
 * ToRadio is one write, each FromRadio is one read -- with no header at all.
 *
 * This is the whole difference, so the bridge rebuilds the stream at the
 * phone: FromRadio reads get the header put back on ([wrap]), and the stream
 * Python writes is cut back into whole ToRadio messages ([ToRadioDeframer]).
 * Python cannot tell the two wires apart, which is the same property the RNode
 * Bluetooth link has and the reason no Python changes were needed.
 *
 * No Android imports, on purpose: this is the part that can be wrong in a way
 * no log would show (a message cut in the wrong place is a protobuf the board
 * silently discards), so it is tested on the JVM.
 */
object MeshtasticBleFraming {
    const val START1 = 0x94
    const val START2 = 0xC3
    const val HEADER_LEN = 4

    /** The package's own MAX_TO_FROM_RADIO_SIZE; a longer "length" is noise. */
    const val MAX_MESSAGE = 512

    /** One FromRadio as the stream would have carried it. */
    fun wrap(message: ByteArray): ByteArray {
        require(message.size <= MAX_MESSAGE) { "FromRadio of ${message.size} B exceeds $MAX_MESSAGE" }
        val out = ByteArray(HEADER_LEN + message.size)
        out[0] = START1.toByte()
        out[1] = START2.toByte()
        out[2] = (message.size shr 8).toByte()
        out[3] = (message.size and 0xFF).toByte()
        System.arraycopy(message, 0, out, HEADER_LEN, message.size)
        return out
    }

    /**
     * Cuts Python's outgoing stream into whole ToRadio messages.
     *
     * Bytes outside a frame are expected, not an error: the package writes 32
     * bytes of 0xC3 at every connect to make a serial board resynchronise.
     * They are counted in [skipped] rather than logged one by one.
     *
     * The socket hands over whatever read() returned, so a frame can arrive in
     * any number of pieces and several frames can share one piece. State is
     * kept across [feed] calls for exactly that reason; [reset] on a new
     * connection, so half a frame from a dead link is not glued to the next.
     */
    class ToRadioDeframer {
        private var state = 0          // 0: want START1, 1: want START2, 2/3: length bytes, 4: payload
        private var length = 0
        private val payload = ByteArrayOutputStream()

        /** Bytes thrown away outside any frame, including the connect wake-up. */
        var skipped = 0L
            private set

        fun reset() {
            state = 0
            length = 0
            payload.reset()
        }

        fun feed(data: ByteArray, count: Int = data.size): List<ByteArray> {
            val out = ArrayList<ByteArray>(1)
            for (i in 0 until count) {
                val b = data[i].toInt() and 0xFF
                when (state) {
                    0 -> if (b == START1) state = 1 else skipped++
                    1 -> when (b) {
                        START2 -> state = 2
                        // A repeated START1 may be the real one.
                        START1 -> skipped++
                        else -> { skipped += 2; state = 0 }
                    }
                    2 -> { length = b shl 8; state = 3 }
                    3 -> {
                        length = length or b
                        if (length > MAX_MESSAGE) {
                            // Not a header after all. Same choice the package's
                            // own reader makes: drop it and hunt for the next.
                            skipped += HEADER_LEN
                            state = 0
                        } else if (length == 0) {
                            out.add(ByteArray(0))
                            state = 0
                        } else {
                            payload.reset()
                            state = 4
                        }
                    }
                    else -> {
                        payload.write(b)
                        if (payload.size() == length) {
                            out.add(payload.toByteArray())
                            payload.reset()
                            state = 0
                        }
                    }
                }
            }
            return out
        }
    }
}
