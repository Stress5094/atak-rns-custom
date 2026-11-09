package io.github.stress5094.customrnsbridge

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class MeshtasticBleFramingTest {

    private fun frame(payload: ByteArray) = MeshtasticBleFraming.wrap(payload)
    private fun bytes(vararg v: Int) = ByteArray(v.size) { v[it].toByte() }

    @Test
    fun wrapPutsTheStreamHeaderBack() {
        val out = frame(ByteArray(300) { it.toByte() })
        assertEquals(304, out.size)
        assertArrayEquals(bytes(0x94, 0xC3, 0x01, 0x2C), out.copyOfRange(0, 4))
        assertEquals(0.toByte(), out[4])
    }

    @Test
    fun theConnectWakeUpIsSkippedAndTheFrameAfterItSurvives() {
        // What StreamInterface.connect() writes: 32 x 0xC3, then want_config.
        val d = MeshtasticBleFraming.ToRadioDeframer()
        val payload = bytes(0x18, 0x2A)
        val out = d.feed(ByteArray(32) { 0xC3.toByte() } + frame(payload))
        assertEquals(1, out.size)
        assertArrayEquals(payload, out[0])
        assertEquals(32L, d.skipped)
    }

    @Test
    fun aFrameSplitAtEveryByteBoundaryComesOutWhole() {
        val payload = ByteArray(231) { (it * 7).toByte() }
        val stream = frame(payload)
        val d = MeshtasticBleFraming.ToRadioDeframer()
        val out = stream.flatMap { d.feed(byteArrayOf(it)) }
        assertEquals(1, out.size)
        assertArrayEquals(payload, out[0])
    }

    @Test
    fun severalFramesInOneReadAllComeOut() {
        val a = bytes(1, 2, 3)
        val b = ByteArray(0x94) { 0x94.toByte() } // payload full of START1
        val c = bytes(0xC3)
        val out = MeshtasticBleFraming.ToRadioDeframer().feed(frame(a) + frame(b) + frame(c))
        assertEquals(3, out.size)
        assertArrayEquals(a, out[0]); assertArrayEquals(b, out[1]); assertArrayEquals(c, out[2])
    }

    @Test
    fun aRepeatedStart1DoesNotLoseTheRealHeader() {
        val payload = bytes(9)
        val out = MeshtasticBleFraming.ToRadioDeframer().feed(bytes(0x94) + frame(payload))
        assertEquals(1, out.size)
        assertArrayEquals(payload, out[0])
    }

    @Test
    fun anOversizedLengthIsNotAHeaderAndTheNextFrameStillParses() {
        val payload = bytes(5, 6)
        val junk = bytes(0x94, 0xC3, 0x02, 0x01) // 513 B claimed
        val out = MeshtasticBleFraming.ToRadioDeframer().feed(junk + frame(payload))
        assertEquals(1, out.size)
        assertArrayEquals(payload, out[0])
    }

    @Test
    fun resetDropsAHalfFrame() {
        val d = MeshtasticBleFraming.ToRadioDeframer()
        d.feed(frame(ByteArray(50)).copyOfRange(0, 20))
        d.reset()
        val payload = bytes(7, 7)
        val out = d.feed(ByteArray(32) { 0xC3.toByte() } + frame(payload))
        assertEquals(1, out.size)
        assertArrayEquals(payload, out[0])
    }

    @Test
    fun theCountArgumentLimitsWhatIsRead() {
        val payload = bytes(1, 2)
        val buf = frame(payload) + bytes(0x94, 0xC3, 0, 1, 0x55)
        val out = MeshtasticBleFraming.ToRadioDeframer().feed(buf, 6)
        assertEquals(1, out.size)
    }
}
