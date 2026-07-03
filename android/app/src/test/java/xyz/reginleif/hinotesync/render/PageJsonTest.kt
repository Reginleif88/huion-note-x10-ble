package xyz.reginleif.hinotesync.render

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test
import xyz.reginleif.hinotesync.protocol.Limits
import xyz.reginleif.hinotesync.protocol.PageData
import xyz.reginleif.hinotesync.protocol.StylusPoint

class PageJsonTest {
    private val page = PageData(
        index = 2,
        limits = Limits(28200f, 37400f, 8191f),
        strokes = listOf(listOf(StylusPoint(16, 16, 5, true), StylusPoint(32, 32, 6, true))),
        complete = true,
    )

    @Test fun matchesPythonRenderJsonByteForByte() {
        // Exact output of huion_notes.render.render_json (json.dumps default separators)
        val expected = "{\"page\": 2, \"max_x\": 28200.0, \"max_y\": 37400.0, " +
            "\"max_press\": 8191.0, \"strokes\": [[" +
            "{\"x\": 16, \"y\": 16, \"press\": 5, \"pen_down\": true}, " +
            "{\"x\": 32, \"y\": 32, \"press\": 6, \"pen_down\": true}]]}"
        assertEquals(expected, pageJson(page))
    }

    @Test fun parsesAsValidJsonWithExpectedStructure() {
        val o = JSONObject(pageJson(page))
        assertEquals(2, o.getInt("page"))
        assertEquals(1, o.getJSONArray("strokes").length())
        assertEquals(2, o.getJSONArray("strokes").getJSONArray(0).length())
    }

    @Test fun emptyPageSerializes() {
        val empty = PageData(0, Limits(), emptyList(), true)
        assertEquals(0, JSONObject(pageJson(empty)).getJSONArray("strokes").length())
    }
}
