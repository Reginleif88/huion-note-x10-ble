package xyz.reginleif.hinotesync.protocol

// Port of huion_notes/session.py DumpSession (protocol §5/§10).

class SyncEngine(
    private val t: Transport,
    private val pin: String? = null,
    private val idleMs: Long = 5_000,
    private val maxPages: Int = 64,
) {
    var limits: Limits = Limits()
        private set
    /** Tablet battery %, read once during the handshake; null if the device didn't answer. */
    var battery: Int? = null
        private set

    suspend fun run(onPage: suspend (PageData) -> Unit): Int {
        t.connect()
        authenticate()
        t.send(requestMaxInfo())
        limits = parseMaxData(recvOp(OrderCode.MAX_DATA).raw)
        battery = queryBattery()
        // Ask the device its logic-page count (CURRENT_PAGE 0x85). Some firmware holds an
        // empty page 0 with content in later pages, so we must iterate up to this count and
        // SKIP empty pages rather than stop at the first one. Fall back to a bounded scan if
        // the device doesn't answer.
        val pageCount = currentPageCount()
        val (d1, d2) = requestSetManyPacketDistance()
        t.send(d1)
        t.send(d2)
        val lastPage = if (pageCount in 1..maxPages) pageCount else maxPages - 1
        var pages = 0
        for (page in 0..lastPage) {
            val (count, packets, complete) = fetchPage(page)
            if (count == 0) continue          // empty page — skip, keep scanning
            onPage(decodePage(packets, limits, page, complete))
            pages++
        }
        return pages
    }

    /** Battery % from ELECTRICITY (0x8e), reply byte [3]; null if the device doesn't answer. */
    private suspend fun queryBattery(): Int? {
        t.send(buildCommand(OrderCode.ELECTRICITY))
        return try {
            val r = recvOp(OrderCode.ELECTRICITY, timeoutMs = 2_000).raw
            if (r.size >= 4) u8(r[3]) else null
        } catch (e: FrameTimeout) {
            null
        } catch (e: TransportClosed) {
            null
        }
    }

    /** Logic-page count from CURRENT_PAGE (0x85); 0 if the device doesn't answer. */
    private suspend fun currentPageCount(): Int {
        t.send(buildCommand(OrderCode.CURRENT_PAGE))
        return try {
            val r = recvOp(OrderCode.CURRENT_PAGE, timeoutMs = 3_000).raw
            if (r.size >= 5) u8(r[3]) or (u8(r[4]) shl 8) else 0
        } catch (e: FrameTimeout) {
            0
        } catch (e: TransportClosed) {
            0
        }
    }

    private suspend fun authenticate() {
        // The device emits its challenge only after being poked (protocol §6).
        t.send(buildCommand(OrderCode.VERIFY_CONNECT))
        val ch = recvOp(OrderCode.VERIFY_CONNECT)
        t.send(buildVerifyResult(u8(ch.raw[3]), u8(ch.raw[4]), u8(ch.raw[5])))
        var status = u8(recvOp(OrderCode.VERIFY_RESULT).raw[3])
        if (status == 2) {
            val p = pin ?: throw PinRequired()
            val (f1, f2) = buildVerifyPwdFrames(p)
            t.send(f1)
            t.send(f2)
            status = u8(recvOp(OrderCode.VERIFY_RESULT).raw[3])
        }
        if (status != 1) throw AuthFailed("auth rejected (status=$status)")
    }

    private suspend fun fetchPage(page: Int): Triple<Int, List<ByteArray>, Boolean> {
        t.send(requestPageData(page, 0))
        val count = parseOfflineCount(recvOp(OrderCode.REQUEST_OFFLINE_DATA).raw) ?: 0
        if (count == 0) return Triple(0, emptyList(), true)
        val got = sortedMapOf<Int, ByteArray>()
        drainStream(got, count)
        fillGaps(page, got, count)
        return Triple(count, got.values.toList(), got.size == count)
    }

    private suspend fun drainStream(got: MutableMap<Int, ByteArray>, count: Int) {
        while (true) {
            val value = try { t.recv(idleMs) } catch (e: FrameTimeout) { return } catch (e: TransportClosed) { return }
            val fr = parseHuionFrame(value) ?: continue
            if (fr.op != OrderCode.RETURN_OFFLINE_DATA) continue
            val seq = packetSeq(fr.raw)
            if (seq in 1..count) {
                got[seq] = fr.raw
                if (seq == count) return
            }
        }
    }

    private suspend fun fillGaps(page: Int, got: MutableMap<Int, ByteArray>, count: Int, maxRounds: Int = 5) {
        repeat(maxRounds) {
            val missing = (1..count).filter { it !in got }
            if (missing.isEmpty()) return
            for (i in missing) t.send(buildGetPagePackage(page, i))
            while (true) {
                val value = try { t.recv(idleMs) } catch (e: FrameTimeout) { break } catch (e: TransportClosed) { break }
                val fr = parseHuionFrame(value) ?: continue
                if (fr.op == OrderCode.GET_PAGE_PACKAGE && fr.raw.size >= 6 && u8(fr.raw[2]) == 0x7E) {
                    val idx = packetSeq(fr.raw)
                    if (idx in 1..count) {
                        got[idx] = fr.raw
                        if (got.size == count) break
                    }
                }
            }
        }
    }

    /** Read frames until one matches `op`, ignoring heartbeats etc. Budget = 3 reads' worth. */
    private suspend fun recvOp(op: Int, timeoutMs: Long = 10_000): HuionFrame {
        val deadline = System.nanoTime() + timeoutMs * 3_000_000L
        while (System.nanoTime() < deadline) {
            val fr = parseHuionFrame(t.recv(timeoutMs)) ?: continue
            if (fr.op == op) return fr
        }
        throw FrameTimeout("no matching frame (op=0x%02x)".format(op))
    }

    // --- destructive; call only after the page is safely stored ---

    suspend fun deletePage(page: Int): Boolean {
        t.send(buildDeletePage(page))
        val resp = try { recvOp(OrderCode.DELETE_PAGE) } catch (e: FrameTimeout) { return false } catch (e: TransportClosed) { return false }
        return resp.raw.size > 3 && u8(resp.raw[3]) == 1
    }

    suspend fun clearCache(): Boolean {
        t.send(buildClearCache())
        val resp = try { recvOp(OrderCode.CLEAR_CACHE) } catch (e: FrameTimeout) { return false } catch (e: TransportClosed) { return false }
        return resp.raw.size > 3 && u8(resp.raw[3]) == 1
    }

    /** Post-sync idle watcher: fires when the device announces a new page (NEXT_PAGE 0x8a). */
    suspend fun watchNextPage(onNewPage: () -> Unit) {
        while (true) {
            val value = try { t.recv(30_000) } catch (e: FrameTimeout) { continue } catch (e: TransportClosed) { return }
            val fr = parseHuionFrame(value) ?: continue
            if (fr.op == OrderCode.NEXT_PAGE) onNewPage()
        }
    }
}
