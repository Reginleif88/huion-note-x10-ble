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

    def test_trailing_single_point_dropped(self):
        pts = [StylusPoint(1, 1, 100, True), StylusPoint(2, 2, 100, True),
               StylusPoint(0, 0, 0, False),       # pen up -> closes first stroke
               StylusPoint(3, 3, 100, True)]      # lone trailing point -> dropped (<2)
        self.assertEqual([len(s) for s in points_to_strokes(pts)], [2])

    def test_zero_pressure_splits_even_when_pen_down(self):
        pts = [StylusPoint(1, 1, 100, True), StylusPoint(2, 2, 100, True),
               StylusPoint(3, 3, 0, True),        # pen_down but press 0 -> boundary
               StylusPoint(4, 4, 100, True), StylusPoint(5, 5, 100, True)]
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
