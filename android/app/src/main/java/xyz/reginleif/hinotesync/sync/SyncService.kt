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

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SYNC -> {
                if (syncJob?.isActive == true) {
                    SyncRepository.notify("sync already running")
                } else {
                    syncJob = scope.launch { runSync() }
                }
            }
            ACTION_DELETE -> {
                val indices = intent.getIntArrayExtra(EXTRA_INDICES) ?: intArrayOf()
                scope.launch { runDelete(indices.toList()) }
            }
            ACTION_DISCONNECT -> scope.launch { teardown(SyncState.Idle) }
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
                attempt()
            }
            SyncRepository.state.value = SyncState.Connected
            SyncRepository.notify("synced $done page(s)")
            scheduleIdleDisconnect()
        } catch (e: PinRequired) {
            teardown(SyncState.Error("device requires a PIN — set it in Settings"))
        } catch (e: AuthFailed) {
            teardown(SyncState.Error(e.message ?: "auth failed"))
        } catch (e: TransportClosed) {
            teardown(SyncState.Error("connection lost: ${e.message}"))
        } catch (e: FrameTimeout) {
            teardown(SyncState.Error("device stopped responding"))
        } catch (e: Exception) {
            teardown(SyncState.Error("sync failed: ${e.message}"))
        }
    }

    private suspend fun runDelete(indices: List<Int>) {
        val e = engine
        val t = transport
        if (e == null || t == null || SyncRepository.state.value != SyncState.Connected) {
            SyncRepository.notify("not connected — sync first, then delete")
            return
        }
        scheduleIdleDisconnect() // reset the idle timer
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
            for (idx in indices.sortedDescending()) {       // descending: indices can't shift
                if (e.deletePage(idx)) ok += 1
            }
        } catch (ex: TransportClosed) {
            teardown(SyncState.Error("connection lost"))
            return
        }
        SyncRepository.notify("deleted $ok/${indices.size} page(s) on tablet")
    }

    private suspend fun teardown(finalState: SyncState) {
        idleJob?.cancel()
        try { transport?.close() } catch (e: Exception) { /* already gone */ }
        transport = null
        engine = null
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
        const val EXTRA_INDICES = "indices"

        fun sync(context: Context) = ContextCompat.startForegroundService(
            context, Intent(context, SyncService::class.java).setAction(ACTION_SYNC))

        fun deleteOnTablet(context: Context, indices: List<Int>) = context.startService(
            Intent(context, SyncService::class.java).setAction(ACTION_DELETE)
                .putExtra(EXTRA_INDICES, indices.toIntArray()))

        fun disconnect(context: Context) = context.startService(
            Intent(context, SyncService::class.java).setAction(ACTION_DISCONNECT))
    }
}
