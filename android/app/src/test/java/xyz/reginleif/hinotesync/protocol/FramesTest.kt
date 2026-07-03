package xyz.reginleif.hinotesync.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

fun ByteArray.hex(): String = joinToString("") { "%02x".format(it) }
fun String.unhex(): ByteArray = chunked(2).map { it.toInt(16).toByte() }.toByteArray()

class FramesTest {
    @Test fun commandBuildersMatchProtocolVectors() {
        assertEquals("cd950800000000ed", requestMaxInfo().hex())
        assertEquals("cd860801000000ed", requestPageData(1).hex())
        assertEquals("cd860800000000ed", requestPageData(0, 0).hex())
        assertEquals("cd880800000200ed", buildGetPagePackage(0, 2).hex())
        assertEquals("cd8b0803000000ed", buildDeletePage(3).hex())
        assertEquals("cd8c0800000000ed", buildClearCache().hex())
        assertEquals("cd800800000000ed", heartBeat().hex())
        val (d1, d2) = requestSetManyPacketDistance()
        assertEquals("cd960801030000ed", d1.hex())
        assertEquals("cd960803020000ed", d2.hex())
    }

    @Test fun sixteenBitPageAndIndexSplitLittleEndian() {
        assertEquals("cd860834021200ed", requestPageData(0x0234, 0x12).hex())
        assertEquals("cd88083402cd01ed", buildGetPagePackage(0x0234, 0x01cd).hex())
    }

    @Test fun parseHuionFrameRoundtrip() {
        val f = parseHuionFrame("cd82050100".unhex())!!
        assertEquals(OrderCode.VERIFY_RESULT, f.op)
        assertEquals(0x05, f.length)
        assertEquals(1, u8(f.raw[3]))
        assertNull(parseHuionFrame("ab8205".unhex()))   // wrong start byte
        assertNull(parseHuionFrame("cd82".unhex()))     // too short
    }

    @Test fun parseOfflineCountReadsU16LE() {
        assertEquals(3, parseOfflineCount("cd86050300".unhex()))
        assertEquals(0x0102, parseOfflineCount("cd86050201".unhex()))
        assertNull(parseOfflineCount("cd87050300".unhex()))  // wrong opcode
        assertNull(parseOfflineCount("cd8605".unhex()))      // too short
    }
}
