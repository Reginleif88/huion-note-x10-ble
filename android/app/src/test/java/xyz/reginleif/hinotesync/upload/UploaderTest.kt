package xyz.reginleif.hinotesync.upload

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class UploaderTest {
    @Test fun postsMultipartWithBothPartsAndAuthHeader() {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(200))
        server.start()
        val result = Uploader().upload(
            server.url("/notes").toString(), "X-Api-Key", "sekrit",
            "page-1000-0", byteArrayOf(0x50, 0x4E, 0x47), "<svg/>",
        )
        assertEquals(UploadResult.Success, result)
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("sekrit", req.getHeader("X-Api-Key"))
        assertTrue(req.getHeader("Content-Type")!!.startsWith("multipart/form-data"))
        val body = req.body.readUtf8()
        // PNG and SVG parts share the stem, in the one request (atomic pairing by stem).
        assertTrue(body.contains("filename=\"page-1000-0.png\""))
        assertTrue(body.contains("filename=\"page-1000-0.svg\""))
        assertTrue(body.contains("name=\"page\"") && body.contains("name=\"svg\""))
        assertTrue(body.contains("image/svg+xml"))
        server.shutdown()
    }

    @Test fun non2xxReportsHttpErrorWithCode() {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(500).setBody("boom"))
        server.start()
        val result = Uploader().upload(server.url("/").toString(), null, null, "s", byteArrayOf(1), "{}")
        assertTrue(result is UploadResult.HttpError)
        result as UploadResult.HttpError
        assertEquals(500, result.code)
        // The server's error text is captured so a failure is diagnosable, not just a bare code.
        assertEquals("boom", result.bodySnippet)
        server.shutdown()
    }

    @Test fun connectionFailureReportsFailedNotThrow() {
        // nothing listens on this port
        val result = Uploader().upload("http://127.0.0.1:1/", null, null, "s", byteArrayOf(1), "{}")
        assertTrue(result is UploadResult.Failed)
    }

    @Test fun malformedUrlReportsFailedNotThrow() {
        // OkHttp's Request.Builder.url() throws IllegalArgumentException on a bad URL;
        // Uploader must swallow it and report failure rather than crashing the caller.
        val result = Uploader().upload("not a url", null, null, "s", byteArrayOf(1), "{}")
        assertTrue(result is UploadResult.Failed)
    }
}
