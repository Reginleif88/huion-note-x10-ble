# Offline Note Extractor — Status / Handoff

**Branch:** `offline-note-export`. **Last updated:** 2026-06-22.

Start here if you're picking this up fresh. This is **Sub-project B** (a Linux BLE
tool that pulls and decodes the X10's stored pages). **Sub-project A (discovery) is
done** — the protocol is fully reverse-engineered.

## Where things stand

- ✅ **Protocol fully mapped:** `docs/offline-note-protocol.md` (transport, framing,
  opcodes, the 6-byte point layout, the keyless auth handshake, command builders).
- ✅ **Sub-project B implemented:** `huion_notes/{frames,auth,codec,render,errors,session,transport,cli}.py`
  + `__main__.py`. 41 stdlib-unittest tests pass. Multi-page confirmed from a real
  multi-page btsnoop capture (`sync-multipage.btsnoop`, gitignored).
- ✅ **Decoder fixture-validated offline:** decoded cleanly against both
  `captures/sync-01.btsnoop` (single page) and `captures/sync-multipage.btsnoop`
  (multi-page, 0 packets missing after retransmit). Captures are gitignored.
- ✅ **Sub-project B design spec:** `docs/specs/2026-06-22-note-extractor-design.md`
  (implemented).
- ✅ **Live verification PASSED (2026-06-22):** `./huion-x10-notes.sh dump` pulled 3 real
  pages over BLE end-to-end (no app/phone) — page-00/01/02, 4/2/1 strokes, MAX read
  live as 28200/37400/8191, page loop stopped cleanly on `count==0`. Both prior
  unknowns confirmed: FFE1 notifications + FFE2 indications both arrive via the single
  `setup_signals` callback, and `count==0` is the correct page-loop terminator. The
  live run also fixed a handshake bug: the client must **send** `VERIFY_CONNECT`
  (`cd 81 08 00 00 00 00 ed`) first; the device only then replies with its challenge
  (fixed in `session._authenticate`, commit 4344e57).
  - **Run via the `huion-x10-notes.sh` launcher** (not bare `python3 -m huion_notes`):
    on NixOS the active python lacks `dbus_fast` and `pip` is PEP-668-blocked, so the
    launcher supplies it via `nix-shell -p 'python3.withPackages(ps: [ps.dbus-fast])'`.
  - **Pairing (Hyprland has no BT agent):** the X10 connects **unbonded** — no BLE bond
    needed (auth is app-level). First time: `bluetoothctl` -> `agent NoInputNoOutput`,
    `default-agent`, `scan on` (keep scanning so BlueZ doesn't evict the device),
    then `trust <MAC>` / `connect <MAC>`. The device must be free of the phone (BLE =
    one central at a time).

## Local-only assets (gitignored — contain personal data / proprietary code)

- `captures/sync-01.btsnoop`, `captures/frames-01.jsonl` — the Act-A single-page
  capture; used as the primary codec fixture.
- `captures/sync-multipage.btsnoop` — the multi-page capture used to confirm the
  page-loop protocol.
- `apk/reference/*.java` — the key decompiled Huion app classes (`OrderCode`,
  `BluePoint`, `BluetoothUtil`, `HiBluetoothManager`, `ByteUtil`) for protocol detail.

## Key facts

- **Transport:** `dbus_fast` `BleTransport` in `huion_notes/transport.py`.
  Characteristics: FFE1 = notify (`service0025/char0026`, value handle `0x0027`),
  FFE2 = write (`service0025/char002a`, value handle `0x002b`).
- **Patched BlueZ is required** (see README) — the X10's duplicate-MTU quirk would
  otherwise disconnect mid-dump.
- **Mode:** offline notes need the device in **note mode** (folio cover closed) —
  *not* the pen/tablet mode the existing driver drives.
- **Auth is keyless & local:** challenge-response `((a+b)<<2)%255` + optional 6-digit
  PIN (`encode_pwd`). The app's login check is app-side only.
- **Multi-page protocol:** count-driven page loop (`cd 86 …` → count reply;
  `count == 0` ⇒ stop); missing packets retransmitted via `0x88`. Confirmed from
  `sync-multipage.btsnoop` and cross-checked against the app source.
- **Device cleanup (delete-by-default):** after a page is exported and its SVG+JSON are
  saved, the extractor sends `DELETE_PAGE (0x8b)` for it and `CLEAR_CACHE (0x8c)` at the end
  (matching the app, so the device doesn't refill). Pass `--keep` to leave pages on the
  device; incomplete or unsaved pages are never deleted.

## Origin of this work

Reverse-engineered in the scratch repo `~/Documents/huion-note-x10-noteboook-re`
(Act-A history lives there). All needed artifacts have been migrated here.
