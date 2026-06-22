"""Live BLE transport (dbus_fast) — the only device-dependent module (spec §4).

Reuses BLEConnection from the pen driver and exposes the session's Transport
contract (connect/send/recv/close). StartNotify is enabled on BOTH FFE1 (data
notifications) and FFE2 (command indications); BLEConnection.setup_signals routes
every characteristic Value change to a single callback, which we queue. The
session dispatches by opcode, so it does not care which characteristic a frame
arrived on.

Imports dbus_fast (and the driver) at module load, so this module must only be
imported on the live `dump` path — never from the pure core or the test suite.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from dbus_fast import Message, MessageType

from huion_ble_driver import BLEConnection, BLUEZ
from huion_notes.errors import TransportClosed
from huion_notes.frames import heart_beat


class BleTransport:
    def __init__(self, mac: str, keepalive: float = 5.0):
        self.conn = BLEConnection(mac)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._keepalive = keepalive
        self._ka_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        await self.conn.connect_bus()
        if not await self.conn.connect():
            raise TransportClosed(
                "BLE connect failed — is the notebook awake, paired, in note mode, "
                "and is patched BlueZ installed? (see README)"
            )
        await self._start_notify(self.conn.ffe1_path)
        await self._start_notify(self.conn.ffe2_path)
        await self.conn.setup_signals(self._on_value, self._on_disconnect)
        self._ka_task = asyncio.create_task(self._keepalive_loop())

    async def _start_notify(self, path: str) -> None:
        reply = await self.conn.bus.call(Message(
            destination=BLUEZ, path=path,
            interface="org.bluez.GattCharacteristic1", member="StartNotify",
        ))
        if reply.message_type == MessageType.ERROR:
            err = reply.error_name or ""
            if "InProgress" not in err:
                raise TransportClosed(f"StartNotify {path} failed: {err}")

    def _on_value(self, data: bytes) -> None:
        self._queue.put_nowait(bytes(data))

    def _on_disconnect(self) -> None:
        self._closed = True
        self._queue.put_nowait(None)  # wake any pending recv()

    async def send(self, frame: bytes) -> None:
        if not await self.conn.write_cmd(frame):
            raise TransportClosed("write failed (device disconnected?)")

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if item is None:
            raise TransportClosed("device disconnected")
        return item

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._keepalive)
                await self.conn.write_cmd(heart_beat())
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._closed = True
        if self._ka_task:
            self._ka_task.cancel()
        await self.conn.close()
