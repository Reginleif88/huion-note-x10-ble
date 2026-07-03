package xyz.reginleif.hinotesync.protocol

// Stroke codec per docs/offline-note-protocol.md §4/§10. Mirrors the app's
// BluetoothUtil#decodePackagePoint / BluePoint: points are parsed PER PACKET,
// N = (len-5)/6, remainder (incl. the checksum byte) discarded.

data class StylusPoint(val x: Int, val y: Int, val press: Int, val penDown: Boolean)

data class Limits(
    val maxX: Float = 28200f,
    val maxY: Float = 37400f,
    val maxPress: Float = 8191f,
)

class PageData(
    val index: Int,
    val limits: Limits,
    val strokes: List<List<StylusPoint>>,
    val complete: Boolean,
)

fun decodePoint(rec: ByteArray, off: Int): StylusPoint = StylusPoint(
    x = u8(rec[off]) or (u8(rec[off + 1]) shl 8),
    y = u8(rec[off + 2]) or (u8(rec[off + 3]) shl 8),
    press = u8(rec[off + 4]) or ((u8(rec[off + 5]) and 0x1F) shl 8),
    penDown = (u8(rec[off + 5]) shr 5) != 0,
)

fun decodePacket(pkt: ByteArray): List<StylusPoint> {
    val n = (pkt.size - 5) / 6
    return (0 until n).map { k -> decodePoint(pkt, 5 + k * 6) }
}

fun packetSeq(pkt: ByteArray): Int = u8(pkt[3]) or (u8(pkt[4]) shl 8)

fun parseMaxData(pkt: ByteArray): Limits {
    if (pkt.size < 11 || u8(pkt[1]) != OrderCode.MAX_DATA) return Limits()
    return Limits(
        maxX = ((u8(pkt[5]) shl 16) or (u8(pkt[4]) shl 8) or u8(pkt[3])).toFloat(),
        maxY = ((u8(pkt[8]) shl 16) or (u8(pkt[7]) shl 8) or u8(pkt[6])).toFloat(),
        maxPress = ((u8(pkt[10]) shl 8) or u8(pkt[9])).toFloat(),
    )
}

fun pointsToStrokes(points: List<StylusPoint>): List<List<StylusPoint>> {
    val strokes = mutableListOf<List<StylusPoint>>()
    var cur = mutableListOf<StylusPoint>()
    for (p in points) {
        if (!p.penDown || p.press == 0) {
            if (cur.size > 1) strokes.add(cur)
            cur = mutableListOf()
        } else {
            cur.add(p)
        }
    }
    if (cur.size > 1) strokes.add(cur)
    return strokes
}

fun decodePage(packets: List<ByteArray>, limits: Limits, index: Int, complete: Boolean): PageData =
    PageData(
        index = index,
        limits = limits,
        strokes = pointsToStrokes(packets.flatMap { decodePacket(it) }),
        complete = complete,
    )
