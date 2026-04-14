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
import json
import logging
import os
import shutil
import signal
import struct
import time

from dbus_fast.aio import MessageBus
from dbus_fast import BusType, Message, MessageType, Variant

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


def build_tablet_cmd(cmd_id: int) -> bytes:
    """Pen Tablet Mode framing: cd CMD 00 00 00 00 00 00 (no length, no end marker)"""
    return bytes([MARKER_START, cmd_id, 0, 0, 0, 0, 0, 0])


TABLET_HANDSHAKE = [
    build_tablet_cmd(CMD_TABLET_NAME),    # 1. get device name
    build_tablet_cmd(CMD_TABLET_INFO),    # 2. get device info
    build_tablet_cmd(CMD_TABLET_VERIFY),  # 3. verify manufacturer
]
KEEPALIVE_INTERVAL = 5.0

# Fallback maxes; overridden at runtime by the cmd 0xC8 device-info response.
# Values match what the X10 firmware self-reports in Pen Tablet Mode.
DEFAULT_MAX_X = 37400
DEFAULT_MAX_Y = 28200
DEFAULT_MAX_PRESSURE = 8191

# ─── Orientation ─────────────────────────────────────────────────────────────
#
# The device's native raw frame is landscape:
#   raw X axis = long edge (37400), raw Y axis = short edge (28200).
# Users hold the X10 in portrait, so the driver rotates raw (x,y) into an
# output frame before emitting to uinput.
#
# Formulas mirror the macOS driver (RE'd from HuionTabletCore):
#   _hn_pt_ratio_rotate{90,180,270}  — rotate a (0..1, 0..1) ratio
#   _hn_angle_rotate{90,180,270}     — rotate a signed (tilt_x, tilt_y)

ORIENTATION_LANDSCAPE    = "landscape"
ORIENTATION_PORTRAIT_CCW = "portrait_ccw"   # 90°
ORIENTATION_INVERTED     = "inverted"       # 180°
ORIENTATION_PORTRAIT_CW  = "portrait_cw"    # 270°

ORIENTATIONS = (ORIENTATION_LANDSCAPE, ORIENTATION_PORTRAIT_CCW,
                ORIENTATION_INVERTED, ORIENTATION_PORTRAIT_CW)


def rotate_ratio(nx: float, ny: float, orient: str) -> tuple[float, float]:
    """Rotate a normalized pen position (nx, ny), each in [0, 1]."""
    if orient == ORIENTATION_PORTRAIT_CCW:
        return 1.0 - ny, nx
    if orient == ORIENTATION_INVERTED:
        return 1.0 - nx, 1.0 - ny
    if orient == ORIENTATION_PORTRAIT_CW:
        return ny, 1.0 - nx
    return nx, ny


def rotate_tilt(tx: int, ty: int, orient: str) -> tuple[int, int]:
    """Rotate a signed tilt pair to match rotate_ratio."""
    if orient == ORIENTATION_PORTRAIT_CCW:
        tx, ty = ty, -tx
    elif orient == ORIENTATION_INVERTED:
        tx, ty = -tx, -ty
    elif orient == ORIENTATION_PORTRAIT_CW:
        tx, ty = -ty, tx
    # Clamp to declared uinput range (−128 becomes +128 after negation)
    return max(-127, min(127, tx)), max(-127, min(127, ty))


def is_rotated_90(orient: str) -> bool:
    return orient in (ORIENTATION_PORTRAIT_CW, ORIENTATION_PORTRAIT_CCW)


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
        # res is in units/mm. The X10 firmware reports 5080 LPI (~200 u/mm)
        # in the cmd 0xC8 handshake response.
        for code, min_val, max_val, res in [
            (ABS_X, 0, self.max_x, 200),
            (ABS_Y, 0, self.max_y, 200),
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


# ─── Region mapping (tablet → editor pane) ───────────────────────────────────

REGION_SOCKET_PATH = "/tmp/tablet-region.sock"


def _resolve_hyprctl() -> str | None:
    """Find hyprctl binary. Systemd user services on NixOS run with a minimal
    PATH (coreutils/findutils/etc only), so hyprctl is not resolvable via
    bare-name lookup. Cache the absolute path once at startup."""
    found = shutil.which("hyprctl")
    if found:
        return found
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "nobody"
    candidates = [
        f"/etc/profiles/per-user/{user}/bin/hyprctl",
        "/run/current-system/sw/bin/hyprctl",
        "/usr/bin/hyprctl",
        "/usr/local/bin/hyprctl",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


HYPRCTL = _resolve_hyprctl()


class RegionMapper:
    """Maps the full tablet surface onto a sub-region of the screen (the
    Obsidian editor pane).

    Operates in post-rotation output space: the driver has already rotated
    raw device coords, so Hyprland sees a "normal" tablet and maps it with
    transform=0 (no compositor-side rotation). This makes the transform a
    direct linear remap.
    """

    def __init__(self, max_x: int, max_y: int):
        # max_x/max_y are the post-rotation output extents advertised to uinput
        self.max_x = max_x
        self.max_y = max_y
        self.active = False
        # Region and monitor in screen-space pixels
        self._region: dict | None = None  # {x, y, width, height}
        self._monitor: dict | None = None  # {x, y, width, height}
        self._monitor_name: str | None = None

    def update(self, region: dict, monitor: dict, monitor_name: str = "") -> None:
        self._region = region
        self._monitor = monitor
        # Switch tablet output to follow Obsidian's monitor
        if monitor_name and monitor_name != self._monitor_name:
            self._monitor_name = monitor_name
            self._switch_tablet_output(monitor_name)
        self.active = True
        log.info("Region mapping: region=(%d,%d %dx%d) monitor=%s (%d,%d %dx%d)",
                 region["x"], region["y"], region["width"], region["height"],
                 monitor_name,
                 monitor["x"], monitor["y"], monitor["width"], monitor["height"])

    @staticmethod
    def _switch_tablet_output(monitor_name: str) -> None:
        """Tell Hyprland to map the tablet to the given monitor."""
        import subprocess
        if not HYPRCTL:
            log.warning("Cannot switch tablet output: hyprctl not found in PATH "
                        "or standard locations")
            return
        try:
            result = subprocess.run(
                [HYPRCTL, "keyword", "input:tablet:output", monitor_name],
                capture_output=True, text=True, timeout=2.0)
            if result.returncode != 0:
                log.warning("hyprctl keyword rc=%d stderr=%r",
                            result.returncode, result.stderr.strip())
                return
            log.info("Switched tablet output to %s", monitor_name)
        except Exception as e:
            log.warning("Failed to switch tablet output: %s", e)

    def deactivate(self) -> None:
        self.active = False
        log.info("Region mapping deactivated")

    def transform(self, out_x: int, out_y: int) -> tuple[int, int]:
        """Remap a post-rotation output coord so Hyprland (transform=0)
        places the pen inside the target region.

        Hyprland's linear map: screen = monitor.origin + (abs / max) * monitor.size.
        We want the pen's full [0..max] range to land inside `region`:
            screen = region.origin + (abs / max) * region.size
        Solving for abs:
            abs = max * (region.origin + (abs / max) * region.size - monitor.origin) / monitor.size
        """
        if not self._region or not self._monitor:
            return out_x, out_y

        r = self._region
        m = self._monitor

        # Normalized pen position across the full tablet, [0..1]
        nx = out_x / self.max_x if self.max_x > 0 else 0
        ny = out_y / self.max_y if self.max_y > 0 else 0

        # Target screen pixel inside the region
        target_sx = r["x"] + nx * r["width"]
        target_sy = r["y"] + ny * r["height"]

        # Tablet coord that produces that screen pixel under Hyprland's linear map
        new_x = self.max_x * (target_sx - m["x"]) / m["width"]  if m["width"]  > 0 else 0
        new_y = self.max_y * (target_sy - m["y"]) / m["height"] if m["height"] > 0 else 0

        new_x = max(0, min(self.max_x, int(new_x)))
        new_y = max(0, min(self.max_y, int(new_y)))
        return new_x, new_y


async def region_socket_client(mapper: RegionMapper, running_check,
                               battery_getter=None,
                               socket_path: str = REGION_SOCKET_PATH):
    """Connect to the Obsidian plugin's Unix socket and receive region updates.
    Reconnects automatically if the plugin restarts."""
    log = logging.getLogger("huion-ble.region")

    while running_check():
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            log.info("Connected to region socket %s", socket_path)

            # Send driver status
            if battery_getter:
                status = {"type": "driver_status", "connected": True,
                          "battery": battery_getter()}
                writer.write((json.dumps(status) + "\n").encode())
                await writer.drain()

            while running_check():
                line = await reader.readline()
                if not line:
                    log.info("Region socket closed by plugin")
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from plugin: %s", line[:100])
                    continue

                if msg.get("type") == "region":
                    mapper.update(msg["region"], msg["monitor"],
                                  msg.get("monitorName", ""))
                elif msg.get("type") == "inactive":
                    mapper.deactivate()

            writer.close()
        except (ConnectionRefusedError, FileNotFoundError):
            # Plugin not running — wait and retry
            pass
        except OSError as e:
            log.debug("Region socket error: %s", e)
        except asyncio.CancelledError:
            break

        mapper.deactivate()
        if running_check():
            await asyncio.sleep(2.0)  # Retry interval


# ─── Protocol parsers ────────────────────────────────────────────────────────


def parse_tablet_pen_report(data: bytes) -> tuple | None:
    """Parse Pen Tablet Mode pen data from raw BLE FFE1 notification.

    Returns (status, x, y, pressure, tilt_x, tilt_y) or None.

    Raw BLE wire format (14 bytes):
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
    """
    if len(data) < 13 or data[0] != 0x55 or data[1] != 0x54:
        return None
    # byte[2] is a report-type discriminator (per Ghidra: < 0x91 = pen,
    # 0xA0 = special, 0xE0/0xE2 = buttons, 0xE1 = touch, 0xE3 = multi-click).
    # 0x00 = pen-leave (stale coords + zero pressure), 0x80 = hover, 0x81 = touch.
    if data[2] not in (0x00, 0x80, 0x81):
        return None

    status = data[2]
    x = data[3] | (data[4] << 8) | (data[9] << 16)
    y = data[5] | (data[6] << 8) | (data[10] << 16)
    pressure = struct.unpack_from("<H", data, 7)[0]
    tilt_x = struct.unpack_from("<b", data, 11)[0]
    tilt_y = struct.unpack_from("<b", data, 12)[0]
    return (status, x, y, pressure, tilt_x, tilt_y)


def parse_tablet_device_info(data: bytes) -> dict:
    """Parse device info from cmd 0xC8 (200) response payload.

    From _parse_cmd_resp_200 in HuionTabletCore:
      [0-2] max_X (24-bit LE)   [3-5] max_Y (24-bit LE)
      [6-7] max_P (16-bit LE)   [8-9] LPI (16-bit LE)
    """
    info = {"max_x": DEFAULT_MAX_X, "max_y": DEFAULT_MAX_Y,
            "max_pressure": DEFAULT_MAX_PRESSURE, "lpi": 0,
            "pen_btn_num": 0, "hkey_num": 0, "skey_num": 0}
    if len(data) < 8:
        return info
    info["max_x"] = data[0] | (data[1] << 8) | (data[2] << 16)
    info["max_y"] = data[3] | (data[4] << 8) | (data[5] << 16)
    info["max_pressure"] = struct.unpack_from("<H", data, 6)[0]
    if len(data) >= 10:
        info["lpi"] = struct.unpack_from("<H", data, 8)[0]
    # Offsets from Ghidra _parse_cmd_resp_200: pen_btn (10), hkey (11), skey (12).
    # hkey_num > 0 is the Mac driver's gate for dispatching 0xE0/0xE2 button frames.
    if len(data) >= 11: info["pen_btn_num"] = data[10]
    if len(data) >= 12: info["hkey_num"] = data[11]
    if len(data) >= 13: info["skey_num"] = data[12]
    return info


def parse_tablet_device_name(data: bytes) -> str:
    """Parse device name from cmd 0xC9 (201) response — plain UTF-8."""
    return data.decode("utf-8", errors="replace").rstrip("\x00") if data else "unknown"


# ─── BLE connection ──────────────────────────────────────────────────────────

class BLEConnection:
    def __init__(self, mac: str):
        self.mac = mac
        self.device_path = f"{ADAPTER_PATH}/dev_{mac.replace(':', '_')}"
        # _write_fd used by write_cmd for raw fd writes (if AcquireWrite succeeded)
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
        try:
            await asyncio.wait_for(self.bus.call(Message(
                destination=BLUEZ, path=self.device_path,
                interface="org.bluez.Device1", member="Disconnect",
            )), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as e:
            log.warning("Disconnect call: %s", e)
        for _ in range(20):
            if not await self.is_connected():
                return
            await asyncio.sleep(0.1)

    async def _services_resolved(self) -> bool:
        return await self._get_prop(self.device_path, "org.bluez.Device1", "ServicesResolved") is True

    async def connect(self) -> bool:
        """Establish a BLE connection with resolved GATT services.
        Reuses existing connection if available — never forces disconnect
        (device in idle mode ignores disconnect, corrupting BlueZ state).
        """
        if await self.is_connected() and await self._services_resolved():
            log.info("Connected to %s, GATT services resolved", self.mac)
            return True

        log.info("Connecting to %s...", self.mac)
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=self.device_path,
            interface="org.bluez.Device1", member="Connect",
        ))
        if reply.message_type == MessageType.ERROR:
            err = reply.error_name or ""
            if "AlreadyConnected" not in err:
                if not await self.is_connected():
                    log.error("Connect failed: %s", err)
                    return False
        # Wait for connection + service resolution
        for _ in range(80):  # up to 8s
            if await self.is_connected() and await self._services_resolved():
                log.info("Connected, GATT services resolved")
                return True
            await asyncio.sleep(0.1)
        if not await self.is_connected():
            log.error("Connection failed — is the tablet awake?")
        else:
            log.error("Connected but GATT services not resolved")
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
        """Write command via raw fd if acquired, otherwise D-Bus WriteValue.
        Uses WriteWithoutResponse (type=command) matching the official driver."""
        if self._write_fd is not None:
            try:
                os.write(self._write_fd, data)
                return True
            except OSError as e:
                log.debug("fd write failed: %s", e)
                return False
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=self.ffe2_path,
            interface="org.bluez.GattCharacteristic1",
            member="WriteValue", signature="aya{sv}",
            body=[bytearray(data), {"type": Variant("s", "command")}],
        ))
        if reply.message_type == MessageType.ERROR:
            log.debug("WriteValue failed: %s", reply.error_name)
            return False
        return True

    async def acquire_fds(self) -> tuple[int | None, int | None]:
        """Acquire raw fds for FFE1 (notify) and FFE2 (write), and enable
        indications on FFE2 via StartNotify.

        No delay — acquire immediately after ServicesResolved. HOGP may kill
        the fds after ~1s, but the retry loop handles that. After HOGP
        settles, fds acquired on a fresh connection are stable.
        """
        notify_fd = None
        write_fd = None

        # AcquireNotify on FFE1 — raw fd for pen data
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
            fd_idx = reply.body[0]
            mtu = reply.body[1]
            fds = getattr(reply, 'unix_fds', None)
            if fds and len(fds) > fd_idx:
                notify_fd = fds[fd_idx]
            else:
                notify_fd = fd_idx
            log.info("AcquireNotify FFE1: fd=%d, MTU=%d", notify_fd, mtu)

        # StartNotify on FFE2 — enable indications (CCCD write for device
        # mode detection, matching macOS driver's setNotifyValue:1 on both)
        reply = await self.bus.call(Message(
            destination=BLUEZ, path=self.ffe2_path,
            interface="org.bluez.GattCharacteristic1",
            member="StartNotify",
        ))
        if reply.message_type == MessageType.ERROR:
            err = reply.error_name or ""
            if "InProgress" not in err:
                log.warning("StartNotify FFE2 failed: %s", err)
        else:
            log.info("StartNotify FFE2: indications enabled")

        # AcquireWrite on FFE2 — raw fd for commands
        self._close_fds()
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
                if self._disconnect_cb:
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
        """Release GATT resources before closing the D-Bus connection.
        Keep FFE2 StartNotify alive — the indication session helps the
        device stay in pen tablet mode across reconnects."""
        if self.bus:
            self._close_fds()
            self.bus.disconnect()


# ─── Driver core ─────────────────────────────────────────────────────────────

class HuionBLEDriver:
    def __init__(self, mac: str, orientation: str = ORIENTATION_PORTRAIT_CW):
        self.ble = BLEConnection(mac)
        self.uinput: UInputDevice | None = None
        self.orientation = orientation
        # Raw = what the device sends. Output = post-rotation, advertised to uinput.
        self.raw_max_x = DEFAULT_MAX_X
        self.raw_max_y = DEFAULT_MAX_Y
        self.max_pressure = DEFAULT_MAX_PRESSURE
        if is_rotated_90(orientation):
            self.max_x, self.max_y = self.raw_max_y, self.raw_max_x
        else:
            self.max_x, self.max_y = self.raw_max_x, self.raw_max_y
        self.device_name = "unknown"
        self._running = True
        self._disconnect_event = asyncio.Event()
        self._notify_fd: int | None = None
        self._pen_was_active = False
        self._handshake_responses: dict[int, bytes] = {}
        self._stats = {"samples": 0, "reconnects": 0,
                       "raw_x_min": None, "raw_x_max": None,
                       "raw_y_min": None, "raw_y_max": None}
        self.region_mapper = RegionMapper(self.max_x, self.max_y)
        log.info("Orientation: %s (raw %dx%d → output %dx%d)",
                 orientation, self.raw_max_x, self.raw_max_y,
                 self.max_x, self.max_y)
        log.info("hyprctl resolved to: %s",
                 HYPRCTL or "(not found — monitor switching disabled)")

    # ── Notification handling ──

    def _on_notification(self, data: bytes):
        if not data:
            return
        log.debug("RAW (%d bytes): %s", len(data), data[:30].hex())

        # Try parsing as pen data (55 54 header, 14 bytes)
        report = parse_tablet_pen_report(data)
        if report is not None:
            status, x, y, pressure, tilt_x, tilt_y = report
            if status == 0x00:
                # Pen left proximity — coords are stale, skip them and lift.
                if self._pen_was_active and self.uinput:
                    self.uinput.pen_up()
                    self._pen_was_active = False
                return
            # Status: 0x80 = hovering (pen near surface), 0x81 = touching
            pen_touching = (status & 0x01) != 0
            self._emit_pen(x, y, pressure if pen_touching else 0, tilt_x, tilt_y)
            return

        # Not pen data — parse as Pen Tablet Mode command response.
        # BLE RX framing is [LEN, CMD_ID, payload...] where LEN == len(frame).
        # (The 0xCD MARKER_START byte only appears on TX, not RX.)
        if len(data) >= 2 and data[0] == len(data):
            cmd_id = data[1]
            payload = data[2:]
            self._handshake_responses[cmd_id] = payload
            log.info("RESP 0x%02x (%d bytes): %s", cmd_id, len(data), data.hex())
        elif len(data) >= 1:
            log.debug("Unknown frame (%d bytes): %s", len(data), data.hex())

    def _emit_pen(self, raw_x: int, raw_y: int, pressure: int,
                  tilt_x: int = 0, tilt_y: int = 0):
        """Rotate raw device coords into output space and emit to uinput."""
        # Track raw input range (pre-rotation) for calibration diagnostics
        s = self._stats
        if s["raw_x_min"] is None or raw_x < s["raw_x_min"]: s["raw_x_min"] = raw_x
        if s["raw_x_max"] is None or raw_x > s["raw_x_max"]: s["raw_x_max"] = raw_x
        if s["raw_y_min"] is None or raw_y < s["raw_y_min"]: s["raw_y_min"] = raw_y
        if s["raw_y_max"] is None or raw_y > s["raw_y_max"]: s["raw_y_max"] = raw_y

        # Rotate raw → output: normalize in raw frame, rotate ratio, denormalize
        # in output frame. Matches macOS HnCoordMap::cRatioInRawCoord path.
        nx = raw_x / self.raw_max_x if self.raw_max_x > 0 else 0
        ny = raw_y / self.raw_max_y if self.raw_max_y > 0 else 0
        nx, ny = rotate_ratio(nx, ny, self.orientation)
        out_x = int(nx * self.max_x)
        out_y = int(ny * self.max_y)
        tilt_x, tilt_y = rotate_tilt(tilt_x, tilt_y, self.orientation)

        if not self.uinput or self.uinput.fd < 0:
            log.info("PEN: x=%d y=%d p=%d tx=%d ty=%d",
                     out_x, out_y, pressure, tilt_x, tilt_y)
            return

        if self.region_mapper.active:
            out_x, out_y = self.region_mapper.transform(out_x, out_y)
        pen_active = pressure > 0 or out_x > 0 or out_y > 0
        if pen_active:
            self.uinput.report(out_x, out_y, pressure, True, tilt_x, tilt_y)
            self._pen_was_active = True
        elif self._pen_was_active:
            self.uinput.pen_up()
            self._pen_was_active = False
        s["samples"] += 1

    def _on_disconnect(self):
        self._disconnect_event.set()

    # ── Connection + handshake ──

    async def _send_handshake(self) -> bool:
        """Send handshake commands and adopt the device's self-reported
        capabilities from the cmd 0xC8 response."""
        log.info("Sending handshake...")
        for i, cmd in enumerate(TABLET_HANDSHAKE):
            if not await self.ble.write_cmd(cmd):
                log.warning("Handshake cmd %d write failed", i + 1)
                return False
            log.debug("  cmd %d: %s", i + 1, cmd.hex())
            await asyncio.sleep(0.15)

        # Grace period for the final response to arrive on FFE2 indications
        await asyncio.sleep(0.3)

        name_payload = self._handshake_responses.get(CMD_TABLET_NAME)
        if name_payload:
            self.device_name = parse_tablet_device_name(name_payload)

        info_payload = self._handshake_responses.get(CMD_TABLET_INFO)
        if info_payload:
            info = parse_tablet_device_info(info_payload)
            self.raw_max_x = info["max_x"]
            self.raw_max_y = info["max_y"]
            self.max_pressure = info["max_pressure"]
            if is_rotated_90(self.orientation):
                self.max_x, self.max_y = self.raw_max_y, self.raw_max_x
            else:
                self.max_x, self.max_y = self.raw_max_x, self.raw_max_y
            self.region_mapper.max_x = self.max_x
            self.region_mapper.max_y = self.max_y
            log.info("Device info: max_x=%d max_y=%d max_p=%d lpi=%d → output %dx%d",
                     info["max_x"], info["max_y"], info["max_pressure"], info["lpi"],
                     self.max_x, self.max_y)
            log.info("Button counts: pen_btn=%d hkey=%d skey=%d "
                     "(hkey>0 is firmware's gate for 0xE0/0xE2 frames on FFE1)",
                     info["pen_btn_num"], info["hkey_num"], info["skey_num"])
        else:
            log.warning("No device-info response; using fallback maxes "
                        "(raw %dx%d)", self.raw_max_x, self.raw_max_y)
        return True

    async def _connect_and_handshake(self) -> bool:
        """Connect, acquire fds, send handshake."""
        self.ble._close_fds()
        self._notify_fd = None

        if not await self.ble.connect():
            return False
        self._disconnect_event.clear()

        notify_fd, write_fd = await self.ble.acquire_fds()
        self._notify_fd = notify_fd

        if not notify_fd:
            log.error("AcquireNotify failed — GATT characteristics not available")
            return False

        if not await self._send_handshake():
            return False
        log.info("Handshake complete")
        return True

    # ── Notification fd reader ──

    async def _notify_fd_reader(self):
        """Read raw notification data from the AcquireNotify fd."""
        if self._notify_fd is None:
            return
        fd = self._notify_fd
        loop = asyncio.get_event_loop()
        log.info("Reading notifications from fd %d", fd)

        data_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_readable():
            try:
                data = os.read(fd, 512)
                if data:
                    data_queue.put_nowait(data)
                else:
                    data_queue.put_nowait(b"")  # EOF — device disconnected
            except BlockingIOError:
                pass
            except OSError:
                data_queue.put_nowait(b"")

        loop.add_reader(fd, _on_readable)
        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(data_queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    log.warning("Data stall (30s no data) — triggering reconnect")
                    self._disconnect_event.set()
                    break
                if not data:
                    log.warning("Notification fd closed — triggering reconnect")
                    self._disconnect_event.set()
                    break
                self._on_notification(data)
        finally:
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
        """Reader + keepalive tasks, reconnect on disconnect."""
        while self._running:
            reader_task = asyncio.create_task(self._notify_fd_reader())
            keepalive_task = asyncio.create_task(self._keepalive_loop())

            await self._disconnect_event.wait()
            self._disconnect_event.clear()

            reader_task.cancel()
            keepalive_task.cancel()
            for task in (reader_task, keepalive_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if not self._running:
                break

            if self._pen_was_active and self.uinput:
                self.uinput.pen_up()
                self._pen_was_active = False

            self._stats["reconnects"] += 1
            while self._running:
                self._disconnect_event.clear()
                try:
                    if await self._connect_and_handshake():
                        log.info("Reconnected OK")
                        break
                except Exception as e:
                    log.warning("Reconnect error: %s", e)
                # AcquireNotify may fail on stale GATT state — wait for
                # a device-initiated disconnect to get a fresh GATT client.
                try:
                    await asyncio.wait_for(
                        self._disconnect_event.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.5)

    # ── Main entry ──

    async def run(self):
        print("╔══════════════════════════════════════════════╗")
        print("║   Huion Note X10 BLE Driver for Linux       ║")
        print("╚══════════════════════════════════════════════╝\n")

        await self.ble.connect_bus()
        await self.ble.setup_signals(self._on_notification, self._on_disconnect)

        backoff = 2.0
        while self._running:
            if await self._connect_and_handshake():
                break
            log.info("Connection failed, retry in %.0fs... "
                     "(close/open tablet cover for fresh BLE link)", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)
        if not self._running:
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

        print(f"  Device:      {self.device_name}")
        print(f"  MAC:         {self.ble.mac}")
        print(f"  Orientation: {self.orientation}")
        print(f"  Output X:    0..{self.max_x}")
        print(f"  Output Y:    0..{self.max_y}")
        print(f"  Pressure:    {self.max_pressure} levels")
        print(f"\n  Driver active — move the pen!")
        print(f"  If no pen data, close and reopen the tablet cover.\n")

        session = asyncio.create_task(self._session_loop())
        region_client = asyncio.create_task(
            region_socket_client(self.region_mapper,
                                 running_check=lambda: self._running))

        try:
            start_time = time.monotonic()
            last_report = start_time
            total_samples = 0
            idle_warned = False
            while self._running:
                await asyncio.sleep(5.0)
                now = time.monotonic()
                elapsed = now - last_report
                s = self._stats
                rate = s["samples"] / elapsed if elapsed > 0 else 0
                rx = (f"{s['raw_x_min']}-{s['raw_x_max']}"
                      if s["raw_x_min"] is not None else "-")
                ry = (f"{s['raw_y_min']}-{s['raw_y_max']}"
                      if s["raw_y_min"] is not None else "-")
                log.info("samples=%d (%.0f/s), reconnects=%d, "
                         "raw_x=%s/%d raw_y=%s/%d",
                         s["samples"], rate, s["reconnects"],
                         rx, self.raw_max_x, ry, self.raw_max_y)
                total_samples += s["samples"]
                s["samples"] = 0
                s["raw_x_min"] = s["raw_x_max"] = None
                s["raw_y_min"] = s["raw_y_max"] = None
                last_report = now
                if total_samples == 0 and not idle_warned and (now - start_time) > 10:
                    log.warning("No pen data yet — close and reopen the tablet "
                                "cover to activate pen tablet mode")
                    idle_warned = True
        except asyncio.CancelledError:
            pass
        finally:
            session.cancel()
            region_client.cancel()

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
    parser.add_argument("--orientation", default=ORIENTATION_PORTRAIT_CW,
                        choices=ORIENTATIONS,
                        help="Tablet physical orientation "
                             "(default: portrait_cw — top-of-tablet on the right)")
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

    driver = HuionBLEDriver(mac, orientation=args.orientation)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(driver.shutdown()))

    await driver.run()


if __name__ == "__main__":
    asyncio.run(main())
