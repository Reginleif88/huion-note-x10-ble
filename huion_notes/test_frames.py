"""Tests for the frame layer (capture parsing + Huion device framing). Stdlib unittest.

Run: python3 -m unittest huion_notes.test_frames -v
"""
import struct
import unittest

from huion_notes.frames import (
    parse_btsnoop, extract_att_frames,
    OrderCode, parse_huion_frame, build_command,
    request_max_info, request_set_many_packet_distance, request_page_data,
    build_get_page_package, parse_offline_count, heart_beat,
    build_delete_page, build_clear_cache,
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
        self.assertEqual(parse_offline_count(bytes.fromhex("cd86051211")), 4370)  # bytes 12 11 -> LE 0x1112 = 4370
        self.assertEqual(parse_offline_count(bytes.fromhex("cd86051200")), 18)
        self.assertEqual(parse_offline_count(bytes.fromhex("cd86050000")), 0)
        self.assertIsNone(parse_offline_count(bytes.fromhex("cd950b286e00189200ff1f")))  # not 0x86
        self.assertIsNone(parse_offline_count(bytes.fromhex("cd860800000000ed")))  # 0x86 request echo (len 0x08), not a count

    def test_heart_beat(self):
        self.assertEqual(heart_beat().hex(), "cd800800000000ed")

    def test_build_delete_page_and_clear_cache(self):
        # Destructive ops; bytes from the capture + app source (requestDeletePage/requestClear).
        self.assertEqual(build_delete_page(0).hex(), "cd8b0800000000ed")
        self.assertEqual(build_delete_page(1).hex(), "cd8b0801000000ed")
        self.assertEqual(build_clear_cache().hex(), "cd8c0800000000ed")

    def test_set_many_packet_distance_pair(self):
        d1, d2 = request_set_many_packet_distance()
        self.assertEqual((d1.hex(), d2.hex()), ("cd960801030000ed", "cd960803020000ed"))


if __name__ == "__main__":
    unittest.main()
