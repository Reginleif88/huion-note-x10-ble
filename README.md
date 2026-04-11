# Huion Note X10 - BLE Linux Driver

[![Platform](https://img.shields.io/badge/Platform-Linux-blue?logo=linux&logoColor=white)](https://github.com/Reginleif88/huion-note-x10-ble)
[![License](https://img.shields.io/badge/License-MIT-blue)](./LICENSE)
[![BLE](https://img.shields.io/badge/BLE-5.0_GATT-0082FC?logo=bluetooth&logoColor=white)](https://www.bluez.org/)
[![Status](https://img.shields.io/badge/Status-Working-green)](./notes/journey.md)

Userspace BLE driver for the Huion Note X10 pen tablet on Linux. Reverse-engineered from Huion's macOS/Windows v15 drivers via Ghidra and Android BLE captures.

> **Requires a patched BlueZ.** The Huion Note X10 firmware has a bug where it sends duplicate ATT protocol requests after every BLE connection parameter update (~every 5-8s). Unpatched BlueZ 5.x treats this as a protocol violation and disconnects, making the tablet unusable. A [2-line patch](#bluez-patch-required) to BlueZ's `src/shared/att.c` fixes this. See [installation](#bluez-patch-required) for details.

> Codebase maintained with [Claude Code](https://claude.ai/code).
>
> **This is not an official Huion product.** The BLE protocol was reverse-engineered because Huion's official Linux driver deliberately disables BLE support.
>
> Only tested on NixOS so far. Should work on any Linux with BlueZ 5.x (patched) — reports from other distros welcome.

## Installation

### 1. Install dependency

```bash
pip install dbus-fast
# or: nix-shell -p python3Packages.dbus-fast
```

### 2. Pair the tablet

Power on the tablet and pair it via your desktop's Bluetooth settings (or `bluetoothctl`). Trust the device so it reconnects automatically.

### 3. Install udev rules

These grant your user access to `/dev/uinput` and auto-unbind `hid-generic` when the tablet connects (preventing HOGP interference).

```bash
sudo cp 99-huion-note-x10.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG input $USER
```

Log out and back in for the group change to take effect.

### 4. Install the driver

```bash
mkdir -p ~/.local/share/huion-note-x10
cp huion_ble_driver.py ~/.local/share/huion-note-x10/

cp huion-note-x10.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now huion-note-x10
```

The driver auto-detects the tablet by its Bluetooth vendor/product ID. If you have multiple Huion devices, set the MAC explicitly by editing the service file:

```bash
systemctl --user edit huion-note-x10
```

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 %h/.local/share/huion-note-x10/huion_ble_driver.py --mac XX:XX:XX:XX:XX:XX
```

### 5. Verify

```bash
# Check service status
systemctl --user status huion-note-x10

# Check the virtual input device
libinput list-devices | grep -A5 "Huion Note X10"

# Watch raw pen events
sudo evtest    # pick "Huion Note X10 BLE"

# Confirm: X 0-28200, Y 0-37400, pressure 0-8191
```

If the pen tracks correctly in evtest, it will work in any application
(Krita, GIMP, xournalpp, etc.) via the standard Linux input stack.

### Quick test (without installing)

```bash
python3 huion_ble_driver.py
```

## Configuring the Tablet

This driver creates a standard Linux input device. Pressure curves, screen mapping,
and active area configuration are handled by existing Linux tools:

- **[OpenTabletDriver](https://opentabletdriver.net/)** — GUI for pressure curves, area mapping, smoothing, and keybinding
- **libinput / xinput** — command-line screen mapping:

  ```bash
  # Map tablet to a specific monitor (X11)
  xinput map-to-output "Huion Note X10 BLE" HDMI-1

  # Coordinate transformation matrix (Wayland/X11)
  xinput set-prop "Huion Note X10 BLE" "Coordinate Transformation Matrix" 1 0 0 0 1 0 0 0 1
  ```

- **Krita / GIMP / MyPaint** — built-in pressure curve editors under tablet/input settings

## BlueZ Patch (Required)

The Huion Note X10 firmware sends duplicate `Exchange MTU Request` packets after every BLE connection parameter update. Unpatched BlueZ treats this as a protocol violation and disconnects, killing the pen data stream every ~4 seconds.

A 2-line patch to `src/shared/att.c` drops the duplicate instead of disconnecting:

```diff
--- a/src/shared/att.c
+++ b/src/shared/att.c
@@ -1082,9 +1082,8 @@
 		if (chan->in_req) {
 			DBG(att, "(chan %p) Received request while "
-					"another is pending: 0x%02x",
+					"another is pending: 0x%02x "
+					"(dropping duplicate)",
 					chan, opcode);
-			io_shutdown(chan->io);
-			bt_att_unref(chan->att);
-			return false;
+			return true;
 		}
```

**NixOS:** Applied automatically via `hardware.bluetooth.package` overlay — see `modules/huion-ble.nix`.

<details>
<summary><strong>Debian / Ubuntu</strong></summary>

```bash
# Install build dependencies
sudo apt build-dep bluez
sudo apt install devscripts

# Get the source
apt source bluez
cd bluez-*/

# Apply the patch
cp /path/to/patches/fix-duplicate-mtu-request.patch .
patch -p1 < fix-duplicate-mtu-request.patch

# Build the patched package
debuild -us -uc -b

# Install it
cd ..
sudo dpkg -i bluez_*.deb

# Pin the package so apt doesn't overwrite it
sudo apt-mark hold bluez

# Restart bluetooth
sudo systemctl restart bluetooth
```

After a distro BlueZ update, re-apply the patch and rebuild.

</details>

<details>
<summary><strong>Arch Linux</strong></summary>

```bash
# Get the PKGBUILD
asp update bluez
asp checkout bluez
cd bluez/trunk/

# Copy the patch
cp /path/to/patches/fix-duplicate-mtu-request.patch .

# Add to PKGBUILD: in the prepare() function, add:
#   patch -p1 < "$srcdir/../fix-duplicate-mtu-request.patch"
# Or add to the source= and sha256sums= arrays

# Build and install
makepkg -si
```

</details>

<details>
<summary><strong>Fedora</strong></summary>

```bash
# Install build tools
sudo dnf install rpm-build dnf-utils
sudo dnf builddep bluez

# Get the source RPM
dnf download --source bluez
rpm -i bluez-*.src.rpm

# Add the patch to ~/rpmbuild/SOURCES/
cp /path/to/patches/fix-duplicate-mtu-request.patch ~/rpmbuild/SOURCES/

# Edit the spec file to apply the patch
# In ~/rpmbuild/SPECS/bluez.spec, add after existing Patch lines:
#   PatchN: fix-duplicate-mtu-request.patch
# And in %prep after existing %patch lines:
#   %patchN -p1

# Build
rpmbuild -bb ~/rpmbuild/SPECS/bluez.spec

# Install
sudo rpm -Uvh ~/rpmbuild/RPMS/x86_64/bluez-*.rpm

sudo systemctl restart bluetooth
```

</details>

<details>
<summary><strong>From source (any distro)</strong></summary>

```bash
wget https://www.kernel.org/pub/linux/bluetooth/bluez-5.84.tar.xz
tar xf bluez-5.84.tar.xz
cd bluez-5.84/

patch -p1 < /path/to/patches/fix-duplicate-mtu-request.patch

./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
  --enable-library --enable-tools
make -j$(nproc)
sudo make install

sudo systemctl restart bluetooth
```

</details>

## How It Works

1. Connects via BlueZ D-Bus, acquires raw fds with `AcquireNotify`/`AcquireWrite`
2. Enables indications on FFE2 via `StartNotify` (CCCD write — triggers pen tablet mode)
3. Sends Pen Tablet Mode handshake: `cd c9/c8/ca 00 00 00 00 00 00`
4. Reads `55 54` pen data packets from FFE1 notification fd
5. Injects `ABS_X`, `ABS_Y`, `ABS_PRESSURE`, `ABS_TILT_X/Y` into `/dev/uinput`
6. Keepalive via battery query (`cd d1`) every 5s

```text
┌──────────────┐    BLE GATT     ┌───────────────────────┐
│  Huion Note  │◄───────────────►│  BlueZ (patched)      │
│     X10      │  FFE0 service   │                       │
│              │  FFE1: notify   │  AcquireNotify → fd   │
│              │  FFE2: indicate │  StartNotify (CCCD)   │
│              │  FFE2: write    │  AcquireWrite  → fd   │
└──────────────┘                 └──────────┬────────────┘
                                            │ raw fds
                                 ┌──────────▼────────────┐
                                 │ huion_ble_driver.py   │
                                 │ parse 55 54 packets   │
                                 │ handshake + keepalive │
                                 └──────────┬────────────┘
                                            │ ioctls
                                 ┌──────────▼───────────┐
                                 │  /dev/uinput         │
                                 │  → libinput → app    │
                                 └──────────────────────┘
```

## Device Info

| Field | Value |
|-------|-------|
| Product | Huion Note X10 |
| Internal model | HUION_T218 |
| Pen | PW320 Scribo Pen (dual nibs: ballpoint for paper, plastic for drawing) |
| BLE | 5.0 GATT, VID `0x256C`, PID `0x8251` |
| Vendor GATT service | `0000FFE0` (FFE1 = pen data notify, FFE2 = command write) |
| Pen data | `55 54` header, 14 bytes (24-bit coords, 16-bit pressure, signed int8 tilt) |
| Resolution | 28200 x 37400 |
| Pressure | 8192 levels (13-bit) |

## Why No Existing Driver Works

- **hid-uclogic** — kernel driver, USB only (bus `0003`), ignores BLE (bus `0005`)
- **Huion Linux driver v15** — BLE code exists but deliberately disabled (`is_ble_tablet_online()` stubs to false)
- **OpenTabletDriver** — USB only, no BLE transport
- **BlueZ HOGP** — claims device as keyboard (HID Report Map = Usage 0x06). Pen data uses vendor FFE0/FFE1, not HID

## Dependencies

- Python 3.10+
- [`dbus-fast`](https://github.com/Bluetooth-Devices/dbus-fast) — async D-Bus client for BlueZ
- BlueZ 5.x (**patched** — see above)
- Linux kernel 4.5+ (for `UI_DEV_SETUP` / `UI_ABS_SETUP` uinput ioctls)

## Reverse Engineering

The BLE protocol is completely undocumented. It was reverse-engineered in three stages:

1. **Binary archaeology** — Extracted Huion's official drivers (Linux v15.0, macOS v15.7, Windows v15.7). Found vendor GATT UUIDs (`FFE0/FFE1/FFE2`) and command IDs via `strings`/`nm`/`objdump`. Discovered the Linux driver's BLE code is deliberately disabled.

2. **Android BLE capture** — Captured traffic between the Huion Note app and tablet using Bluetooth HCI snoop logs. Decoded the Note-Taking Mode protocol: `cd XX 08 P0 P1 P2 P3 ed` framing, 11-byte pen data with 16-bit coords.

3. **Ghidra RE of macOS driver** — Analyzed `libTabletSession.dylib` to find Pen Tablet Mode, which uses different command IDs (`0xC8/0xC9/0xCA/0xD1`) and pen data format (`55 54` header, 14 bytes, 24-bit coords + tilt).

Key discoveries:
- The device enters pen tablet mode when FFE2's CCCD indicate bit is written (`StartNotify` on FFE2) — found by RE of macOS driver's `setNotifyValue:1` on both characteristics.
- The device firmware sends 9 duplicate ATT MTU requests after every connection parameter update, which crashes BlueZ's GATT client — fixed by a 2-line patch to `att.c`.

See [`notes/journey.md`](notes/journey.md) for the full RE session log.

## Project Structure

```text
huion_ble_driver.py             — BLE tablet driver (dbus_fast + uinput)
patches/
  fix-duplicate-mtu-request.patch — BlueZ att.c patch for firmware MTU bug
huion-note-x10.service          — systemd user service
99-huion-note-x10.rules         — udev rules (uinput access + HOGP unbind)
notes/journey.md                — Full RE session log
Archives/                       — RE artifacts (Ghidra, captures, scripts)
```

## License

Licensed under [MIT License](LICENSE).

Huion and Note X10 are trademarks of Huion. This project is not affiliated with or endorsed by Huion.
