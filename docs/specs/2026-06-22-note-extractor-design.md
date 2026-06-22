# Huion Note X10 — Linux Note Extractor (Sub-project B) Design

**Date:** 2026-06-22
**Status:** Implemented (multi-page)
**Repo:** `huion-note-x10-ble`, branch `offline-note-export`
**Depends on:** Act-A discovery (`docs/offline-note-protocol.md`) — protocol fully mapped.

---

## 1. Motivation

Act-A proved we can decode the X10's offline pages from a phone-captured Bluetooth
log, entirely locally. Sub-project B removes the phone: a Linux CLI that connects
to the notebook over BLE, triggers the offline dump itself, and writes each page
as SVG + PNG + JSON — no Huion app, no cloud. JSON (ordered points + pressure) is
included specifically to feed AI handwriting→text transcription.

This builds in the same repo as the existing pen driver, reusing its proven
`dbus_fast` BLE transport and the patched BlueZ it already requires.

## 2. Goal / Definition of Done

A reusable CLI:

- `huion-x10-notes dump -o OUT/` — connect, dump, decode all stored pages to
  `OUT/page-NN.{svg,png,json}`.
- `huion-x10-notes decode FILE.btsnoop -o OUT/` — decode an existing capture
  (replay path; keeps Act-A captures working and backs the test suite).

Done when: a live `dump` against the notebook reproduces pages a human can read,
and the same codec passes its test suite against the `sync-01` fixture offline.

## 3. Architecture — replayable core + thin transport

The protocol/decode logic is **pure** and operates on byte frames regardless of
their origin (live BLE notification or a `.btsnoop` file). Only the BLE I/O is
live and hard to test. This split lets the entire dump-and-decode pipeline be
unit-tested against `sync-01` with no device attached; the transport is a thin,
swappable adapter.

```
            ┌─────────────── pure, device-free, unit-tested ───────────────┐
 BLE/btsnoop → frames → (session orchestration) → codec → render → files
            └──────────────────────────────────────────────────────────────┘
                  ▲
            transport (dbus_fast)  ── the only live part
```

## 4. Module Boundaries

| Module | Responsibility | Device-free tests |
|--------|----------------|-------------------|
| `huion_notes/frames.py` | `cd <op> <len> … ed` framing, opcode constants (`OrderCode`), checksum | ✅ |
| `huion_notes/auth.py` | challenge→response `((a+b)<<2)%255`; `encode_pwd` (PIN + "huion#" offsets) | ✅ |
| `huion_notes/codec.py` | `0x87` packets → points (`BluePoint` layout) → strokes → **pages**; reads MAX_X/Y/PRESS from `0x95` | ✅ (replay `sync-01`) |
| `huion_notes/render.py` | strokes → SVG; SVG → PNG (ImageMagick subprocess); strokes → JSON | ✅ |
| `huion_notes/session.py` | dump orchestration: handshake → `requestPageData` → collect `0x87` → hand to codec; retransmit on gaps | ✅ via fake transport |
| `huion_notes/transport.py` | `dbus_fast` BLE: connect, `AcquireNotify` FFE1, `WriteValue` FFE2, keepalive | ❌ (thin) |
| `huion_notes/cli.py` | `dump` / `decode` subcommands, output paths, `--pin`, `--mac`, `--verbose` | ✅ |

`extract_gatt.py` (already migrated) provides btsnoop→frames for the `decode` path
and is folded into / reused by `frames.py`; `decode_offline.py`'s logic moves into
`codec.py` + `render.py`.

## 5. Live Handshake Sequence

From `docs/offline-note-protocol.md` §6–7. All commands written to FFE2 (`0x002b`);
responses arrive as FFE1 (`0x0027`) notifications.

1. Connect (reuse driver `BLEConnection`); enable notify on FFE1 + indications on FFE2.
2. Device → `VERIFY_CONNECT (0x81)` with 3-byte challenge `a,b,c`.
3. Reply `VERIFY_RESULT (0x82)`: `cd 82 08 r1 r2 r3 00 ed`,
   `r1=((a+b)<<2)%255, r2=((b+c)<<2)%255, r3=((c+10)<<2)%255`.
4. Device → status: `1` = ok; `2` = PIN required → send `VERIFY_PWD (0x83)` two-frame
   `encode_pwd(--pin)`; `0`/no `--pin` when required = clear error.
5. `requestMaxInfo` (`cd 95 08 …`) → record MAX_X/Y/PRESS.
6. `requestSetManyPacketDistance` (`cd 96 …` ×2).
7. `requestPageData` (`cd 86 08 <page_lo> <page_hi> <idx> 00 ed`) → device streams
   `RETURN_OFFLINE_DATA (0x87)` packets; collect until complete.
8. Decode via codec → render.

The app's "must be logged in" (cookie/token) check is app-side only and is not
replicated; the device does not enforce it.

## 6. Multi-page

**Discovery complete.** A real multi-page btsnoop capture (`sync-multipage.btsnoop`,
gitignored) was used to reverse-engineer and confirm the full page-loop protocol:

- Per page `p`: send `request_page_data(p, sub=0)` → device replies with a packet
  count (u16 LE); `count == 0` means the page is empty and the loop stops.
- Device streams `count` × `RETURN_OFFLINE_DATA (0x87)` packets; missing sequence
  numbers are retransmitted via `GET_PAGE_PACKAGE (0x88)`.
- The stop condition (`count == 0`) was confirmed against the app source
  (`HiBluetoothManager`) and validated against the multi-page fixture (0 packets
  missing after retransmit).

This is implemented in `session.DumpSession` (page loop) and `frames.py` (command
builders). A safety `max_pages` cap is applied.

## 7. Output Formats

- **SVG** — vector master (pure Python), pressure→stroke-width optional.
- **PNG** — `magick in.svg out.png` (ImageMagick already present); if absent, emit
  SVG/JSON and warn (non-fatal).
- **JSON** — `{page, max_x, max_y, max_press, strokes:[[{x,y,press,pen_down}]]}`,
  ordered, lossless — the high-accuracy input for online/AI HWR.

## 8. Error Handling

Explicit, actionable failures: not paired / connect fails (point to BlueZ pairing
+ patched-BlueZ requirement), challenge timeout, PIN required but missing/invalid,
missing packets (use `requestReGetPackageData` retransmit; report if still short),
ImageMagick missing (degrade to SVG+JSON). No silent truncation — log dropped/short
pages.

## 9. Testing

- Pure modules: `unittest` (stdlib, matching the migrated tests).
- `codec`/`render`: validated by reproducing the known `sync-01` page from the fixture.
- `session`: driven by a **fake transport** that replays `sync-01` frames in sequence,
  asserting the handshake emits the correct command bytes and assembles all `0x87`.
- Live BLE: manual verification against the device (cannot be unit-tested).

## 10. Dependencies

- `dbus_fast` (already used by the pen driver).
- ImageMagick (`magick`) for PNG — optional at runtime.
- Standard library otherwise. Patched BlueZ (repo prerequisite) required for live use.

## 11. Out of Scope (YAGNI)

- Built-in AI transcription (the CLI emits PNG/JSON; calling a model is a later phase).
- PDF output (derivable from SVG later if wanted).
- Pen/tablet (live) mode — that's the existing driver.
- Writing/modifying device firmware; USB extraction (USB exposes HID only — confirmed).
- GUI.

## 12. Privacy

Captures and decoded pages contain personal handwriting (and possibly a replayable
token) and stay gitignored/local. Only code and protocol docs are committed.
