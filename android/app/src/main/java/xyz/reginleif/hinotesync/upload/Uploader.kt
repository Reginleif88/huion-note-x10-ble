package xyz.reginleif.hinotesync.upload

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

/**
 * Outcome of one page upload. Carries enough detail to say WHY an upload failed
 * (from logcat or an in-app message) instead of collapsing every distinct failure
 * — server rejection, cleartext block, connection refused, bad URL — into one
 * indistinguishable `false`. This class stays framework-free (no android.*) so the
 * uploader remains JVM-unit-testable; the caller does the android logging.
 */
sealed interface UploadResult {
    val ok: Boolean get() = this is Success

    /** Server answered 2xx. */
    data object Success : UploadResult

    /** Reached the server, but it answered non-2xx (auth, wrong path, payload too large, 5xx…). */
    data class HttpError(val code: Int, val message: String, val bodySnippet: String) : UploadResult

    /** Never got a usable HTTP response: network/TLS/cleartext error, or a rejected URL/header. */
    data class Failed(val reason: String) : UploadResult
}

class Uploader(private val client: OkHttpClient = OkHttpClient()) {
    /**
     * Multipart POST of one page: the PNG raster and the SVG vector as two parts of a
     * single request, sharing the same stem (`<stem>.png` + `<stem>.svg`) so the server
     * receives the pair atomically.
     */
    fun upload(
        url: String,
        headerName: String?,
        headerValue: String?,
        stem: String,
        png: ByteArray,
        svg: String,
    ): UploadResult {
        val request = try {
            val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("page", "$stem.png", png.toRequestBody("image/png".toMediaType()))
                .addFormDataPart("svg", "$stem.svg", svg.toRequestBody("image/svg+xml".toMediaType()))
                .build()
            Request.Builder().url(url).post(body).apply {
                if (!headerName.isNullOrBlank() && !headerValue.isNullOrEmpty()) header(headerName, headerValue)
            }.build()
        } catch (e: IllegalArgumentException) {
            // Malformed URL or header name/value rejected by OkHttp's builders.
            return UploadResult.Failed("bad URL/header: ${e.message}")
        }
        return try {
            client.newCall(request).execute().use { resp ->
                if (resp.isSuccessful) {
                    UploadResult.Success
                } else {
                    // The body usually holds the server's own error text; cap it so a huge
                    // HTML error page can't flood the log.
                    UploadResult.HttpError(resp.code, resp.message, resp.body?.string()?.take(500).orEmpty())
                }
            }
        } catch (e: IOException) {
            // Network down, connection refused, timeout, TLS failure, or a cleartext-HTTP
            // block (java.net.UnknownServiceException) on targetSdk>=28 when the URL is http://.
            UploadResult.Failed("${e.javaClass.simpleName}: ${e.message}")
        }
    }
}
