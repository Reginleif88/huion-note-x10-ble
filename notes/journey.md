# Huion Note X10 BLE Reverse Engineering — Session Log

## 2026-04-09: Initial Exploration and Protocol Crack

### Starting Point

The Huion Note X10 is a pen tablet with BLE 5.0 support that works wirelessly on
Windows/macOS/Android, but has zero Linux BLE support. The goal: make it work on Linux.

**What was known:**
- Device pairs via BlueZ, briefly creates `/dev/hidraw0`, but hid-generic claims it as a keyboard
- hid-uclogic (kernel driver) only handles USB bus 0x0003, ignores BLE bus 0x0005
- Huion's official Linux driver v15 has BLE code but deliberately disabled (stubs return false)
- OpenTabletDriver has USB support but no BLE transport
- GATT services were visible via `bluetoothctl` but unexplored

**Constraints:**
- Linux only (no Windows/macOS access)
- Android phone available as fallback for BLE sniffing

---

### Phase 1: Binary Archaeology

Examined two extracted Huion drivers:

| Driver | Version | Source |
|--------|---------|--------|
| Linux (deb) | v15.0.0.175 | `drivers/extracted_deb/` |
| macOS (dmg) | v15.7.24 | `drivers/extracted_dmg/` |
| Windows (exe) | v15.7.6.1858 | `drivers/extracted_win/` |

#### Key discovery: Linux BLE is deliberately disabled

Using `strings`, `nm`, and `objdump` on `libTabletSession.so`, found:

1. **Full BLE stack exists** — `blz_init`, `blz_connect`, `blz_char_write`,
   `blz_char_notify_start`, `HnBluetooth::openBLE()` — all present and linked
2. **But two gatekeeper functions are stubs that always return false:**
   - `is_ble_tablet_online()` at 0xdfa0: just `movb $0x0` and returns
   - `_check_huion_blz_device_setup()` at 0xdf62: same pattern
3. **BLE code is dead code** — the full protocol handler exists but never executes

#### Vendor-specific GATT UUIDs found in binaries

```
0000ffe0-0000-1000-8000-00805f9b34fb  — service (pen data)
0000ffe1-0000-1000-8000-00805f9b34fb  — FFE1 "chrtReport" (notify)
0000ffe2-0000-1000-8000-00805f9b34fb  — FFE2 "chrtCmd" (write)
```

Strings also revealed BLE command IDs: `cmd 200`, `cmd 202`, `cmd 209`, `cmd 212`.

#### Device identification from config files

From `tablet.cfg` in both drivers:
- Internal model: `HUION_T218`
- Pen model in config: `HUION_P211` → `PW100` (config mapping; actual pen is **PW320** "Scribo Pen"
  with dual nibs — ballpoint for paper, plastic for digital drawing)

#### macOS binary provided BLE architecture clues

The macOS `libTabletSession.dylib` uses CoreBluetooth with an `HnBle` Objective-C class.
Strings revealed: `_chrtCmd` (FFE2), `_chrtReport` (FFE1), `FFE0` service UUID.
The init sequence: USB attempt → if fail → BLE attempt → `hn_ble_open()`.

---

### Phase 2: Direct GATT Exploration from Linux

#### BlueZ D-Bus exploration

Bleak (Python BLE library) failed with `BleakDeviceNotFoundError` despite the device
being paired. Root cause: bleak's discovery scans for actively advertising devices,
but a paired-and-idle BLE device doesn't advertise. Separate issue: the device also
auto-disconnects within ~2-3 seconds if no GATT interaction occurs after connect.

**Workaround:** Used `busctl` to talk to BlueZ's D-Bus API directly, which always
works for paired devices.

Discovered that the device requires a **fresh disconnect + reconnect** for GATT reads
to work — stale connections report `Connected: yes` at L2CAP level but `Not connected`
at GATT level.

#### Complete GATT service map

```
[0001] 00001800 GAP
  [0002] 2A00 Device Name     [read, write-without-response, notify]
  [0004] 2A01 Appearance      [read]
  [0006] 2A04 Conn Params     [read]

[0008] 00001801 GATT
  [0009] 2A05 Service Changed [indicate]

[000c] 0000180A Device Info
  [000d] 2A29 Manufacturer    [read]           → "HUION"
  [000f] 2A50 PnP ID          [read]           → VID=0x256C, PID=0x8251
  [0011] 2A23 System ID       [read]

[0013] 00001812 HID
  [0014] 2A4E Protocol Mode   [read, write-without-response]
  [0016] 2A4D HID Report      [read, notify]   → KEYBOARD ONLY (no pen data!)
  [001a] 2A4B Report Map      [read]
  [001d] 2A4A HID Information  [read]           → v1.17, RemoteWake
  [001f] 2A4C Control Point   [write-without-response]

[0021] 0000180F Battery
  [0022] 2A19 Battery Level   [read, notify]   → 99%

[0025] 0000FFE0 Vendor (Pen Data)               ← THE TARGET
  [0026] FFE1 Report/Notify   [read, notify]
  [002a] FFE2 Command/Write   [read, write-without-response, indicate]

[002e] 00010203...1912 Vendor (Config)
  [002f] 00010203...2B12      [read, write-without-response]
```

**Critical finding:** The HID Report Map (2A4B) describes a **keyboard only**
(Usage Page 0x07, Usage 0x06). No Digitizer/Pen descriptor in the HID service.
In Note-Taking Mode, pen data flows through vendor-specific FFE0/FFE1. In Pen
Tablet Mode, the data path is unknown (possibly different HID reports or FFE1
with different commands).

#### Command probing attempt

Rewrote probe scripts using `dbus_fast` (bleak's D-Bus backend) for zero-latency
operations. Sent command IDs 200/202/209/212 in multiple framings to FFE2.

Result: all writes accepted (write-without-response always "succeeds") but **no
response notifications** — the framing was wrong. The USB HID feature report format
(`[report_id, cmd_id, ...]`) doesn't apply to the BLE vendor service.

---

### Phase 3: Android BLE Traffic Capture

Since direct probing couldn't crack the command framing, used the Android fallback.

#### Capture setup

1. Unihertz Titan 2 phone with Huion Note app
2. Enabled Bluetooth HCI snoop log in Developer Options
3. **Critical step:** toggled Bluetooth off/on (snoop log was `EMPTY` without this)
4. Connected Note X10 via Huion Note app, moved pen around
5. Pulled btsnoop via `adb bugreport` (direct pull is permission-denied on modern Android)

#### Result: 108KB btsnoop capture, 1963 HCI packets, 1324 FFE1 notifications

---

### Phase 4: Protocol Decode

Wrote `decode_btsnoop.py` to parse the capture. The protocol is clean and simple.

#### Command Format (Host → Device, WRITE_CMD to FFE2)

```
cd XX 08 P0 P1 P2 P3 ed    (always 8 bytes)
│  │  │  │           │
│  │  │  params(4B)  end marker (0xed)
│  │  length (always 0x08)
│  command ID
start marker (0xcd)
```

#### Response Format (Device → Host, NOTIFICATION on FFE1)

```
cd XX LL [payload...]       (variable length)
│  │  │
│  │  total packet length
│  type (matches command ID)
start marker (0xcd)
```

**Checksum:** Last byte of every packet = `sum(all_bytes) & 0xFF`

#### Handshake Sequence (from Android btsnoop — Note-Taking Mode)

| # | Command | Response | Purpose |
|---|---------|----------|---------|
| 1 | `cd 95 08 00 00 00 00 ed` | `cd 95 0b 28 6e 00 18 92 00 ff 1f` | Get device info (max X/Y/pressure) |
| 2 | `cd 96 08 01 03 00 00 ed` | `cd 96 04 03` | Set mode (param 01, 03) |
| 3 | `cd 96 08 03 02 00 00 ed` | `cd 96 04 02` | Set mode (param 03, 02) |
| 4 | `cd 91 08 00 00 00 00 ed` | `cd 91 13 "Huion Tablet_X10"` | Get device name |
| 5 | `cd 8d 08 01 00 00 00 ed` | `cd 8d 04 01` | Enable pen reporting |
| 6 | `cd 81 08 00 00 00 00 ed` | `cd 81 06 ...` | Get firmware info |
| 7 | `cd 82 08 42 fe 3d 00 ed` | — | Set parameter (time/config?) |
| 8 | `cd 93 08 00 00 00 00 ed` | — | Query (unknown) |
| 9-12 | repeated 0xc9, 0x8d | pen data starts | Keep-alive / re-enable reporting |

**Before handshake:** Device sends `cd 80 03` heartbeat packets (~15 of them).

#### Pen Data Format

```
cd 8d 0b SS XX XX YY YY PP PP CC    (11 bytes per sample)
│  │  │  │  │     │     │     │
│  │  │  │  │     │     │     checksum (sum & 0xFF)
│  │  │  │  │     │     pressure (LE u16, 0-16383)
│  │  │  │  │     Y coordinate (LE u16, 0-37400)
│  │  │  │  X coordinate (LE u16, 0-28200)
│  │  │  status (0x02 = pen active)
│  │  packet length
│  type (0x8d = pen data)
start marker
```

Device packs 2 samples per GATT notification for throughput (22 bytes).

**Measured ranges:**
- X: 3484 — 28200
- Y: 1961 — 36725
- Pressure: 0 — 16383 (14-bit, 0x3FFF max)
- 2483 pen samples captured in ~15 seconds

#### Device Info Decode (cmd 0x95 response)

```
28 6e 00 18 92 00 ff 1f
│     │  │     │  │
│     │  │     │  max_pressure = 0x1FFF = 8191
│     │  │     separator
│     │  max_Y = 0x9218 = 37400
│     separator
max_X = 0x6E28 = 28200
```

---

### Phase 5: Userspace Driver Development

Built `huion_ble_driver.py` — a single-file Python daemon using `dbus_fast` for BLE
and raw `fcntl`/`os.write` for uinput (no python-evdev available in nix env).

#### Architecture

Three concurrent async tasks:
1. **D-Bus signal handler** — receives FFE1 notifications, parses pen data, writes
   to uinput with microsecond latency (no queue)
2. **Keepalive loop** — sends `cd 8d 08 01 00 00 00 ed` every 2s
3. **Reconnect loop** — watches for disconnect signals, reconnects with backoff

Uses the modern `UI_DEV_SETUP` + `UI_ABS_SETUP` uinput ioctls (kernel 4.5+) instead
of the legacy 356-byte `uinput_user_dev` struct.

#### Connection challenges solved

**Problem: bleak can't find paired BLE devices**
- Solution: use `dbus_fast` directly to talk to BlueZ D-Bus, bypassing bleak's
  discovery layer entirely

**Problem: device disconnects in ~3s if no interaction**
- Solution: send handshake immediately after `StartNotify` (no heartbeat wait),
  with 50ms delays between commands, and immediate keepalive after

**Problem: `force_reconnect()` triggers cascading reconnect loop**
- Solution: `_suppress_disconnect` flag during reconnect/handshake prevents
  D-Bus disconnect signals from starting new reconnect cycles

**Problem: handshake burst mode causes out-of-order responses**
- Solution: 50ms delays between commands — device processes them in order,
  responses match Android capture exactly (0x95, 0x96 `03`, 0x96 `02`, 0x91, 0x8d)

#### The hid-generic problem (current blocker)

**Discovery:** BlueZ's HOGP (HID over GATT Profile) handler automatically claims
the Note X10 when it connects. It subscribes to the HID Report characteristic (2A4D)
and creates a kernel HID device via uhid:

```text
/sys/bus/hid/devices/0005:256C:8251.00AA
  DRIVER=hid-generic
  HID_NAME=Huion Note-X10
```

The kernel `hid-generic` driver claims this device as a keyboard (matching the
HID Report Map which only describes Usage 0x06 = Keyboard).

**Initial theory:** HOGP was suppressing FFE1 pen data. This was investigated
extensively (unbinding, StopNotify, etc.) but turned out NOT to be the root cause.
The real issue was discovered later: the device was in idle mode (green LED),
not in Note-Taking or Pen Tablet mode. See Phase 6.

**Root cause found:** `StartNotify` returns success but `Notifying` stays `false`.
BlueZ's HOGP handler blocks the CCCD write. The "responses" we see via D-Bus are
just Value property updates — NOT real GATT notifications. Pen data (continuous
high-frequency stream) requires actual GATT notifications to be enabled.

**Approaches tried:**
1. **StartNotify** — silently fails (`Notifying: false`), HOGP handler blocks CCCD
2. **Direct CCC write** to descriptor — `org.bluez.Error.NotPermitted`
3. **Unbinding hid-generic** — destabilizes connection (BlueZ loses state)
4. **StopNotify on HID Report** — no effect on pen data
5. **AcquireNotify** with `negotiate_unix_fd=True` — **WORKS!** Returns fd + MTU=141,
   receives data through fd (confirmed: command responses arrive on fd). BUT
   destabilizes connection (keepalive writes to FFE2 fail after AcquireNotify locks FFE1)
6. **Re-pairing** — fresh bond, no change

**Most promising next step:** `AcquireNotify` + `AcquireWrite` on FFE2.
Since AcquireNotify locks FFE1 (preventing D-Bus WriteValue on other chars?),
using AcquireWrite to get a write fd for FFE2 might solve the keepalive failure.
Both characteristics would then use raw fds instead of D-Bus GATT methods.

#### Driver audit and rewrite

After many debugging iterations, a full audit of the driver found 4 critical bugs:

1. **Keepalive task died permanently** — used `return` on failure, never restarted
2. **`finally` block overrode successful returns** — Python gotcha where `return`
   in `finally` overrides `return True` from `try`
3. **Reconnect never restarted keepalive** — dead task after first reconnect
4. **2-second gap before first keepalive** — sleep-first loop left device idle

Rewrote with session-based architecture: each connection session creates its own
disposable keepalive task, send-first sleep-second loop, no suppress flag.

#### The notification pipeline investigation

**Key diagnostic:** Used `busctl introspect` to check `Notifying` property after
`StartNotify` returned success — it was `false`!

```text
.Notifying   property  b  false    ← StartNotify silently failed!
.Value       property  ay  4 205 141 4 1   ← cached cd 8d 04 01 ack
```

The "command responses" we saw via D-Bus were just Value property updates from
BlueZ's internal GATT cache, NOT real GATT notifications. The CCCD was never
written, so the device never started sending unsolicited pen data.

**Android btsnoop confirmed:** Zero CCC descriptor writes to FFE1/FFE2 in the
capture — Android cached the CCC from a prior session. The device started sending
notifications because the CCC was already set from a previous bond.

**Approaches tried (in order):**

| # | Approach | Result |
| --- | --- | --- |
| 1 | `StartNotify` | Silently fails — `Notifying` stays false |
| 2 | Direct CCC write (`desc0028`) | `org.bluez.Error.NotPermitted` |
| 3 | Unbind hid-generic | Connection destabilizes |
| 4 | StopNotify on HID Report | No effect |
| 5 | `AcquireNotify` (`negotiate_unix_fd=True`) | **Works!** fd=0, MTU=141, data flows |
| 6 | Re-pair (remove + pair fresh) | Clean bond, no change to pen data |

**AcquireNotify breakthrough:**

```text
00:48:36 INFO AcquireNotify FFE1: fd=7 (idx=0, fds=[7]), MTU=141
00:48:36 INFO AcquireWrite FFE2: fd=8, MTU=141
00:48:48 DEBUG response 0x96: cd960403       ← data arrives on fd!
00:48:48 DEBUG response 0x91: cd9113...      ← device name on fd!
00:48:48 DEBUG response 0x8d: cd8d0401       ← enable ack on fd!
```

`AcquireNotify` with `negotiate_unix_fd=True` properly enables GATT notifications
and returns a raw file descriptor. Data arrives! Key fix: `dbus_fast` returns the
fd INDEX in `reply.body[0]`, actual fd is in `reply.unix_fds[index]`.

Connection stability solved by acquiring BOTH fds immediately after connect
(before HOGP handler activates ~1-2s later) and using AcquireWrite for FFE2
alongside AcquireNotify for FFE1.

---

### Phase 6: The Missing Piece — Device Mode Detection

With the BLE pipeline fully working (AcquireNotify fd=7, AcquireWrite fd=8,
handshake acked, connection stable), pen data STILL didn't arrive. Extensive
testing over many iterations confirmed: 0 pen data packets in any configuration.

#### Mode investigation

Internet research revealed the critical missing context about the device's modes.

**Hardware:** The Note X10 uses EMR (electromagnetic resonance) technology. The
digitizer is embedded under the surface. Any regular A5 paper works (thickness
below 10mm) — no special paper required. A replacement panel is included for
Pen Tablet Mode (smooth surface for drawing). The PW320 pen has swappable nibs:
ballpoint refills for writing on paper, plastic nibs for digital drawing.

**The device has two BLE operating modes, determined by which app connects:**

| Mode | Trigger | LED | Protocol | App |
| --- | --- | --- | --- | --- |
| Note-Taking | Huion Note phone app connects | Blue | `cd/ed` on FFE0/FFE1 (0x95, 0x96, 0x91, 0x8D) | Huion Note (iOS/Android) |
| Pen Tablet | Huion PC driver connects | White | Unknown (different cmd set or HID-over-GATT) | Huion Tablet Driver (Win/macOS v20) |
| Idle | Generic BLE connection (no recognized app) | **Green** | Responds to commands but sends no pen data | N/A |

The mode is **software-triggered**, not determined by the physical surface. The
device identifies which app is connecting and switches mode accordingly. The
surface (paper vs panel) is user convenience, not a hardware switch.

**User's LED was GREEN** throughout all testing — the device was in idle mode.
It connects, responds to all handshake commands, acks enable-reporting, but never
enters a data-sending mode because it doesn't recognize our driver as either
the Huion Note phone app or the Huion PC driver.

Huion's support page explicitly states: **"Make sure that you have installed the
correct driver on your PC/laptop before you try to connect via Bluetooth, or it
will cause Bluetooth connection failure."**

#### What the Android capture actually was

The btsnoop capture was from the **Huion Note App** in **Note-Taking Mode**.
The `cd/ed` protocol with commands 0x95/0x96/0x91/0x8D is the Note-Taking
protocol. We decoded it correctly and our driver implements it faithfully.

The device DID enter Note-Taking Mode on Android (blue LED) because it recognized
the Huion Note App. On Linux, the same commands get acknowledged but the device
stays in idle mode (green LED) because it doesn't recognize us as the app.

**Key question:** What does the Huion Note App send that we don't? Possibly:
- A specific BLE advertising data or scan response check
- An authentication/token exchange on the second vendor service (00010203...1912)
- A specific ATT operation ordering or timing pattern
- Something in the initial GATT service discovery that identifies the host

#### Pen Tablet Mode command IDs (unconfirmed)

The Linux driver binary strings revealed different command IDs:

```text
cmd 200 = 0xC8  (vs Note-Taking 0x95)
cmd 202 = 0xCA  (vs Note-Taking 0x96)
cmd 209 = 0xD1  (vs Note-Taking 0x91)
cmd 212 = 0xD4  (vs Note-Taking 0x8D)
```

Tested with `cd/ed` framing over BLE: **no response, connection dropped**. These
may be USB-only command IDs, or may need different framing for BLE Pen Tablet Mode.

---

### Summary of What Works

| Component | Status | Details |
| --- | --- | --- |
| BLE connect + service resolution | Working | BlueZ D-Bus, dbus_fast |
| AcquireNotify (FFE1 notification fd) | Working | fd=7, MTU=141, `negotiate_unix_fd=True` |
| AcquireWrite (FFE2 write fd) | Working | fd=8, MTU=141 |
| Note-Taking handshake | Working | All 5 responses match Android capture exactly |
| Connection stability | Working | 0 reconnects, session-based keepalive architecture |
| uinput virtual tablet device | Working | ABS_X/Y/PRESSURE, BTN_TOUCH/TOOL_PEN |
| Protocol decode (Note-Taking) | Complete | Command format, pen data format, checksum |
| Pen data reception | **Not working** | Device never sends pen data (wrong mode) |

### What's Next

#### Path A: Identify what triggers Note-Taking Mode

The device enters Note-Taking Mode (blue LED, pen data flows) when the Huion
Note App connects, but stays idle (green LED) when our driver connects. The
mode switch is software-triggered. To find what's different:

1. **Fresh Android btsnoop** — clear Huion Note app data, capture from the very
   first connection (including service discovery, descriptor reads, all ATT ops).
   Compare every single ATT operation against what our driver does.
2. **Check the second vendor service** — UUID `00010203-0405-0607-0809-0a0b0c0d1912`
   with characteristic `2B12`. We never wrote to this. It might be an
   authentication/mode-switch channel.
3. **BLE advertising identity** — the device might check the central's identity
   (device name, appearance, or manufacturer data) to decide which mode to enter.

#### Path B: Pen Tablet Mode RE (full solution)

1. **Ghidra RE of driver binaries** — three binaries available:
   - **macOS** `libTabletSession.dylib` (v15.7.24, already extracted, Objective-C
     names preserved — easiest to RE)
   - **Windows** `extracted_win/payload_C/` (extracted — see 2026-04-10 session)
   - **Linux** `libTabletSession.so` (v15.0.0.175, already extracted, BLE disabled
     but protocol code present)
   All three are v15.x — same generation, should share the same BLE protocol code.
   The v20 series (recommended by Huion for BLE tablet mode) is NOT available.

2. **Windows VM capture** — install Huion driver v15.7 in a Windows VM with USB
   BT adapter passthrough, capture the BLE Pen Tablet Mode traffic.

#### Path C: HID-over-GATT investigation

The device's HID service (0x1812) has a Report Map that only describes a keyboard.
But in Pen Tablet Mode, the device might expose a different Report Map or send
vendor-specific HID reports. Investigate:

1. Read hidraw device while connected (check if any pen data flows via HID)
2. Check if the Report Map changes after sending specific commands
3. Try writing to the HID Control Point (2A4C) to change report mode

---

## 2026-04-10: Windows Driver Extraction

### Goal

Extract `HuionTablet_WinDriver_v15.7.6.1858.exe` for RE — specifically to find the
Pen Tablet Mode BLE command set that the Windows driver sends (Path B).

### Extraction Method

The EXE is a custom installer (not NSIS/Inno Setup/WiX Burn). Standard tools
(`7z`, `innoextract`) failed — `7z` read only the PE header and extracted a lone
`CERTIFICATE` file.

**Root cause:** The payload is stored as **custom PE resource types** (IDs 3208,
3216, 3238) rather than standard `RT_RCDATA`. Naive tools miss these entirely.

**Method:** Parsed the PE section table manually, located the `.rsrc` section
(45.1MB — virtually the entire file), then walked the resource directory tree to
find all data blobs and their raw file offsets.

### Payloads Found

| Resource | Type ID | Entries | Size | Extracted to | Content |
|----------|---------|---------|------|--------------|---------|
| — | 3208/176 | 119 | 10.5MB | *(discarded)* | 32-bit Windows CRT DLLs |
| — | 3208/180 | 119 | 12.5MB | *(discarded)* | 64-bit Windows CRT DLLs |
| `driver_main` | 3208/182 | 748 | 21.8MB | `extracted_win/driver_main/` | **Main driver payload** (see below) |
| `installer_ui` | 3216/178 | 9   | 0.1MB | `extracted_win/installer_ui/` | Installer UI images (PNG) |
| `config_json_small.json` | 3238/177 | — | 0.2KB | `extracted_win/` | Installer metadata |
| `config_json_large.json` | 3238/178 | — | 154KB | `extracted_win/` | Full install config (multiple JSON docs) |

### Key Binaries in payload_C

```text
extracted_win/driver_main/
  AddPSUserConfig.exe          — per-user config helper
  DeletePSUserConfig.exe
  Uninstall.exe
  ReleaseWintab32.exe / X64
  RemoveDongle.exe / X64

  driver/HuionHID/
    amd64/
      vmulti.sys               ← virtual HID multitouch kernel driver  [RE target]
      hidkmdf.sys              ← HID KMDF filter driver                [RE target]
      vmulti.inf
      devcon.exe, DIFxAPI.dll, DIFxCmd.exe
    i386/                      — same, 32-bit

  driver/TabletDriver/
    amd64/
      TabletDriver.inf         ← USB INF: VID/PID, device class       [read first]
      winusb.dll               ← WinUSB transport (USB comms)
      winusbcoinstaller2.dll
      dpinst.exe, devcon.exe, DIFxAPI.dll
    i386/                      — same, 32-bit

  x64/wintab32.dll             ← Wintab API compatibility shim (x64 host)
  x86/wintab32.dll             ← Wintab API compatibility shim (x86 host)
```

### Architecture Notes

The driver splits responsibility across two sub-drivers:

- **HuionHID** — handles the HID protocol layer. `vmulti.sys` presents a virtual
  HID multitouch device to Windows. `hidkmdf.sys` is a KMDF filter between the
  HID class driver and vmulti. This is likely where pen report descriptors live.

- **TabletDriver** — handles USB communication via WinUSB. This is the layer
  that sends BLE commands when in Pen Tablet Mode. `TabletDriver.inf` will
  contain the exact USB VID/PID (0x256C:0x006E for USB, possibly different for BLE).

- **wintab32.dll** — userspace shim that exposes the Wintab API to legacy
  applications (e.g., Photoshop). Reads from the virtual HID device.

The BLE Pen Tablet Mode commands are most likely in the **userspace service
executable** (not extracted yet — likely in `x64/` or the `res/` tree; the main
UI process is in `extracted_win/payload_C/res/` or a separate ZIP).

### RE Priority Order

1. `driver/TabletDriver/amd64/TabletDriver.inf` — read first, gives exact VID/PID
   and device class GUID, costs nothing
2. `driver/HuionHID/amd64/vmulti.sys` — HID report descriptor, pen data layout
3. `driver/HuionHID/amd64/hidkmdf.sys` — filter logic
4. Main UI service EXE (if present in payload_C/res/) — BLE command logic

---

### Files Created This Session

```text
huion_ble_driver.py          — Userspace BLE tablet driver (dbus_fast + uinput)

scripts/
  explore_gatt.py            — GATT service enumeration (dbus_fast)
  listen_notifications.py    — Notification listener (dbus_fast)
  ble_probe.py               — BLE probe with command testing
  probe_commands.py          — Command probe (bleak, superseded)
  decode_gatt_dump.py        — Decoder for busctl GATT dump
  decode_btsnoop.py          — btsnoop HCI log parser
  raw_ble_test.py            — Minimal AcquireNotify test
  tablet_mode_test.py        — Pen Tablet Mode command test

captures/
  btsnoop_hci.log            — Android BLE capture (108KB, Note-Taking Mode)
  bugreport.zip              — Android bugreport
  protocol_decode.txt        — Decoded Note-Taking Mode protocol
  probe_*.log/json           — Linux probe sessions
  tablet_mode_*.log/json     — Tablet mode command test results

drivers/extracted_win/
  driver_main/               — Main driver payload (748 files): vmulti.sys,
                               hidkmdf.sys, TabletDriver.inf, wintab32.dll, …
  installer_ui/              — Installer UI PNGs
  config_json_small.json     — Installer metadata
  config_json_large.json     — Full install config (multiple JSON docs)
```

---

## 2026-04-10: Ghidra Headless RE — macOS Driver v15.7.24

### Goal

Reverse-engineer the macOS driver binaries using Ghidra headless analyzer to
discover the Pen Tablet Mode BLE protocol, understand the mode-switch mechanism,
and find the missing piece that prevents our Linux driver from receiving pen data.

### Binaries Analyzed

| Binary | Size | Location | Role |
|--------|------|----------|------|
| `libTabletSession.dylib` | 274K | `HuionTablet.app/Contents/Resources/` | BLE + USB transport layer |
| `HuionTabletCore` | 574K | `HuionTablet.app/Contents/Resources/` | Core tablet logic (commands, pen data) |
| `HuionTablet` | 5.9MB | `HuionTablet.app/Contents/MacOS/` | Main app (Swift/Obj-C UI) |

All are fat Mach-O (x86_64 + ARM64). Analyzed the x86_64 slices with Ghidra 12.0.4.

Source paths preserved in debug info: `/Users/wyt_m1/Documents/GitHub/driver_v15/`

### Architecture (from RE)

```text
HuionTablet (main app, Swift/Obj-C UI)
  ↓ CFNotification IPC (cn.huion.tablet.notify.*)
HuionTabletCore (background daemon)
  ├── HnTabletThread — main processing thread
  │   ├── dispatch() → HnDoerPen (pen data, report_type < 0x91)
  │   ├── onCmd()    → command response handler (switch on cmd ID)
  │   └── loopSync() / loopAsync()
  ├── HnSessionTablet — command interface (reqCmd, connect_flag, getProtocolFlag)
  └── libTabletSession.dylib (transport abstraction)
      ├── HnTabletSession — tries USB first, falls back to BLE
      ├── HnUsbHid — USB HID transport (IOKit)
      └── HnBluetooth → HnBle — BLE transport (CoreBluetooth)
          ├── CBCentralManager + CBPeripheral delegates
          ├── FFE0 service → FFE1 (pen reports) + FFE2 (commands)
          └── callbacks → onOpened / onClosed / onRead / onCmd
```

### CRITICAL FINDING 1: Pen Tablet Mode Command Format

The v15 macOS driver uses a **different command framing** than Note-Taking Mode:

```text
Pen Tablet Mode (v15 driver):   cd CMD 00 00 00 00 00 00  (no length, no end marker)
Note-Taking Mode (Huion Note):  cd CMD 08 P0 P1 P2 P3 ed  (length=8, end marker 0xED)
```

Found in `HnBle::getStringDesc:` (the BLE command sender):
```c
local_18 = 0xcd;                  // byte[0] = start marker
local_17 = (unsigned char)cmd_id; // byte[1] = command ID
local_16 = 0;                     // bytes[2-7] = zeros (no params, no length, no end marker)
// ... sends 8-byte packet via writeValue:forCharacteristic:type:
```

**Our driver was sending `cd C8 08 00 00 00 00 ed` — wrong framing!** The device likely
rejected the malformed packets or stayed in idle mode because it didn't recognize the
command format. The correct format is `cd C8 00 00 00 00 00 00`.

### CRITICAL FINDING 2: Pen Data Format (Pen Tablet Mode)

Pen Tablet Mode uses a completely different pen data format than Note-Taking Mode:

```text
Pen Tablet Mode (14 bytes, USB HID report format):
08  TYPE  X0 X1 X2  Y0 Y1 Y2  P0 P1  AX AY  PenIdx
│   │     │         │         │       │  │    │
│   │     │         │         │       │  │    pen index byte
│   │     │         │         │       tilt X/Y (signed int8)
│   │     │         │         pressure (16-bit LE)
│   │     │         Y coordinate (24-bit: Y0 | Y1<<8 | Y2<<16)
│   │     X coordinate (24-bit: X0 | X1<<8 | X2<<16)
│   report type (< 0x91 = pen, 0xa0 = special, 0xe0+ = buttons)
report ID = 0x08 (injected by BLE layer)

Note-Taking Mode (11 bytes, cd/ed framing):
cd 8d 0b SS XX XX YY YY PP PP CC
                  ^16bit  ^16bit
```

Key differences:
- **24-bit vs 16-bit coordinates** (larger address space in tablet mode)
- **Tilt axes** (AX/AY) only in tablet mode
- **Pen index** for multi-pen support
- **Report type** byte determines data routing (pen, buttons, dial, touch)
- **No cd/ed framing** — uses USB HID report format with report ID 0x08

The BLE layer in `didUpdateValueForCharacteristic:` converts FFE1 notifications:
- Overwrites byte[1] with 0x08 (report ID)
- Passes from byte[1] onwards to the `onRead` callback
- This makes BLE data look identical to USB HID reports

Validation: `_is_invalid_report(data)` checks `data[0] == 8` (report ID).

### CRITICAL FINDING 3: Device Authentication via verify_str_202

Command 0xCA (202) response triggers `verify_str_202()`:
1. Convert first 0x22 words from UTF-16 to UTF-8
2. Compute checksum of first 0x44 bytes → must equal **0xBEF**
3. Strcmp result against **"HUION Animation Technology Co.,ltd"**
4. Both must pass for the device to be considered legitimate

This may be required before the device enters active pen data mode.

### CRITICAL FINDING 4: BLE Data Routing

```text
FFE1 (notify)     → pen data reports    → onRead callback → dispatch → HnDoerPen
FFE2 (indicate)   → command responses   → onCmd callback  → switch(cmd_id)
FFE2 (write)      ← command packets     ← reqCmd / getStringDesc
```

Both FFE1 and FFE2 have `setNotifyValue:YES` called in `didDiscoverCharacteristicsForService:`.
Commands written to FFE2, responses come back on FFE2 (indicate), pen data on FFE1 (notify).

### Protocol Flag

`HnSessionTablet::getProtocolFlag()` returns `this[0x0c]`:
- **0** = USB connection
- **2** = BLE connection

Set during `HnTabletSession::open()`. Used for BLE-specific response parsing
(e.g., battery command 0xD1 returns 2 bytes on BLE vs 1 on USB).

### Complete Pen Tablet Mode Command Map (from onCmd switch + opt* functions)

| Dec | Hex  | Function | Response Parser |
|-----|------|----------|-----------------|
| 200 | 0xC8 | Get device info (maxX/Y/P, LPI, rate, btn counts) | `parse_cmd_resp_200` |
| 201 | 0xC9 | Get device name (UTF-8 or UTF-16) | `parse_cmd_resp_201[_utf8]` |
| 202 | 0xCA | Get manufacturer string + verify | `verify_str_202` (checksum + strcmp) |
| 206 | 0xCE | LED off | — |
| 207 | 0xCF | LED on | — |
| 208 | 0xD0 | LED query | `parse_cmd_resp_byte` → UI notify 0x8020 |
| 209 | 0xD1 | Battery query | `parse_cmd_resp_byte` (USB) / `battery_level_switch` (BLE) |
| 211 | 0xD3 | Dial on | — |
| 212 | 0xD4 | Dial off | — |
| 214 | 0xD6 | Touch screen enable | — |
| 215 | 0xD7 | LED light increment | — |
| 216 | 0xD8 | LED light decrement | — |
| 217 | 0xD9 | LED light query | `parse_cmd_resp_byte` → UI notify 0x8060 |
| 220 | 0xDC | Unknown | `parse_cmd_resp_byte` → UI notify 0x8070 |
| 221 | 0xDD | THkey query | `parse_cmd_resp_byte` |
| 222 | 0xDE | THkey on/off | `parse_cmd_resp_byte` → UI notify 0x8090 |
| 223 | 0xDF | Sleep state | `parse_cmd_resp_byte` → UI notify 0x8080 |
| 224 | 0xE0 | Long work warn | `parse_cmd_resp_byte` → UI notify 0x80a0 |
| 226 | 0xE2 | Work time query | `parse_cmd_resp_byte` → UI notify 0x80b0 |
| 228 | 0xE4 | Touch state | `parse_cmd_resp_byte` → UI notify 0x80c0 |
| 229 | 0xE5 | Touch screen query | `parse_cmd_resp_byte` → UI notify 0x8050 |

### BLE Handshake Sequence (Pen Tablet Mode, from initTabletInfo)

| # | Action | Command Bytes | Purpose |
|---|--------|---------------|---------|
| 1 | Send cmd 0xC9 | `cd c9 00 00 00 00 00 00` | Get device name |
| 2 | Parse 0xC9 response | UTF-8 model string → sModel | Device identification |
| 3 | Send cmd 0xC8 | `cd c8 00 00 00 00 00 00` | Get device info |
| 4 | Parse 0xC8 response | maxX/Y/P, LPI, rate, buttons | Hardware capabilities |
| 5 | Send cmd 0xCA | `cd ca 00 00 00 00 00 00` | Get manufacturer string |
| 6 | Verify 0xCA response | checksum=0xBEF + "HUION Animation..." | Device authentication |
| 7 | → initDriverInfo | Read tablet.cfg for sModel | Apply device config |
| 8 | → connect_flag(1) | Internal | Mark as connected |
| 9 | → pen data starts | FFE1 notifications | Report type < 0x91 |

BLE uses async commands: `reqCmdAsync(0xC9)` → response arrives via `onCmd` callback →
triggers next command. USB uses synchronous `reqCmd` → blocks until response.

### parse_cmd_resp_200 (Device Info) Format

```text
Offset  Size  Field
0       3B    max_X (24-bit LE: byte0 | byte1<<8 | byte2<<16)
3       3B    max_Y (24-bit LE)
6       2B    max_P (16-bit LE)
8       2B    LPI (16-bit LE)
10      1B    pen_btn_num
11      1B    hkey_num (hardware keys)
12      1B    skey_num (soft keys)
13      1B    rate_raw (actual_rate = rate_raw << 2)
14      1B    is_monitor flag
15      1B    is_passive flag
```

Note: 24-bit coordinates match the pen data report format (also 24-bit).
Note-Taking Mode used 16-bit max values in the 0x95 response.

### Connection Flow (no explicit mode switch!)

The v15 driver does **NOT** send an explicit "enter Pen Tablet Mode" command.
It simply:
1. Connects to the device via CoreBluetooth
2. Discovers FFE0/FFE1/FFE2
3. Enables notifications on both characteristics
4. Sends the Pen Tablet Mode commands (0xC9/0xC8/0xCA)

The device likely determines the mode from the **command format**:
- `cd CMD 08 ... ed` → device recognizes Note-Taking protocol → blue LED
- `cd CMD 00 00 00 00 00 00` → device recognizes Pen Tablet protocol → white LED
- Unrecognized format or no commands → idle mode → green LED

### What Was Wrong With Our Previous Attempts

1. **Wrong framing:** We sent `cd C8 08 00 00 00 00 ed` (Note-Taking framing with
   Pen Tablet command IDs). The device got confused and dropped the connection.

2. **Wrong command IDs with right framing:** We sent `cd 95 08 00 00 00 00 ed`
   (Note-Taking commands). The device acked them (it recognizes both command sets)
   but stayed in idle mode because our host wasn't identified as the Huion Note App.

3. **Never tried Pen Tablet framing:** The correct approach — `cd C8 00 00 00 00 00 00`
   — was never attempted because we didn't know the framing was different until this
   Ghidra RE session.

### What To Try Next

**Immediate test:** Modify `huion_ble_driver.py` to use Pen Tablet Mode protocol:

1. Change command format to `cd CMD 00 00 00 00 00 00` (no length, no end marker)
2. Change handshake to: cmd 0xC9 → cmd 0xC8 → cmd 0xCA
3. Watch for FFE2 indications (command responses) separately from FFE1 (pen data)
4. Parse pen data as 14-byte USB HID format (24-bit coords, tilt, pen index)
5. Expect report ID 0x08 at byte[0] of pen reports (or inject it like the macOS driver)

If this works, the device should switch to white LED (Pen Tablet Mode) and start
streaming pen data on FFE1.

**Fallback:** If v15 commands don't trigger the mode (Note X10 might need v20),
try a hybrid approach: v15 framing (`cd CMD 00...`) with Note-Taking command IDs
(0x95/0x96/0x91/0x8D).

### Ghidra Output Files

```text
ghidra_out/
  libTabletSession_dylib_all_functions.c   — 179K, all decompiled functions
  libTabletSession_dylib_ble_functions.c   — 158K, BLE-related functions
  libTabletSession_dylib_all_symbols.txt   — 202K, complete symbol table
  libTabletSession_dylib_ble_symbols.txt   —  86K, BLE-related symbols

  HuionTabletCore_all_functions.c          — 488K, all decompiled functions
  HuionTabletCore_ble_functions.c          — 104K, BLE-related functions
  HuionTabletCore_all_symbols.txt          — 565K, complete symbol table
  HuionTabletCore_ble_symbols.txt          —  61K, BLE-related symbols

  HuionTablet_all_functions.c              — 146K, all decompiled functions
  HuionTablet_ble_functions.c              — 2.3M, BLE-related functions (mostly Swift UI)
  HuionTablet_all_symbols.txt              — 4.5M, complete symbol table

scripts/ghidra/
  ExtractBLE.java      — Ghidra Java script: symbol dump + decompile all functions
```

---

## 2026-04-10: Pen Tablet Mode Breakthrough — Pen Data Flows!

### Goal

Implement the Pen Tablet Mode protocol discovered in the Ghidra RE session into
the Linux BLE driver, and test whether the device sends pen data.

### What Changed in the Driver

Modified `huion_ble_driver.py` with dual-mode support (Pen Tablet + Note-Taking):

**1. Command framing** — two distinct formats:
```
Pen Tablet:  cd CMD 00 00 00 00 00 00   (no length, no end marker)
Note-Taking: cd CMD 08 P0 P1 P2 P3 ed   (length=8, end marker 0xED)
```

**2. Handshake sequence** — tablet mode sends:
```
cd c9 00 00 00 00 00 00   → get device name
cd c8 00 00 00 00 00 00   → get device info
cd ca 00 00 00 00 00 00   → verify manufacturer
```

**3. Auto-fallback** — `--mode auto` tries tablet mode first (2s timeout), then
falls back to note-taking mode. `--mode tablet` forces tablet-only.

**4. UInput** — added `ABS_TILT_X` and `ABS_TILT_Y` axes (range -127 to +127).

**5. Connection race fix** — `force_reconnect()` now returns the instant
`Connected=true` (no waiting for `ServicesResolved`), and `_wait_services()`
accepts the HOGP-cached GATT characteristics. This wins the HOGP race.

**6. Keepalive bug fixed** — `_keepalive_loop()` was defined but never spawned
as a task in `_session_loop()`. Now both reader and keepalive tasks are created.

### The Connection Race Problem

The biggest engineering challenge was NOT the protocol but the BLE connection
lifecycle. The HOGP (HID-over-GATT Profile) handler in BlueZ's kernel module
claims vendor characteristics ~1-2s after connect, blocking `AcquireNotify`.

**Attempts that failed:**
1. Skip disconnect if already connected → stale GATT state, `AcquireNotify` fails
2. Wait for `ServicesResolved` → HOGP already activated by the time it resolves
3. Remove + re-pair → fresh GATT state, but still need to race HOGP

**What works:**
1. Full disconnect (wait for `Connected=false`)
2. Reconnect (D-Bus `Connect` call)
3. Don't wait for `ServicesResolved` — just check `Connected=true`
4. IMMEDIATELY call `AcquireNotify` + `AcquireWrite` (within ~1s window)
5. HOGP activates after we already have the fds

The window is tight. Adding `_wait_services()` polling between connect and
acquire loses the race. The fix: `force_reconnect()` returns as soon as
`Connected=true` (polling at 100ms intervals), then the caller calls
`acquire_fds()` immediately.

### PEN DATA FORMAT DISCOVERY

The device started streaming pen data **immediately** after `AcquireNotify`
succeeded — even before the handshake commands were sent! The tablet handshake
commands (`cd c9/c8/ca ...`) got no responses (the device doesn't need them to
start pen data in Pen Tablet Mode).

**Raw BLE notification format (14 bytes):**
```
55 54 SS XX XX YY YY PP PP XH YH TX TY CK

[0-1]  0x55 0x54  — BLE pen data magic header
[2]    STATUS     — 0x80 = hovering (pen near), 0x81 = touching (pen contact)
[3-4]  X_lo       — X coordinate, 16-bit LE
[5-6]  Y_lo       — Y coordinate, 16-bit LE
[7-8]  PRESSURE   — 16-bit LE (0 when hovering)
[9]    X_hi       — X high byte (24-bit extension, 0x00 for this device)
[10]   Y_hi       — Y high byte (24-bit extension, 0x00 for this device)
[11]   TILT_X     — signed int8 (pen tilt angle)
[12]   TILT_Y     — signed int8 (pen tilt angle)
[13]   CHECKSUM   — packet checksum
```

**Full coordinate extraction:**
```
X = byte[3] | (byte[4] << 8) | (byte[9] << 16)
Y = byte[5] | (byte[6] << 8) | (byte[10] << 16)
P = byte[7] | (byte[8] << 8)
```

**Connection to macOS Ghidra RE:** The macOS BLE layer (`HnBle::didUpdateValue`)
receives this same format, skips byte[0] (`0x55`), overwrites byte[1] (`0x54`)
with `0x08` (USB HID report ID), and passes `&byte[1]` to the upper layer.
The upper layer then sees `08 81 XX XX YY YY PP PP XH YH TX TY` — which
matches the `_report_x`/`_report_y` split-byte layout found in Ghidra:
```
_report_x(buf) = buf[2] | buf[3]<<8 | buf[8]<<16
_report_y(buf) = buf[4] | buf[5]<<8 | buf[9]<<16
```

**Measured values from first live capture:**
- X range: ~14400 — 24000+ (pen moving across tablet)
- Y range: ~9900 — 14000+ (pen moving across tablet)
- Pressure range: 0 — 4426+ (0 when hovering, >0 when touching)
- Tilt X: -12 to +17 (varies with pen angle)
- Tilt Y: -12 to +6 (varies with pen angle)
- Status: 0x80 (hovering), 0x81 (touching surface)
- Data rate: **56 samples/second** (278 samples in ~5s)

### Key Insight: No Handshake Needed!

The device enters Pen Tablet Mode and starts streaming pen data **just from
the BLE connection + AcquireNotify on FFE1**. No handshake commands are
required. The tablet mode commands (`cd c9/c8/ca ...`) got no responses.

This means the "mode switch" is not triggered by specific commands at all —
it's triggered by the BLE notification subscription on FFE1. When a host
subscribes to FFE1 notifications (via AcquireNotify), the device starts
sending pen data in Pen Tablet Mode format.

The Note-Taking Mode (Huion Note app) might use a different mechanism:
the Android app likely writes a specific CCCD value or uses a different
subscription method that the device recognizes as "Note-Taking Mode."

### HOGP — The Real Stability Problem

After the initial 20s stable test, subsequent runs showed disconnects every
3-4 seconds. Investigation revealed BlueZ's **hid-generic** kernel driver
(HOGP — HID-over-GATT Profile) was the root cause.

**The HOGP kill mechanism:**
1. We connect and acquire FFE1 notify fd (within ~1s)
2. HOGP handler activates ~3s after connect
3. HOGP claims the HID service (0x1812) and subscribes to HID Report (2A4D)
4. This disrupts FFE1 GATT notifications — data stops flowing
5. The notify fd stays open but goes silent (no EOF, just no data)

**Investigation steps:**
1. Suppressed D-Bus `Connected=false` signals while notify fd alive — stopped
   false reconnect triggers, but data still died after ~3s
2. Added 4-second data stall timeout — detected the silent death, reconnected
   automatically, but created a 3s-data/4s-stall cycle
3. Identified that hid-generic unbind was needed AFTER each connect

**The fix:** Call `_unbind_hid_generic()` right after `acquire_fds()` succeeds,
before HOGP can activate. This unbinds the `0005:256C:8251.*` device from
hid-generic on every reconnect. The driver already runs as root (for uinput),
so the sysfs write succeeds.

### Code Audit Fixes

Full audit of the driver found and fixed:

1. **Dead code removed** — `_wait_services()`, `connect()`, `_parse_tablet_responses()`
   were never called after refactoring. Also removed Note-Taking Mode entirely
   (only relevant on Android).

2. **Write fd leak** — `self.ble._write_fd` was set to `None` on reconnect
   without `os.close()`. Added `_close_fds()` method called at reconnect start.

3. **D-Bus disconnect suppression** — `_notify_fd_alive` flag prevents the
   D-Bus `Connected=false` signal from triggering reconnect while the notify
   fd is still streaming data. Only the fd reader (stall timeout or EOF)
   triggers reconnect.

4. **Shutdown crash** — `pen_up()` called on already-closed uinput fd. Added
   `fd >= 0` guard.

### Stability Test Result (Final)

30-second test run with pen actively drawing:

```
03:59:58 INFO samples=1003 (201/s), reconnects=0
04:00:03 INFO samples=987 (197/s), reconnects=0
04:00:08 INFO samples=984 (197/s), reconnects=0
04:00:13 INFO samples=877 (175/s), reconnects=0
04:00:18 INFO samples=601 (120/s), reconnects=0
```

- **201 samples/sec** peak, **175-197 samples/sec** sustained
- **0 reconnects** across the full 30s session
- Pen cursor moves on screen via uinput — full stack working
- Tilt axes reporting correct values
- hid-generic successfully unbound on every connect

### Remaining Issues

**1. Command responses** — the tablet handshake commands (`cd c9/c8/ca ...`)
got no responses. Device info (max_x/y/pressure) uses hardcoded defaults.

**2. Button support** — the PW320 pen has two side buttons. The status byte
may encode button states in higher bits. Not yet tested.

**3. Coordinate calibration** — X/Y ranges span the full digitizer area.
Mapping to screen coordinates is left to libinput / the compositor.

### What Works Now

| Component | Status | Details |
| --- | --- | --- |
| BLE connect + HOGP race | Working | disconnect → connect → immediate acquire → unbind |
| AcquireNotify (FFE1) | Working | fd=7, MTU=141, pen data flows |
| AcquireWrite (FFE2) | Acquired | fd=8, keepalive writes maintain link |
| hid-generic unbind | Working | Auto-unbind after each connect, prevents HOGP kill |
| Pen data reception | **WORKING** | 175-201 samples/sec, 14-byte packets |
| Pen data parsing | Working | `55 54` format: status, 24-bit X/Y, pressure, tilt |
| uinput virtual tablet | **WORKING** | ABS_X/Y/PRESSURE/TILT_X/TILT_Y, cursor moves |
| Connection stability | **STABLE** | 0 reconnects over 30s, stall timeout as safety net |
| Data stall detection | Working | 4s timeout triggers reconnect if HOGP wins the race |
