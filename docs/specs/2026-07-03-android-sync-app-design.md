# Huion Note X10 — Android Sync App ("HiNote Sync") Design

Date: 2026-07-03
Status: approved (brainstorm 2026-07-03)

## Purpose

A native Android app (sideloaded APK) that connects to the Huion Note X10 over
BLE, downloads all pages stored in tablet memory, shows them as a local gallery,
and lets the user delete pages from the tablet or upload them to a
user-configured HTTP endpoint. No Huion app, no cloud, no account.

The app is a port of the proven Linux extractor in this repo (`huion_notes/`),
using the reverse-engineered protocol in `docs/offline-note-protocol.md`.

## Decisions made during brainstorming

- **Native Kotlin APK** (not Web Bluetooth PWA, not Flutter). Chromium-only
  support and screen-must-stay-on made the PWA unattractive as the daily tool.
- **Upload target:** user's own server/webhook — a plain multipart HTTP POST,
  shape defined by us.
- **Upload payload:** `page.png` (rendered raster) + `strokes.json` (raw
  x/y/pressure point data) per page, both parts in one multipart POST.
- **Location:** new `android/` folder at the repo root; Gradle project is
  self-contained there.

## Constraints from the protocol (see docs/offline-note-protocol.md)

- There is **no list/thumbnail command**. Pages are discovered by requesting
  index 0, 1, 2, … (`REQUEST_OFFLINE_DATA 0x86`) until the device answers with
  packet count 0. "Browse pages" therefore means: sync everything to the phone,
  then browse local copies.
- Command channel = FFE2 (handle 0x002b, write + indications); data channel =
  FFE1 (handle 0x0027, notifications).
- Auth is a keyless arithmetic challenge-response (`VERIFY_CONNECT`/
  `VERIFY_RESULT`), plus an optional 6-digit PIN (`VERIFY_PWD`) encoded with the
  fixed "huion#" offsets. The client must poll for the challenge.
- Page data arrives as `0x87` packets (5-byte header, 20 six-byte points,
  1 checksum byte); points are parsed per packet, never concatenated. Gaps are
  refetched per missing sequence with `GET_PAGE_PACKAGE 0x88`.
- Delete = `DELETE_PAGE 0x8b` by page index; reply byte [3] == 1 means ok.

## Stack

- Kotlin, single Gradle module, Jetpack Compose UI.
- `minSdk 26`, current `targetSdk`. Runtime permissions `BLUETOOTH_SCAN` +
  `BLUETOOTH_CONNECT` (Android 12+ model).
- OkHttp for uploads. No database — plain files + JSON manifest.
- Package: `xyz.reginleif.hinotesync`.

## Architecture

Mirrors the Python module boundaries that already proved themselves:

| Package | Ported from | Responsibility |
|---|---|---|
| `protocol` | `frames.py`, `auth.py`, `codec.py` | **Pure Kotlin, zero Android imports.** `cd…ed` framing, OrderCode constants, challenge-response + PIN encoding, 0x87/0x88 packet → points → strokes → page. JVM-unit-testable. |
| `ble` | `transport.py` | Thin `BluetoothGatt` wrapper: scan/connect, enable notify (FFE1) + indications (FFE2), expose inbound frames as a `Flow`, suspend `write()`. |
| `sync` | `session.py` | Orchestration state machine inside a **foreground service**: handshake → (PIN if demanded) → `MAX_DATA` → `SET_MANY_PACKET_DISTANCE` → page loop until count 0 → gap retransmit → emit completed pages. |
| `store` | — | App-private storage: one folder per synced page containing `strokes.json`, `page.png`, `meta.json` (sync time, source page index, uploaded flag, complete flag). |
| `render` | `render.py` | Strokes → `Bitmap` via `Canvas`. `page_x = x/MAX_X × W`, top-left origin, no axis swap/flip (X10 is not the T910 variant). Simple linear pressure → stroke width. |
| `upload` | — | OkHttp multipart POST (`page.png`, `strokes.json`) to configured URL with optional custom auth header. Filename stem `page-<syncTimestamp>-<index>`. |
| `ui` | — | Compose screens (below). |

## UI

1. **Home / Gallery** — grid of locally synced page thumbnails (newest first),
   sync status bar, big **Sync** button. Multi-select for batch upload/delete.
2. **Page viewer** — full-screen render; actions: **Upload**, **Delete on
   tablet**, delete local copy.
3. **Settings** — server URL, optional auth header (name + value), device PIN
   (only used if the tablet answers "PIN required"; user's device currently
   reports none), and **"delete from tablet after successful upload"** toggle,
   default **off**.

## Delete semantics (the risky part)

The Linux extractor deletes each page immediately after saving, so indices never
go stale. A delete-later gallery must guard against index drift (new pages drawn
since sync):

- "Delete on tablet" is offered only while a session is live **and** the page
  set is unchanged since sync (re-check page packet counts cheaply first).
- Batch deletes run in **descending index order** so earlier deletions cannot
  shift later targets.
- Fallback if drift proves unreliable in practice: a "sync & clear" mode that
  deletes each page right after it is saved (the extractor's proven model).

## Error handling

- Mid-transfer disconnect → reconnect, re-handshake, resume via `0x88`
  retransmits. A page that cannot be completed is marked *incomplete*, rendered
  anyway, and excluded from tablet-delete.
- Upload failure → page keeps `uploaded=false`; manual retry. **Nothing is ever
  deleted (tablet or phone) as a consequence of a failed upload.**
- Packet checksum mismatch → treat as a missing sequence and retransmit (same
  policy as `session.py`).
- Tablet answers PIN-required with no stored PIN → prompt in UI, retry
  handshake.

## Testing

- `protocol` gets JVM unit tests ported 1:1 from `test_frames.py`,
  `test_auth.py`, `test_codec.py`.
- Golden-file parity test: replay a frames dump derived from the real captures
  (`captures/` is gitignored — the fixture committed is derived, containing only
  synthetic or already-published test vectors) and assert the Kotlin codec
  produces the same strokes as the Python one.
- `ble`/`sync` are verified against the physical tablet (manual test plan in the
  implementation plan).

## Out of scope

- Handwriting recognition / transcription.
- Background/scheduled sync; the app only syncs when opened and asked.
- Pressure-accurate Bézier smoothing to match Huion's visual rendering
  (linear width is enough for legibility; revisit later if desired).
- iOS, desktop, Play Store distribution.
