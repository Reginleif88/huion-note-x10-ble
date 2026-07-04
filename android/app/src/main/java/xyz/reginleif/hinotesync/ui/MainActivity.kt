package xyz.reginleif.hinotesync.ui

import android.Manifest
import android.graphics.BitmapFactory
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import xyz.reginleif.hinotesync.settings.AppSettings
import xyz.reginleif.hinotesync.store.PageStore
import xyz.reginleif.hinotesync.store.StoredPage
import xyz.reginleif.hinotesync.sync.SyncRepository
import xyz.reginleif.hinotesync.sync.SyncService
import xyz.reginleif.hinotesync.sync.SyncState
import xyz.reginleif.hinotesync.upload.Uploader

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { MaterialTheme { App() } }
    }
}

private fun neededPermissions(): Array<String> = when {
    Build.VERSION.SDK_INT >= 33 -> arrayOf(
        Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT,
        Manifest.permission.POST_NOTIFICATIONS)
    Build.VERSION.SDK_INT >= 31 -> arrayOf(
        Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
    else -> arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
}

/** The tablet's offline protocol carries no per-note authoring date, so the only date we
 *  have is when the page was pulled (syncedAt). Pages from one sync share this timestamp. */
private fun fmtSynced(epochMs: Long): String =
    java.text.SimpleDateFormat("MMM d, HH:mm", java.util.Locale.getDefault())
        .format(java.util.Date(epochMs))

private sealed class Screen {
    data object Gallery : Screen()
    data class Viewer(val stem: String) : Screen()
    data object Settings : Screen()
}

@Composable
private fun App() {
    val context = LocalContext.current
    val store = remember { PageStore(context.filesDir) }
    var screen by remember { mutableStateOf<Screen>(Screen.Gallery) }
    val snackbar = remember { SnackbarHostState() }
    val message by SyncRepository.message.collectAsState()

    LaunchedEffect(message) {
        message?.let { snackbar.showSnackbar(it.second); SyncRepository.message.value = null }
    }

    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()) { grants ->
        if (grants.values.all { it }) SyncService.sync(context)
        else SyncRepository.notify("Bluetooth permissions are required to sync")
    }

    Scaffold(snackbarHost = { SnackbarHost(snackbar) }) { pad ->
        Box(Modifier.padding(pad)) {
            when (val s = screen) {
                is Screen.Gallery -> GalleryScreen(
                    store = store,
                    onSync = { permLauncher.launch(neededPermissions()) },
                    onOpen = { screen = Screen.Viewer(it) },
                    onSettings = { screen = Screen.Settings },
                )
                is Screen.Viewer -> ViewerScreen(store, s.stem, onBack = { screen = Screen.Gallery })
                is Screen.Settings -> SettingsScreen(onBack = { screen = Screen.Gallery })
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun GalleryScreen(
    store: PageStore,
    onSync: () -> Unit,
    onOpen: (String) -> Unit,
    onSettings: () -> Unit,
) {
    val context = LocalContext.current
    val state by SyncRepository.state.collectAsState()
    val version by SyncRepository.pagesVersion.collectAsState()
    val pages = remember(version) { store.list() }
    var selected by remember { mutableStateOf(setOf<String>()) }
    val scope = rememberCoroutineScope()
    val settings = remember { AppSettings(context) }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("HiNote Sync", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Spacer(Modifier.weight(1f))
            TextButton(onClick = onSettings) { Text("Settings") }
        }
        Text(
            when (val s = state) {
                is SyncState.Idle -> "disconnected"
                is SyncState.Scanning -> "scanning for tablet…"
                is SyncState.Connecting -> "connecting…"
                is SyncState.Syncing -> "syncing… ${s.pagesDone} page(s)"
                is SyncState.Connected -> "connected — tablet deletes available"
                is SyncState.Error -> "error: ${s.message}"
            },
            style = MaterialTheme.typography.bodyMedium,
        )
        Spacer(Modifier.height(8.dp))
        Row {
            Button(onClick = onSync, enabled = state !is SyncState.Syncing) { Text("Sync") }
            Spacer(Modifier.width(8.dp))
            if (state is SyncState.Connected) {
                OutlinedButton(onClick = { SyncService.disconnect(context) }) { Text("Disconnect") }
            }
        }
        if (selected.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Row {
                Button(onClick = {
                    val stems = selected.toList(); selected = setOf()
                    scope.launch { uploadPages(context, store, stems, settings) }
                }) { Text("Upload (${selected.size})") }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(
                    enabled = SyncRepository.canDeleteOnTablet(),
                    onClick = {
                        // spec: incomplete pages are excluded from tablet-delete
                        val stems = selected.mapNotNull { store.get(it) }
                            .filter { it.complete }.map { it.stem }
                        selected = setOf()
                        if (stems.isNotEmpty()) SyncService.deleteOnTablet(context, stems, alsoLocal = true)
                        else SyncRepository.notify("selected pages are incomplete — not deleting on tablet")
                    },
                ) { Text("Delete (tablet + local)") }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(onClick = {
                    selected.forEach { store.deleteLocal(it) }; selected = setOf()
                    SyncRepository.bumpPages()
                }) { Text("Delete local") }
            }
        }
        Spacer(Modifier.height(12.dp))
        if (pages.isEmpty()) {
            Text("No pages yet. Turn the tablet on and tap Sync.")
        } else {
            LazyVerticalGrid(columns = GridCells.Adaptive(110.dp)) {
                items(pages, key = { it.stem }) { p ->
                    PageThumb(
                        page = p,
                        selected = p.stem in selected,
                        onClick = {
                            if (selected.isNotEmpty()) {
                                selected = if (p.stem in selected) selected - p.stem else selected + p.stem
                            } else onOpen(p.stem)
                        },
                        onLongClick = { selected = selected + p.stem },
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun PageThumb(page: StoredPage, selected: Boolean, onClick: () -> Unit, onLongClick: () -> Unit) {
    val bmp = remember(page.stem) { BitmapFactory.decodeFile(page.pngFile.absolutePath) }
    Card(
        Modifier.padding(4.dp).combinedClickable(onClick = onClick, onLongClick = onLongClick),
        border = if (selected) CardDefaults.outlinedCardBorder() else null,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            bmp?.let {
                Image(it.asImageBitmap(), contentDescription = "page ${page.sourceIndex}",
                    modifier = Modifier.aspectRatio(900f / 1190f), contentScale = ContentScale.Fit)
            }
            Text(
                buildString {
                    append(fmtSynced(page.syncedAt))
                    if (page.uploaded) append(" ↑")
                    if (!page.complete) append(" ⚠")
                },
                style = MaterialTheme.typography.labelSmall,
            )
        }
    }
}

@Composable
private fun ViewerScreen(store: PageStore, stem: String, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val settings = remember { AppSettings(context) }
    val syncState by SyncRepository.state.collectAsState()
    val version by SyncRepository.pagesVersion.collectAsState()
    val page = remember(stem, version) { store.get(stem) } ?: run { onBack(); return }
    val bmp = remember(stem) { BitmapFactory.decodeFile(page.pngFile.absolutePath) }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row {
            TextButton(onClick = onBack) { Text("< Back") }
            Spacer(Modifier.weight(1f))
            Text(fmtSynced(page.syncedAt) + if (page.uploaded) " (uploaded)" else "")
        }
        bmp?.let {
            Image(it.asImageBitmap(), contentDescription = null,
                modifier = Modifier.weight(1f).fillMaxWidth(), contentScale = ContentScale.Fit)
        }
        Row(Modifier.padding(top = 8.dp)) {
            Button(onClick = { scope.launch { uploadPages(context, store, listOf(stem), settings) } }) {
                Text("Upload")
            }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(
                // spec: incomplete pages are excluded from tablet-delete
                enabled = syncState is SyncState.Connected && page.complete,
                onClick = { SyncService.deleteOnTablet(context, listOf(page.stem), alsoLocal = true); onBack() },
            ) { Text("Delete (tablet + local)") }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { store.deleteLocal(stem); SyncRepository.bumpPages(); onBack() }) {
                Text("Delete local")
            }
        }
    }
}

@Composable
private fun SettingsScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    val settings = remember { AppSettings(context) }
    var url by remember { mutableStateOf(settings.serverUrl) }
    var headerName by remember { mutableStateOf(settings.headerName) }
    var headerValue by remember { mutableStateOf(settings.headerValue) }
    var pin by remember { mutableStateOf(settings.pin) }
    var deleteAfterUpload by remember { mutableStateOf(settings.deleteAfterUpload) }
    var deleteAfterSync by remember { mutableStateOf(settings.deleteAfterSync) }
    var urlError by remember { mutableStateOf<String?>(null) }

    Column(Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        TextButton(onClick = onBack) { Text("< Back") }
        OutlinedTextField(url, { url = it; urlError = null }, label = { Text("Server URL (https://…)") },
            isError = urlError != null,
            supportingText = urlError?.let { msg -> { Text(msg) } },
            modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(headerName, { headerName = it }, label = { Text("Auth header name (optional)") },
            modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(headerValue, { headerValue = it }, label = { Text("Auth header value") },
            modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(pin, { pin = it }, label = { Text("Device PIN (only if the tablet asks)") },
            modifier = Modifier.fillMaxWidth(), singleLine = true)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(deleteAfterSync, { deleteAfterSync = it })
            Spacer(Modifier.width(8.dp))
            Text("Delete pages from tablet after sync (frees tablet memory)")
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(deleteAfterUpload, { deleteAfterUpload = it })
            Spacer(Modifier.width(8.dp))
            Text("Delete page from tablet after successful upload")
        }
        Button(onClick = {
            val trimmedUrl = url.trim()
            // Validate with the SAME parser OkHttp's Request.Builder().url() uses, so a URL
            // that saves here cannot throw at upload time. Empty is allowed (clears config).
            if (trimmedUrl.isNotEmpty() && trimmedUrl.toHttpUrlOrNull() == null) {
                urlError = "Enter a valid http:// or https:// URL (include the scheme)"
            } else {
                urlError = null
                settings.serverUrl = trimmedUrl
                settings.headerName = headerName.trim()
                settings.headerValue = headerValue
                settings.pin = pin.trim()
                settings.deleteAfterUpload = deleteAfterUpload
                settings.deleteAfterSync = deleteAfterSync
                SyncRepository.notify("settings saved")
                onBack()
            }
        }) { Text("Save") }
    }
}

/** Uploads pages sequentially off the main thread; marks uploaded; optionally
 *  requests tablet-delete afterwards (only when enabled AND still connected). */
private suspend fun uploadPages(
    context: android.content.Context,
    store: PageStore,
    stems: List<String>,
    settings: AppSettings,
) {
    if (settings.serverUrl.isEmpty()) {
        SyncRepository.notify("set the server URL in Settings first")
        return
    }
    val uploader = Uploader()
    var ok = 0
    // Only complete pages are eligible for tablet-delete: deleting an incomplete page
    // would destroy the tablet's only full copy (spec: incomplete-page exclusion).
    val deletableStems = mutableListOf<String>()
    withContext(Dispatchers.IO) {
        for (stem in stems) {
            val p = store.get(stem) ?: continue
            val success = uploader.upload(
                settings.serverUrl, settings.headerName.ifEmpty { null }, settings.headerValue,
                stem, p.pngFile.readBytes(), p.svgFile.readText(),
            )
            if (success) {
                store.markUploaded(stem)
                ok += 1
                if (p.complete) deletableStems += stem
            }
        }
    }
    SyncRepository.bumpPages()
    SyncRepository.notify("uploaded $ok/${stems.size} page(s)")
    if (settings.deleteAfterUpload && deletableStems.isNotEmpty() && SyncRepository.canDeleteOnTablet()) {
        SyncService.deleteOnTablet(context, deletableStems)
    }
}
