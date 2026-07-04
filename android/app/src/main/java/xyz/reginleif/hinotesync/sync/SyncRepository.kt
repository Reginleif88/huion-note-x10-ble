package xyz.reginleif.hinotesync.sync

import kotlinx.coroutines.flow.MutableStateFlow

sealed class SyncState {
    data object Idle : SyncState()
    data object Scanning : SyncState()
    data object Connecting : SyncState()
    data class Syncing(val pagesDone: Int) : SyncState()
    data object Connected : SyncState()   // sync finished, link still up: tablet-deletes allowed
    data class Error(val message: String) : SyncState()
}

object SyncRepository {
    val state = MutableStateFlow<SyncState>(SyncState.Idle)
    /** Bumped whenever local page storage changed; the gallery re-lists on it. */
    val pagesVersion = MutableStateFlow(0)
    /**
     * One-shot user-visible notices (snackbar); UI resets to null after showing.
     * Paired with a monotonically increasing counter so that two consecutive, textually
     * identical notifications are still distinct StateFlow values — otherwise the second
     * `.value = ...` would be a value-equality no-op and never trigger the collector.
     */
    val message = MutableStateFlow<Pair<Long, String>?>(null)
    private var counter = 0L

    /** Tablet battery %, set from the last handshake; null when disconnected/unknown. */
    val battery = MutableStateFlow<Int?>(null)

    fun canDeleteOnTablet(): Boolean = state.value == SyncState.Connected
    fun bumpPages() { pagesVersion.value += 1 }
    fun notify(text: String) { message.value = counter++ to text }
}
