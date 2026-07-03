package xyz.reginleif.hinotesync.upload

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class UploaderTest {
    @Test fun postsMultipartWithBothPartsAndAuthHeader() {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(200))
        server.start()
        val ok = Uploader().upload(
            server.url("/notes").toString(), "X-Api-Key", "sekrit",
            "page-1000-0", byteArrayOf(0x50, 0x4E, 0x47), """{"page": 0}""",
        )
        assertTrue(ok)
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("sekrit", req.getHeader("X-Api-Key"))
        assertTrue(req.getHeader("Content-Type")!!.startsWith("multipart/form-data"))
        val body = req.body.readUtf8()
        assertTrue(body.contains("filename=\"page-1000-0.png\""))
        assertTrue(body.contains("filename=\"page-1000-0.json\""))
        assertTrue(body.contains("name=\"page\"") && body.contains("name=\"strokes\""))
        server.shutdown()
    }

    @Test fun non2xxReturnsFalse() {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(500))
        server.start()
        assertFalse(Uploader().upload(server.url("/").toString(), null, null, "s", byteArrayOf(1), "{}"))
        server.shutdown()
    }

    @Test fun connectionFailureReturnsFalseNotThrow() {
        // nothing listens on this port
        assertFalse(Uploader().upload("http://127.0.0.1:1/", null, null, "s", byteArrayOf(1), "{}"))
    }

    @Test fun malformedUrlReturnsFalseNotThrow() {
        // OkHttp's Request.Builder.url() throws IllegalArgumentException on a bad URL;
        // Uploader must swallow it and report failure rather than crashing the caller.
        assertFalse(Uploader().upload("not a url", null, null, "s", byteArrayOf(1), "{}"))
    }
}
