package xyz.reginleif.hinotesync.upload

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

class Uploader(private val client: OkHttpClient = OkHttpClient()) {
    /** Multipart POST of one page. True iff the server answered 2xx. */
    fun upload(
        url: String,
        headerName: String?,
        headerValue: String?,
        stem: String,
        png: ByteArray,
        strokesJson: String,
    ): Boolean {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("page", "$stem.png", png.toRequestBody("image/png".toMediaType()))
            .addFormDataPart("strokes", "$stem.json", strokesJson.toRequestBody("application/json".toMediaType()))
            .build()
        val request = Request.Builder().url(url).post(body).apply {
            if (!headerName.isNullOrBlank() && !headerValue.isNullOrEmpty()) header(headerName, headerValue)
        }.build()
        return try {
            client.newCall(request).execute().use { it.isSuccessful }
        } catch (e: IOException) {
            false
        }
    }
}
