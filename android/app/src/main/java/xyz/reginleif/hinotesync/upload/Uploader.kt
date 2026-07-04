package xyz.reginleif.hinotesync.upload

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

class Uploader(private val client: OkHttpClient = OkHttpClient()) {
    /**
     * Multipart POST of one page: the PNG raster and the SVG vector as two parts of a
     * single request, sharing the same stem (`<stem>.png` + `<stem>.svg`) so the server
     * receives the pair atomically. True iff the server answered 2xx.
     */
    fun upload(
        url: String,
        headerName: String?,
        headerValue: String?,
        stem: String,
        png: ByteArray,
        svg: String,
    ): Boolean {
        return try {
            val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("page", "$stem.png", png.toRequestBody("image/png".toMediaType()))
                .addFormDataPart("svg", "$stem.svg", svg.toRequestBody("image/svg+xml".toMediaType()))
                .build()
            val request = Request.Builder().url(url).post(body).apply {
                if (!headerName.isNullOrBlank() && !headerValue.isNullOrEmpty()) header(headerName, headerValue)
            }.build()
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: IOException) {
            false
        } catch (e: IllegalArgumentException) {
            // Malformed URL or header name/value rejected by OkHttp's builders.
            false
        }
    }
}
