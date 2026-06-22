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
    DELETE_PAGE = 0x8B          # destructive — sent only after a verified export (opt out: --keep)
    CLEAR_CACHE = 0x8C          # destructive — clears the offline cache after deleting exported pages
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


def build_delete_page(page: int) -> bytes:
    """DELETE_PAGE — remove one stored page by index: cd 8b 08 <page_lo> <page_hi> 00 00 ed.
    DESTRUCTIVE — send only after that page has been exported and saved to disk."""
    return bytes([
        START, OrderCode.DELETE_PAGE, 0x08,
        page & 0xFF, (page >> 8) & 0xFF, 0x00, 0x00, END,
    ])


def build_clear_cache() -> bytes:
    """CLEAR_CACHE — clear the device's offline cache: cd 8c 08 00 00 00 00 ed.
    DESTRUCTIVE — send only after exported pages have been deleted."""
    return build_command(OrderCode.CLEAR_CACHE, 0, 0, 0, 0)


def parse_offline_count(value: bytes) -> "int | None":
    """Parse a REQUEST_OFFLINE_DATA response `cd 86 05 <count_lo> <count_hi>` ->
    packet count (u16 LE). Returns None if not such a frame."""
    if len(value) >= 5 and value[0] == START and value[1] == OrderCode.REQUEST_OFFLINE_DATA and value[2] == 0x05:
        return value[3] | (value[4] << 8)
    return None


def heart_beat() -> bytes:
    return build_command(OrderCode.HEART_BEAT, 0, 0, 0, 0)
