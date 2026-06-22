# Device Facts — Discovery Environment

Recorded during Task 1 of the discovery plan. Sensitive identifiers (full BT MAC
addresses, ADB serials) are deliberately **omitted** — they live only in the
gitignored `captures/` artifacts.

## Phone (capture host's target)

- Model: Samsung Galaxy S22 Ultra (`SM-S908B`, codename `b0s`)
- Android version: **16**
- Security patch: 2026-05-05
- Root status: **NON-ROOT** → snoop log must be extracted via `adb bugreport`
  (no direct `/data/misc/bluetooth/logs` pull).
- Users present: `0` (main profile) and `150` (Samsung Secure Folder).

## Huion app

- Package: **`com.huion.hinotes`**
- Installed in: **user 0 (main profile)** — confirmed via `pm list packages -3 --user 0`.
  (An earlier `SecurityException` on user 150 had suggested Secure Folder; the
  app was subsequently available in the main profile, so `adb pull` of the APK
  is **not** blocked.)
- APK is **split**: `base.apk` (code — the jadx target), plus
  `split_config.arm64_v8a.apk` (native libs — check here if the stroke decoder
  is native), `split_config.en.apk`, `split_config.xxhdpi.apk`.
- Has a live GATT client registered while connected (`app_if: 51`).

## Notebook

- Advertised name: **`Huion Note-X10`**
- Connection: BLE, currently **STATE_CONNECTED**, bonded.
- Advertised profiles seen: `BATTERY`, `Hogp` (HID-over-GATT — the pen HID
  channel). The offline-note sync service is a **custom** GATT service, to be
  identified from the capture in Task 5.

## Implications for the plan

- Capture (Task 3) is unaffected by Secure Folder: the HCI snoop log records at
  the system Bluetooth-stack level, system-wide.
- APK decompile (Task 2) proceeds normally via `adb pull` of `base.apk` since
  the app is in user 0.
- Watch for a **native** stroke decoder: if `jadx` on `base.apk` doesn't reveal
  the decode logic, inspect the `arm64_v8a` split's `.so` files.
