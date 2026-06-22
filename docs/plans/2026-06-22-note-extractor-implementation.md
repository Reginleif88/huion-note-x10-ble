# Huion Note X10 — Linux Note Extractor (Sub-project B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a device-free, unit-tested core plus a thin live BLE transport so a Linux CLI (`python -m huion_notes ...`) can dump **all** stored offline pages from the X10 and decode each to SVG + PNG + JSON, with no Huion app and no cloud.

**Architecture:** A pure pipeline — `frames → (auth) → codec → render` — operates on byte frames regardless of origin (a `.btsnoop` capture for `decode`, or live BLE notifications for `dump`). `session.py` orchestrates the live multi-page handshake against a duck-typed `Transport`; the only device-dependent module is `transport.py`, which wraps the existing pen driver's `BLEConnection`. Everything except `transport.py` is unit-tested with synthetic vectors.

**Tech Stack:** Python 3.13, standard library only for the pure core. `dbus_fast` (live transport only, imported lazily). ImageMagick `magick` (optional, for PNG). Tests: stdlib `unittest`.

## Global Constraints

These apply to **every** task. Values are copied verbatim from `docs/specs/2026-06-22-note-extractor-design.md` and `docs/offline-note-protocol.md` (§10 is the multi-page protocol, reverse-engineered from `captures/sync-multipage.btsnoop` + the app's `HiBluetoothManager`).

- **No `pytest`.** Use stdlib `unittest`. Run the suite with: `python3 -m unittest discover -s huion_notes -p 'test_*.py'`.
- **Pure core must import without `dbus_fast`.** `dbus_fast` is not installed in the dev/test environment. `frames.py`, `auth.py`, `codec.py`, `render.py`, `errors.py`, `session.py`, and the top of `cli.py` must import cleanly with no `dbus_fast` (direct or transitive). `transport.py`, `huion_ble_driver`, and `_find_tablet_mac` are imported **lazily inside the `dump` code path only**.
- **Framing:** every command is `cd <op> 08 <a> <b> <c> <d> ed` (8 bytes), written to characteristic `char002a` (value handle `0x002b`). Responses/data arrive as notifications/indications on `char0026` (value handle `0x0027`) and indications on `char002a`. Start byte `0xCD`, end byte `0xED`.
- **6-byte point record:** `x = b0|b1<<8`, `y = b2|b3<<8`, `press = b4|(b5&0x1f)<<8` (13-bit), `status = b5>>5` (0 = pen up). Points are parsed **per packet**: `N = (len-5)//6`, remainder (incl. trailing checksum byte) discarded. This applies identically to `0x87` stream packets and `0x88` retransmit packets.
- **Device limits** from `MAX_DATA (0x95)`: `MAX_X = b5<<16|b4<<8|b3`, `MAX_Y = b8<<16|b7<<8|b6`, `MAX_PRESS = b10<<8|b9`. Defaults if absent: `MAX_X=28200.0`, `MAX_Y=37400.0`, `MAX_PRESS=8191.0`.
- **Multi-page protocol (§10):**
  - Per page `p`: send `request_page_data(p, sub=0)` → `cd 86 08 <p_lo> <p_hi> <sub> 00 ed`.
  - Device replies `cd 86 05 <count_lo> <count_hi>` → `count` = number of packets (u16 LE). **`count == 0` ⇒ page empty ⇒ stop the page loop.**
  - Device streams `count` × `RETURN_OFFLINE_DATA (0x87)`: `cd 87 7e <seq_lo> <seq_hi> <points> <checksum>`, **`seq = 1..count`, reset per page**; stream complete when the packet with `seq == count` arrives.
  - Missing `seq i` ⇒ `build_get_page_package(p, i)` → `cd 88 08 <p_lo> <p_hi> <i_lo> <i_hi> ed`; reply `cd 88 7e <i_lo> <i_hi> <points> <checksum>` (decode like `0x87`, index at bytes 3–4).
  - Loop `p = 0,1,2,…` until `count == 0` (cap at a safety `max_pages`).
- **READ-ONLY — never modify the device.** The extractor must **never** send `DELETE_PAGE (0x8b)` or `CLEAR_CACHE (0x8c)`. The official app deletes pages after syncing; we do not.
- **Page mapping:** origin top-left, **no axis flip / no swap** (X10 is a non-A4 device).
- **Auth (keyless, local):** `r1=((a+b)<<2)%255`, `r2=((b+c)<<2)%255`, `r3=((c+10)<<2)%255`. PIN encoding: `e[i] = ord(pin[i]) + offset[i]` with offsets `(104,117,105,111,110,35)` = `ascii("huion#")`.
- **Output:** `OUT/page-NN.{svg,png,json}`, one set per page, `NN` = zero-padded page index. JSON schema exactly: `{"page", "max_x", "max_y", "max_press", "strokes":[[{"x","y","press","pen_down"}]]}`.
- **Privacy:** `captures/` and decoded pages stay gitignored. Automated tests use **synthetic** byte vectors only; any test touching `captures/*.btsnoop` must `skipUnless` it exists.
- **Live use requires patched BlueZ** (see README) — relevant only to manual `dump` verification, not to tests.
- **Frequent commits:** one commit per task, only after its tests pass.

## File Structure

| File | Responsibility | dbus_fast? |
|------|----------------|-----------|
| `huion_notes/frames.py` | Capture parsing (btsnoop→ATT) **and** Huion `cd…ed` framing, `OrderCode`, command builders, count parser | no |
| `huion_notes/auth.py` | Challenge→response, PIN encoding, auth frame builders | no |
| `huion_notes/codec.py` | `0x87`/`0x88` packets → points → strokes → `Page`; `MAX_DATA`; per-page split for replay | no |
| `huion_notes/render.py` | `Page` → SVG / JSON / PNG (PNG via `magick` subprocess) | no |
| `huion_notes/errors.py` | Shared exceptions | no |
| `huion_notes/session.py` | Live multi-page dump orchestration against a duck-typed `Transport` | no |
| `huion_notes/transport.py` | Live BLE transport wrapping `BLEConnection` | **yes (lazy)** |
| `huion_notes/cli.py` | `decode` / `dump` subcommands, per-page output, flags | yes (lazy, dump only) |
| `huion_notes/__main__.py` | `python -m huion_notes` entry | no |

Retired during the refactor: `huion_notes/extract_gatt.py` (→ `frames.py`), `huion_notes/decode_offline.py` (→ `codec.py` + `render.py`), and their tests.

---

## Task 1: `frames.py` — capture parsing + Huion framing + multi-page builders

Foundational frame layer. Moves `extract_gatt.py`'s btsnoop/ATT parsing in verbatim (renaming `Frame`→`AttFrame`) and adds the device protocol: `OrderCode`, `parse_huion_frame`, command builders (including the multi-page `request_page_data`, `build_get_page_package`) and the `parse_offline_count` response parser.

**Files:**
- Create: `huion_notes/frames.py`
- Create: `huion_notes/test_frames.py`
- Modify: `huion_notes/decode_offline.py:31` (repoint import to `frames`)
- Delete: `huion_notes/extract_gatt.py`, `huion_notes/test_extract_gatt.py`

**Interfaces:**
- Produces:
  - `Record(timestamp_us:int, sent:bool, payload:bytes)`
  - `AttFrame(timestamp_us:int, direction:str, opcode:str, handle:int, value:bytes)`
  - `parse_btsnoop(data:bytes) -> list[Record]`
  - `extract_att_frames(records:list[Record]) -> list[AttFrame]`
  - `class OrderCode` (int constants)
  - `HuionFrame(op:int, length:int, payload:bytes, raw:bytes)`
  - `parse_huion_frame(value:bytes) -> HuionFrame | None`
  - `build_command(op:int, a=0, b=0, c=0, d=0) -> bytes`
  - `request_max_info() -> bytes`, `request_set_many_packet_distance() -> tuple[bytes,bytes]`
  - `request_page_data(page:int=0, sub:int=0) -> bytes`
  - `build_get_page_package(page:int, idx:int) -> bytes`
  - `parse_offline_count(value:bytes) -> int | None`
  - `heart_beat() -> bytes`
  - `START=0xCD`, `END=0xED`

- [ ] **Step 1: Write the failing tests**

Create `huion_notes/test_frames.py` (vectors below are real bytes from `sync-multipage.btsnoop`):

```python
"""Tests for the frame layer (capture parsing + Huion device framing). Stdlib unittest.

Run: python3 -m unittest huion_notes.test_frames -v
"""
import struct
import unittest

from huion_notes.frames import (
    parse_btsnoop, extract_att_frames,
    OrderCode, parse_huion_frame, build_command,
    request_max_info, request_set_many_packet_distance, request_page_data,
    build_get_page_package, parse_offline_count,
)

BTSNOOP_HEADER = b"btsnoop\x00" + struct.pack(">II", 1, 1002)  # version 1, datalink H4/1002


def _record(payload, *, sent, ts=0):
    flags = 0 if sent else 1  # bit0: 0=sent(host->ctrl), 1=received
    return struct.pack(">IIIIq", len(payload), len(payload), flags, 0, ts) + payload


def _acl(att_pdu, *, handle=0x0040):
    l2cap = struct.pack("<HH", len(att_pdu), 0x0004) + att_pdu  # ATT CID = 0x0004
    acl = struct.pack("<HH", handle, len(l2cap)) + l2cap
    return b"\x02" + acl                                        # H4 type 0x02 = ACL


class CaptureParsingTests(unittest.TestCase):
    def test_parse_btsnoop_reads_records(self):
        pdu = b"\x1b" + struct.pack("<H", 0x0027) + b"\xde\xad"
        data = BTSNOOP_HEADER + _record(_acl(pdu), sent=False, ts=123)
        records = parse_btsnoop(data)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].sent)
        self.assertEqual(records[0].timestamp_us, 123)

    def test_rejects_non_btsnoop(self):
        with self.assertRaises(ValueError):
            parse_btsnoop(b"NOPE" + b"\x00" * 32)

    def test_extracts_notification_frame(self):
        pdu = b"\x1b" + struct.pack("<H", 0x0027) + b"\xde\xad"
        frames = extract_att_frames(parse_btsnoop(BTSNOOP_HEADER + _record(_acl(pdu), sent=False)))
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual((f.opcode, f.direction, f.handle, f.value), ("notification", "rx", 0x0027, b"\xde\xad"))

    def test_extracts_write_command_frame(self):
        pdu = b"\x52" + struct.pack("<H", 0x002b) + b"\x01\x02\x03"
        frames = extract_att_frames(parse_btsnoop(BTSNOOP_HEADER + _record(_acl(pdu), sent=True)))
        self.assertEqual((frames[0].opcode, frames[0].direction, frames[0].value), ("write_command", "tx", b"\x01\x02\x03"))

    def test_ignores_non_att_l2cap(self):
        sig = struct.pack("<HH", 4, 0x0005) + b"\x00\x00\x00\x00"  # CID 0x0005 signaling
        acl = struct.pack("<HH", 0x0040, len(sig)) + sig
        data = BTSNOOP_HEADER + _record(b"\x02" + acl, sent=False)
        self.assertEqual(extract_att_frames(parse_btsnoop(data)), [])


class HuionFramingTests(unittest.TestCase):
    def test_parse_huion_frame_fields(self):
        fr = parse_huion_frame(bytes([0xCD, 0x87, 0x7E, 0x01, 0x00]) + b"\x00" * 121)
        self.assertEqual((fr.op, fr.length), (OrderCode.RETURN_OFFLINE_DATA, 0x7E))

    def test_parse_huion_frame_rejects_garbage(self):
        self.assertIsNone(parse_huion_frame(b"\x00\x01"))
        self.assertIsNone(parse_huion_frame(b"\xaa\x81\x08"))  # wrong start byte

    def test_build_command_shape(self):
        self.assertEqual(request_max_info().hex(), "cd950800000000ed")

    def test_request_page_data(self):
        self.assertEqual(request_page_data(0, 0).hex(), "cd860800000000ed")
        self.assertEqual(request_page_data(1, 0).hex(), "cd860801000000ed")  # page 1, from capture

    def test_build_get_page_package(self):
        # page 0, idx 0x0114=276  -> cd 88 08 00 00 14 01 ed  (from capture)
        self.assertEqual(build_get_page_package(0, 276).hex(), "cd880800001401ed")
        # page 1, idx 0x0163=355  -> cd 88 08 01 00 63 01 ed  (from capture)
        self.assertEqual(build_get_page_package(1, 355).hex(), "cd880801006301ed")

    def test_parse_offline_count(self):
        self.assertEqual(parse_offline_count(bytes.fromhex("cd86051211")), 4370)  # 0x1112
        self.assertEqual(parse_offline_count(bytes.fromhex("cd86051200")), 18)
        self.assertEqual(parse_offline_count(bytes.fromhex("cd86050000")), 0)
        self.assertIsNone(parse_offline_count(bytes.fromhex("cd950b286e00189200ff1f")))  # not 0x86

    def test_set_many_packet_distance_pair(self):
        d1, d2 = request_set_many_packet_distance()
        self.assertEqual((d1.hex(), d2.hex()), ("cd960801030000ed", "cd960803020000ed"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest huion_notes.test_frames -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'huion_notes.frames'`.

- [ ] **Step 3: Create `huion_notes/frames.py`**

```python
"""Frame layer for the Huion Note X10 offline protocol.

Two concerns, both about *frame bytes*:
  (A) Capture parsing — an Android btsnoop HCI log -> ATT characteristic values
      (the `decode` replay path). Moved verbatim from extract_gatt.py.
  (B) Huion device framing — the `cd <op> <len> ... ed` structure, OrderCode
      opcode constants, command builders, and the offline-count response parser
      for the live multi-page `dump` path.

Pure and device-free; no dbus_fast. See docs/offline-note-protocol.md (§2,3,7,10).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# ─── (A) Capture parsing: btsnoop -> ATT frames ──────────────────────────────
# Android datalink 1002 (H4 UART): each record payload begins with a one-byte H4
# type indicator (0x02 = ACL). Layering: btsnoop record -> H4 -> HCI ACL ->
# L2CAP -> ATT. Assumes ATT PDUs are not fragmented across ACL packets.

BTSNOOP_MAGIC = b"btsnoop\x00"

_ATT_OPCODES = {
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
class AttFrame:
    timestamp_us: int
    direction: str  # "tx" (host->controller) or "rx"
    opcode: str
    handle: int
    value: bytes


def parse_btsnoop(data: bytes) -> list[Record]:
    """Parse a btsnoop file into HCI records."""
    if data[:8] != BTSNOOP_MAGIC:
        raise ValueError("not a btsnoop file (bad magic)")
    off = 16  # 8 magic + 4 version + 4 datalink
    records: list[Record] = []
    while off + 24 <= len(data):
        orig_len, incl_len, flags, _drops, ts = struct.unpack_from(">IIIIq", data, off)
        off += 24
        payload = data[off : off + incl_len]
        off += incl_len
        records.append(Record(timestamp_us=ts, sent=(flags & 1) == 0, payload=payload))
    return records


def extract_att_frames(records: list[Record]) -> list[AttFrame]:
    """Reduce HCI records to ATT writes/notifications/indications."""
    frames: list[AttFrame] = []
    for r in records:
        p = r.payload
        if not p or p[0] != 0x02:  # H4 type 0x02 = ACL data
            continue
        if len(p) < 9:  # 1 H4 + 4 ACL hdr + 4 L2CAP hdr minimum
            continue
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
        if op not in _ATT_OPCODES:
            continue
        handle = struct.unpack_from("<H", att, 1)[0]
        frames.append(
            AttFrame(
                timestamp_us=r.timestamp_us,
                direction="tx" if r.sent else "rx",
                opcode=_ATT_OPCODES[op],
                handle=handle,
                value=att[3:],
            )
        )
    return frames


# ─── (B) Huion device framing ────────────────────────────────────────────────
START = 0xCD
END = 0xED


class OrderCode:
    """Opcode constants (from the app's OrderCode.java; see protocol §3, §10)."""
    HEART_BEAT = 0x80
    VERIFY_CONNECT = 0x81
    VERIFY_RESULT = 0x82
    VERIFY_PWD = 0x83
    MODE = 0x84
    CURRENT_PAGE = 0x85
    REQUEST_OFFLINE_DATA = 0x86
    RETURN_OFFLINE_DATA = 0x87
    GET_PAGE_PACKAGE = 0x88     # also the retransmit channel (§10)
    NEXT_PAGE = 0x8A
    DELETE_PAGE = 0x8B          # never sent — extractor is read-only
    CLEAR_CACHE = 0x8C          # never sent — extractor is read-only
    DEVICE_NAME = 0x91
    GET_PWD = 0x93
    MAX_DATA = 0x95
    SET_MANY_PACKET_DISTANCE = 0x96
    VERSION = 0xC9


@dataclass
class HuionFrame:
    op: int
    length: int
    payload: bytes  # bytes after the length byte (best-effort; not length-trimmed)
    raw: bytes      # the full characteristic value (cd ... ed/checksum)


def parse_huion_frame(value: bytes) -> "HuionFrame | None":
    """Parse a characteristic value into a HuionFrame, or None if not a frame."""
    if len(value) < 3 or value[0] != START:
        return None
    return HuionFrame(op=value[1], length=value[2], payload=bytes(value[3:]), raw=bytes(value))


def build_command(op: int, a: int = 0, b: int = 0, c: int = 0, d: int = 0) -> bytes:
    """Build a fixed 8-byte command frame: cd <op> 08 a b c d ed (protocol §7)."""
    return bytes([START, op, 0x08, a & 0xFF, b & 0xFF, c & 0xFF, d & 0xFF, END])


def request_max_info() -> bytes:
    return build_command(OrderCode.MAX_DATA, 0, 0, 0, 0)


def request_set_many_packet_distance() -> tuple[bytes, bytes]:
    return (
        build_command(OrderCode.SET_MANY_PACKET_DISTANCE, 1, 3, 0, 0),
        build_command(OrderCode.SET_MANY_PACKET_DISTANCE, 3, 2, 0, 0),
    )


def request_page_data(page: int = 0, sub: int = 0) -> bytes:
    """REQUEST_OFFLINE_DATA — page download trigger:
    cd 86 08 <page_lo> <page_hi> <sub> 00 ed  (sub is the app's i2, normally 0)."""
    return build_command(
        OrderCode.REQUEST_OFFLINE_DATA, page & 0xFF, (page >> 8) & 0xFF, sub & 0xFF, 0
    )


def build_get_page_package(page: int, idx: int) -> bytes:
    """GET_PAGE_PACKAGE — retransmit one packet by index (§10):
    cd 88 08 <page_lo> <page_hi> <idx_lo> <idx_hi> ed."""
    return bytes([
        START, OrderCode.GET_PAGE_PACKAGE, 0x08,
        page & 0xFF, (page >> 8) & 0xFF, idx & 0xFF, (idx >> 8) & 0xFF, END,
    ])


def parse_offline_count(value: bytes) -> "int | None":
    """Parse a REQUEST_OFFLINE_DATA response `cd 86 05 <count_lo> <count_hi>` ->
    packet count (u16 LE). Returns None if not such a frame."""
    if len(value) >= 5 and value[0] == START and value[1] == OrderCode.REQUEST_OFFLINE_DATA:
        return value[3] | (value[4] << 8)
    return None


def heart_beat() -> bytes:
    return build_command(OrderCode.HEART_BEAT, 0, 0, 0, 0)
```

- [ ] **Step 4: Repoint `decode_offline.py`'s import, then delete the old extractor**

Edit `huion_notes/decode_offline.py` line 31, from:

```python
from huion_notes.extract_gatt import parse_btsnoop, extract_att_frames
```

to:

```python
from huion_notes.frames import parse_btsnoop, extract_att_frames
```

Then delete the superseded files:

```bash
git rm huion_notes/extract_gatt.py huion_notes/test_extract_gatt.py
```

- [ ] **Step 5: Run the full suite to verify green**

Run: `python3 -m unittest discover -s huion_notes -p 'test_*.py' -v`
Expected: PASS — `test_frames` passes; `test_decode_offline` still passes (now importing from `frames`); no `extract_gatt` references remain.

- [ ] **Step 6: Commit**

```bash
git add huion_notes/frames.py huion_notes/test_frames.py huion_notes/decode_offline.py
git commit -m "feat(notes): add frames.py (capture parsing + framing + multi-page builders); retire extract_gatt"
```

---

## Task 2: `auth.py` — keyless challenge/response + PIN

Pure arithmetic auth (protocol §6). No secret key, no server.

**Files:**
- Create: `huion_notes/auth.py`
- Create: `huion_notes/test_auth.py`

**Interfaces:**
- Consumes: `frames.build_command`, `frames.OrderCode` (Task 1).
- Produces:
  - `verify_response(a:int, b:int, c:int) -> tuple[int,int,int]`
  - `build_verify_result(a:int, b:int, c:int) -> bytes`
  - `encode_pwd(pin:str) -> list[int]`
  - `build_verify_pwd_frames(pin:str) -> tuple[bytes,bytes]`

- [ ] **Step 1: Write the failing tests**

Create `huion_notes/test_auth.py`. Two captured challenge/response vectors (single-page `sync-01` and multi-page session 2):

```python
"""Tests for keyless local auth (protocol §6). Stdlib unittest.

Run: python3 -m unittest huion_notes.test_auth -v
"""
import unittest

from huion_notes.auth import (
    verify_response, build_verify_result, encode_pwd, build_verify_pwd_frames,
)


class ChallengeResponseTests(unittest.TestCase):
    def test_verify_response_matches_captured_vectors(self):
        # sync-01: challenge (22,122,69) -> reply (0x42,0xfe,0x3d)
        self.assertEqual(verify_response(22, 122, 69), (0x42, 0xFE, 0x3D))
        # sync-multipage session 2: challenge (28,63,239) -> reply (0x6d,0xbc,0xe7)
        self.assertEqual(verify_response(28, 63, 239), (0x6D, 0xBC, 0xE7))

    def test_build_verify_result_matches_capture(self):
        self.assertEqual(build_verify_result(22, 122, 69).hex(), "cd820842fe3d00ed")
        self.assertEqual(build_verify_result(28, 63, 239).hex(), "cd82086dbce700ed")

    def test_formula_wraps_mod_255(self):
        r1, r2, r3 = verify_response(200, 200, 200)
        self.assertEqual(r1, ((200 + 200) << 2) % 255)
        self.assertTrue(all(0 <= r < 255 for r in (r1, r2, r3)))


class PinEncodingTests(unittest.TestCase):
    def test_encode_pwd_applies_huion_offsets(self):
        self.assertEqual(encode_pwd("123456"), [153, 167, 156, 163, 163, 89])

    def test_encode_pwd_rejects_bad_pin(self):
        for bad in ("12345", "1234567", "12345a", ""):
            with self.assertRaises(ValueError):
                encode_pwd(bad)

    def test_build_verify_pwd_frames_two_frame_layout(self):
        f1, f2 = build_verify_pwd_frames("123456")
        self.assertEqual(f1.hex(), "cd83080199a79ced")  # marker 01 + e0,e1,e2
        self.assertEqual(f2.hex(), "cd830802a3a359ed")  # marker 02 + e3,e4,e5


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest huion_notes.test_auth -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'huion_notes.auth'`.

- [ ] **Step 3: Create `huion_notes/auth.py`**

```python
"""Local keyless auth for the X10 offline protocol (protocol §6).

Challenge-response is pure arithmetic; the optional 6-digit PIN is a fixed-offset
encoding. No secret key, no server — fully reproducible on Linux.
"""
from __future__ import annotations

from huion_notes.frames import OrderCode, build_command

_PWD_OFFSETS = (104, 117, 105, 111, 110, 35)  # ascii("huion#")


def verify_response(a: int, b: int, c: int) -> tuple[int, int, int]:
    """Compute the 3 response bytes for a VERIFY_CONNECT challenge (a,b,c)."""
    r1 = ((a + b) << 2) % 255
    r2 = ((b + c) << 2) % 255
    r3 = ((c + 10) << 2) % 255
    return r1, r2, r3


def build_verify_result(a: int, b: int, c: int) -> bytes:
    """VERIFY_RESULT command: cd 82 08 r1 r2 r3 00 ed."""
    r1, r2, r3 = verify_response(a, b, c)
    return build_command(OrderCode.VERIFY_RESULT, r1, r2, r3, 0)


def encode_pwd(pin: str) -> list[int]:
    """Encode a 6-digit PIN: e[i] = ord(pin[i]) + offset[i]."""
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError("PIN must be exactly 6 digits")
    return [ord(pin[i]) + _PWD_OFFSETS[i] for i in range(6)]


def build_verify_pwd_frames(pin: str) -> tuple[bytes, bytes]:
    """Two-frame VERIFY_PWD: cd 83 08 01 e0 e1 e2 ed + cd 83 08 02 e3 e4 e5 ed."""
    e = encode_pwd(pin)
    return (
        build_command(OrderCode.VERIFY_PWD, 0x01, e[0], e[1], e[2]),
        build_command(OrderCode.VERIFY_PWD, 0x02, e[3], e[4], e[5]),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest huion_notes.test_auth -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add huion_notes/auth.py huion_notes/test_auth.py
git commit -m "feat(notes): add auth.py (keyless challenge/response + PIN encoding)"
```

---

## Task 3: `codec.py` — packets → points → strokes → Page (+ per-page replay split)

The decode brain, ported from `decode_offline.py`, extended with a `Page` model, `MAX_DATA` parsing, packet sequence numbers, and `pages_from_att` which splits a multi-page capture into per-page ordered packet lists (merging `0x88` retransmits).

**Files:**
- Create: `huion_notes/codec.py`
- Create: `huion_notes/test_codec.py`

**Interfaces:**
- Consumes: `frames.OrderCode` (Task 1).
- Produces:
  - `StylusPoint(x:int, y:int, press:int, pen_down:bool)`
  - `Limits(max_x:float, max_y:float, max_press:float)`
  - `Page(index:int, max_x:float, max_y:float, max_press:float, strokes:list)`
  - `decode_point(rec:bytes) -> StylusPoint`
  - `decode_packet(pkt:bytes) -> list[StylusPoint]`
  - `packet_seq(pkt:bytes) -> int`  (bytes 3..4 LE — works for `0x87` seq and `0x88` idx)
  - `parse_max_data(pkt:bytes) -> Limits`
  - `points_to_strokes(points:list) -> list`
  - `decode_page(packets:list[bytes], limits:Limits, index:int=0) -> Page`
  - `pages_from_att(frames:list) -> list[list[bytes]]`  (replay: per-page ordered packets)
  - `limits_from_att(frames:list) -> Limits`
  - `DEFAULT_MAX_X`, `DEFAULT_MAX_Y`, `DEFAULT_MAX_PRESS`

- [ ] **Step 1: Write the failing tests**

Create `huion_notes/test_codec.py`:

```python
"""Tests for the offline stroke codec (protocol §4, §10). Stdlib unittest.

Run: python3 -m unittest huion_notes.test_codec -v
"""
import unittest

from huion_notes.frames import AttFrame
from huion_notes.codec import (
    decode_point, decode_packet, packet_seq, parse_max_data,
    points_to_strokes, decode_page, pages_from_att, limits_from_att,
    StylusPoint, Limits, DEFAULT_MAX_X, DEFAULT_MAX_Y, DEFAULT_MAX_PRESS,
)

_DOWN = bytes([0x10, 0x00, 0x10, 0x00, 0x05, 0x20])
_DOWN2 = bytes([0x20, 0x00, 0x20, 0x00, 0x06, 0x20])


def _p87(seq, pts):
    return bytes([0xCD, 0x87, 0x7E, seq & 0xFF, (seq >> 8) & 0xFF]) + b"".join(pts) + bytes([0xEE])


def _p88(idx, pts):
    return bytes([0xCD, 0x88, 0x7E, idx & 0xFF, (idx >> 8) & 0xFF]) + b"".join(pts) + bytes([0xEE])


def _att(direction, op_value):
    handle = 0x002B if direction == "tx" else 0x0027
    opcode = "write_command" if direction == "tx" else "notification"
    return AttFrame(0, direction, opcode, handle, op_value)


class DecodePointTests(unittest.TestCase):
    def test_decode_point_layout(self):
        p = decode_point(bytes([0x31, 0x0D, 0x00, 0x00, 0xDB, 0x20]))
        self.assertEqual((p.x, p.y, p.press), (3377, 0, 219))
        self.assertTrue(p.pen_down)

    def test_pressure_high_bits_and_penup(self):
        p = decode_point(bytes([0x00, 0x00, 0x00, 0x00, 0x10, 0x1F]))
        self.assertEqual(p.press, 31 * 256 + 0x10)
        self.assertFalse(p.pen_down)


class PacketTests(unittest.TestCase):
    def test_packet_parses_n_points_and_drops_remainder(self):
        pts = decode_packet(_p87(1, [_DOWN, _DOWN2]))
        self.assertEqual([(p.x, p.y) for p in pts], [(16, 16), (32, 32)])

    def test_packet_seq_little_endian(self):
        self.assertEqual(packet_seq(bytes([0xCD, 0x87, 0x7E, 0x02, 0x01])), 0x0102)


class MaxDataTests(unittest.TestCase):
    def test_parse_max_data_vector(self):
        lim = parse_max_data(bytes.fromhex("cd950b286e00189200ff1f"))
        self.assertEqual((lim.max_x, lim.max_y, lim.max_press), (28200.0, 37400.0, 8191.0))

    def test_parse_max_data_defaults_on_wrong_opcode(self):
        lim = parse_max_data(bytes.fromhex("cd870b286e00189200ff1f"))
        self.assertEqual((lim.max_x, lim.max_y, lim.max_press),
                         (DEFAULT_MAX_X, DEFAULT_MAX_Y, DEFAULT_MAX_PRESS))


class StrokeAndPageTests(unittest.TestCase):
    def test_splits_on_penup(self):
        up = StylusPoint(0, 0, 0, False)
        pts = [StylusPoint(1, 1, 100, True), StylusPoint(2, 2, 100, True), up,
               StylusPoint(3, 3, 100, True), StylusPoint(4, 4, 100, True)]
        self.assertEqual([len(s) for s in points_to_strokes(pts)], [2, 2])

    def test_decode_page_assembles(self):
        page = decode_page([_p87(1, [_DOWN, _DOWN2])], Limits(100.0, 200.0, 8191.0), index=2)
        self.assertEqual((page.index, page.max_x, page.max_y), (2, 100.0, 200.0))
        self.assertEqual(len(page.strokes), 1)


class ReplaySplitTests(unittest.TestCase):
    def test_pages_from_att_splits_and_merges_retransmit(self):
        frames = [
            _att("tx", bytes.fromhex("cd860800000000ed")),       # request page 0
            _att("rx", bytes.fromhex("cd86050300")),             # count=3
            _att("rx", _p87(1, [_DOWN, _DOWN2])),
            _att("rx", _p87(3, [_DOWN, _DOWN2])),                # seq 2 missing in stream
            _att("rx", _p88(2, [_DOWN, _DOWN2])),                # retransmit fills seq 2
            _att("tx", bytes.fromhex("cd860801000000ed")),       # request page 1
            _att("rx", bytes.fromhex("cd86050100")),             # count=1
            _att("rx", _p87(1, [_DOWN, _DOWN2])),
        ]
        pages = pages_from_att(frames)
        self.assertEqual(len(pages), 2)
        self.assertEqual([packet_seq(p) for p in pages[0]], [1, 2, 3])  # ordered, retransmit merged
        self.assertEqual(len(pages[1]), 1)

    def test_limits_from_att_reads_max_data(self):
        frames = [_att("rx", bytes.fromhex("cd950b286e00189200ff1f"))]
        self.assertEqual(limits_from_att(frames).max_x, 28200.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest huion_notes.test_codec -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'huion_notes.codec'`.

- [ ] **Step 3: Create `huion_notes/codec.py`**

```python
"""Decode RETURN_OFFLINE_DATA (0x87) / retransmit (0x88) packets into strokes /
pages (protocol §4, §10).

Pure; mirrors the app's BluetoothUtil#decodePackagePoint and BluePoint. Points
are parsed PER PACKET (N = (len-5)//6, remainder incl. checksum discarded), the
same way for 0x87 stream packets and 0x88 retransmit packets. Coordinates map to
the page origin top-left with no axis flip (non-A4 device).
"""
from __future__ import annotations

from dataclasses import dataclass

from huion_notes.frames import OrderCode

DEFAULT_MAX_X = 28200.0
DEFAULT_MAX_Y = 37400.0
DEFAULT_MAX_PRESS = 8191.0


@dataclass
class StylusPoint:
    x: int
    y: int
    press: int
    pen_down: bool


@dataclass
class Limits:
    max_x: float = DEFAULT_MAX_X
    max_y: float = DEFAULT_MAX_Y
    max_press: float = DEFAULT_MAX_PRESS


@dataclass
class Page:
    index: int
    max_x: float
    max_y: float
    max_press: float
    strokes: list  # list[list[StylusPoint]]


def decode_point(rec: bytes) -> StylusPoint:
    """Decode one 6-byte point record (mirrors BluePoint's constructor)."""
    x = rec[0] | (rec[1] << 8)
    y = rec[2] | (rec[3] << 8)
    press = rec[4] | ((rec[5] & 0x1F) << 8)
    status = rec[5] >> 5
    return StylusPoint(x=x, y=y, press=press, pen_down=status != 0)


def decode_packet(pkt: bytes) -> list[StylusPoint]:
    """Decode all points in one 0x87/0x88 packet (per-packet, remainder dropped)."""
    n = (len(pkt) - 5) // 6
    return [decode_point(pkt[5 + k * 6 : 5 + k * 6 + 6]) for k in range(n)]


def packet_seq(pkt: bytes) -> int:
    """Sequence/index number at bytes 3..4 (u16 LE) — 0x87 seq or 0x88 index."""
    return pkt[3] | (pkt[4] << 8)


def parse_max_data(pkt: bytes) -> Limits:
    """Read MAX_X/MAX_Y/MAX_PRESS from a 0x95 packet, else defaults."""
    if len(pkt) < 11 or pkt[1] != OrderCode.MAX_DATA:
        return Limits()
    return Limits(
        max_x=float(pkt[5] << 16 | pkt[4] << 8 | pkt[3]),
        max_y=float(pkt[8] << 16 | pkt[7] << 8 | pkt[6]),
        max_press=float(pkt[10] << 8 | pkt[9]),
    )


def points_to_strokes(points: list) -> list:
    """Split into strokes on pen-up (status 0 / pressure 0). Drops 1-point strokes."""
    strokes, cur = [], []
    for p in points:
        if not p.pen_down or p.press == 0:
            if len(cur) > 1:
                strokes.append(cur)
            cur = []
        else:
            cur.append(p)
    if len(cur) > 1:
        strokes.append(cur)
    return strokes


def decode_page(packets: list, limits: Limits, index: int = 0) -> Page:
    """Decode an ordered list of 0x87/0x88 packets into a Page."""
    pts = [p for pkt in packets for p in decode_packet(pkt)]
    return Page(
        index=index,
        max_x=limits.max_x,
        max_y=limits.max_y,
        max_press=limits.max_press,
        strokes=points_to_strokes(pts),
    )


def pages_from_att(frames: list) -> list:
    """Replay path: split a capture into per-page ordered packet lists.

    A REQUEST_OFFLINE_DATA (0x86) write starts a page; subsequent 0x87
    notifications (by seq) and 0x88 retransmit replies (by index) up to the next
    0x86 write are collected and ordered. Empty pages (no packets) are skipped.
    """
    pages: list = []
    cur: "dict | None" = None
    for f in frames:
        v = f.value
        if len(v) < 2 or v[0] != 0xCD:
            continue
        op = v[1]
        if f.direction == "tx" and op == OrderCode.REQUEST_OFFLINE_DATA:
            cur = {}
            pages.append(cur)
        elif (
            cur is not None
            and f.direction == "rx"
            and op in (OrderCode.RETURN_OFFLINE_DATA, OrderCode.GET_PAGE_PACKAGE)
            and len(v) >= 6
            and v[2] == 0x7E
        ):
            cur[packet_seq(v)] = bytes(v)
    return [[d[k] for k in sorted(d)] for d in pages if d]


def limits_from_att(frames: list) -> Limits:
    """Replay path: read limits from the capture's 0x95 packet, else defaults."""
    for f in frames:
        if (
            f.opcode == "notification"
            and len(f.value) >= 11
            and f.value[1] == OrderCode.MAX_DATA
        ):
            return parse_max_data(f.value)
    return Limits()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest huion_notes.test_codec -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add huion_notes/codec.py huion_notes/test_codec.py
git commit -m "feat(notes): add codec.py (packets -> Page; multi-page replay split)"
```

---

## Task 4: `render.py` — SVG / JSON / PNG, and retire `decode_offline.py`

Renders a `Page` to the three output formats, then removes the superseded `decode_offline.py` + its tests (their logic now lives in `codec.py` + `render.py`).

**Files:**
- Create: `huion_notes/render.py`
- Create: `huion_notes/test_render.py`
- Delete: `huion_notes/decode_offline.py`, `huion_notes/test_decode_offline.py`

**Interfaces:**
- Consumes: `codec.Page` (Task 3).
- Produces:
  - `render_svg(page, width:int=900, height:int=1190, pad:int=15) -> str`
  - `render_json(page) -> str`
  - `render_png(svg_path:str, png_path:str, *, which=shutil.which, runner=subprocess.run) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `huion_notes/test_render.py`:

```python
"""Tests for rendering (SVG/JSON/PNG). Stdlib unittest.

Run: python3 -m unittest huion_notes.test_render -v
"""
import json
import unittest

from huion_notes.codec import Page, StylusPoint
from huion_notes.render import render_svg, render_json, render_png


def _page():
    s1 = [StylusPoint(10, 10, 100, True), StylusPoint(20, 30, 120, True)]
    return Page(index=0, max_x=100.0, max_y=200.0, max_press=8191.0, strokes=[s1])


class SvgTests(unittest.TestCase):
    def test_svg_has_path_and_dimensions(self):
        svg = render_svg(_page())
        self.assertIn("<path", svg)
        self.assertIn('width="900"', svg)
        self.assertTrue(svg.startswith("<svg"))


class JsonTests(unittest.TestCase):
    def test_json_schema_roundtrips(self):
        obj = json.loads(render_json(_page()))
        self.assertEqual(obj["page"], 0)
        self.assertEqual(obj["max_x"], 100.0)
        self.assertEqual(len(obj["strokes"]), 1)
        self.assertEqual(obj["strokes"][0][0], {"x": 10, "y": 10, "press": 100, "pen_down": True})


class PngTests(unittest.TestCase):
    def test_png_returns_false_when_magick_absent(self):
        self.assertFalse(render_png("in.svg", "out.png", which=lambda name: None))

    def test_png_invokes_runner_with_correct_argv(self):
        calls = []
        ok = render_png(
            "in.svg", "out.png",
            which=lambda name: "/usr/bin/magick" if name == "magick" else None,
            runner=lambda argv, **kw: calls.append(argv),
        )
        self.assertTrue(ok)
        self.assertEqual(calls, [["/usr/bin/magick", "in.svg", "out.png"]])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest huion_notes.test_render -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'huion_notes.render'`.

- [ ] **Step 3: Create `huion_notes/render.py`**

```python
"""Render decoded pages to SVG / JSON / PNG (spec §7).

SVG is the pure-Python vector master; JSON is the ordered, lossless point dump
(the high-accuracy input for AI handwriting recognition); PNG is produced by
shelling out to ImageMagick and degrades gracefully (returns False) if absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess


def render_svg(page, width: int = 900, height: int = 1190, pad: int = 15) -> str:
    """strokes -> SVG paths. Origin top-left, no axis flip (non-A4 device)."""
    def sx(x: int) -> float:
        return pad + (x / page.max_x) * (width - 2 * pad)

    def sy(y: int) -> float:
        return pad + (y / page.max_y) * (height - 2 * pad)

    paths = []
    for s in page.strokes:
        d = " ".join(
            f"{'M' if i == 0 else 'L'}{sx(p.x):.1f},{sy(p.y):.1f}"
            for i, p in enumerate(s)
        )
        paths.append(f'<path d="{d}" fill="none" stroke="#111" stroke-width="2.5"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="background:#fff">' + "".join(paths) + "</svg>"
    )


def render_json(page) -> str:
    """Ordered, lossless point dump (spec §7 schema)."""
    return json.dumps(
        {
            "page": page.index,
            "max_x": page.max_x,
            "max_y": page.max_y,
            "max_press": page.max_press,
            "strokes": [
                [{"x": p.x, "y": p.y, "press": p.press, "pen_down": p.pen_down} for p in s]
                for s in page.strokes
            ],
        }
    )


def render_png(svg_path: str, png_path: str, *, which=shutil.which, runner=subprocess.run) -> bool:
    """Convert an SVG file to PNG via ImageMagick. Returns False if unavailable."""
    exe = which("magick") or which("convert")
    if not exe:
        return False
    runner([exe, svg_path, png_path], check=True)
    return True
```

- [ ] **Step 4: Delete the superseded decoder + its tests**

```bash
git rm huion_notes/decode_offline.py huion_notes/test_decode_offline.py
```

- [ ] **Step 5: Run the full suite to verify green**

Run: `python3 -m unittest discover -s huion_notes -p 'test_*.py' -v`
Expected: PASS — `test_frames`, `test_auth`, `test_codec`, `test_render`; no `decode_offline` references remain.

- [ ] **Step 6: Commit**

```bash
git add huion_notes/render.py huion_notes/test_render.py
git commit -m "feat(notes): add render.py (SVG/JSON/PNG); retire decode_offline"
```

---

## Task 5: `errors.py` + `session.py` — live multi-page dump orchestration

The handshake + per-page download state machine (protocol §5, §10). Pure of dbus — it talks to a duck-typed `Transport`, so it is fully unit-tested with a `FakeTransport` that scripts inbound frames and records outbound commands.

**Files:**
- Create: `huion_notes/errors.py`
- Create: `huion_notes/session.py`
- Create: `huion_notes/test_session.py`

**Interfaces:**
- Consumes: `frames` (Task 1), `auth` (Task 2), `codec` (Task 3).
- Produces:
  - `errors.TransportClosed`, `errors.PinRequired`, `errors.AuthFailed`, `errors.IncompleteDump`
  - `class Transport(Protocol)` — `connect()`, `send(frame:bytes)`, `recv(timeout:float|None=None) -> bytes`, `close()` (all async); `recv` raises `TransportClosed` at EOF and `asyncio.TimeoutError` when idle.
  - `class DumpSession(transport, pin:str|None=None, idle_timeout:float=5.0, max_pages:int=64)` with `async run() -> list[codec.Page]`

**Design notes:**
- **Completion is count-driven** (§10): each page's `request_page_data` reply carries the packet `count`; the stream is done when the packet with `seq == count` arrives. Idle timeout / EOF are only fallbacks.
- **Page loop** stops when a page returns `count == 0` (capped at `max_pages`).
- **Retransmit** uses `frames.build_get_page_package(page, idx)` (`0x88`) — replies are `0x88` frames keyed by index.
- **Read-only:** never sends `DELETE_PAGE`/`CLEAR_CACHE`.

- [ ] **Step 1: Write the failing tests**

Create `huion_notes/test_session.py`:

```python
"""Tests for live multi-page dump orchestration via a fake transport. Stdlib unittest.

Run: python3 -m unittest huion_notes.test_session -v
"""
import asyncio
import unittest

from huion_notes import frames
from huion_notes.frames import OrderCode
from huion_notes.errors import TransportClosed, PinRequired, AuthFailed
from huion_notes.session import DumpSession

_DOWN = bytes([0x10, 0x00, 0x10, 0x00, 0x05, 0x20])
_DOWN2 = bytes([0x20, 0x00, 0x20, 0x00, 0x06, 0x20])


def _vc(a, b, c): return bytes([0xCD, OrderCode.VERIFY_CONNECT, 0x08, a, b, c, 0x00, 0xED])
def _vr(status): return bytes([0xCD, OrderCode.VERIFY_RESULT, 0x08, status, 0, 0, 0, 0xED])
def _maxd(): return bytes.fromhex("cd950b286e00189200ff1f")
def _count(n): return bytes([0xCD, OrderCode.REQUEST_OFFLINE_DATA, 0x05, n & 0xFF, (n >> 8) & 0xFF])
def _p87(seq): return bytes([0xCD, 0x87, 0x7E, seq & 0xFF, (seq >> 8) & 0xFF]) + _DOWN + _DOWN2 + bytes([0xEE])
def _p88(idx): return bytes([0xCD, 0x88, 0x7E, idx & 0xFF, (idx >> 8) & 0xFF]) + _DOWN + _DOWN2 + bytes([0xEE])


class FakeTransport:
    """Scripts inbound frames; records outbound. recv() drains the script, then
    raises TimeoutError (idle) until closed (TransportClosed)."""

    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []
        self._closed = False

    async def connect(self): pass
    async def send(self, frame): self.sent.append(frame)

    async def recv(self, timeout=None):
        if self._inbound:
            return self._inbound.pop(0)
        if self._closed:
            raise TransportClosed()
        raise asyncio.TimeoutError()

    async def close(self): self._closed = True

    def page_requests(self):
        return [b for b in self.sent
                if (fr := frames.parse_huion_frame(b)) and fr.op == OrderCode.REQUEST_OFFLINE_DATA]

    def retransmits(self):
        return [b for b in self.sent
                if (fr := frames.parse_huion_frame(b)) and fr.op == OrderCode.GET_PAGE_PACKAGE]


class GapFillTransport(FakeTransport):
    """Answers each GET_PAGE_PACKAGE(page, idx) with the matching 0x88 packet."""

    async def send(self, frame):
        await super().send(frame)
        fr = frames.parse_huion_frame(frame)
        if fr and fr.op == OrderCode.GET_PAGE_PACKAGE:
            self._inbound.append(_p88(fr.raw[5] | (fr.raw[6] << 8)))


class MultiPageTests(unittest.TestCase):
    def test_handshake_then_two_pages_then_stop_on_empty(self):
        inbound = [
            _vc(22, 122, 69), _vr(1), _maxd(),
            _count(2), _p87(1), _p87(2),   # page 0
            _count(1), _p87(1),            # page 1
            _count(0),                     # page 2 empty -> stop
        ]
        t = FakeTransport(inbound)
        pages = asyncio.run(DumpSession(t, idle_timeout=0.01).run())
        # Auth + setup commands first.
        self.assertEqual(t.sent[0].hex(), "cd820842fe3d00ed")
        self.assertEqual(t.sent[1], frames.request_max_info())
        d1, d2 = frames.request_set_many_packet_distance()
        self.assertEqual((t.sent[2], t.sent[3]), (d1, d2))
        # Page loop probed 0,1,2 (2 was empty).
        self.assertEqual([frames.parse_huion_frame(b).raw[3] for b in t.page_requests()], [0, 1, 2])
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].index, 0)
        self.assertEqual(pages[0].max_x, 28200.0)


class PinPathTests(unittest.TestCase):
    def test_pin_required_then_accepted(self):
        inbound = [_vc(22, 122, 69), _vr(2), _vr(1), _maxd(), _count(0)]
        t = FakeTransport(inbound)
        asyncio.run(DumpSession(t, pin="123456", idle_timeout=0.01).run())
        self.assertEqual(t.sent[1].hex(), "cd83080199a79ced")
        self.assertEqual(t.sent[2].hex(), "cd830802a3a359ed")

    def test_pin_required_but_missing_raises(self):
        t = FakeTransport([_vc(22, 122, 69), _vr(2)])
        with self.assertRaises(PinRequired):
            asyncio.run(DumpSession(t, idle_timeout=0.01).run())

    def test_auth_failure_raises(self):
        t = FakeTransport([_vc(22, 122, 69), _vr(0)])
        with self.assertRaises(AuthFailed):
            asyncio.run(DumpSession(t, idle_timeout=0.01).run())


class GapFillTests(unittest.TestCase):
    def test_missing_seq_triggers_retransmit_and_completes(self):
        # page 0 count=3, stream delivers seq 1 and 3 (seq 2 dropped).
        inbound = [_vc(22, 122, 69), _vr(1), _maxd(), _count(3), _p87(1), _p87(3)]
        t = GapFillTransport(inbound)
        pages = asyncio.run(DumpSession(t, idle_timeout=0.01, max_pages=1).run())
        idxs = [frames.parse_huion_frame(b).raw[5] | (frames.parse_huion_frame(b).raw[6] << 8)
                for b in t.retransmits()]
        self.assertIn(2, idxs)                      # retransmit asked for seq 2
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0].strokes), 1)  # 3 packets * 2 down-points -> 1 stroke


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest huion_notes.test_session -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'huion_notes.session'` (and `errors`).

- [ ] **Step 3: Create `huion_notes/errors.py`**

```python
"""Shared exceptions for the note extractor."""
from __future__ import annotations


class TransportClosed(Exception):
    """The BLE transport closed (device disconnected / EOF)."""


class PinRequired(Exception):
    """Device requested a PIN but none was supplied."""


class AuthFailed(Exception):
    """The device rejected the auth handshake."""


class IncompleteDump(Exception):
    """A page could not be fully assembled after retransmit attempts."""
```

- [ ] **Step 4: Create `huion_notes/session.py`**

```python
"""Live multi-page dump orchestration (protocol §5, §10). Pure of dbus — talks to
a Transport. The contract (below) is implemented by transport.BleTransport and by
the test suite's FakeTransport.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Protocol

from huion_notes import auth, codec, frames
from huion_notes.frames import OrderCode
from huion_notes.errors import AuthFailed, PinRequired, TransportClosed

log = logging.getLogger("huion_notes.session")


class Transport(Protocol):
    async def connect(self) -> None: ...
    async def send(self, frame: bytes) -> None: ...
    async def recv(self, timeout: Optional[float] = None) -> bytes: ...
    async def close(self) -> None: ...


async def _recv_op(transport: "Transport", op: int, timeout: float = 10.0) -> frames.HuionFrame:
    """Read frames until one has opcode `op`; ignore others (heartbeats, etc.)."""
    while True:
        value = await transport.recv(timeout=timeout)
        fr = frames.parse_huion_frame(value)
        if fr and fr.op == op:
            return fr


class DumpSession:
    def __init__(self, transport: "Transport", pin: Optional[str] = None,
                 idle_timeout: float = 5.0, max_pages: int = 64):
        self.t = transport
        self.pin = pin
        self.idle = idle_timeout
        self.max_pages = max_pages

    async def run(self) -> list:
        await self.t.connect()
        await self._authenticate()

        await self.t.send(frames.request_max_info())
        limits = codec.parse_max_data((await _recv_op(self.t, OrderCode.MAX_DATA)).raw)
        d1, d2 = frames.request_set_many_packet_distance()
        await self.t.send(d1)
        await self.t.send(d2)

        pages: list = []
        for page in range(self.max_pages):
            count, packets = await self._fetch_page(page)
            if count == 0:
                break
            pages.append(codec.decode_page(packets, limits, index=page))
        return pages

    async def _authenticate(self) -> None:
        ch = await _recv_op(self.t, OrderCode.VERIFY_CONNECT)
        a, b, c = ch.raw[3], ch.raw[4], ch.raw[5]
        await self.t.send(auth.build_verify_result(a, b, c))
        status = (await _recv_op(self.t, OrderCode.VERIFY_RESULT)).raw[3]
        if status == 2:
            if not self.pin:
                raise PinRequired("device requires a 6-digit PIN; pass --pin")
            f1, f2 = auth.build_verify_pwd_frames(self.pin)
            await self.t.send(f1)
            await self.t.send(f2)
            status = (await _recv_op(self.t, OrderCode.VERIFY_RESULT)).raw[3]
        if status != 1:
            raise AuthFailed(f"auth rejected (status={status})")

    async def _fetch_page(self, page: int) -> tuple:
        """Download one page: returns (count, ordered_packets). count 0 = empty."""
        await self.t.send(frames.request_page_data(page, 0))
        count = frames.parse_offline_count((await _recv_op(self.t, OrderCode.REQUEST_OFFLINE_DATA)).raw)
        if not count:
            return 0, []
        got: dict = {}
        await self._drain_stream(got, count)
        await self._fill_gaps(page, got, count)
        missing = [s for s in range(1, count + 1) if s not in got]
        if missing:
            log.warning("page %d incomplete: %d/%d packets; missing %s",
                        page, len(got), count, missing[:20])
        return count, [got[s] for s in sorted(got)]

    async def _drain_stream(self, got: dict, count: int) -> None:
        """Collect 0x87 packets until seq == count is seen, or idle/closed."""
        while True:
            try:
                value = await self.t.recv(timeout=self.idle)
            except (asyncio.TimeoutError, TransportClosed):
                return
            fr = frames.parse_huion_frame(value)
            if not fr or fr.op != OrderCode.RETURN_OFFLINE_DATA:
                continue
            seq = codec.packet_seq(fr.raw)
            got[seq] = fr.raw
            if seq == count:
                return

    async def _fill_gaps(self, page: int, got: dict, count: int, max_rounds: int = 5) -> None:
        """Re-request missing packets via GET_PAGE_PACKAGE (0x88); collect replies."""
        for _ in range(max_rounds):
            missing = [s for s in range(1, count + 1) if s not in got]
            if not missing:
                return
            for i in missing:
                await self.t.send(frames.build_get_page_package(page, i))
            while True:
                try:
                    value = await self.t.recv(timeout=self.idle)
                except (asyncio.TimeoutError, TransportClosed):
                    break
                fr = frames.parse_huion_frame(value)
                if fr and fr.op == OrderCode.GET_PAGE_PACKAGE and len(fr.raw) >= 6 and fr.raw[2] == 0x7E:
                    got[codec.packet_seq(fr.raw)] = fr.raw
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest huion_notes.test_session -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add huion_notes/errors.py huion_notes/session.py huion_notes/test_session.py
git commit -m "feat(notes): add session.py (live multi-page dump orchestration) + errors"
```

---

## Task 6: `transport.py` — live BLE transport (thin, device-only)

The only device-dependent module. Wraps the pen driver's `BLEConnection`, using `StartNotify` on both characteristics (so notifications on FFE1 *and* indications on FFE2 arrive via one D-Bus signal callback into a queue). **Not unit-tested** (requires real hardware + `dbus_fast`); it implements the same `Transport` contract `FakeTransport` already verified. Verification is the manual checklist below.

**Files:**
- Create: `huion_notes/transport.py`

**Interfaces:**
- Consumes: `huion_ble_driver.BLEConnection`, `huion_ble_driver.BLUEZ` (confirmed module-level), `frames.heart_beat` (Task 1), `errors.TransportClosed` (Task 5).
- Produces: `class BleTransport(mac:str, keepalive:float=5.0)` implementing `connect`/`send`/`recv`/`close`.

- [ ] **Step 1: Create `huion_notes/transport.py`**

```python
"""Live BLE transport (dbus_fast) — the only device-dependent module (spec §4).

Reuses BLEConnection from the pen driver and exposes the session's Transport
contract (connect/send/recv/close). StartNotify is enabled on BOTH FFE1 (data
notifications) and FFE2 (command indications); BLEConnection.setup_signals routes
every characteristic Value change to a single callback, which we queue. The
session dispatches by opcode, so it does not care which characteristic a frame
arrived on.

Imports dbus_fast (and the driver) at module load, so this module must only be
imported on the live `dump` path — never from the pure core or the test suite.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from dbus_fast import Message, MessageType

from huion_ble_driver import BLEConnection, BLUEZ
from huion_notes.errors import TransportClosed
from huion_notes.frames import heart_beat


class BleTransport:
    def __init__(self, mac: str, keepalive: float = 5.0):
        self.conn = BLEConnection(mac)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._keepalive = keepalive
        self._ka_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        await self.conn.connect_bus()
        if not await self.conn.connect():
            raise TransportClosed(
                "BLE connect failed — is the notebook awake, paired, in note mode, "
                "and is patched BlueZ installed? (see README)"
            )
        await self._start_notify(self.conn.ffe1_path)
        await self._start_notify(self.conn.ffe2_path)
        await self.conn.setup_signals(self._on_value, self._on_disconnect)
        self._ka_task = asyncio.create_task(self._keepalive_loop())

    async def _start_notify(self, path: str) -> None:
        reply = await self.conn.bus.call(Message(
            destination=BLUEZ, path=path,
            interface="org.bluez.GattCharacteristic1", member="StartNotify",
        ))
        if reply.message_type == MessageType.ERROR:
            err = reply.error_name or ""
            if "InProgress" not in err:
                raise TransportClosed(f"StartNotify {path} failed: {err}")

    def _on_value(self, data: bytes) -> None:
        self._queue.put_nowait(bytes(data))

    def _on_disconnect(self) -> None:
        self._closed = True
        self._queue.put_nowait(None)  # wake any pending recv()

    async def send(self, frame: bytes) -> None:
        if not await self.conn.write_cmd(frame):
            raise TransportClosed("write failed (device disconnected?)")

    async def recv(self, timeout: Optional[float] = None) -> bytes:
        item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if item is None:
            raise TransportClosed("device disconnected")
        return item

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._keepalive)
                await self.conn.write_cmd(heart_beat())
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._closed = True
        if self._ka_task:
            self._ka_task.cancel()
        await self.conn.close()
```

- [ ] **Step 2: Verify the pure core still imports without dbus_fast**

Run: `python3 -c "import huion_notes.session, huion_notes.codec, huion_notes.cli"`
Expected: no error. (Task 7 creates `cli`; until then drop `huion_notes.cli` from the import list.)

Run: `python3 -c "import huion_notes.transport"`
Expected: FAIL with `ModuleNotFoundError: No module named 'dbus_fast'` — **correct**: confirms `transport.py` is the only module that pulls in `dbus_fast`.

- [ ] **Step 3: Manual hardware verification (documented; not automated)**

Against the device with patched BlueZ and the notebook in **note mode** (folio cover closed):

```bash
python3 -m huion_notes dump -o /tmp/x10-out --verbose   # (cli arrives in Task 7)
```

Checklist: connects; `VERIFY_CONNECT` answered (status 1, or PIN with `--pin`); `MAX_DATA` logged `28200/37400/8191`; per page a `cd 86 05 <count>` then `count` × `0x87`; page loop stops on `count == 0`; one `page-NN.{svg,png,json}` per page, legible. If packets are missing, confirm `0x88` retransmit recovered them. Record results in `notes/offline-extractor-status.md`.

- [ ] **Step 4: Commit**

```bash
git add huion_notes/transport.py
git commit -m "feat(notes): add transport.py (live dbus_fast BLE transport)"
```

---

## Task 7: `cli.py` + `__main__.py` — `decode` and `dump` subcommands

Wires the pipeline into a CLI. The `decode` path is pure (tested end-to-end on a synthetic multi-page capture); the `dump` path imports `transport`/`session`/driver **lazily** so the module and `decode` work without `dbus_fast`.

**Files:**
- Create: `huion_notes/cli.py`
- Create: `huion_notes/__main__.py`
- Create: `huion_notes/test_cli.py`

**Interfaces:**
- Consumes: `frames`, `codec`, `render` (top-level); `transport.BleTransport`, `session.DumpSession`, `huion_ble_driver._find_tablet_mac` (lazy, dump only).
- Produces: `build_parser() -> argparse.ArgumentParser`, `cmd_decode(args) -> int`, `cmd_dump(args) -> int`, `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `huion_notes/test_cli.py` (decode path only — dump needs hardware; this capture has TWO pages):

```python
"""Tests for the CLI decode path. Stdlib unittest.

Run: python3 -m unittest huion_notes.test_cli -v
"""
import json
import os
import struct
import tempfile
import unittest

from huion_notes.cli import main

BTSNOOP_HEADER = b"btsnoop\x00" + struct.pack(">II", 1, 1002)
_DOWN = bytes([0x10, 0x00, 0x10, 0x00, 0x05, 0x20])
_DOWN2 = bytes([0x20, 0x00, 0x20, 0x00, 0x06, 0x20])


def _record(payload, *, sent):
    flags = 0 if sent else 1
    return struct.pack(">IIIIq", len(payload), len(payload), flags, 0, 0) + payload


def _acl(att_pdu, *, handle=0x0040):
    l2cap = struct.pack("<HH", len(att_pdu), 0x0004) + att_pdu
    return b"\x02" + struct.pack("<HH", handle, len(l2cap)) + l2cap


def _notif(value):
    return _record(_acl(b"\x1b" + struct.pack("<H", 0x0027) + value), sent=False)


def _write(value):
    return _record(_acl(b"\x52" + struct.pack("<H", 0x002b) + value), sent=True)


def _p87(seq):
    return bytes([0xCD, 0x87, 0x7E, seq, 0x00]) + _DOWN + _DOWN2 + bytes([0xEE])


def _synthetic_capture():
    return (
        BTSNOOP_HEADER
        + _notif(bytes.fromhex("cd950b286e00189200ff1f"))  # MAX_DATA
        + _write(bytes.fromhex("cd860800000000ed")) + _notif(bytes.fromhex("cd86050100")) + _notif(_p87(1))   # page 0
        + _write(bytes.fromhex("cd860801000000ed")) + _notif(bytes.fromhex("cd86050100")) + _notif(_p87(1))   # page 1
    )


class DecodeCliTests(unittest.TestCase):
    def test_decode_writes_per_page_svg_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            cap = os.path.join(d, "sample.btsnoop")
            out = os.path.join(d, "out")
            with open(cap, "wb") as fh:
                fh.write(_synthetic_capture())
            self.assertEqual(main(["decode", cap, "-o", out]), 0)
            for nn in ("00", "01"):
                self.assertTrue(os.path.exists(os.path.join(out, f"page-{nn}.svg")))
            with open(os.path.join(out, "page-00.json")) as fh:
                obj = json.load(fh)
            self.assertEqual(obj["max_x"], 28200.0)
            self.assertEqual(len(obj["strokes"]), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest huion_notes.test_cli -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'huion_notes.cli'`.

- [ ] **Step 3: Create `huion_notes/cli.py`**

```python
"""huion-x10-notes CLI: `decode` (replay a capture) and `dump` (live BLE).

Pure modules are imported at top; the live `dump` dependencies (transport,
session, driver) are imported lazily inside cmd_dump so this module and the
`decode` path work without dbus_fast installed.
"""
from __future__ import annotations

import argparse
import os
import sys

from huion_notes import codec, frames, render


def _write_page(page, out_dir: str) -> str:
    base = os.path.join(out_dir, f"page-{page.index:02d}")
    with open(base + ".svg", "w") as fh:
        fh.write(render.render_svg(page))
    with open(base + ".json", "w") as fh:
        fh.write(render.render_json(page))
    if not render.render_png(base + ".svg", base + ".png"):
        print(f"warning: ImageMagick not found; wrote SVG+JSON only for {base}", file=sys.stderr)
    return base


def cmd_decode(args) -> int:
    with open(args.file, "rb") as fh:
        att = frames.extract_att_frames(frames.parse_btsnoop(fh.read()))
    limits = codec.limits_from_att(att)
    page_streams = codec.pages_from_att(att)
    if not page_streams:
        print("error: no offline page data (0x87 packets) found in capture", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)
    for i, packets in enumerate(page_streams):
        page = codec.decode_page(packets, limits, index=i)
        base = _write_page(page, args.out)
        print(f"page {i}: {len(page.strokes)} strokes -> {base}.{{svg,png,json}}")
    return 0


def cmd_dump(args) -> int:
    import asyncio

    from huion_notes.session import DumpSession
    from huion_notes.transport import BleTransport

    mac = args.mac
    if not mac:
        from huion_ble_driver import _find_tablet_mac
        mac = _find_tablet_mac()
    if not mac:
        print("error: no --mac given and the notebook could not be autodetected", file=sys.stderr)
        return 2

    async def _run():
        t = BleTransport(mac)
        try:
            return await DumpSession(t, pin=args.pin).run()
        finally:
            await t.close()

    try:
        pages = asyncio.run(_run())
    except Exception as e:  # actionable message, not a traceback
        print(f"error: dump failed: {e}", file=sys.stderr)
        return 1
    if not pages:
        print("no offline pages found on the device.")
        return 0
    os.makedirs(args.out, exist_ok=True)
    for page in pages:
        base = _write_page(page, args.out)
        print(f"page {page.index}: {len(page.strokes)} strokes -> {base}.{{svg,png,json}}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="huion-x10-notes", description="Huion Note X10 offline note extractor")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decode", help="decode an existing .btsnoop capture")
    d.add_argument("file", help="path to a .btsnoop capture")
    d.add_argument("-o", "--out", required=True, help="output directory")
    d.set_defaults(func=cmd_decode)

    u = sub.add_parser("dump", help="connect over BLE and dump all stored pages")
    u.add_argument("-o", "--out", required=True, help="output directory")
    u.add_argument("--mac", help="notebook BT MAC (autodetected if omitted)")
    u.add_argument("--pin", help="6-digit device PIN, if set")
    u.add_argument("--verbose", action="store_true", help="verbose logging")
    u.set_defaults(func=cmd_dump)
    return p


def main(argv=None) -> int:
    import logging

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Create `huion_notes/__main__.py`**

```python
"""Enable `python -m huion_notes ...`."""
from huion_notes.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest huion_notes.test_cli -v`
Expected: PASS (1 test). An ImageMagick warning may print if `magick` is absent — expected, non-fatal.

- [ ] **Step 6: Verify the full suite and the `-m` entry**

Run: `python3 -m unittest discover -s huion_notes -p 'test_*.py'`
Expected: PASS (frames/auth/codec/render/session/cli).

Run: `python3 -m huion_notes --help`
Expected: usage listing `decode` and `dump` (no `dbus_fast` import error).

- [ ] **Step 7: Commit**

```bash
git add huion_notes/cli.py huion_notes/__main__.py huion_notes/test_cli.py
git commit -m "feat(notes): add CLI (decode/dump, multi-page) and python -m entry point"
```

---

## Task 8: Documentation — usage, status, and spec updates

Folds the remaining docs deliverable: document the multi-page CLI and mark Sub-project B implemented.

**Files:**
- Modify: `README.md` (add an "Offline note extraction (CLI)" section)
- Modify: `docs/offline-notes-overview.md` (status + "What's next")
- Modify: `notes/offline-extractor-status.md` (mark implemented; record manual-verification result)
- Modify: `docs/specs/2026-06-22-note-extractor-design.md` (status line → Implemented; §6 multi-page now done)

- [ ] **Step 1: Add a CLI usage section to `README.md`**

Insert (after the pen-driver usage, before licensing):

```markdown
## Offline note extraction (no app, no cloud)

Pull and decode every page stored on the notebook over BLE. Requires patched
BlueZ (see installation) and the notebook in **note mode** (folio cover closed).
Outputs `page-NN.svg`, `page-NN.png` (if ImageMagick is installed), and
`page-NN.json` (ordered points + pressure, for AI handwriting recognition).
Read-only: it never deletes pages from the device.

    # Live dump of all stored pages:
    python3 -m huion_notes dump -o ./notes-out [--mac AA:BB:..] [--pin 123456]

    # Decode an existing Android btsnoop capture (replay / offline):
    python3 -m huion_notes decode capture.btsnoop -o ./notes-out

The decoder is fully offline and unit-tested; only `dump` touches the device.
```

- [ ] **Step 2: Update status in the overview, status note, and spec**

- `docs/offline-notes-overview.md`: change "Act B — Extractor … ← *not started*" to *done (multi-page)*; update "What's next" to the remaining hardening (live verification, optional AI transcription).
- `notes/offline-extractor-status.md`: flip "⏭️ Next step" to "✅ Implemented (`huion_notes/{frames,auth,codec,render,session,transport,cli}.py`); multi-page confirmed from `sync-multipage.btsnoop`", and add a "Manual verification" line with the Task 6 result (or "pending hardware run").
- `docs/specs/2026-06-22-note-extractor-design.md`: change `**Status:** Approved (design)…` to `**Status:** Implemented (multi-page)`; note §6's multi-page discovery step is complete.

- [ ] **Step 3: Verify docs and suite, then commit**

Run: `python3 -m unittest discover -s huion_notes -p 'test_*.py'`
Expected: PASS (unchanged — docs-only task).

```bash
git add README.md docs/offline-notes-overview.md notes/offline-extractor-status.md \
        docs/specs/2026-06-22-note-extractor-design.md
git commit -m "docs(notes): document multi-page extractor CLI; mark Sub-project B implemented"
```

---

## Self-Review

**Spec coverage** (`docs/specs/2026-06-22-note-extractor-design.md` + protocol §10):

- §2 DoD `dump` / `decode` subcommands → Task 7. ✅
- §3 replayable core + thin transport → core Tasks 1–5; transport Task 6. ✅
- §4 module boundaries: `frames` (T1), `auth` (T2), `codec` (T3), `render` (T4), `session` (T5), `transport` (T6), `cli` (T7). ✅
- §5 live handshake → `session.DumpSession` (T5). ✅
- §6 multi-page → **implemented** (count-driven page loop + `0x88` retransmit), fixture-confirmed from `sync-multipage.btsnoop`. ✅
- §7 output SVG/PNG/JSON, per page → `render` (T4) + `cli` per-page loop (T7); JSON schema in Global Constraints, tested in T4. ✅
- §8 error handling: connect/PIN/auth/missing-packets/ImageMagick → `errors`+`session` (T5), `render_png` degrade (T4), `cmd_dump` actionable message (T7). ✅
- §9 testing: unittest; codec/render synthetic; session via fake transport (multi-page, PIN, auth-fail, gap-fill) → T3/T4/T5. ✅
- §10 dependencies: dbus_fast lazy, ImageMagick optional, stdlib → Global Constraints + T6/T7. ✅
- §11 out of scope (AI/PDF/pen-mode/firmware/GUI) → not built. ✅
- §12 privacy: captures gitignored, synthetic tests → Global Constraints. ✅
- **Read-only** (no `DELETE_PAGE`/`CLEAR_CACHE`) → Global Constraints + `frames` documents but never builds them. ✅

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N" — every code step shows complete code. All command/parse vectors are real bytes from `sync-01`/`sync-multipage` and were arithmetically validated.

**Type/name consistency:** `StylusPoint`/`Page`/`Limits` (codec) used identically in render/session/cli; `OrderCode`/`build_command`/`request_page_data`/`build_get_page_package`/`parse_offline_count` (frames) consistent across auth/session; `Transport` contract (`connect`/`send`/`recv`/`close`) matches `FakeTransport` (T5) and `BleTransport` (T6); `decode_page(packets, limits, index)` consistent in codec/session/cli; `pages_from_att` used by cli decode (T7); `render_png(svg_path, png_path, *, which, runner)` consistent in render/cli.

**Known deferred (stated, not gaps):** the page-loop stop condition is `count == 0` (the app pre-knew its page count; `count == 0` is the safe terminator) — confirm on the live device in Task 6. The optional end-to-end check against `sync-multipage.btsnoop` is developer-run (fixture gitignored).
