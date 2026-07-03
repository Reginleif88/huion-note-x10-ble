package xyz.reginleif.hinotesync.protocol

// Keyless local auth per docs/offline-note-protocol.md §6.

private val PWD_OFFSETS = intArrayOf(104, 117, 105, 111, 110, 35) // ascii("huion#")

fun verifyResponse(a: Int, b: Int, c: Int): Triple<Int, Int, Int> = Triple(
    ((a + b) shl 2) % 255,
    ((b + c) shl 2) % 255,
    ((c + 10) shl 2) % 255,
)

fun buildVerifyResult(a: Int, b: Int, c: Int): ByteArray {
    val (r1, r2, r3) = verifyResponse(a, b, c)
    return buildCommand(OrderCode.VERIFY_RESULT, r1, r2, r3, 0)
}

fun encodePwd(pin: String): IntArray {
    require(pin.length == 6 && pin.all { it.isDigit() }) { "PIN must be exactly 6 digits" }
    return IntArray(6) { i -> pin[i].code + PWD_OFFSETS[i] }
}

fun buildVerifyPwdFrames(pin: String): Pair<ByteArray, ByteArray> {
    val e = encodePwd(pin)
    return Pair(
        buildCommand(OrderCode.VERIFY_PWD, 0x01, e[0], e[1], e[2]),
        buildCommand(OrderCode.VERIFY_PWD, 0x02, e[3], e[4], e[5]),
    )
}
