# Huion Note X10 — Offline Note Extraction (without the Huion app)

Reverse-engineering the Huion Note X10's Bluetooth offline-sync so handwritten
pages can be pulled and decoded **locally**, with no Huion app and no cloud — for
privacy and interoperability on hardware we own.

**Status:** Sub-project B is implemented. The CLI (`python3 -m huion_notes`) can
decode an existing btsnoop capture or connect live and dump all stored pages.
Multi-page is supported and confirmed against a real multi-page capture.
Live end-to-end hardware verification (running `dump` against the physical device)
is the one remaining step.

---

## What we set out to do

Pull the notes stored offline on the notebook without using Huion's phone app. The
app talks to the notebook over Bluetooth Low Energy (BLE) and runs a proprietary
sync. We wanted to understand that conversation well enough to reproduce it
ourselves on Linux.

The plan was two acts:

- **Act A — Discovery:** capture a real sync + read the app's own code, and write
  down the protocol in enough detail to rebuild it. ← *done*
- **Act B — Extractor:** a Linux tool that connects, asks for the pages,
  and decodes them. ← *done (multi-page)*

## How it went (timeline)

1. **Set up the device link.** Connected the Android phone (Galaxy S22 Ultra,
   non-rooted) over USB/ADB. Found the Huion app: `com.huion.hinotes`.

2. **Captured a real sync.** Enabled Android's Bluetooth HCI snoop log, drew a test
   page on the notebook, ran an offline Synchronize in the app, and pulled the
   capture out via `adb bugreport`. Result: a 142 KB log of the exact Bluetooth
   conversation.

3. **Wrote a parser.** A small, tested Python tool turns that raw log into a clean
   list of Bluetooth commands and data packets (`huion_notes/extract_gatt.py`).

4. **Read the conversation.** Found two channels: a **command** channel and a
   **data** channel. The data channel carried 793 packets — the page. Figured out
   the framing (`cd <opcode> <len> … ed`) and that coordinates are 14-bit.

5. **Hit a wall, then read the app's source.** A first decode attempt produced
   noise after the first stroke. Decompiling the app (`jadx`) revealed the exact
   record format — and that points are parsed **per packet** (not concatenated),
   which was the bug. The app's own classes (`OrderCode`, `BluePoint`) were the
   Rosetta Stone.

6. **Decoded the page.** Rebuilt the decoder to match the app exactly
   (`huion_notes/decode_offline.py`). It rendered the captured page as fully legible
   handwriting — proof the decode is correct.

7. **Cracked the security gate.** The notebook's "auth" is a trivial local
   challenge-response (simple arithmetic, no secret key) plus an optional 6-digit
   PIN encoded with a fixed offset. No server is involved, so it's fully
   reproducible on Linux. The app's "must be logged in" check is app-side only and
   doesn't apply to a custom client.

## What we learned (the protocol, briefly)

- **Two BLE characteristics:** `0x002b` for commands, `0x0027` for data/notifications.
- **Frames:** `cd <opcode> <len> <payload> [ed]`. Opcodes are named in the app's
  `OrderCode` class (e.g. `REQUEST_OFFLINE_DATA`, `RETURN_OFFLINE_DATA`, `MAX_DATA`).
- **Page data:** streamed as `0x87` packets of 126 bytes = 5-byte header + 20
  six-byte points + 1 checksum. Each point is `[x:2][y:2][pressure+pen-status:2]`.
- **Coordinate space:** `MAX_X=28200`, `MAX_Y=37400`, `MAX_PRESS=8191` (read live
  from the device's `MAX_DATA` reply).
- Full details: [`docs/offline-note-protocol.md`](docs/offline-note-protocol.md).

## Repo layout

| Path | What it is |
|------|-----------|
| `huion_notes/frames.py` | `cd <op> … ed` framing, `OrderCode` constants, command builders |
| `huion_notes/auth.py` | challenge-response and PIN encoding |
| `huion_notes/codec.py` | `0x87` / `0x88` packets → points → strokes → pages |
| `huion_notes/render.py` | strokes → SVG / PNG (ImageMagick) / JSON |
| `huion_notes/session.py` | live dump orchestration: handshake, page loop, retransmit |
| `huion_notes/transport.py` | `dbus_fast` BLE transport (thin, lazy import) |
| `huion_notes/cli.py` | `decode` and `dump` subcommands |
| `huion_notes/errors.py` | typed exceptions |
| `docs/specs/` | design spec for Sub-project B |
| `docs/plans/` | step-by-step implementation plan |
| `docs/offline-note-protocol.md` | the reverse-engineered protocol |
| `notes/` | device facts and analysis pointers |
| `captures/`, `apk/` | raw captures + decompiled app — **gitignored** (personal data) |

> Captures and the decompiled app stay out of git on purpose: a raw sync log
> contains your actual handwriting and could contain a replayable token, so only
> derived code and docs are committed.

## What's next

**Sub-project B is implemented.** The remaining steps are:

1. **Live hardware verification** — run `python3 -m huion_notes dump -o ./out`
   against the physical device (patched BlueZ, note mode). The decoder is
   validated offline; this closes the end-to-end loop.
2. **Optional AI transcription** — the JSON output (`strokes`, `x/y/press`) is
   ready to feed a handwriting-recognition model; that's a separate phase.
