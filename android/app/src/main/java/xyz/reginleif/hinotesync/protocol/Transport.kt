package xyz.reginleif.hinotesync.protocol

/** BLE-agnostic byte transport; implemented by ble.GattTransport and test fakes. */
interface Transport {
    suspend fun connect()
    suspend fun send(frame: ByteArray)
    /** Next inbound characteristic value. Throws FrameTimeout / TransportClosed. */
    suspend fun recv(timeoutMs: Long): ByteArray
    suspend fun close()
}

class TransportClosed(msg: String = "transport closed") : Exception(msg)
class FrameTimeout(msg: String = "timed out waiting for a frame") : Exception(msg)
class PinRequired(msg: String = "device requires a 6-digit PIN") : Exception(msg)
class AuthFailed(msg: String) : Exception(msg)
