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
        a, b, c = 200, 100, 50  # distinct + (a+b)<<2 and (b+c)<<2 exceed 255 (wrap)
        r1, r2, r3 = verify_response(a, b, c)
        self.assertEqual(r1, ((a + b) << 2) % 255)
        self.assertEqual(r2, ((b + c) << 2) % 255)
        self.assertEqual(r3, ((c + 10) << 2) % 255)
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
