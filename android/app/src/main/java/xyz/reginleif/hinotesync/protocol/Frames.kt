package xyz.reginleif.hinotesync.protocol

// Framing per docs/offline-note-protocol.md §2/§3/§7: cd <op> <len> <payload> [ed]

const val START = 0xCD
const val END = 0xED

fun u8(b: Byte): Int = b.toInt() and 0xFF

object OrderCode {
    const val HEART_BEAT = 0x80
    const val VERIFY_CONNECT = 0x81
    const val VERIFY_RESULT = 0x82
    const val VERIFY_PWD = 0x83
    const val MODE = 0x84
    const val CURRENT_PAGE = 0x85
    const val REQUEST_OFFLINE_DATA = 0x86
    const val RETURN_OFFLINE_DATA = 0x87
    const val GET_PAGE_PACKAGE = 0x88   // also the retransmit channel (§10)
    const val NEXT_PAGE = 0x8A          // device→app "page created" notice
    const val DELETE_PAGE = 0x8B        // destructive
    const val CLEAR_CACHE = 0x8C        // destructive
    const val DEVICE_NAME = 0x91
    const val GET_PWD = 0x93
    const val MAX_DATA = 0x95
    const val SET_MANY_PACKET_DISTANCE = 0x96
    const val VERSION = 0xC9
}

class HuionFrame(val op: Int, val length: Int, val raw: ByteArray)

fun parseHuionFrame(value: ByteArray): HuionFrame? {
    if (value.size < 3 || u8(value[0]) != START) return null
    return HuionFrame(op = u8(value[1]), length = u8(value[2]), raw = value)
}

fun buildCommand(op: Int, a: Int = 0, b: Int = 0, c: Int = 0, d: Int = 0): ByteArray =
    byteArrayOf(
        START.toByte(), op.toByte(), 0x08,
        a.toByte(), b.toByte(), c.toByte(), d.toByte(), END.toByte(),
    )

fun requestMaxInfo(): ByteArray = buildCommand(OrderCode.MAX_DATA)

fun requestSetManyPacketDistance(): Pair<ByteArray, ByteArray> = Pair(
    buildCommand(OrderCode.SET_MANY_PACKET_DISTANCE, 1, 3, 0, 0),
    buildCommand(OrderCode.SET_MANY_PACKET_DISTANCE, 3, 2, 0, 0),
)

fun requestPageData(page: Int, sub: Int = 0): ByteArray =
    buildCommand(OrderCode.REQUEST_OFFLINE_DATA, page and 0xFF, (page shr 8) and 0xFF, sub and 0xFF, 0)

fun buildGetPagePackage(page: Int, idx: Int): ByteArray = byteArrayOf(
    START.toByte(), OrderCode.GET_PAGE_PACKAGE.toByte(), 0x08,
    (page and 0xFF).toByte(), ((page shr 8) and 0xFF).toByte(),
    (idx and 0xFF).toByte(), ((idx shr 8) and 0xFF).toByte(), END.toByte(),
)

fun buildDeletePage(page: Int): ByteArray = byteArrayOf(
    START.toByte(), OrderCode.DELETE_PAGE.toByte(), 0x08,
    (page and 0xFF).toByte(), ((page shr 8) and 0xFF).toByte(), 0x00, 0x00, END.toByte(),
)

fun buildClearCache(): ByteArray = buildCommand(OrderCode.CLEAR_CACHE)

fun heartBeat(): ByteArray = buildCommand(OrderCode.HEART_BEAT)

fun parseOfflineCount(value: ByteArray): Int? {
    if (value.size >= 5 && u8(value[0]) == START &&
        u8(value[1]) == OrderCode.REQUEST_OFFLINE_DATA && u8(value[2]) == 0x05
    ) return u8(value[3]) or (u8(value[4]) shl 8)
    return null
}
