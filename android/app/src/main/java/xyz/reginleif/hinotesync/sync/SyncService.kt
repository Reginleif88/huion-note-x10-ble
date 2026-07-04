package xyz.reginleif.hinotesync.sync

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import xyz.reginleif.hinotesync.ble.GattTransport
import xyz.reginleif.hinotesync.ble.findTablet
import xyz.reginleif.hinotesync.protocol.AuthFailed
import xyz.reginleif.hinotesync.protocol.FrameTimeout
import xyz.reginleif.hinotesync.protocol.OrderCode
import xyz.reginleif.hinotesync.protocol.PinRequired
import xyz.reginleif.hinotesync.protocol.SyncEngine
import xyz.reginleif.hinotesync.protocol.Transport
import xyz.reginleif.hinotesync.protocol.TransportClosed
import xyz.reginleif.hinotesync.protocol.parseHuionFrame
import xyz.reginleif.hinotesync.render.PageRenderer
import xyz.reginleif.hinotesync.settings.AppSettings
import xyz.reginleif.hinotesync.store.PageStore
import java.io.ByteArrayOutputStream

class SyncService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var transport: Transport? = null
    private var engine: SyncEngine? = null
    private var idleJob: Job? = null
    private var syncJob: Job? = null
    // The syncedAt stamp of the most recent successful sync. Tablet-delete requests
    // that resolve to pages from any older sync are refused (their sourceIndex is stale).
    private var lastSyncedAt: Long = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SYNC -> {
                if (syncJob?.isActive == true) {
                    SyncRepository.notify("sync already running")
                } else {
                    syncJob = scope.launch {
                        // A prior sync may have left a live GATT session (state Connected).
                        // Close it before opening a new one so the old transport is never
                        // leaked or overwritten by the fresh attempt below.
                        if (transport != null) {
                            idleJob?.cancel()
                            try { transport?.close() } catch (e: Exception) { /* already gone */ }
                            transport = null
                            engine = null
                        }
                        runSync()
                    }
                }
            }
            ACTION_DELETE -> {
                val stems = intent.getStringArrayExtra(EXTRA_STEMS)?.toList() ?: emptyList()
                val alsoLocal = intent.getBooleanExtra(EXTRA_ALSO_LOCAL, false)
                scope.launch { runDelete(stems, alsoLocal) }
            }
            ACTION_DISCONNECT -> {
                // Cancel the sync coroutine first so its automatic-retry logic can't
                // reconnect after the user has explicitly asked to disconnect.
                syncJob?.cancel()
                scope.launch { teardown(SyncState.Idle) }
            }
        }
        return START_NOT_STICKY
    }

    private suspend fun runSync() {
        startInForeground()
        try {
            val settings = AppSettings(this)
            SyncRepository.state.value = SyncState.Scanning
            val device = findTablet(this, settings.lastMac)
                ?: return teardown(SyncState.Error("tablet not found — is it on and in range?"))
            settings.lastMac = device.address

            val store = PageStore(filesDir)
            // Reused across the retry below: same stems overwrite instead of duplicating.
            val syncedAt = System.currentTimeMillis()
            var done = 0
            // Tablet indices of the COMPLETE pages we extracted this session — the only pages
            // eligible for auto-delete (never delete an incomplete page: the tablet holds its
            // only full copy).
            val syncedComplete = mutableSetOf<Int>()

            suspend fun attempt() {
                SyncRepository.state.value = SyncState.Connecting
                val t = GattTransport(this@SyncService, device)
                transport = t
                val e = SyncEngine(t, pin = settings.pin.ifEmpty { null })
                engine = e
                e.run { page ->
                    val png = ByteArrayOutputStream().also {
                        PageRenderer.render(page).compress(Bitmap.CompressFormat.PNG, 100, it)
                    }.toByteArray()
                    store.save(page, png, syncedAt)
                    done += 1
                    if (page.complete) syncedComplete.add(page.index)
                    SyncRepository.state.value = SyncState.Syncing(done)
                    SyncRepository.bumpPages()
                    if (!page.complete) SyncRepository.notify(
                        "page ${page.index} incomplete (packets missing) — kept anyway")
                }
            }

            try {
                attempt()
            } catch (e: TransportClosed) {
                // spec: mid-transfer drop -> reconnect, re-handshake, resume (one automatic retry;
                // the dump restarts from page 0, re-saved pages overwrite, 0x88 covers packet loss)
                SyncRepository.notify("connection dropped — reconnecting…")
                try { transport?.close() } catch (ex: Exception) { /* already gone */ }
                done = 0
                syncedComplete.clear()
                attempt()
            }
            lastSyncedAt = syncedAt   // this session's pages are now the only delete-eligible set
            SyncRepository.battery.value = engine?.battery
            SyncRepository.notify("synced $done page(s)")

            if (settings.deleteAfterSync && syncedComplete.isNotEmpty()) {
                // Free tablet memory: delete exactly the complete pages we just extracted,
                // highest index first so surviving indices can't shift. The pages are already
                // saved on the phone; a delete failure here never loses data.
                val e = engine
                var deleted = 0
                if (e != null) {
                    try {
                        for (idx in syncedComplete.sortedDescending()) if (e.deletePage(idx)) deleted++
                    } catch (ex: TransportClosed) { /* link dropped during cleanup; pages are stored */ }
                }
                SyncRepository.notify("auto-deleted $deleted/${syncedComplete.size} page(s) from tablet")
                // The tablet's page set has changed; end the session so no now-stale index is reused.
                teardown(SyncState.Idle)
            } else {
                SyncRepository.state.value = SyncState.Connected
                scheduleIdleDisconnect()
            }
        } catch (e: PinRequired) {
            teardown(SyncState.Error("device requires a PIN — set it in Settings"))
        } catch (e: AuthFailed) {
            teardown(SyncState.Error(e.message ?: "auth failed"))
        } catch (e: TransportClosed) {
            teardown(SyncState.Error("connection lost: ${e.message}"))
        } catch (e: FrameTimeout) {
            teardown(SyncState.Error("device stopped responding"))
        } catch (e: CancellationException) {
            // User-requested disconnect (or service teardown): don't swallow it as a
            // generic failure — let cancellation propagate so no error state is published.
            throw e
        } catch (e: Exception) {
            teardown(SyncState.Error("sync failed: ${e.message}"))
        }
    }

    private suspend fun runDelete(stems: List<String>, alsoLocal: Boolean) {
        val e = engine
        val t = transport
        if (e == null || t == null || SyncRepository.state.value != SyncState.Connected) {
            SyncRepository.notify("not connected — sync first, then delete")
            return
        }
        scheduleIdleDisconnect() // reset the idle timer
        // Resolve stems -> stored pages. sourceIndex is only meaningful for pages from THIS
        // session's sync: an older sync's index may now point at a different tablet page.
        // Drop anything synced before this session, then require completeness, dedup, and
        // delete highest-index-first so surviving indices can't shift under us.
        // NOTE: resolution reads each stem's meta.json, so a combined "tablet + local" delete
        // MUST remove the local copy only AFTER this resolution (done post-loop below).
        val store = PageStore(filesDir)
        val resolved = stems.mapNotNull { store.get(it) }
        val stale = resolved.filter { it.syncedAt != lastSyncedAt }
        if (stale.isNotEmpty()) {
            SyncRepository.notify("${stale.size} page(s) skipped: synced before this session")
        }
        val deletable = resolved.filter { it.syncedAt == lastSyncedAt && it.complete }
        val indices = deletable
            .map { it.sourceIndex }
            .distinct()
            .sortedDescending()               // descending: indices can't shift
        if (indices.isEmpty()) return
        // Guardrail: refuse if the tablet created pages since the sync (NEXT_PAGE seen).
        // Bounded by a wall-clock deadline rather than a full second of silence, so
        // sub-second device chatter can't make this drain hang forever.
        val deadline = System.currentTimeMillis() + 1_500
        while (System.currentTimeMillis() < deadline) {
            val v = try { t.recv(250) } catch (ex: FrameTimeout) { continue } catch (ex: TransportClosed) {
                teardown(SyncState.Error("connection lost")); return
            }
            if (parseHuionFrame(v)?.op == OrderCode.NEXT_PAGE) {
                SyncRepository.notify("page set changed on tablet — sync again before deleting")
                return
            }
        }
        var ok = 0
        try {
            for (idx in indices) {
                if (e.deletePage(idx)) ok += 1
            }
        } catch (ex: TransportClosed) {
            teardown(SyncState.Error("connection lost"))
            return
        }
        // Combined "tablet + local": now that resolution + tablet delete are done, prune the
        // local copies of exactly the pages we acted on. Only reached on the success path
        // (aborts above returned early), so a refused/guarded delete never removes local.
        if (alsoLocal) {
            deletable.forEach { store.deleteLocal(it.stem) }
            SyncRepository.bumpPages()
        }
        SyncRepository.notify(
            if (alsoLocal) "deleted $ok/${indices.size} page(s) on tablet + removed local copies"
            else "deleted $ok/${indices.size} page(s) on tablet",
        )
        // After any confirmed delete the tablet's page set has shifted: force a fresh sync
        // before further tablet-deletes so no now-stale sourceIndex stays usable.
        if (ok >= 1) teardown(SyncState.Idle)
    }

    private suspend fun teardown(finalState: SyncState) {
        idleJob?.cancel()
        try { transport?.close() } catch (e: Exception) { /* already gone */ }
        transport = null
        engine = null
        SyncRepository.battery.value = null   // disconnected: battery reading no longer valid
        SyncRepository.state.value = finalState
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun scheduleIdleDisconnect() {
        idleJob?.cancel()
        idleJob = scope.launch {
            delay(5 * 60_000L)
            teardown(SyncState.Idle)
        }
    }

    private fun startInForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, "Sync", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val notification = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("HiNote Sync")
            .setContentText("Talking to the tablet…")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            ServiceCompat.startForeground(this, NOTIF_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    override fun onDestroy() {
        scope.cancel()
        // scope.cancel() only requests cancellation of any in-flight coroutine; it doesn't
        // synchronously close the transport or reset process-lifetime state. Do that here so
        // a mid-sync teardown of the service can't leak the GATT connection or leave
        // SyncRepository.state stuck on Scanning/Connecting/Syncing forever.
        runBlocking { try { transport?.close() } catch (e: Exception) { /* already gone */ } }
        transport = null
        engine = null
        when (SyncRepository.state.value) {
            is SyncState.Scanning, is SyncState.Connecting, is SyncState.Syncing ->
                SyncRepository.state.value = SyncState.Idle
            else -> {}
        }
        super.onDestroy()
    }

    companion object {
        private const val CHANNEL = "sync"
        private const val NOTIF_ID = 1
        const val ACTION_SYNC = "xyz.reginleif.hinotesync.SYNC"
        const val ACTION_DELETE = "xyz.reginleif.hinotesync.DELETE"
        const val ACTION_DISCONNECT = "xyz.reginleif.hinotesync.DISCONNECT"
        const val EXTRA_STEMS = "stems"
        const val EXTRA_ALSO_LOCAL = "alsoLocal"

        fun sync(context: Context) = ContextCompat.startForegroundService(
            context, Intent(context, SyncService::class.java).setAction(ACTION_SYNC))

        /** Delete the given pages on the tablet. When [alsoLocal] is true, the local copies
         *  are pruned too (after the tablet-side resolution), giving one "delete everywhere". */
        fun deleteOnTablet(context: Context, stems: List<String>, alsoLocal: Boolean = false) =
            context.startService(
                Intent(context, SyncService::class.java).setAction(ACTION_DELETE)
                    .putExtra(EXTRA_STEMS, stems.toTypedArray())
                    .putExtra(EXTRA_ALSO_LOCAL, alsoLocal))

        fun disconnect(context: Context) = context.startService(
            Intent(context, SyncService::class.java).setAction(ACTION_DISCONNECT))
    }
}
