package xyz.reginleif.hinotesync.protocol

import org.junit.Assert.assertEquals
import org.junit.Test
import xyz.reginleif.hinotesync.render.pageJson

/**
 * Cross-implementation golden parity test (spec-mandated): proves the Kotlin codec +
 * JSON renderer produce byte-for-byte the same output as the Python reference
 * (huion_notes.codec + huion_notes.render) for the same 0x87 packets + MAX_DATA vector.
 *
 * The three packets are synthetic 0x87 stream packets (seq 1..3), 6 points each, mixing
 * pen-down, pen-up (status 0), zero-pressure boundaries and 13-bit pressure values
 * (incl. the 0x1FFF max). They also exercise cross-packet stroke continuation: the lone
 * trailing point of packet 2 joins the opening points of packet 3.
 *
 * EXPECTED_JSON was captured verbatim by running, from the repository ROOT:
 *
 *   python3 -c "
 *   from huion_notes import codec, render
 *   mk = bytes.fromhex('cd950b286e00189200ff1f')
 *   lim = codec.parse_max_data(mk)
 *   p1 = bytes.fromhex('cd877e010010012001052100020003ff3f50025003e82360026003002000030004f421100310045822ee')
 *   p2 = bytes.fromhex('cd877e020000040005000010041005d02720042005b82b30043005ff3f400440050000500450056420ee')
 *   p3 = bytes.fromhex('cd877e0300000500062a2010051006403f20052006002030053006583b40054006d224500550062e36ee')
 *   page = codec.decode_page([p1,p2,p3], lim, index=0)
 *   print(render.render_json(page))"
 */
class CodecParityTest {
    // MAX_DATA (0x95) vector: max_x=28200, max_y=37400, max_press=8191.
    private val MAX_DATA = "cd950b286e00189200ff1f"
    private val P1 = "cd877e010010012001052100020003ff3f50025003e82360026003002000030004f421100310045822ee"
    private val P2 = "cd877e020000040005000010041005d02720042005b82b30043005ff3f400440050000500450056420ee"
    private val P3 = "cd877e0300000500062a2010051006403f20052006002030053006583b40054006d224500550062e36ee"

    // Captured verbatim from the Python reference (see class KDoc for the exact command).
    private val EXPECTED_JSON =
        """{"page": 0, "max_x": 28200.0, "max_y": 37400.0, "max_press": 8191.0, "strokes": [[{"x": 272, "y": 288, "press": 261, "pen_down": true}, {"x": 512, "y": 768, "press": 8191, "pen_down": true}, {"x": 592, "y": 848, "press": 1000, "pen_down": true}], [{"x": 768, "y": 1024, "press": 500, "pen_down": true}, {"x": 784, "y": 1040, "press": 600, "pen_down": true}], [{"x": 1040, "y": 1296, "press": 2000, "pen_down": true}, {"x": 1056, "y": 1312, "press": 3000, "pen_down": true}, {"x": 1072, "y": 1328, "press": 8191, "pen_down": true}], [{"x": 1104, "y": 1360, "press": 100, "pen_down": true}, {"x": 1280, "y": 1536, "press": 42, "pen_down": true}, {"x": 1296, "y": 1552, "press": 8000, "pen_down": true}], [{"x": 1328, "y": 1584, "press": 7000, "pen_down": true}, {"x": 1344, "y": 1600, "press": 1234, "pen_down": true}, {"x": 1360, "y": 1616, "press": 5678, "pen_down": true}]]}"""

    @Test fun kotlinCodecAndJsonMatchPythonReferenceByteForByte() {
        val limits = parseMaxData(MAX_DATA.unhex())
        val page = decodePage(
            listOf(P1.unhex(), P2.unhex(), P3.unhex()),
            limits,
            index = 0,
            complete = true,
        )
        assertEquals(EXPECTED_JSON, pageJson(page))
    }
}
