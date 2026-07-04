package xyz.reginleif.hinotesync.render

import org.junit.Assert.assertEquals
import org.junit.Test
import xyz.reginleif.hinotesync.protocol.Limits
import xyz.reginleif.hinotesync.protocol.PageData
import xyz.reginleif.hinotesync.protocol.StylusPoint

class PageSvgTest {
    private val page = PageData(
        index = 2,
        limits = Limits(28200f, 37400f, 8191f),
        strokes = listOf(listOf(StylusPoint(16, 16, 5, true), StylusPoint(32, 32, 6, true))),
        complete = true,
    )

    @Test fun matchesPythonRenderSvgByteForByte() {
        // Captured verbatim from huion_notes.render.render_svg for the same fixture:
        //   python3 -c "from huion_notes import render; from huion_notes.codec import Page, StylusPoint
        //   print(render.render_svg(Page(2, 28200.0, 37400.0, 8191.0,
        //       [[StylusPoint(16,16,5,True), StylusPoint(32,32,6,True)]])))"
        val expected =
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"900\" height=\"1190\" " +
                "style=\"background:#fff\"><path d=\"M15.5,15.5 L16.0,16.0\" " +
                "fill=\"none\" stroke=\"#111\" stroke-width=\"2.5\"/></svg>"
        assertEquals(expected, pageSvg(page))
    }

    @Test fun emptyPageHasNoPaths() {
        val empty = PageData(0, Limits(28200f, 37400f, 8191f), emptyList(), true)
        assertEquals(
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"900\" height=\"1190\" " +
                "style=\"background:#fff\"></svg>",
            pageSvg(empty),
        )
    }
}
