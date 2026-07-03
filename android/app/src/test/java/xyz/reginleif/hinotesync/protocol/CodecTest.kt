package xyz.reginleif.hinotesync.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

private val DOWN = byteArrayOf(0x10, 0x00, 0x10, 0x00, 0x05, 0x20)
private val DOWN2 = byteArrayOf(0x20, 0x00, 0x20, 0x00, 0x06, 0x20)

/** Build a 0x87/0x88 packet: cd <op> 7e <seq u16 LE> <points> <checksum byte>. */
fun packet(op: Int, seq: Int, pts: List<ByteArray>): ByteArray {
    var out = byteArrayOf(0xCD.toByte(), op.toByte(), 0x7E, (seq and 0xFF).toByte(), ((seq shr 8) and 0xFF).toByte())
    for (p in pts) out += p
    return out + byteArrayOf(0xEE.toByte()) // trailing checksum byte (dropped by decoder)
}

class CodecTest {
    @Test fun decodePointLayout() {
        val p = decodePoint(byteArrayOf(0x31, 0x0D, 0x00, 0x00, 0xDB.toByte(), 0x20), 0)
        assertEquals(3377, p.x); assertEquals(0, p.y); assertEquals(219, p.press)
        assertTrue(p.penDown)
    }

    @Test fun pressureHighBitsAndPenUp() {
        val p = decodePoint(byteArrayOf(0x00, 0x00, 0x00, 0x00, 0x10, 0x1F), 0)
        assertEquals(31 * 256 + 0x10, p.press)
        assertFalse(p.penDown)
    }

    @Test fun packetParsesNPointsAndDropsRemainder() {
        val pts = decodePacket(packet(0x87, 1, listOf(DOWN, DOWN2)))
        assertEquals(listOf(16 to 16, 32 to 32), pts.map { it.x to it.y })
    }

    @Test fun packetSeqLittleEndian() {
        assertEquals(0x0102, packetSeq(byteArrayOf(0xCD.toByte(), 0x87.toByte(), 0x7E, 0x02, 0x01)))
    }

    @Test fun parseMaxDataVector() {
        val lim = parseMaxData("cd950b286e00189200ff1f".unhex())
        assertEquals(28200f, lim.maxX); assertEquals(37400f, lim.maxY); assertEquals(8191f, lim.maxPress)
    }

    @Test fun parseMaxDataDefaultsOnWrongOpcode() {
        assertEquals(Limits(), parseMaxData("cd870b286e00189200ff1f".unhex()))
    }

    @Test fun splitsOnPenUpAndZeroPressureDropsSingles() {
        val up = StylusPoint(0, 0, 0, false)
        fun dn(v: Int, press: Int = 100) = StylusPoint(v, v, press, true)
        assertEquals(listOf(2, 2), pointsToStrokes(listOf(dn(1), dn(2), up, dn(3), dn(4))).map { it.size })
        // lone trailing point dropped (<2 points)
        assertEquals(listOf(2), pointsToStrokes(listOf(dn(1), dn(2), up, dn(3))).map { it.size })
        // zero pressure splits even when penDown
        assertEquals(listOf(2, 2),
            pointsToStrokes(listOf(dn(1), dn(2), dn(3, press = 0), dn(4), dn(5))).map { it.size })
    }

    @Test fun decodePageAssembles() {
        val page = decodePage(listOf(packet(0x87, 1, listOf(DOWN, DOWN2))), Limits(100f, 200f, 8191f), 2, true)
        assertEquals(2, page.index)
        assertEquals(100f, page.limits.maxX)
        assertEquals(1, page.strokes.size)
        assertTrue(page.complete)
    }
}
