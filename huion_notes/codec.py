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
    strokes: list[list[StylusPoint]]


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
