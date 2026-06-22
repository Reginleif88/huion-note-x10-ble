# Huion Note X10 — Offline Note Extraction: Discovery Phase Design

**Date:** 2026-06-21
**Status:** Approved (design), pending spec review
**Scope:** Sub-project A — Protocol Discovery only

---

## 1. Motivation

The Huion Note X10 stores handwritten pages offline on the notebook. The only
official way to retrieve them is Huion's phone app, which connects to the
notebook over Bluetooth Low Energy (BLE) and runs a proprietary sync handshake
(a "Synchronize" step, a "notes waiting to upload" indicator, and a "Bind This
Notebook" option). For privacy reasons we want to extract our own strokes
**without** the Huion app, using only local tooling on hardware we own.

Crucially, this device model has **no cloud storage** — all decode logic lives
in the Android app and on the device itself. That makes the entire
transfer-and-decode path fully observable locally, which is the central
advantage this project exploits.

### What we are NOT reversing

- USB tablet-mode HID (live pen/pressure/tilt for desktop drawing). A
  community Linux driver for that already exists. It is a *different* channel
  and does not carry stored pages.
- The over-the-air radio layer. We capture **above** BLE encryption, at the
  host stack level, so bonding/encryption never blocks us.

### Starting point

We are starting effectively **from scratch** on the offline-note channel. No
part of its BLE conversation (service UUIDs, dump command, chunk framing,
stroke payload) has been mapped yet.

---

## 2. Two-Act Structure

The work splits into two sub-projects with a hard data dependency between them.
A Linux extractor cannot be specified before we know the protocol it speaks, so
discovery must complete first.

- **Sub-project A — Discovery (this document):** Capture a real offline sync and
  decompile the app, then produce a written **protocol map** sufficient to build
  the extractor without further captures.
- **Sub-project B — Extractor (future, separate spec):** A `bleak`-based Linux
  tool that connects to the X10, issues the dump command, reassembles chunks,
  decodes strokes, and exports to SVG/PDF. Specced only after A delivers facts.

---

## 3. Goal / Definition of Done (Sub-project A)

A written, validated `protocol-map.md` that documents, with byte-level
precision:

1. The offline-sync BLE **service and characteristic UUIDs** (which characteristic
   we write commands to, which we subscribe to for notifications).
2. The **"dump stored pages" command** sequence — the exact opcode(s) and payload
   that trigger a bulk upload of stored pages.
3. The **chunk / framing scheme** — how the bulk payload is split across BLE
   notifications and how to reassemble it (length prefixes, sequence numbers,
   end-of-transfer markers).
4. The **stroke payload layout** — how an individual stroke is encoded
   (x / y / pressure / pen-up-down, page boundaries, timestamps if present).
5. Whether a **bind / auth token** gates the dump, and if so where it lives and
   whether it is replayable locally.

The map is considered correct when it can predict the bytes of a *second,
independent* sync capture.

---

## 4. Approach — Option 1: Hybrid (dynamic capture ⨯ static decompile)

We use two independent ground-truth sources and cross-reference them. They fail
in opposite directions, so combining them collapses both failure modes:

- **Dynamic capture** yields unambiguous real bytes, but those bytes are
  semantically opaque on their own.
- **Static decompile** yields human-readable meaning, but may contain dead code
  or obfuscated names and cannot confirm which runtime path actually executes.

The capture grounds the static reading in reality; the source spares us from
guessing payload semantics byte-by-byte.

### Environment / constraints

- Host: this Linux machine. The Android phone connects over **USB with ADB**
  (USB debugging enabled). Nothing leaves local hardware — consistent with the
  privacy motivation.
- Phone is **not rooted**, with the Huion app already installed and the notebook
  already bound, with offline pages ready to sync.
- Because the phone is not rooted, the HCI snoop log is extracted via
  `adb bugreport` (the log is bundled inside the report zip) rather than a direct
  `/data/misc/bluetooth/logs/` pull.
- Android only begins a *fresh* snoop log after the Developer-Options
  "Bluetooth HCI snoop log" switch is enabled **and** Bluetooth is cycled
  off/on. The capture procedure must enforce this to avoid empty/stale logs.

---

## 5. Module Boundaries

Each unit has one purpose, a well-defined output artifact, and can be inspected
independently.

| Unit | Purpose | Output artifact |
|------|---------|-----------------|
| `capture/` | Drive the phone over ADB: enable snoop log, cycle BT, trigger a real sync, extract the log via bugreport, grab logcat + GATT table | `captures/sync-NN.btsnoop`, `captures/logcat-NN.txt`, `captures/gatt-table.txt` |
| `apk/` | Pull the installed Huion APK off the device and decompile it | `apk/huion.apk`, `apk/jadx-out/` |
| `huion_notes/` | A small, tested btsnoop → GATT-ops parser that reduces thousands of HCI packets to just the Huion characteristic writes/notifications as readable frames | `huion_notes/extract_gatt.py`, `captures/frames-NN.jsonl` |
| `docs/` | The human-written protocol map synthesizing capture + source | `docs/.../protocol-map.md` |

---

## 6. Data Flow

```
plug phone (ADB/USB)
      │
      ▼
capture/ ──► captures/sync-NN.btsnoop  (one real offline sync)
      │            │
      │            ▼
      │     huion_notes/extract_gatt.py ──► captures/frames-NN.jsonl
      │            │   (thousands of HCI packets → handful of Huion GATT frames)
      ▼            │
apk/ ──► apk/jadx-out/  (decompiled decoder source)
                   │
                   ▼
   read decoder source + annotate each frame's meaning
                   │
                   ▼
            docs/.../protocol-map.md
```

The two branches (dynamic capture, static decompile) run in parallel and meet at
the annotation step, where decompiled meaning is attached to captured frames.

---

## 7. The Bind / Auth Gate (known risk)

This is the project's main feasibility wildcard. Because the app already bound
the notebook, the bind *handshake* may not re-fire during a plain sync. If the
dump is gated on a stored token, we expect to find it in one of two places:

- **(a)** replayed at sync start in the capture (visible as an early write), or
- **(b)** read from local app storage in the decompiled code
  (`SharedPreferences`, an SQLite DB, or a file).

The `huion_notes/` unit explicitly hunts for this. Because this model performs no
server-side check, any token we find is **replayable locally**, so a positive
finding is a surmountable obstacle rather than a blocker. If a plain sync never
exercises the bind path, a fallback is to capture an explicit unbind/re-bind
cycle to observe the full handshake.

---

## 8. Testing

The only substantial code in this phase is `huion_notes/extract_gatt.py`. It is
developed test-first against a small hand-crafted btsnoop fixture (known packets
in → known GATT frames out), because Sub-project B will depend on this parser to
interpret live notifications.

The protocol map itself is validated empirically: it must correctly predict the
GATT frames of a **second, independent** sync capture. Agreement means the model
is right; divergence points precisely at the under-specified field.

---

## 9. Out of Scope (YAGNI)

Deferred to Sub-project B, not designed here:

- The `bleak` Linux connection/extraction tool.
- Chunk reassembly *implementation* (we only document the scheme here).
- Stroke decoding *implementation* and SVG/PDF rendering.
- Any handling of the live-pen channel or USB HID.

---

## 10. Privacy / Legitimacy Note

All activity targets hardware the user owns, reading the user's own stored notes,
entirely on local devices over USB. No data is sent to Huion or any third party;
the explicit aim is to avoid the vendor app/cloud. This is interoperability
reverse engineering for personal data access.
