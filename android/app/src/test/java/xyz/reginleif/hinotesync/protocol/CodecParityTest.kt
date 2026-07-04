package xyz.reginleif.hinotesync.protocol

import org.junit.Assert.assertEquals
import org.junit.Test
import xyz.reginleif.hinotesync.render.pageSvg

/**
 * Cross-implementation golden parity test (spec-mandated): proves the Kotlin codec +
 * SVG renderer produce byte-for-byte the same output as the Python reference
 * (huion_notes.codec + huion_notes.render) for the same 0x87 packets + MAX_DATA vector.
 *
 * SVG is the shipped upload format, so this pins parity on what actually leaves the app.
 * The three packets are synthetic 0x87 stream packets (seq 1..3), 6 points each, mixing
 * pen-down, pen-up (status 0), zero-pressure boundaries and 13-bit pressure values
 * (incl. the 0x1FFF max). They also exercise cross-packet stroke continuation: the lone
 * trailing point of packet 2 joins the opening points of packet 3.
 *
 * EXPECTED_SVG was captured verbatim by running, from the repository ROOT:
 *
 *   python3 -c "
 *   from huion_notes import codec, render
 *   mk = bytes.fromhex('cd950b286e00189200ff1f')
 *   lim = codec.parse_max_data(mk)
 *   p1 = bytes.fromhex('cd877e010010012001052100020003ff3f50025003e82360026003002000030004f421100310045822ee')
 *   p2 = bytes.fromhex('cd877e020000040005000010041005d02720042005b82b30043005ff3f400440050000500450056420ee')
 *   p3 = bytes.fromhex('cd877e0300000500062a2010051006403f20052006002030053006583b40054006d224500550062e36ee')
 *   page = codec.decode_page([p1,p2,p3], lim, index=0)
 *   print(render.render_svg(page))"
 */
class CodecParityTest {
    // MAX_DATA (0x95) vector: max_x=28200, max_y=37400, max_press=8191.
    private val MAX_DATA = "cd950b286e00189200ff1f"
    private val P1 = "cd877e010010012001052100020003ff3f50025003e82360026003002000030004f421100310045822ee"
    private val P2 = "cd877e020000040005000010041005d02720042005b82b30043005ff3f400440050000500450056420ee"
    private val P3 = "cd877e0300000500062a2010051006403f20052006002030053006583b40054006d224500550062e36ee"

    // Captured verbatim from the Python reference (see class KDoc for the exact command).
    private val EXPECTED_SVG =
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"900\" height=\"1190\" style=\"background:#fff\">" +
            "<path d=\"M23.4,23.9 L30.8,38.8 L33.3,41.3\" fill=\"none\" stroke=\"#111\" stroke-width=\"2.5\"/>" +
            "<path d=\"M38.7,46.8 L39.2,47.3\" fill=\"none\" stroke=\"#111\" stroke-width=\"2.5\"/>" +
            "<path d=\"M47.1,55.2 L47.6,55.7 L48.1,56.2\" fill=\"none\" stroke=\"#111\" stroke-width=\"2.5\"/>" +
            "<path d=\"M49.1,57.2 L54.5,62.6 L55.0,63.1\" fill=\"none\" stroke=\"#111\" stroke-width=\"2.5\"/>" +
            "<path d=\"M56.0,64.1 L56.5,64.6 L57.0,65.1\" fill=\"none\" stroke=\"#111\" stroke-width=\"2.5\"/>" +
            "</svg>"

    @Test fun kotlinCodecAndSvgMatchPythonReferenceByteForByte() {
        val limits = parseMaxData(MAX_DATA.unhex())
        val page = decodePage(
            listOf(P1.unhex(), P2.unhex(), P3.unhex()),
            limits,
            index = 0,
            complete = true,
        )
        assertEquals(EXPECTED_SVG, pageSvg(page))
    }
}
