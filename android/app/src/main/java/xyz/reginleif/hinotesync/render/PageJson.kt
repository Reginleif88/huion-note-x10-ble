package xyz.reginleif.hinotesync.render

import xyz.reginleif.hinotesync.protocol.PageData

/** Byte-compatible with huion_notes.render.render_json (json.dumps defaults). */
fun pageJson(page: PageData): String {
    val sb = StringBuilder()
    sb.append("{\"page\": ").append(page.index)
    sb.append(", \"max_x\": ").append(page.limits.maxX.toDouble())
    sb.append(", \"max_y\": ").append(page.limits.maxY.toDouble())
    sb.append(", \"max_press\": ").append(page.limits.maxPress.toDouble())
    sb.append(", \"strokes\": [")
    page.strokes.forEachIndexed { si, stroke ->
        if (si > 0) sb.append(", ")
        sb.append("[")
        stroke.forEachIndexed { pi, p ->
            if (pi > 0) sb.append(", ")
            sb.append("{\"x\": ").append(p.x)
                .append(", \"y\": ").append(p.y)
                .append(", \"press\": ").append(p.press)
                .append(", \"pen_down\": ").append(p.penDown)
                .append("}")
        }
        sb.append("]")
    }
    sb.append("]}")
    return sb.toString()
}
