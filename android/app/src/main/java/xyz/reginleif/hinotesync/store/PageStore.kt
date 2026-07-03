package xyz.reginleif.hinotesync.store

import org.json.JSONObject
import xyz.reginleif.hinotesync.protocol.PageData
import xyz.reginleif.hinotesync.render.pageJson
import java.io.File

class StoredPage(
    val stem: String,
    val dir: File,
    val sourceIndex: Int,
    val syncedAt: Long,
    val uploaded: Boolean,
    val complete: Boolean,
) {
    val pngFile: File get() = File(dir, "page.png")
    val jsonFile: File get() = File(dir, "strokes.json")
}

class PageStore(private val baseDir: File) {
    private val pagesDir = File(baseDir, "pages")

    fun save(page: PageData, png: ByteArray, syncedAt: Long): StoredPage {
        val stem = "page-$syncedAt-${page.index}"
        val dir = File(pagesDir, stem).apply { mkdirs() }
        File(dir, "strokes.json").writeText(pageJson(page))
        File(dir, "page.png").writeBytes(png)
        val meta = JSONObject()
            .put("sourceIndex", page.index)
            .put("syncedAt", syncedAt)
            .put("uploaded", false)
            .put("complete", page.complete)
        File(dir, "meta.json").writeText(meta.toString())
        return read(dir)!!
    }

    fun list(): List<StoredPage> =
        (pagesDir.listFiles()?.toList() ?: emptyList())
            .mapNotNull { read(it) }
            .sortedWith(compareByDescending<StoredPage> { it.syncedAt }.thenByDescending { it.sourceIndex })

    fun get(stem: String): StoredPage? = read(File(pagesDir, stem))

    fun markUploaded(stem: String) {
        val metaFile = File(File(pagesDir, stem), "meta.json")
        if (!metaFile.exists()) return
        val meta = JSONObject(metaFile.readText()).put("uploaded", true)
        metaFile.writeText(meta.toString())
    }

    fun deleteLocal(stem: String) {
        File(pagesDir, stem).deleteRecursively()
    }

    private fun read(dir: File): StoredPage? {
        val metaFile = File(dir, "meta.json")
        if (!metaFile.isFile) return null
        val meta = JSONObject(metaFile.readText())
        return StoredPage(
            stem = dir.name,
            dir = dir,
            sourceIndex = meta.getInt("sourceIndex"),
            syncedAt = meta.getLong("syncedAt"),
            uploaded = meta.optBoolean("uploaded", false),
            complete = meta.optBoolean("complete", true),
        )
    }
}
