package xyz.reginleif.hinotesync.render

import xyz.reginleif.hinotesync.protocol.PageData
import java.math.BigDecimal
import java.math.RoundingMode

/**
 * strokes -> SVG paths. Byte-compatible with huion_notes.render.render_svg:
 * origin top-left, no axis flip (non-A4 device), `<path d="M.. L..">` per stroke.
 *
 * Two details are load-bearing for byte-parity with the Python reference:
 *  - arithmetic is done in Double (Python uses float64; Limits stores Float),
 *  - coordinates are rounded to one decimal with round-half-EVEN on the exact
 *    double value, matching CPython's `%.1f` (dtoa). java.lang.String.format
 *    rounds half-UP, which would diverge on .x5 boundaries, so we use BigDecimal.
 */
fun pageSvg(page: PageData, width: Int = 900, height: Int = 1190, pad: Int = 15): String {
    fun sx(x: Int): Double = pad + (x / page.limits.maxX.toDouble()) * (width - 2 * pad)
    fun sy(y: Int): Double = pad + (y / page.limits.maxY.toDouble()) * (height - 2 * pad)
    fun fmt1(v: Double): String = BigDecimal(v).setScale(1, RoundingMode.HALF_EVEN).toPlainString()

    val sb = StringBuilder()
    sb.append("<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"").append(width)
        .append("\" height=\"").append(height).append("\" style=\"background:#fff\">")
    for (stroke in page.strokes) {
        sb.append("<path d=\"")
        stroke.forEachIndexed { i, p ->
            if (i > 0) sb.append(" ")
            sb.append(if (i == 0) "M" else "L")
                .append(fmt1(sx(p.x))).append(",").append(fmt1(sy(p.y)))
        }
        sb.append("\" fill=\"none\" stroke=\"#111\" stroke-width=\"2.5\"/>")
    }
    sb.append("</svg>")
    return sb.toString()
}
