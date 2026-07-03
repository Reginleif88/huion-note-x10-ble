package xyz.reginleif.hinotesync.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.Build
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.ClosedReceiveChannelException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import xyz.reginleif.hinotesync.protocol.FrameTimeout
import xyz.reginleif.hinotesync.protocol.Transport
import xyz.reginleif.hinotesync.protocol.TransportClosed
import java.util.UUID

private const val TAG = "GattTransport"
private val SERVICE_UUID = UUID.fromString("0000ffe0-0000-1000-8000-00805f9b34fb")
private val DATA_CHAR_UUID = UUID.fromString("0000ffe1-0000-1000-8000-00805f9b34fb")  // notifications (0x0027)
private val CMD_CHAR_UUID = UUID.fromString("0000ffe2-0000-1000-8000-00805f9b34fb")   // write + indications (0x002b)
private val CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

@SuppressLint("MissingPermission") // UI gates BLUETOOTH_CONNECT before any transport is built
class GattTransport(private val context: Context, private val device: BluetoothDevice) : Transport {
    private val inbox = Channel<ByteArray>(Channel.UNLIMITED)
    private val ready = CompletableDeferred<Unit>()
    private val writeMutex = Mutex()
    private var gatt: BluetoothGatt? = null
    private var cmdChar: BluetoothGattCharacteristic? = null

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            Log.d(TAG, "connection state=$newState status=$status")
            when {
                newState == BluetoothProfile.STATE_CONNECTED -> g.discoverServices()
                newState == BluetoothProfile.STATE_DISCONNECTED -> {
                    ready.completeExceptionally(TransportClosed("disconnected (status=$status)"))
                    inbox.close()
                    g.close()
                    gatt = null
                }
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            // MTU first: 126-byte data values need ATT_MTU >= 129.
            g.requestMtu(247)
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.d(TAG, "mtu=$mtu status=$status")
            if (status != BluetoothGatt.GATT_SUCCESS || mtu < 129) {
                ready.completeExceptionally(TransportClosed("MTU negotiation failed (mtu=$mtu status=$status)"))
                return
            }
            val svc = g.getService(SERVICE_UUID)
            val data = svc?.getCharacteristic(DATA_CHAR_UUID)
            val cmd = svc?.getCharacteristic(CMD_CHAR_UUID)
            if (data == null || cmd == null) {
                ready.completeExceptionally(TransportClosed("FFE0/FFE1/FFE2 not found"))
                return
            }
            cmdChar = cmd
            g.setCharacteristicNotification(data, true)
            g.setCharacteristicNotification(cmd, true)
            writeCccd(g, data, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
            // FFE2's CCCD is written from onDescriptorWrite, sequentially.
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                ready.completeExceptionally(TransportClosed("CCCD write failed on ${d.characteristic.uuid} (status=$status)"))
                return
            }
            if (d.characteristic.uuid == DATA_CHAR_UUID) {
                writeCccd(g, g.getService(SERVICE_UUID).getCharacteristic(CMD_CHAR_UUID),
                    BluetoothGattDescriptor.ENABLE_INDICATION_VALUE)
            } else {
                ready.complete(Unit)
            }
        }

        @Deprecated("pre-33 callback")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            @Suppress("DEPRECATION")
            inbox.trySend(c.value.copyOf())
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            inbox.trySend(value.copyOf())
        }
    }

    private fun writeCccd(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
        val cccd = c.getDescriptor(CCCD_UUID) ?: run {
            ready.completeExceptionally(TransportClosed("missing CCCD on ${c.uuid}")); return
        }
        if (Build.VERSION.SDK_INT >= 33) {
            g.writeDescriptor(cccd, value)
        } else {
            @Suppress("DEPRECATION")
            cccd.value = value
            @Suppress("DEPRECATION")
            g.writeDescriptor(cccd)
        }
    }

    override suspend fun connect() {
        val g = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
        gatt = g
        try {
            withTimeoutOrNull(20_000) { ready.await() } ?: throw TransportClosed("setup timed out")
        } catch (e: Throwable) {
            g.close()
            gatt = null
            throw e
        }
    }

    override suspend fun send(frame: ByteArray) {
        val g = gatt ?: throw TransportClosed("not connected")
        val c = cmdChar ?: throw TransportClosed("no command characteristic")
        writeMutex.withLock {
            if (Build.VERSION.SDK_INT >= 33) {
                g.writeCharacteristic(c, frame, BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE)
            } else {
                @Suppress("DEPRECATION")
                c.value = frame
                c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                @Suppress("DEPRECATION")
                g.writeCharacteristic(c)
            }
        }
    }

    override suspend fun recv(timeoutMs: Long): ByteArray = try {
        withTimeoutOrNull(timeoutMs) { inbox.receive() } ?: throw FrameTimeout()
    } catch (e: ClosedReceiveChannelException) {
        throw TransportClosed()
    }

    override suspend fun close() {
        inbox.close()
        gatt?.close()
        gatt = null
    }
}
