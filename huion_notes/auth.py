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
