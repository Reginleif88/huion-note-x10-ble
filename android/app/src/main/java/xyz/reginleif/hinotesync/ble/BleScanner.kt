package xyz.reginleif.hinotesync.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.util.Log
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

private const val TAG = "BleScanner"

/** Find the tablet: direct by remembered MAC when possible, else scan for a name
 *  containing "huion". Requires BLUETOOTH_SCAN/CONNECT (UI gates this). */
@SuppressLint("MissingPermission")
suspend fun findTablet(context: Context, lastMac: String, timeoutMs: Long = 15_000): BluetoothDevice? {
    val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter ?: return null
    if (lastMac.isNotEmpty()) {
        try { return adapter.getRemoteDevice(lastMac) } catch (e: IllegalArgumentException) { /* fall through to scan */ }
    }
    val scanner = adapter.bluetoothLeScanner ?: return null
    return withTimeoutOrNull(timeoutMs) {
        suspendCancellableCoroutine { cont ->
            val cb = object : ScanCallback() {
                override fun onScanResult(callbackType: Int, result: ScanResult) {
                    val name = result.device.name ?: result.scanRecord?.deviceName ?: return
                    Log.d(TAG, "seen: $name ${result.device.address}")
                    if (name.contains("huion", ignoreCase = true) && cont.isActive) {
                        scanner.stopScan(this)
                        cont.resume(result.device)
                    }
                }
            }
            scanner.startScan(cb)
            cont.invokeOnCancellation { scanner.stopScan(cb) }
        }
    }
}
