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
def _del_ack(): return bytes([0xCD, OrderCode.DELETE_PAGE, 0x04, 0x01])    # device confirms delete
def _clr_ack(): return bytes([0xCD, OrderCode.CLEAR_CACHE, 0x04, 0x01])    # device confirms clear


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
        # Client pokes VERIFY_CONNECT first, then VERIFY_RESULT, then setup.
        self.assertEqual(t.sent[0], frames.build_command(OrderCode.VERIFY_CONNECT))
        self.assertEqual(t.sent[1].hex(), "cd820842fe3d00ed")
        self.assertEqual(t.sent[2], frames.request_max_info())
        d1, d2 = frames.request_set_many_packet_distance()
        self.assertEqual((t.sent[3], t.sent[4]), (d1, d2))
        # Page loop probed 0,1,2 (2 was empty).
        self.assertEqual([frames.parse_huion_frame(b).raw[3] for b in t.page_requests()], [0, 1, 2])
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].index, 0)
        self.assertEqual(pages[0].max_x, 28200.0)


class PinPathTests(unittest.TestCase):
    def test_pin_required_then_accepted(self):
        inbound = [_vc(22, 122, 69), _vr(2), _vr(1), _maxd(), _count(0)]
        t = FakeTransport(inbound)
        pages = asyncio.run(DumpSession(t, pin="123456", idle_timeout=0.01).run())
        self.assertEqual(t.sent[2].hex(), "cd83080199a79ced")
        self.assertEqual(t.sent[3].hex(), "cd830802a3a359ed")
        self.assertEqual(t.sent[4], frames.request_max_info())  # proceeded past auth
        self.assertEqual(pages, [])                              # count 0 -> no pages

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


class DeleteAndCleanupTests(unittest.TestCase):
    def test_delete_pages_then_clear(self):
        t = FakeTransport([_del_ack(), _del_ack(), _clr_ack()])
        s = DumpSession(t, idle_timeout=0.01)

        async def go():
            n = await s.delete_pages([0, 2])
            ok = await s.clear_cache()
            return n, ok

        deleted, cleared = asyncio.run(go())
        self.assertEqual(deleted, 2)
        self.assertTrue(cleared)
        self.assertEqual(t.sent[0], frames.build_delete_page(0))
        self.assertEqual(t.sent[1], frames.build_delete_page(2))
        self.assertEqual(t.sent[2], frames.build_clear_cache())

    def test_incomplete_page_is_tracked(self):
        # count=2 but only seq 1 arrives and no retransmit answers -> page 0 incomplete.
        inbound = [_vc(22, 122, 69), _vr(1), _maxd(), _count(2), _p87(1)]
        t = FakeTransport(inbound)
        s = DumpSession(t, idle_timeout=0.01, max_pages=1)
        pages = asyncio.run(s.run())
        self.assertEqual(len(pages), 1)
        self.assertIn(0, s.incomplete)   # cmd_dump uses this to refuse deleting it


if __name__ == "__main__":
    unittest.main()
