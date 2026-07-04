package xyz.reginleif.hinotesync.protocol

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

private val DOWN = byteArrayOf(0x10, 0x00, 0x10, 0x00, 0x05, 0x20)
private val DOWN2 = byteArrayOf(0x20, 0x00, 0x20, 0x00, 0x06, 0x20)

class FakeTransport(private val respond: (ByteArray) -> List<ByteArray>) : Transport {
    val sent = mutableListOf<ByteArray>()
    private val inbox = ArrayDeque<ByteArray>()
    override suspend fun connect() {}
    override suspend fun send(frame: ByteArray) { sent += frame; inbox.addAll(respond(frame)) }
    override suspend fun recv(timeoutMs: Long): ByteArray =
        inbox.removeFirstOrNull() ?: throw FrameTimeout()
    override suspend fun close() {}
}

/** Standard device script: auth ok (no PIN), limits, then pages as configured. */
private fun device(
    pages: Map<Int, List<ByteArray>>,          // page index -> stream packets (0x87)
    authStatus: Int = 1,
): (ByteArray) -> List<ByteArray> = { frame ->
    val f = parseHuionFrame(frame)!!
    when (f.op) {
        OrderCode.VERIFY_CONNECT -> listOf("cd8106164a45".unhex())        // challenge 22,74,69
        OrderCode.VERIFY_RESULT -> listOf(byteArrayOf(0xCD.toByte(), 0x82.toByte(), 0x05, authStatus.toByte(), 0x00))
        OrderCode.VERIFY_PWD ->
            if (u8(f.raw[3]) == 2) listOf("cd82050100".unhex()) else emptyList() // ok after 2nd PIN frame
        OrderCode.MAX_DATA -> listOf("cd950b286e00189200ff1f".unhex())
        OrderCode.CURRENT_PAGE -> {
            // Report a logic-page count one past the highest page index we hold.
            val count = (pages.keys.maxOrNull()?.plus(1)) ?: 0
            listOf(byteArrayOf(0xCD.toByte(), 0x85.toByte(), 0x05,
                (count and 0xFF).toByte(), ((count shr 8) and 0xFF).toByte()))
        }
        OrderCode.REQUEST_OFFLINE_DATA -> {
            val page = u8(f.raw[3]) or (u8(f.raw[4]) shl 8)
            val pkts = pages[page] ?: emptyList()
            listOf(byteArrayOf(0xCD.toByte(), 0x86.toByte(), 0x05,
                (pkts.size and 0xFF).toByte(), ((pkts.size shr 8) and 0xFF).toByte())) + pkts
        }
        OrderCode.DELETE_PAGE -> listOf("cd8b050100".unhex())
        else -> emptyList()
    }
}

class SyncEngineTest {
    @Test fun happyPathDumpsPagesUntilEmpty() = runTest {
        val t = FakeTransport(device(pages = mapOf(
            0 to listOf(packet(0x87, 1, listOf(DOWN, DOWN2)), packet(0x87, 2, listOf(DOWN, DOWN2))),
            1 to listOf(packet(0x87, 1, listOf(DOWN, DOWN2))),
        )))
        val got = mutableListOf<PageData>()
        val n = SyncEngine(t).run { got.add(it) }
        assertEquals(2, n)
        assertEquals(28200f, got[0].limits.maxX)   // limits came from the 0x95 reply
        assertTrue(got.all { it.complete })
        // stream packets carry pen-down runs with no pen-up separator between packets,
        // so page 0's four points merge into one stroke
        assertEquals(1, got[0].strokes.size)
        assertEquals(4, got[0].strokes[0].size)
    }

    @Test fun scansAllPagesWhenPageZeroIsEmpty() = runTest {
        // Regression (hardware-found): some firmware reports N logic pages with an EMPTY
        // page 0 and content only in later pages. The engine must ask CURRENT_PAGE for the
        // count and scan every page up to it, skipping empties — not stop at the first empty.
        val t = FakeTransport(device(pages = mapOf(
            2 to listOf(packet(0x87, 1, listOf(DOWN, DOWN2))),
            3 to listOf(packet(0x87, 1, listOf(DOWN, DOWN2)), packet(0x87, 2, listOf(DOWN, DOWN2))),
        )))
        val got = mutableListOf<PageData>()
        val n = SyncEngine(t).run { got.add(it) }
        assertEquals(2, n)                                       // pages 2 and 3, not halted by empty 0/1
        assertEquals(listOf(2, 3), got.map { it.index })
        assertTrue(t.sent.any { it.hex() == "cd850800000000ed" })  // CURRENT_PAGE was queried
    }

    @Test fun gapIsFilledViaGetPagePackage() = runTest {
        // The device() helper derives count from the packets it delivers, so a
        // deliberate hole (count=3, seq 2 missing) needs an explicit script:
        var page0Requested = false
        val t = FakeTransport { frame ->
            val f = parseHuionFrame(frame)!!
            when {
                f.op == OrderCode.VERIFY_CONNECT -> listOf("cd8106164a45".unhex())
                f.op == OrderCode.VERIFY_RESULT -> listOf("cd82050100".unhex())
                f.op == OrderCode.MAX_DATA -> listOf("cd950b286e00189200ff1f".unhex())
                f.op == OrderCode.REQUEST_OFFLINE_DATA && !page0Requested -> {
                    page0Requested = true
                    listOf("cd86050300".unhex(),                       // count = 3
                        packet(0x87, 1, listOf(DOWN, DOWN2)),
                        packet(0x87, 3, listOf(DOWN, DOWN2)))          // seq 2 missing
                }
                f.op == OrderCode.REQUEST_OFFLINE_DATA -> listOf("cd86050000".unhex()) // page 1 empty
                f.op == OrderCode.GET_PAGE_PACKAGE -> listOf(packet(0x88, 2, listOf(DOWN, DOWN2)))
                else -> emptyList()
            }
        }
        val got = mutableListOf<PageData>()
        SyncEngine(t).run { got.add(it) }
        assertEquals(1, got.size)
        assertTrue(got[0].complete)
        assertEquals(6, got[0].strokes.sumOf { it.size })              // 3 packets × 2 points
        assertTrue(t.sent.any { it.hex() == "cd880800000200ed" })      // retransmit request for seq 2
    }

    @Test fun incompletePageIsFlaggedNotDropped() = runTest {
        var asked = false
        val t = FakeTransport { frame ->
            val f = parseHuionFrame(frame)!!
            when {
                f.op == OrderCode.VERIFY_CONNECT -> listOf("cd8106164a45".unhex())
                f.op == OrderCode.VERIFY_RESULT -> listOf("cd82050100".unhex())
                f.op == OrderCode.MAX_DATA -> listOf("cd950b286e00189200ff1f".unhex())
                f.op == OrderCode.REQUEST_OFFLINE_DATA && !asked -> {
                    asked = true
                    listOf("cd86050200".unhex(), packet(0x87, 1, listOf(DOWN, DOWN2))) // 1 of 2
                }
                f.op == OrderCode.REQUEST_OFFLINE_DATA -> listOf("cd86050000".unhex())
                else -> emptyList()                                    // retransmits go unanswered
            }
        }
        val got = mutableListOf<PageData>()
        SyncEngine(t).run { got.add(it) }
        assertEquals(1, got.size)
        assertFalse(got[0].complete)
    }

    @Test fun pinRequiredThrowsWithoutPinAndSucceedsWithIt() = runTest {
        fun pinDevice(): (ByteArray) -> List<ByteArray> {
            var pinSeen = false
            return { frame ->
                val f = parseHuionFrame(frame)!!
                when (f.op) {
                    OrderCode.VERIFY_CONNECT -> listOf("cd8106164a45".unhex())
                    OrderCode.VERIFY_RESULT -> listOf(if (pinSeen) "cd82050100".unhex() else "cd82050200".unhex())
                    OrderCode.VERIFY_PWD -> { if (u8(f.raw[3]) == 2) { pinSeen = true; listOf("cd82050100".unhex()) } else emptyList() }
                    OrderCode.MAX_DATA -> listOf("cd950b286e00189200ff1f".unhex())
                    OrderCode.REQUEST_OFFLINE_DATA -> listOf("cd86050000".unhex())
                    else -> emptyList()
                }
            }
        }
        assertThrows(PinRequired::class.java) {
            kotlinx.coroutines.runBlocking { SyncEngine(FakeTransport(pinDevice())).run { } }
        }
        val t = FakeTransport(pinDevice())
        assertEquals(0, SyncEngine(t, pin = "123456").run { })
        assertTrue(t.sent.any { it.hex() == "cd83080199a79ced" })      // PIN frame 1 was sent
    }

    @Test fun deletePageConfirmed() = runTest {
        val t = FakeTransport(device(pages = emptyMap()))
        assertTrue(SyncEngine(t).deletePage(0))
        assertTrue(t.sent.any { it.hex() == "cd8b0800000000ed" })
    }
}
