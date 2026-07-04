package xyz.reginleif.hinotesync.store

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import xyz.reginleif.hinotesync.protocol.Limits
import xyz.reginleif.hinotesync.protocol.PageData
import xyz.reginleif.hinotesync.protocol.StylusPoint
import java.io.File

class PageStoreTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun page(index: Int) = PageData(
        index, Limits(),
        listOf(listOf(StylusPoint(1, 1, 9, true), StylusPoint(2, 2, 9, true))),
        complete = true,
    )

    @Test fun saveWritesAllThreeFilesAndListReadsThemBack() {
        val store = PageStore(tmp.root)
        val sp = store.save(page(0), byteArrayOf(1, 2, 3), syncedAt = 1000L)
        assertEquals("page-1000-0", sp.stem)
        assertTrue(sp.pngFile.exists() && sp.svgFile.exists())
        assertTrue(sp.svgFile.readText().startsWith("<svg"))
        val listed = store.list().single()
        assertEquals(0, listed.sourceIndex)
        assertEquals(1000L, listed.syncedAt)
        assertFalse(listed.uploaded)
        assertTrue(listed.complete)
    }

    @Test fun listSortsNewestFirst() {
        val store = PageStore(tmp.root)
        store.save(page(0), byteArrayOf(1), 1000L)
        store.save(page(1), byteArrayOf(1), 1000L)
        store.save(page(0), byteArrayOf(1), 2000L)
        assertEquals(listOf("page-2000-0", "page-1000-1", "page-1000-0"), store.list().map { it.stem })
    }

    @Test fun markUploadedPersists() {
        val store = PageStore(tmp.root)
        val sp = store.save(page(0), byteArrayOf(1), 1000L)
        store.markUploaded(sp.stem)
        assertTrue(store.get(sp.stem)!!.uploaded)
        assertTrue(PageStore(tmp.root).list().single().uploaded)  // fresh instance re-reads meta
    }

    @Test fun deleteLocalRemovesDirectory() {
        val store = PageStore(tmp.root)
        val sp = store.save(page(0), byteArrayOf(1), 1000L)
        store.deleteLocal(sp.stem)
        assertNull(store.get(sp.stem))
        assertTrue(store.list().isEmpty())
    }

    @Test fun listSkipsCorruptMetaJson() {
        val store = PageStore(tmp.root)
        val valid = store.save(page(0), byteArrayOf(1), 1000L)
        val corrupt = store.save(page(1), byteArrayOf(2), 2000L)
        // Corrupt the second page's meta.json
        File(corrupt.dir, "meta.json").writeText("{not json")
        // list() should return only the valid page
        val listed = store.list()
        assertEquals(1, listed.size)
        assertEquals(valid.stem, listed.single().stem)
        // get() should return null for the corrupt stem
        assertNull(store.get(corrupt.stem))
    }
}
