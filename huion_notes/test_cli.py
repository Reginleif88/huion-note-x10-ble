"""Tests for the CLI decode path. Stdlib unittest.

Run: python3 -m unittest huion_notes.test_cli -v
"""
import glob
import json
import os
import struct
import tempfile
import unittest

from huion_notes.cli import main, _page_saved

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
            # Files are named page{N}-DD-MM.{svg,png,json}: 1-based page + dump date.
            for n in (1, 2):
                self.assertEqual(len(glob.glob(os.path.join(out, f"page{n}-*.svg"))), 1)
            page1_json = glob.glob(os.path.join(out, "page1-*.json"))
            self.assertEqual(len(page1_json), 1)
            with open(page1_json[0]) as fh:
                obj = json.load(fh)
            self.assertEqual(obj["max_x"], 28200.0)
            self.assertEqual(len(obj["strokes"]), 1)


class PageSavedGateTests(unittest.TestCase):
    """The gate that protects against deleting a page that wasn't safely written."""

    def test_page_saved_requires_nonempty_svg_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "page1-01-01")
            self.assertFalse(_page_saved(base))            # nothing written yet
            with open(base + ".svg", "w") as fh:
                fh.write("<svg/>")
            self.assertFalse(_page_saved(base))            # JSON still missing
            with open(base + ".json", "w") as fh:
                fh.write("{}")
            self.assertTrue(_page_saved(base))             # both present + non-empty
            open(base + ".svg", "w").close()               # SVG truncated to empty
            self.assertFalse(_page_saved(base))            # empty file -> not saved


if __name__ == "__main__":
    unittest.main()
