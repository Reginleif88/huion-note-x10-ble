# Huion Note X10 — Linux BLE Driver & Note Extractor

[![Platform](https://img.shields.io/badge/Platform-Linux-blue?logo=linux&logoColor=white)](https://github.com/Reginleif88/huion-note-x10-ble)
[![License](https://img.shields.io/badge/License-MIT-blue)](./LICENSE)
[![BLE](https://img.shields.io/badge/BLE-5.0_GATT-0082FC?logo=bluetooth&logoColor=white)](https://www.bluez.org/)
[![Status](https://img.shields.io/badge/Status-Working-green)](./docs/notes/journey.md)

Use the Huion Note X10 fully on Linux — **no Huion app, no cloud.** Huion's official
Linux driver deliberately disables BLE, so the protocol was reverse-engineered from
the macOS/Windows drivers (Ghidra), Android BLE captures + APK. The repo gives you two
tools that share that work:

- **Tablet driver** — live pen input (pressure + tilt) for Linux.
- **Pages extractor** — pull the pages you wrote *offline* on the notebook and decode
  them to SVG + PNG + JSON, locally.

Both talk to the device over BLE and both need a small [BlueZ patch](#requirements-patch-bluez)
(firmware bug).

> Codebase maintained with [Claude Code](https://claude.ai/code). Not an official Huion
> product. Tested on NixOS; should work on any Linux with patched BlueZ 5.x — reports
> from other distros welcome.

---

## What you can do

### 🖊️ Use it as a drawing tablet

Live pen data → `/dev/uinput` → works in Krita, GIMP, xournalpp, etc. via the standard
Linux input stack. Pressure (8192 levels), tilt, and orientation-aware rotation.

```bash
python3 huion_ble_driver.py --orientation portrait_cw    # quick test, no install
```

Full setup (systemd service, udev, orientation) → [**Tablet driver**](#tablet-driver).

### 📓 Pull your handwritten notes

Connect over BLE, dump **every** stored page, and decode each to SVG (vector), PNG, and
JSON (ordered points + pressure — ready to feed an AI handwriting-recognition model).
By default, **each page is deleted from the device once it's exported and saved** (like the
Huion app, so the device doesn't refill); pass `--keep` to leave pages on the device. A page
is deleted only after its SVG + JSON are confirmed written.

```bash
./huion-x10-notes.sh dump   -o ./notes-out                  # all stored pages, live over BLE
./huion-x10-notes.sh decode capture.btsnoop -o ./notes-out  # or replay an existing capture
```

Validated end-to-end on hardware. Details → [**Note extractor**](#note-extractor).

---

## Requirements: patch BlueZ

The X10 firmware sends duplicate `Exchange MTU Request` packets after every BLE
connection-parameter update (~every 5–8 s). Unpatched BlueZ 5.x treats this as a
protocol violation and disconnects — killing the pen stream and any note dump. A 2-line
patch to `src/shared/att.c` drops the duplicate instead:

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

The patch ships at [`patches/fix-duplicate-mtu-request.patch`](patches/fix-duplicate-mtu-request.patch).

**NixOS:** applied automatically via the `hardware.bluetooth.package` overlay — see `modules/huion-ble.nix`.

<details>
<summary><strong>Debian / Ubuntu</strong></summary>

```bash
sudo apt build-dep bluez && sudo apt install devscripts
apt source bluez && cd bluez-*/
patch -p1 < /path/to/patches/fix-duplicate-mtu-request.patch
debuild -us -uc -b
cd .. && sudo dpkg -i bluez_*.deb && sudo apt-mark hold bluez
sudo systemctl restart bluetooth
```

</details>

<details>
<summary><strong>Arch Linux</strong></summary>

```bash
asp update bluez && asp checkout bluez && cd bluez/trunk/
cp /path/to/patches/fix-duplicate-mtu-request.patch .
# Add to PKGBUILD prepare(): patch -p1 < "$srcdir/../fix-duplicate-mtu-request.patch"
makepkg -si
```

</details>

<details>
<summary><strong>Fedora</strong></summary>

```bash
sudo dnf install rpm-build dnf-utils && sudo dnf builddep bluez
dnf download --source bluez && rpm -i bluez-*.src.rpm
cp /path/to/patches/fix-duplicate-mtu-request.patch ~/rpmbuild/SOURCES/
# Edit ~/rpmbuild/SPECS/bluez.spec — add PatchN and %patchN lines
rpmbuild -bb ~/rpmbuild/SPECS/bluez.spec
sudo rpm -Uvh ~/rpmbuild/RPMS/x86_64/bluez-*.rpm && sudo systemctl restart bluetooth
```

</details>

<details>
<summary><strong>From source (any distro)</strong></summary>

```bash
wget https://www.kernel.org/pub/linux/bluetooth/bluez-5.84.tar.xz
tar xf bluez-5.84.tar.xz && cd bluez-5.84/
patch -p1 < /path/to/patches/fix-duplicate-mtu-request.patch
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var --enable-library --enable-tools
make -j$(nproc) && sudo make install && sudo systemctl restart bluetooth
```

</details>

Then pair the device once (`bluetoothctl` or your desktop's Bluetooth settings) and
**trust** it so it reconnects automatically.

---

## Tablet driver

Drives the X10 as a live pen tablet. Needs `dbus-fast` and access to `/dev/uinput`.

```bash
pip install dbus-fast            # or: nix-shell -p python3Packages.dbus-fast
```

**1. udev rules** — grant `/dev/uinput` access and auto-unbind `hid-generic` (prevents HOGP interference):

```bash
sudo cp 99-huion-note-x10.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo usermod -aG input $USER       # log out/in for the group change to apply
```

**2. Install + enable a service** (install **one** — match how you hold the tablet):

```bash
mkdir -p ~/.local/share/huion-note-x10
cp huion_ble_driver.py ~/.local/share/huion-note-x10/
cp huion-note-x10-portrait.service ~/.config/systemd/user/    # or -landscape
systemctl --user daemon-reload
systemctl --user enable --now huion-note-x10-portrait
```

For `portrait_ccw` / `inverted` or a pinned `--mac`, override `ExecStart` with
`systemctl --user edit huion-note-x10-portrait`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 %h/.local/share/huion-note-x10/huion_ble_driver.py --orientation portrait_ccw --mac XX:XX:XX:XX:XX:XX
```

**3. Verify** — `sudo evtest` → pick "Huion Note X10 BLE"; the pen should report pressure
0–8191 and tracking X/Y. If it tracks in evtest, it works in any app.

### Orientation

The device's native frame is landscape, but the X10 is held in portrait for writing. The
driver rotates raw coordinates so apps see the orientation you actually hold.

| `--orientation` | Rotation | Notes                                          |
|-----------------|----------|------------------------------------------------|
| `landscape`     | 0°       | Native device frame (short edge top/bottom)    |
| `portrait_cw`   | 270°     | Default — tablet held in portrait              |
| `portrait_ccw`  | 90°      | Portrait, mirrored from `portrait_cw`          |
| `inverted`      | 180°     | Upside-down relative to native                 |

Pick by sweeping the pen top→bottom; the cursor should follow. **Turn off compositor-side
tablet rotation** (Hyprland `input:tablet:transform = 0`; reset libinput matrices) — the
driver owns rotation, and double rotation compounds. Fine-tune pressure/area with
[OpenTabletDriver](https://opentabletdriver.net/) or `xinput map-to-output`.

---

## Note extractor

Pulls and decodes the pages stored offline on the notebook. Run it with the
`huion-x10-notes.sh` launcher; each page is written as
`page{N}-{DD}-{MM}.{svg,png,json}` (1-based page + dump date, e.g. `page1-22-06`).
Output goes to `-o OUT`, or the launcher's `OUT_DIR` default (`./notes-out`,
overridable via the `HUION_NOTES_OUT` env var) when you omit `-o`.

```bash
./huion-x10-notes.sh dump   [-o ./notes-out] [--keep] [--mac AA:BB:..] [--pin 123456] [--verbose]
./huion-x10-notes.sh decode capture.btsnoop [-o ./notes-out]
```

`--keep` leaves pages on the device; without it, each successfully-saved page is deleted.

- **`dump`** connects over BLE (notebook in **note mode** — folio cover closed), runs the
  keyless handshake, and pulls every page until the device reports an empty one. By default
  each page is deleted from the device once its files are saved (`--keep` to opt out). The
  launcher auto-pauses the pen-driver service during the dump and restarts it after.
- **`decode`** replays an existing Android btsnoop capture — fully offline, no device.
- **Output:** SVG (vector master), PNG (if ImageMagick is present; otherwise skipped with a
  warning), and JSON (`{page, max_x, max_y, max_press, strokes:[[{x,y,press,pen_down}]]}`).

The launcher runs `python3 -m huion_notes` with the package on `PYTHONPATH` (works from any
directory). For `dump` it ensures its one dependency, `dbus_fast`, is available — using the
active Python if it has it, else a Nix-provided one
(`nix-shell -p 'python3.withPackages(ps: [ps.dbus-fast])'`), else `pip install dbus-fast` in
a venv. `decode` needs only the standard library; the decoder is fully unit-tested.

Background and protocol: [`docs/offline-notes-overview.md`](docs/offline-notes-overview.md),
[`docs/offline-note-protocol.md`](docs/offline-note-protocol.md),
[`docs/specs/2026-06-22-note-extractor-design.md`](docs/specs/2026-06-22-note-extractor-design.md).

> Captures and decoded pages contain your handwriting — they stay gitignored. Only code and
> protocol docs are committed.

---

## How it works

**Driver:** connect via BlueZ D-Bus → `AcquireNotify`/`AcquireWrite` raw fds + `StartNotify`
on FFE2 (CCCD write triggers pen-tablet mode) → handshake (`0xC8/0xC9/0xCA`) → parse
`55 54` pen packets from FFE1 → inject `ABS_X/Y/PRESSURE/TILT` into `/dev/uinput`; keepalive
(`0xD1`) every 5 s.

**Extractor:** connect → keyless challenge/response (`((a+b)<<2)%255`, optional PIN) → read
device limits → per page: `REQUEST_OFFLINE_DATA` → device replies a packet count, streams
`0x87` point packets (gaps re-fetched via `0x88`) → decode 6-byte points → strokes → render.
The decode core is pure and runs on a `.btsnoop` capture or live BLE identically.

Full reverse-engineering log: [`docs/notes/journey.md`](docs/notes/journey.md).

## Device info

| Field | Value |
|-------|-------|
| Product | Huion Note X10 (internal `HUION_T218`) |
| Pen | PW320 Scribo (dual nibs: ballpoint + plastic) |
| BLE | 5.0 GATT, VID `0x256C`, PID `0x8251` |
| Vendor GATT | `0000FFE0` (FFE1 = data notify, FFE2 = command write/indicate) |
| Resolution / pressure | 28200 × 37400, 8192 levels (13-bit) |

**Why no existing driver works:** `hid-uclogic` is USB-only; Huion's Linux v15 has BLE code
but stubs it off; OpenTabletDriver is USB-only; BlueZ HOGP mis-claims the device as a
keyboard (pen data is on vendor FFE0, not HID).

## Repo layout

```text
huion_ble_driver.py        — live pen/tablet driver (dbus_fast + uinput)
huion_notes/               — offline note extractor package (frames/auth/codec/render/session/transport/cli)
android/                   — HiNote Sync — native Android app: sync pages over BLE, upload PNG+JSON, delete on tablet
huion-x10-notes.sh         — launcher for the extractor (handles dbus_fast / Nix)
patches/                   — BlueZ att.c patch for the firmware MTU bug
*.service, *.rules         — systemd user services + udev rules (driver)
docs/                      — protocol map, specs, overview, RE log (docs/notes/)
captures/, apk/, notes-out/ — gitignored (personal captures, decompiled app, decoded pages)
```

## License

[MIT](LICENSE). Huion and Note X10 are trademarks of Huion; this project is not affiliated
with or endorsed by Huion.
