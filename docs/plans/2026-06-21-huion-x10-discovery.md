# Huion X10 Offline-Sync Discovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a byte-level protocol map of the Huion Note X10's offline-note BLE sync, sufficient to build a Linux extractor without further captures.

**Architecture:** Hybrid reverse engineering. Capture a real offline sync as an Android HCI snoop log (dynamic ground truth) and decompile the Huion APK (static ground truth), then cross-reference the two through a small, tested btsnoop→GATT-frame parser to annotate captured bytes with their meaning.

**Tech Stack:** `adb` (USB, no root), `jadx` (APK decompile), Python 3 stdlib only for `extract_gatt.py` (no third-party deps in discovery phase), Wireshark optional for manual inspection.

## Global Constraints

- Phone is **not rooted** → snoop log is extracted via `adb bugreport`, never a direct `/data/misc` pull.
- All work is **local over USB**. No artifact is sent off-device. Raw captures/APK are `.gitignore`d; only the derived `protocol-map.md` is committed.
- A fresh snoop log requires: enable Developer-Options "Bluetooth HCI snoop log" **then** cycle Bluetooth off/on, **before** the sync.
- `extract_gatt.py` uses **Python standard library only** (no `pip install`).
- Before each capture, the X10 must have **at least one fresh, known test page** unsynced, to give the decode a recognizable target.

---

### Task 1: Verify device access and enumerate the GATT table

**Files:**
- Create: `captures/gatt-table.txt` (gitignored — artifact)
- Create: `notes/device-facts.md` (committed — non-sensitive facts: Android version, package name, root status, log path scheme)

**Interfaces:**
- Produces: the Huion app package name (e.g. `com.huion.xxx`) and confirmation that ADB + USB debugging work. Consumed by Tasks 2 and 3.

- [ ] **Step 1: Confirm ADB sees the phone**

Run: `adb devices -l`
Expected: exactly one device listed as `device` (not `unauthorized` / `offline`). If `unauthorized`, accept the RSA prompt on the phone.

- [ ] **Step 2: Record device facts**

Run:
```bash
adb shell getprop ro.build.version.release   # Android version
adb shell which su >/dev/null 2>&1 && echo ROOTED || echo NON-ROOT
adb shell pm list packages | grep -i huion
```
Expected: an Android version string, `NON-ROOT`, and one or more `package:` lines containing `huion`. Record the exact package name.

- [ ] **Step 3: Enumerate the GATT table while bonded**

With the X10 powered on and bonded, dump the Bluetooth manager state:
```bash
adb shell dumpsys bluetooth_manager > captures/gatt-table.txt
```
Expected: file contains the X10's MAC address and bond state `BONDED`. (Full service/characteristic UUIDs may only appear during an active connection — Task 3 captures those authoritatively; this is a first look.)

- [ ] **Step 4: Write device-facts note and commit**

Create `notes/device-facts.md` with the Android version, package name, root status, and the snoop-log path scheme expected (bugreport route for non-root). Then:
```bash
git add notes/device-facts.md
git commit -m "docs: record X10/phone device facts for discovery"
```

---

### Task 2: Pull and decompile the Huion APK

**Files:**
- Create: `apk/huion.apk` (gitignored)
- Create: `apk/jadx-out/` (gitignored)
- Create: `notes/static-huion_notes.md` (committed — class/method pointers, no proprietary code copied)

**Interfaces:**
- Consumes: package name from Task 1.
- Produces: a decompiled source tree and a `notes/static-huion_notes.md` listing the candidate BLE classes (GATT callbacks, UUID constants, the stroke decoder). Consumed by Tasks 5–7.

- [ ] **Step 1: Locate and pull the APK**

Run (substitute the package name from Task 1):
```bash
adb shell pm path <pkg> | sed 's/package://' | tr -d '\r'
adb pull <path-from-above> apk/huion.apk
```
Expected: `apk/huion.apk` exists and is non-trivial in size (`ls -la apk/huion.apk`). If `pm path` returns multiple split APKs, pull `base.apk`.

- [ ] **Step 2: Decompile with jadx**

Run:
```bash
jadx -d apk/jadx-out apk/huion.apk 2>&1 | tail -5
```
Expected: `apk/jadx-out/sources/` populated. Partial decompile errors are acceptable (note them); a total failure is not.

- [ ] **Step 3: Locate BLE and UUID code**

Run:
```bash
grep -rilE 'BluetoothGatt|onCharacteristicChanged|UUID' apk/jadx-out/sources | head -30
grep -rinE '0000[0-9a-fA-F]{4}-0000-1000-8000-00805f9b34fb|fff[0-9a-fA-F]' apk/jadx-out/sources | head -30
```
Expected: a shortlist of classes handling GATT callbacks and UUID constants. Record the most promising file paths.

- [ ] **Step 4: Write static-analysis pointers and commit**

In `notes/static-huion_notes.md`, record (as *pointers*, not pasted proprietary code): the class handling `onCharacteristicChanged`, any UUID constant definitions, and the suspected stroke-decode method names. Then:
```bash
git add notes/static-huion_notes.md
git commit -m "docs: map Huion APK BLE classes and UUID constants"
```

---

### Task 3: Capture a real offline sync

**Files:**
- Create: `captures/sync-01.btsnoop` (gitignored)
- Create: `captures/logcat-01.txt` (gitignored)
- Create: `notes/capture-log.md` (committed — what test page was drawn, timestamps, observations)

**Interfaces:**
- Produces: `captures/sync-01.btsnoop`, a btsnoop log containing one full offline sync. Consumed by Tasks 4–8.

- [ ] **Step 1: Stage a known test page**

On the X10, draw a recognizable test page (e.g. a large "L" shape in one corner, then a horizontal line). Record exactly what was drawn and where in `notes/capture-log.md`. Ensure the app shows pages "waiting to upload".

- [ ] **Step 2: Arm a fresh snoop log**

Enable Developer-Options → "Bluetooth HCI snoop log" (verify: `adb shell settings get secure bluetooth_hci_log` or the Developer Options UI), then cycle Bluetooth:
```bash
adb shell svc bluetooth disable && adb shell svc bluetooth enable
```
Expected: Bluetooth turns off then on. This starts a clean log.

- [ ] **Step 3: Clear logcat, then trigger the sync**

Run:
```bash
adb logcat -c
```
Then in the Huion app, perform the offline **Synchronize** and wait for it to report complete. Note the wall-clock start/end in `notes/capture-log.md`.

- [ ] **Step 4: Capture logcat**

Run:
```bash
adb logcat -d > captures/logcat-01.txt
```
Expected: file contains Huion app lines around the sync window (grep the package name to confirm).

- [ ] **Step 5: Extract the snoop log via bugreport**

Run:
```bash
adb bugreport captures/bugreport-01.zip
```
Then extract the btsnoop from inside the zip (path is typically `FS/data/misc/bluetooth/logs/btsnoop_hci.log` within the report):
```bash
unzip -o captures/bugreport-01.zip 'FS/data/misc/bluetooth/logs/*' -d captures/bugreport-01/
cp captures/bugreport-01/FS/data/misc/bluetooth/logs/btsnoop_hci.log captures/sync-01.btsnoop
```
Expected: `captures/sync-01.btsnoop` exists and begins with the magic bytes `btsnoop\0` (`xxd captures/sync-01.btsnoop | head -1`). If the path differs, locate it: `unzip -l captures/bugreport-01.zip | grep -i btsnoop`.

- [ ] **Step 6: Commit the capture log**

```bash
git add notes/capture-log.md
git commit -m "docs: log first offline-sync capture (test page + timings)"
```

---

### Task 4: Build the btsnoop → GATT-frame parser (TDD)

**Files:**
- Create: `huion_notes/extract_gatt.py`
- Test: `huion_notes/test_extract_gatt.py`

**Interfaces:**
- Consumes: a btsnoop file path.
- Produces:
  - `parse_btsnoop(data: bytes) -> list[Record]` where `Record` is a dataclass `(timestamp_us: int, sent: bool, payload: bytes)` — `payload` is the HCI packet body (H4, datalink 1002).
  - `extract_att_frames(records: list[Record]) -> list[Frame]` where `Frame` is a dataclass `(timestamp_us: int, direction: str, opcode: str, handle: int, value: bytes)`; `direction` is `"tx"` (host→controller) or `"rx"`; `opcode` is one of `"write_command"`, `"write_request"`, `"notification"`, `"indication"`.
  - CLI: `python huion_notes/extract_gatt.py <file.btsnoop>` prints one JSON object per frame (JSONL) with hex-encoded `value`.
- Consumed by: Tasks 5–8.

- [ ] **Step 1: Write the failing tests**

Create `huion_notes/test_extract_gatt.py`. The helper builds real layered packets so the fixture is self-documenting:

```python
import struct
from huion_notes.extract_gatt import parse_btsnoop, extract_att_frames

BTSNOOP_HEADER = b"btsnoop\x00" + struct.pack(">II", 1, 1002)  # version 1, datalink H4/1002

def _record(payload: bytes, *, sent: bool, ts: int = 0) -> bytes:
    flags = 0 if sent else 1  # btsnoop flag bit0: 0=sent(host->ctrl), 1=received
    return struct.pack(">IIIIq", len(payload), len(payload), flags, 0, ts) + payload

def _acl(att_pdu: bytes, *, handle: int = 0x0040) -> bytes:
    l2cap = struct.pack("<HH", len(att_pdu), 0x0004) + att_pdu       # ATT CID = 0x0004
    acl = struct.pack("<HH", handle, len(l2cap)) + l2cap            # ACL: handle+flags, length
    return b"\x02" + acl                                           # H4 type 0x02 = ACL

def test_parse_btsnoop_reads_records():
    pdu = b"\x1b" + struct.pack("<H", 0x0025) + b"\xde\xad"  # notification handle 0x25
    data = BTSNOOP_HEADER + _record(_acl(pdu), sent=False, ts=123)
    records = parse_btsnoop(data)
    assert len(records) == 1
    assert records[0].sent is False
    assert records[0].timestamp_us == 123

def test_extracts_notification_frame():
    pdu = b"\x1b" + struct.pack("<H", 0x0025) + b"\xde\xad"
    data = BTSNOOP_HEADER + _record(_acl(pdu), sent=False)
    frames = extract_att_frames(parse_btsnoop(data))
    assert len(frames) == 1
    f = frames[0]
    assert f.opcode == "notification"
    assert f.direction == "rx"
    assert f.handle == 0x0025
    assert f.value == b"\xde\xad"

def test_extracts_write_command_frame():
    pdu = b"\x52" + struct.pack("<H", 0x0023) + b"\x01\x02\x03"  # write command
    data = BTSNOOP_HEADER + _record(_acl(pdu), sent=True)
    frames = extract_att_frames(parse_btsnoop(data))
    assert frames[0].opcode == "write_command"
    assert frames[0].direction == "tx"
    assert frames[0].value == b"\x01\x02\x03"

def test_ignores_non_att_l2cap():
    sig = struct.pack("<HH", 4, 0x0005) + b"\x00\x00\x00\x00"  # CID 0x0005 (signaling), not ATT
    acl = struct.pack("<HH", 0x0040, len(sig)) + sig
    data = BTSNOOP_HEADER + _record(b"\x02" + acl, sent=False)
    assert extract_att_frames(parse_btsnoop(data)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest huion_notes/test_extract_gatt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'huion_notes.extract_gatt'` (or import error).

- [ ] **Step 3: Implement the parser**

Create `huion_notes/__init__.py` (empty) and `huion_notes/extract_gatt.py`:

```python
"""Extract ATT (GATT) frames from an Android btsnoop HCI log (datalink 1002, H4)."""
from __future__ import annotations
import json
import struct
import sys
from dataclasses import dataclass

BTSNOOP_MAGIC = b"btsnoop\x00"
_OPCODES = {
    0x52: "write_command",
    0x12: "write_request",
    0x1b: "notification",
    0x1d: "indication",
}

@dataclass
class Record:
    timestamp_us: int
    sent: bool
    payload: bytes  # HCI packet body including the H4 type byte

@dataclass
class Frame:
    timestamp_us: int
    direction: str  # "tx" or "rx"
    opcode: str
    handle: int
    value: bytes

def parse_btsnoop(data: bytes) -> list[Record]:
    if data[:8] != BTSNOOP_MAGIC:
        raise ValueError("not a btsnoop file")
    # 8 magic + 4 version + 4 datalink
    off = 16
    records: list[Record] = []
    while off + 24 <= len(data):
        orig_len, incl_len, flags, _drops, ts = struct.unpack_from(">IIIIq", data, off)
        off += 24
        payload = data[off : off + incl_len]
        off += incl_len
        records.append(Record(timestamp_us=ts, sent=(flags & 1) == 0, payload=payload))
    return records

def extract_att_frames(records: list[Record]) -> list[Frame]:
    frames: list[Frame] = []
    for r in records:
        p = r.payload
        if not p or p[0] != 0x02:  # H4 type 0x02 = ACL data
            continue
        if len(p) < 9:
            continue
        # ACL header: handle+flags (2), length (2) -> then L2CAP
        l2cap = p[5:]
        if len(l2cap) < 4:
            continue
        l2_len, cid = struct.unpack_from("<HH", l2cap, 0)
        if cid != 0x0004:  # ATT channel only
            continue
        att = l2cap[4 : 4 + l2_len]
        if len(att) < 3:
            continue
        op = att[0]
        if op not in _OPCODES:
            continue
        handle = struct.unpack_from("<H", att, 1)[0]
        frames.append(
            Frame(
                timestamp_us=r.timestamp_us,
                direction="tx" if r.sent else "rx",
                opcode=_OPCODES[op],
                handle=handle,
                value=att[3:],
            )
        )
    return frames

def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: extract_gatt.py <file.btsnoop>", file=sys.stderr)
        return 2
    with open(argv[1], "rb") as fh:
        data = fh.read()
    for f in extract_att_frames(parse_btsnoop(data)):
        print(json.dumps({
            "ts": f.timestamp_us,
            "dir": f.direction,
            "op": f.opcode,
            "handle": f.handle,
            "hex": f.value.hex(),
        }))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest huion_notes/test_extract_gatt.py -v`
Expected: 4 passed.

- [ ] **Step 5: Smoke-test against the real capture**

Run: `python huion_notes/extract_gatt.py captures/sync-01.btsnoop | head -40`
Expected: JSONL frames showing a handful of distinct handles with `write_command`/`notification` ops. If empty, the capture may use ACL fragmentation or a different datalink — note it; reassembly handling becomes a follow-up step.

- [ ] **Step 6: Commit**

```bash
git add huion_notes/__init__.py huion_notes/extract_gatt.py huion_notes/test_extract_gatt.py
git commit -m "feat: add tested btsnoop->GATT ATT-frame extractor"
```

---

### Task 5: Identify the sync service, characteristics, and dump command

**Files:**
- Create: `docs/offline-note-protocol.md` (committed) — sections: Service/Characteristics, Dump Command
- Create: `captures/frames-01.jsonl` (gitignored)

**Interfaces:**
- Consumes: `extract_gatt.py` (Task 4), `captures/sync-01.btsnoop` (Task 3), `notes/static-huion_notes.md` (Task 2).
- Produces: the Service/Characteristics and Dump-Command sections of `protocol-map.md`. Consumed by Tasks 6–8.

- [ ] **Step 1: Reduce the capture to frames**

Run: `python huion_notes/extract_gatt.py captures/sync-01.btsnoop > captures/frames-01.jsonl`
Expected: a JSONL file. Inspect the distinct handles used: `python -c "import json,sys,collections; c=collections.Counter(json.loads(l)['handle'] for l in open('captures/frames-01.jsonl')); print(c)"`.

- [ ] **Step 2: Identify command vs. notify characteristics**

The handle that receives your `write_command`/`write_request` frames near the sync start is the **command** characteristic; the handle producing the burst of `notification` frames is the **data/notify** characteristic. Cross-reference these handles to UUIDs in `captures/gatt-table.txt` and to the UUID constants found in Task 2.

- [ ] **Step 3: Isolate the dump-trigger command**

The first `tx` write that immediately precedes the notification burst is the **dump command** candidate. Record its exact bytes (the `hex` field). Confirm against the decompiled code: find where the app builds that write payload (search `jadx-out` for the opcode byte / characteristic UUID).

- [ ] **Step 4: Write the two sections and commit**

In `docs/offline-note-protocol.md`, fill in Service/Characteristics (UUIDs + handles + roles) and Dump Command (exact trigger bytes, with the decompiled method that constructs it cited by path). Then:
```bash
git add docs/offline-note-protocol.md
git commit -m "docs(protocol): map sync service, characteristics, dump command"
```

---

### Task 6: Decode the chunk framing and stroke payload

**Files:**
- Modify: `docs/offline-note-protocol.md` — add sections: Chunk Framing, Stroke Payload

**Interfaces:**
- Consumes: `captures/frames-01.jsonl` (Task 5), the stroke-decode method located in Task 2.
- Produces: the Chunk Framing and Stroke Payload sections. Consumed by Task 8 and all of Sub-project B.

- [ ] **Step 1: Read the decompiled decoder**

Open the suspected stroke-decode method in `apk/jadx-out`. Identify how it reads the notification stream: any length prefix, sequence/index field, page-marker opcode, and the per-point fields (X, Y, pressure, pen-up/down). Record the field order, sizes, and endianness it expects.

- [ ] **Step 2: Confirm framing against captured notifications**

Walk the `rx` notification `hex` values in `captures/frames-01.jsonl` and confirm the decompiled framing holds: e.g. if the code expects a 2-byte length prefix, the notification lengths should agree. Identify the end-of-transfer marker (a sentinel notification or a final status write).

- [ ] **Step 3: Cross-check geometry against the known test page**

Decode a few points by hand using the layout from Step 1 and confirm the coordinate trend matches the test page drawn in Task 3 (e.g. the "L" corner produces a cluster of points at one extreme of the X/Y range). This is the correctness oracle.

- [ ] **Step 4: Write the two sections and commit**

Document Chunk Framing (reassembly rules, sentinel) and Stroke Payload (exact field layout table: offset, size, type, meaning, endianness) in `protocol-map.md`. Then:
```bash
git add docs/offline-note-protocol.md
git commit -m "docs(protocol): document chunk framing and stroke payload layout"
```

---

### Task 7: Characterize the bind / auth gate

**Files:**
- Modify: `docs/offline-note-protocol.md` — add section: Bind / Auth Gate

**Interfaces:**
- Consumes: `captures/frames-01.jsonl`, `notes/static-huion_notes.md`, the decompiled app-storage code.
- Produces: the Bind/Auth section — whether a token gates the dump, where it is stored, and whether it is replayable. Consumed by Sub-project B.

- [ ] **Step 1: Look for an auth exchange before the dump**

In `captures/frames-01.jsonl`, inspect every `tx`/`rx` frame *before* the dump command. A challenge/response or a fixed token write appearing there indicates a gate. Record any such bytes.

- [ ] **Step 2: Trace the token in the decompiled code**

Search `jadx-out` for where the bind credential is stored and read: `SharedPreferences`, an SQLite DB, or a file under the app's data dir. Determine whether the value is static (replayable) or derived per-session (challenge/response).

- [ ] **Step 3: Decide the gate verdict**

Conclude one of: (a) **no gate** — dump works on a bare connection; (b) **static token gate** — record the token source and that it is replayable; (c) **dynamic challenge** — record the algorithm inputs. If a plain sync never exercised binding, note that a re-bind capture is the fallback to observe the full handshake.

- [ ] **Step 4: Write the section and commit**

```bash
git add docs/offline-note-protocol.md
git commit -m "docs(protocol): characterize bind/auth gate and replayability"
```

---

### Task 8: Validate the map against a second independent capture

**Files:**
- Create: `captures/sync-02.btsnoop`, `captures/frames-02.jsonl` (gitignored)
- Modify: `docs/offline-note-protocol.md` — add section: Validation

**Interfaces:**
- Consumes: the complete `protocol-map.md` (Tasks 5–7), the capture procedure (Task 3), `extract_gatt.py` (Task 4).
- Produces: a validated protocol map — the definition of done for Sub-project A.

- [ ] **Step 1: Capture a second, different sync**

Repeat Task 3's procedure with a *different* known test page (record it). Produce `captures/sync-02.btsnoop`, then:
```bash
python huion_notes/extract_gatt.py captures/sync-02.btsnoop > captures/frames-02.jsonl
```

- [ ] **Step 2: Predict before you look**

Using `protocol-map.md` alone, predict: the dump command bytes, the notify handle, and the decoded shape of the second test page. Write the predictions down first.

- [ ] **Step 3: Compare predictions to reality**

Check predictions against `captures/frames-02.jsonl` and a hand-decode of a few points. Every divergence marks an under-specified field — fix that section of the map.

- [ ] **Step 4: Mark the map validated and commit**

Add a Validation section noting the second capture confirmed the map (or listing the corrections made). Then:
```bash
git add docs/offline-note-protocol.md
git commit -m "docs(protocol): validate map against second independent capture"
```

**Definition of done for Sub-project A:** `protocol-map.md` predicts an independent capture. Sub-project B (the `bleak` Linux extractor) is then specced from this map in its own brainstorming → plan cycle.
