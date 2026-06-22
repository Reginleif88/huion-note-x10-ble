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
    """Read frames until one has opcode `op`; ignore others (heartbeats, etc.).

    Bounded by an overall deadline so a device that streams only non-matching
    frames cannot hang the dump indefinitely.
    """
    async def _await_op() -> frames.HuionFrame:
        while True:
            value = await transport.recv(timeout=timeout)
            fr = frames.parse_huion_frame(value)
            if fr and fr.op == op:
                return fr
    return await asyncio.wait_for(_await_op(), timeout=timeout * 3)


class DumpSession:
    def __init__(self, transport: "Transport", pin: Optional[str] = None,
                 idle_timeout: float = 5.0, max_pages: int = 64):
        self.t = transport
        self.pin = pin
        self.idle = idle_timeout
        self.max_pages = max_pages
        self.incomplete: set = set()  # page indices that finished with missing packets

    async def run(self) -> list[codec.Page]:
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
        # The device emits its challenge only AFTER the client pokes it with a
        # VERIFY_CONNECT request (confirmed live + in capture: the app writes
        # `cd 81 08 00 00 00 00 ed` first, then the device replies `cd 81 06 a b c`).
        await self.t.send(frames.build_command(OrderCode.VERIFY_CONNECT))
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
            self.incomplete.add(page)
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
            if 1 <= seq <= count:
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
                    idx = codec.packet_seq(fr.raw)
                    if 1 <= idx <= count:
                        got[idx] = fr.raw
                        if all(s in got for s in range(1, count + 1)):
                            break

    # --- destructive: device cleanup after a verified export (opt out with --keep) ---

    async def delete_page(self, page: int) -> bool:
        """Delete one stored page by index. Returns True iff the device confirms.
        Call ONLY after the page has been exported and saved to disk."""
        await self.t.send(frames.build_delete_page(page))
        try:
            resp = await _recv_op(self.t, OrderCode.DELETE_PAGE)
        except (asyncio.TimeoutError, TransportClosed):
            return False
        return len(resp.raw) > 3 and resp.raw[3] == 1

    async def delete_pages(self, pages) -> int:
        """Delete each page index in turn; returns how many the device confirmed."""
        deleted = 0
        for p in pages:
            if await self.delete_page(p):
                deleted += 1
            else:
                log.warning("device did not confirm delete of page %d", p)
        return deleted

    async def clear_cache(self) -> bool:
        """Clear the device's offline cache (after deleting exported pages)."""
        await self.t.send(frames.build_clear_cache())
        try:
            resp = await _recv_op(self.t, OrderCode.CLEAR_CACHE)
        except (asyncio.TimeoutError, TransportClosed):
            return False
        return len(resp.raw) > 3 and resp.raw[3] == 1
