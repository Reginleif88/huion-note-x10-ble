#!/usr/bin/env python3
"""
Huion Note X10 BLE tablet driver for Linux.

Connects to the tablet via BlueZ D-Bus, performs the vendor handshake,
and injects pen events into the Linux input subsystem via /dev/uinput.

Usage:
    python3 huion_ble_driver.py [-v]
    python3 huion_ble_driver.py --mac AA:BB:CC:DD:EE:FF

Requires: dbus_fast (pip install dbus-fast), /dev/uinput accessible
"""

import argparse
import asyncio
import fcntl
import logging
import os
import signal
import struct
import time

from dbus_fast.aio import MessageBus
from dbus_fast import BusType, Message, MessageType

log = logging.getLogger("huion-ble")

# ─── BLE / D-Bus constants ───────────────────────────────────────────────────

BLUEZ = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"

# ─── Protocol constants ──────────────────────────────────────────────────────

MARKER_START = 0xCD

# Pen Tablet Mode (v15 PC driver protocol — from Ghidra RE of macOS driver)
CMD_TABLET_NAME    = 0xC9  # 201 — get device name (UTF-8)
CMD_TABLET_INFO    = 0xC8  # 200 — get device info (maxX/Y/P, LPI, rate)
CMD_TABLET_VERIFY  = 0xCA  # 202 — get manufacturer string (verify)
CMD_TABLET_BATTERY = 0xD1  # 209 — battery query (also used as keepalive)
REPORT_ID_PEN = 0x08       # USB HID report ID injected by BLE layer
PEN_TYPE_MAX  = 0x91       # report_type < this = pen data


def build_tablet_cmd(cmd_id: int) -> bytes:
    """Pen Tablet Mode framing: cd CMD 00 00 00 00 00 00 (no length, no end marker)"""
    return bytes([MARKER_START, cmd_id, 0, 0, 0, 0, 0, 0])


TABLET_HANDSHAKE = [
    build_tablet_cmd(CMD_TABLET_NAME),    # 1. get device name
    build_tablet_cmd(CMD_TABLET_INFO),    # 2. get device info
    build_tablet_cmd(CMD_TABLET_VERIFY),  # 3. verify manufacturer
]
KEEPALIVE_INTERVAL = 5.0

DEFAULT_MAX_X = 28200
DEFAULT_MAX_Y = 37400
DEFAULT_MAX_PRESSURE = 8191

# ─── Linux input constants ────────────────────────────────────────────────────

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0x00
ABS_X, ABS_Y, ABS_PRESSURE = 0x00, 0x01, 0x18
ABS_TILT_X, ABS_TILT_Y = 0x1A, 0x1B
BTN_TOUCH, BTN_TOOL_PEN, BTN_STYLUS = 0x14A, 0x140, 0x14B
BUS_BLUETOOTH = 0x05
INPUT_PROP_DIRECT = 0x01

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E


# ─── UInput device ────────────────────────────────────────────────────────────

class UInputDevice:
    def __init__(self, max_x: int, max_y: int, max_pressure: int):
        self.max_x = max_x
        self.max_y = max_y
        self.max_pressure = max_pressure
        self.fd = -1

    def open(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_SYN, EV_KEY, EV_ABS):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for btn in (BTN_TOUCH, BTN_TOOL_PEN, BTN_STYLUS):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, btn)
        for code, min_val, max_val, res in [
            (ABS_X, 0, self.max_x, 111),
            (ABS_Y, 0, self.max_y, 111),
            (ABS_PRESSURE, 0, self.max_pressure, 0),
            (ABS_TILT_X, -127, 127, 0),
            (ABS_TILT_Y, -127, 127, 0),
        ]:
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            fcntl.ioctl(self.fd, UI_ABS_SETUP,
                        struct.pack("<HHiiiiii", code, 0, min_val, 0, max_val, 0, 0, res))
        fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        name = b"Huion Note X10 BLE"[:80].ljust(80, b"\x00")
        fcntl.ioctl(self.fd, UI_DEV_SETUP,
                    struct.pack("<HHHH80sI", BUS_BLUETOOTH, 0x256C, 0x8251, 1, name, 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        log.info("uinput device created: Huion Note X10 BLE")

    def _emit(self, ev_type: int, ev_code: int, ev_value: int):
        os.write(self.fd, struct.pack("<qqHHi", 0, 0, ev_type, ev_code, ev_value))

    def report(self, x: int, y: int, pressure: int, pen_active: bool,
               tilt_x: int = 0, tilt_y: int = 0):
        self._emit(EV_ABS, ABS_X, x)
        self._emit(EV_ABS, ABS_Y, y)
        self._emit(EV_ABS, ABS_PRESSURE, pressure)
        self._emit(EV_ABS, ABS_TILT_X, tilt_x)
        self._emit(EV_ABS, ABS_TILT_Y, tilt_y)
        self._emit(EV_KEY, BTN_TOUCH, 1 if pressure > 0 else 0)
        self._emit(EV_KEY, BTN_TOOL_PEN, 1 if pen_active else 0)
        self._emit(EV_SYN, SYN_REPORT, 0)

    def pen_up(self):
        self._emit(EV_ABS, ABS_PRESSURE, 0)
        self._emit(EV_KEY, BTN_TOUCH, 0)
        self._emit(EV_KEY, BTN_TOOL_PEN, 0)
        self._emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = -1


# ─── Protocol parsers (from Ghidra RE of macOS v15 driver) ──────────────────

PEN_MAGIC = 0x5554  # BLE pen data header (big-endian: 0x55, 0x54)


def parse_tablet_pen_report(data: bytes) -> tuple | None:
    """Parse Pen Tablet Mode pen data from raw BLE FFE1 notification.

    Returns (status, x, y, pressure, tilt_x, tilt_y) or None.

    Raw BLE wire format (14 bytes, discovered from live capture):
      [0-1]  0x55 0x54 — magic header
      [2]    STATUS    — 0x80=hovering, 0x81=touching
      [3-4]  X_lo      — 16-bit LE
      [5-6]  Y_lo      — 16-bit LE
      [7-8]  PRESSURE  — 16-bit LE
      [9]    X_hi      — 24-bit extension
      [10]   Y_hi      — 24-bit extension
      [11]   TILT_X    — signed int8
      [12]   TILT_Y    — signed int8
      [13]   CHECKSUM

    The macOS driver skips byte[0], overwrites byte[1] with 0x08, and passes
    &byte[1] onward — making it look like USB HID report ID 0x08.
    """
    if len(data) < 13:
        return None

    # Check for the 55 54 magic header
    if data[0] == 0x55 and data[1] == 0x54:
        status = data[2]
        x = data[3] | (data[4] << 8) | (data[9] << 16)
        y = data[5] | (data[6] << 8) | (data[10] << 16)
        pressure = struct.unpack_from("<H", data, 7)[0]
        tilt_x = struct.unpack_from("<b", data, 11)[0]
        tilt_y = struct.unpack_from("<b", data, 12)[0]
        return (status, x, y, pressure, tilt_x, tilt_y)

    # Fallback: USB HID format (0x08 prefix, from cached/converted data)
    if data[0] == REPORT_ID_PEN and len(data) >= 12:
        status = data[1]
        if status >= PEN_TYPE_MAX:
            return None
        x = data[2] | (data[3] << 8)
        y = data[4] | (data[5] << 8)
        if len(data) > 9:
            x |= data[8] << 16
            y |= data[9] << 16
        pressure = struct.unpack_from("<H", data, 6)[0]
        tilt_x = struct.unpack_from("<b", data, 10)[0] if len(data) > 10 else 0
        tilt_y = struct.unpack_from("<b", data, 11)[0] if len(data) > 11 else 0
        return (status, x, y, pressure, tilt_x, tilt_y)

    return None


def parse_tablet_device_info(data: bytes) -> dict:
    """Parse device info from cmd 0xC8 (200) response payload.

    From _parse_cmd_resp_200 in HuionTabletCore:
      [0-2] max_X (24-bit LE)   [3-5] max_Y (24-bit LE)
      [6-7] max_P (16-bit LE)   [8-9] LPI (16-bit LE)
    """
    info = {"max_x": DEFAULT_MAX_X, "max_y": DEFAULT_MAX_Y,
            "max_pressure": DEFAULT_MAX_PRESSURE, "lpi": 0}
    if len(data) < 8:
        return info
    info["max_x"] = data[0] | (data[1] << 8) | (data[2] << 16)
    info["max_y"] = data[3] | (data[4] << 8) | (data[5] << 16)
    info["max_pressure"] = struct.unpack_from("<H", data, 6)[0]
    if len(data) >= 10:
        info["lpi"] = struct.unpack_from("<H", data, 8)[0]
    return info


def parse_tablet_device_name(data: bytes) -> str:
    """Parse device name from cmd 0xC9 (201) response — plain UTF-8."""
    return data.decode("utf-8", errors="replace").rstrip("\x00") if data else "unknown"


# ─── BLE connection ──────────────────────────────────────────────────────────

class BLEConnection:
    def __init__(self, mac: str):
        self.mac = mac
        self.device_path = f"{ADAPTER_PATH}/dev_{mac.replace(':', '_')}"
        self._notify_fd_alive = False  # set True when fd reader is active
        self.ffe1_path = f"{self.device_path}/service0025/char0026"
        self.ffe2_path = f"{self.device_path}/service0025/char002a"
        self.bus: MessageBus | None = None
        self._notification_cb = None
        self._disconnect_cb = None
        self._write_fd: int | None = None

    async def connect_bus(self):
        # negotiate_unix_fd=True enables fd passing for AcquireNotify
        self.bus = await MessageBus(bus_type=BusType.SYSTEM,
                                    negotiate_unix_fd=True).connect()

    async def _get_prop(self, path: str, iface: str, prop: str):
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=path,
            interface="org.freedesktop.DBus.Properties",
            member="Get", signature="ss", body=[iface, prop],
        ))
        if reply.message_type == MessageType.ERROR:
            return None
        return reply.body[0].value if reply.body else None

    async def is_connected(self) -> bool:
        return await self._get_prop(self.device_path, "org.bluez.Device1", "Connected") is True

    async def disconnect(self):
        await self.bus.call(Message(
            destination=BLUEZ, path=self.device_path,
            interface="org.bluez.Device1", member="Disconnect",
        ))
        for _ in range(20):
            if not await self.is_connected():
                return
            await asyncio.sleep(0.1)

    async def force_reconnect(self) -> bool:
        """Disconnect, reconnect, return as fast as possible.
        Caller should acquire fds IMMEDIATELY after this returns."""
        log.info("Reconnecting to %s...", self.mac)
        await self.disconnect()
        await asyncio.sleep(0.3)
        # Connect but DON'T wait for ServicesResolved — we need to race HOGP
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=self.device_path,
            interface="org.bluez.Device1", member="Connect",
        ))
        if reply.message_type == MessageType.ERROR:
            err = reply.error_name or ""
            if "AlreadyConnected" not in err:
                log.error("Connect failed: %s", err)
                return False
        # Minimal wait — just enough for GATT to be reachable, not for full resolution
        for _ in range(8):
            if await self.is_connected():
                log.info("Connected (racing HOGP...)")
                return True
            await asyncio.sleep(0.1)
        if await self.is_connected():
            log.info("Connected (late, HOGP may have won)")
            return True
        log.error("Connection failed — is the tablet awake?")
        return False

    def _close_fds(self):
        """Close any previously acquired fds to prevent leaks."""
        if self._write_fd is not None:
            try:
                os.close(self._write_fd)
            except OSError:
                pass
            self._write_fd = None

    async def write_cmd(self, data: bytes) -> bool:
        """Write command via raw fd if acquired, otherwise D-Bus WriteValue."""
        if self._write_fd is not None:
            try:
                os.write(self._write_fd, data)
                return True
            except OSError:
                return False
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=self.ffe2_path,
            interface="org.bluez.GattCharacteristic1",
            member="WriteValue", signature="aya{sv}",
            body=[bytearray(data), {}],
        ))
        return reply.message_type != MessageType.ERROR

    async def acquire_fds(self) -> tuple[int | None, int | None]:
        """Acquire raw fds for FFE1 (notify) and FFE2 (write).
        Bypasses BlueZ's GATT layer entirely — required because StartNotify
        silently fails when the HOGP handler claims the device.
        Returns (notify_fd, write_fd)."""
        notify_fd = None
        write_fd = None

        # Acquire notification fd for FFE1
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=self.ffe1_path,
            interface="org.bluez.GattCharacteristic1",
            member="AcquireNotify",
            signature="a{sv}",
            body=[{}],
        ))
        if reply.message_type == MessageType.ERROR:
            log.warning("AcquireNotify FFE1 failed: %s", reply.error_name)
        else:
            # body[0] is the fd INDEX, actual fd is in unix_fds
            fd_idx = reply.body[0]
            mtu = reply.body[1]
            fds = getattr(reply, 'unix_fds', None)
            if fds and len(fds) > fd_idx:
                notify_fd = fds[fd_idx]
            else:
                notify_fd = fd_idx  # fallback if fds not available
            log.info("AcquireNotify FFE1: fd=%d (idx=%d, fds=%s), MTU=%d",
                     notify_fd, fd_idx, fds, mtu)

        # Acquire write fd for FFE2
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=self.ffe2_path,
            interface="org.bluez.GattCharacteristic1",
            member="AcquireWrite",
            signature="a{sv}",
            body=[{}],
        ))
        if reply.message_type == MessageType.ERROR:
            log.warning("AcquireWrite FFE2 failed: %s", reply.error_name)
        else:
            fd_idx = reply.body[0]
            mtu = reply.body[1]
            fds = getattr(reply, 'unix_fds', None)
            if fds and len(fds) > fd_idx:
                write_fd = fds[fd_idx]
            else:
                write_fd = fd_idx
            self._write_fd = write_fd
            log.info("AcquireWrite FFE2: fd=%d, MTU=%d", write_fd, mtu)

        return notify_fd, write_fd

    def _on_signal(self, msg: Message):
        if not msg.body or len(msg.body) < 2:
            return
        changed = msg.body[1]
        if not isinstance(changed, dict):
            return

        # Catch Value changes from any characteristic under the device
        if "Value" in changed and msg.path and isinstance(msg.path, str) and self.device_path in msg.path:
            data = bytes(changed["Value"].value)
            if msg.path not in (self.ffe1_path, self.ffe2_path):
                log.info("DATA ON %s: %s (len=%d)", msg.path.split("/")[-1], data.hex(), len(data))
            elif msg.path == self.ffe2_path:
                log.debug("Data on FFE2: %s", data.hex())
            if self._notification_cb:
                self._notification_cb(data)

        elif msg.path == self.device_path and "Connected" in changed:
            if not changed["Connected"].value:
                log.warning("Device disconnected (D-Bus signal)")
                # Don't trigger reconnect from D-Bus signal alone — the notify fd
                # reader is the authority. HOGP can cause transient Connected=false
                # while our acquired fd is still alive and streaming data.
                if self._disconnect_cb and not self._notify_fd_alive:
                    self._disconnect_cb()

    async def setup_signals(self, on_notification, on_disconnect):
        self._notification_cb = on_notification
        self._disconnect_cb = on_disconnect
        self.bus.add_message_handler(self._on_signal)
        await self.bus.call(Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch", signature="s",
            body=[
                f"type='signal',sender='{BLUEZ}',"
                f"interface='org.freedesktop.DBus.Properties',"
                f"member='PropertiesChanged',"
                f"path_namespace='{self.device_path}'"
            ],
        ))

    async def close(self):
        if self.bus:
            self.bus.disconnect()


# ─── HOGP mitigation ────────────────────────────────────────────────────────

def _unbind_hid_generic():
    """Unbind the Note X10 from hid-generic to prevent HOGP from killing
    FFE1 notifications. Called after each reconnect.

    With the udev rule installed (99-huion-note-x10.rules), this happens
    automatically. This is a fallback for when udev hasn't fired yet or
    the rule isn't installed."""
    import glob as glob_mod
    for dev_path in glob_mod.glob("/sys/bus/hid/devices/0005:256C:8251.*"):
        dev_id = os.path.basename(dev_path)
        driver_link = os.path.join(dev_path, "driver")
        if os.path.islink(driver_link) and "hid-generic" in os.readlink(driver_link):
            try:
                with open("/sys/bus/hid/drivers/hid-generic/unbind", "w") as f:
                    f.write(dev_id)
                log.info("Unbound %s from hid-generic", dev_id)
            except PermissionError:
                log.warning("Cannot unbind hid-generic (not root). "
                            "Install 99-huion-note-x10.rules to fix this.")
            except OSError as e:
                log.debug("Unbind skipped: %s", e)


# ─── Driver core ─────────────────────────────────────────────────────────────

class HuionBLEDriver:
    def __init__(self, mac: str):
        self.ble = BLEConnection(mac)
        self.uinput: UInputDevice | None = None
        self.max_x = DEFAULT_MAX_X
        self.max_y = DEFAULT_MAX_Y
        self.max_pressure = DEFAULT_MAX_PRESSURE
        self.device_name = "unknown"
        self._running = True
        self._disconnect_event = asyncio.Event()
        self._notify_fd: int | None = None
        self._pen_was_active = False
        self._handshake_responses: dict[int, bytes] = {}
        self._stats = {"samples": 0, "reconnects": 0}

    # ── Notification handling ──

    def _on_notification(self, data: bytes):
        if not data:
            return
        # Log raw data — verbose for first 10 packets, then debug only
        if self._stats["samples"] < 10:
            log.info("RAW[%d] (%d bytes): %s", self._stats["samples"], len(data), data.hex())
        else:
            log.debug("RAW (%d bytes): %s", len(data), data[:30].hex())

        # Try parsing as pen data (handles 55 54 and 08 formats)
        report = parse_tablet_pen_report(data)
        if report is not None:
            status, x, y, pressure, tilt_x, tilt_y = report
            # Status: 0x80 = hovering (pen near surface), 0x81 = touching
            pen_touching = (status & 0x01) != 0
            self._emit_pen(x, y, pressure if pen_touching else 0, tilt_x, tilt_y)
            return

        # Not pen data — treat as command response
        if len(data) >= 2:
            if data[0] == MARKER_START and len(data) >= 3:
                cmd_id = data[1]
                payload = data[3:] if len(data) > 3 else b""
                self._handshake_responses[cmd_id] = payload
                log.info("RESP 0x%02x (%d bytes): %s", cmd_id, len(data), data.hex())
            else:
                self._handshake_responses[data[0]] = data[1:]
                log.info("DATA 0x%02x (%d bytes): %s", data[0], len(data), data.hex())

    def _emit_pen(self, x: int, y: int, pressure: int, tilt_x: int = 0, tilt_y: int = 0):
        """Emit pen data to uinput."""
        if not self.uinput or self.uinput.fd < 0:
            log.info("PEN: x=%d y=%d p=%d tx=%d ty=%d", x, y, pressure, tilt_x, tilt_y)
            return
        pen_active = pressure > 0 or x > 0 or y > 0
        if pen_active:
            self.uinput.report(x, y, pressure, True, tilt_x, tilt_y)
            self._pen_was_active = True
        elif self._pen_was_active:
            self.uinput.pen_up()
            self._pen_was_active = False
        self._stats["samples"] += 1

    def _on_disconnect(self):
        self._disconnect_event.set()

    # ── Connection + handshake ──

    async def _send_handshake_cmds(self, write_fd: int, cmds: list[bytes], label: str) -> bool:
        """Send a list of handshake commands via the write fd."""
        log.info("Sending %s handshake via fd...", label)
        for i, cmd in enumerate(cmds):
            try:
                os.write(write_fd, cmd)
                log.debug("  %s cmd %d: %s", label, i + 1, cmd.hex())
                await asyncio.sleep(0.15)
            except OSError as e:
                log.warning("%s handshake cmd %d write failed: %s", label, i + 1, e)
                return False
        return True

    async def _connect_and_handshake(self) -> bool:
        """Connect, IMMEDIATELY acquire fds (before HOGP handler activates ~1s),
        unbind hid-generic to prevent HOGP from killing notifications,
        then send the tablet handshake."""
        # Clean up fds from previous session
        self.ble._close_fds()
        self._notify_fd = None

        if not await self.ble.force_reconnect():
            return False
        self._disconnect_event.clear()

        # Race the HOGP handler — acquire fds IMMEDIATELY after connect.
        # HOGP activates ~1-2s after connect and blocks AcquireNotify.
        notify_fd, write_fd = await self.ble.acquire_fds()
        self._notify_fd = notify_fd

        if not notify_fd:
            log.error("AcquireNotify failed — HOGP may have claimed the device")
            return False

        # Unbind hid-generic AFTER acquiring fds — prevents HOGP from killing
        # the notification stream. Run immediately AND schedule a delayed retry,
        # because BlueZ may create a new HID device after our first unbind.
        _unbind_hid_generic()

        await self._send_handshake_cmds(write_fd, TABLET_HANDSHAKE, "tablet")
        log.info("Handshake complete")

        # Schedule repeated unbinds to catch late HID device creation
        asyncio.get_event_loop().call_later(1.0, _unbind_hid_generic)
        asyncio.get_event_loop().call_later(2.5, _unbind_hid_generic)
        asyncio.get_event_loop().call_later(5.0, _unbind_hid_generic)
        return True

    # ── Notification fd reader ──

    async def _notify_fd_reader(self):
        """Read raw notification data from the AcquireNotify fd."""
        if self._notify_fd is None:
            return
        fd = self._notify_fd
        loop = asyncio.get_event_loop()
        log.info("Reading notifications from fd %d", fd)

        # Use add_reader for proper non-blocking I/O
        data_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_readable():
            try:
                data = os.read(fd, 512)
                if data:
                    data_queue.put_nowait(data)
            except BlockingIOError:
                pass  # EAGAIN — no data right now
            except OSError:
                data_queue.put_nowait(b"")  # signal EOF

        loop.add_reader(fd, _on_readable)
        self.ble._notify_fd_alive = True
        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(data_queue.get(), timeout=4.0)
                except asyncio.TimeoutError:
                    # No data for 4s — HOGP likely killed the notification stream
                    log.warning("Data stall (4s no data) — triggering reconnect")
                    self.ble._notify_fd_alive = False
                    self._disconnect_event.set()
                    break
                if not data:
                    log.warning("Notification fd closed — triggering reconnect")
                    self.ble._notify_fd_alive = False
                    self._disconnect_event.set()
                    break
                self._on_notification(data)
        finally:
            self.ble._notify_fd_alive = False
            loop.remove_reader(fd)
            try:
                os.close(fd)
            except OSError:
                pass

    # ── Keepalive (send-first, sleep-second) ──

    async def _keepalive_loop(self):
        """Send battery query as keepalive. Recreated per session."""
        cmd = build_tablet_cmd(CMD_TABLET_BATTERY)
        while self._running:
            await self.ble.write_cmd(cmd)
            await asyncio.sleep(KEEPALIVE_INTERVAL)

    # ── Session loop (keepalive lifecycle + reconnect) ──

    async def _session_loop(self):
        """Each connection session gets a reader task and keepalive task."""
        while self._running:
            reader_task = asyncio.create_task(self._notify_fd_reader())
            keepalive_task = asyncio.create_task(self._keepalive_loop())

            await self._disconnect_event.wait()
            self._disconnect_event.clear()

            reader_task.cancel()
            keepalive_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

            if not self._running:
                break

            if self._pen_was_active and self.uinput:
                self.uinput.pen_up()
                self._pen_was_active = False

            self._stats["reconnects"] += 1
            backoff = 1.0
            while self._running:
                self._disconnect_event.clear()
                try:
                    if await self._connect_and_handshake():
                        log.info("Reconnected OK")
                        break
                except Exception as e:
                    log.warning("Reconnect error: %s", e)
                log.info("Retry in %.1fs...", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ── Main entry ──

    async def run(self):
        print("╔══════════════════════════════════════════════╗")
        print("║   Huion Note X10 BLE Driver for Linux       ║")
        print("╚══════════════════════════════════════════════╝\n")

        await self.ble.connect_bus()
        await self.ble.setup_signals(self._on_notification, self._on_disconnect)

        if not await self._connect_and_handshake():
            log.error("Initial connection failed. Make sure the tablet is powered on.")
            return

        self.uinput = UInputDevice(self.max_x, self.max_y, self.max_pressure)
        try:
            self.uinput.open()
        except PermissionError:
            log.error("Cannot open /dev/uinput — run as root or check udev rules")
            return
        except OSError as e:
            log.error("Cannot open /dev/uinput: %s", e)
            return

        print(f"  Device:   {self.device_name}")
        print(f"  MAC:      {self.ble.mac}")
        print(f"  Max X:    {self.max_x}")
        print(f"  Max Y:    {self.max_y}")
        print(f"  Pressure: {self.max_pressure} levels")
        print(f"\n  Driver active — move the pen! (Ctrl+C to stop)\n")

        session = asyncio.create_task(self._session_loop())

        try:
            last_report = time.monotonic()
            while self._running:
                await asyncio.sleep(5.0)
                now = time.monotonic()
                elapsed = now - last_report
                rate = self._stats["samples"] / elapsed if elapsed > 0 else 0
                log.info("samples=%d (%.0f/s), reconnects=%d",
                         self._stats["samples"], rate, self._stats["reconnects"])
                self._stats["samples"] = 0
                last_report = now
        except asyncio.CancelledError:
            pass
        finally:
            session.cancel()

    async def shutdown(self):
        log.info("Shutting down...")
        self._running = False
        self._disconnect_event.set()
        if self._pen_was_active and self.uinput and self.uinput.fd >= 0:
            self.uinput.pen_up()
        if self.uinput:
            self.uinput.close()
        await self.ble.close()
        log.info("Goodbye.")


# ─── Main ────────────────────────────────────────────────────────────────────

def _find_tablet_mac() -> str | None:
    """Scan BlueZ paired devices for a Huion Note X10 (VID 256C, PID 8251)."""
    import subprocess
    try:
        # List all BlueZ device paths
        out = subprocess.check_output(
            ["busctl", "tree", "--list", "org.bluez"],
            text=True, stderr=subprocess.DEVNULL)
        # Filter to top-level device paths (contain /dev_ but no further /)
        dev_paths = []
        for line in out.splitlines():
            line = line.strip()
            if "/dev_" in line and "/" not in line.split("/dev_")[-1]:
                dev_paths.append(line)
        # Check each device's Modalias for our VID:PID
        for path in dev_paths:
            try:
                modalias = subprocess.check_output(
                    ["busctl", "get-property", "org.bluez", path,
                     "org.bluez.Device1", "Modalias"],
                    text=True, stderr=subprocess.DEVNULL)
                if "256C" in modalias.upper() and "8251" in modalias:
                    return path.split("/dev_")[-1].replace("_", ":")
            except (subprocess.CalledProcessError, OSError):
                continue
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return None


async def main():
    parser = argparse.ArgumentParser(description="Huion Note X10 BLE driver")
    parser.add_argument("--mac",
                        help="BLE MAC address (auto-detected if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    mac = args.mac
    if not mac:
        mac = _find_tablet_mac()
        if not mac:
            log.error("No Huion Note X10 found. Pair it first, or use --mac XX:XX:XX:XX:XX:XX")
            return
        log.info("Auto-detected tablet: %s", mac)

    driver = HuionBLEDriver(mac)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(driver.shutdown()))

    await driver.run()


if __name__ == "__main__":
    asyncio.run(main())
