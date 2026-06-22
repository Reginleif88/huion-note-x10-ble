"""huion-x10-notes CLI: `decode` (replay a capture) and `dump` (live BLE).

Pure modules are imported at top; the live `dump` dependencies (transport,
session, driver) are imported lazily inside cmd_dump so this module and the
`decode` path work without dbus_fast installed.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

from huion_notes import codec, frames, render


def _write_page(page, out_dir: str, date_tag: str) -> str:
    # 1-based page number + DD-MM dump date, e.g. page1-22-06.{svg,png,json}
    base = os.path.join(out_dir, f"page{page.index + 1}-{date_tag}")
    with open(base + ".svg", "w") as fh:
        fh.write(render.render_svg(page))
    with open(base + ".json", "w") as fh:
        fh.write(render.render_json(page))
    if not render.render_png(base + ".svg", base + ".png"):
        print(f"warning: ImageMagick not found; wrote SVG+JSON only for {base}", file=sys.stderr)
    return base


def _page_saved(base: str) -> bool:
    """True iff the page's SVG + JSON exist and are non-empty (PNG is optional).
    This is the gate for deleting a page from the device — never delete unsaved data."""
    return all(
        os.path.exists(base + ext) and os.path.getsize(base + ext) > 0
        for ext in (".svg", ".json")
    )


def cmd_decode(args) -> int:
    with open(args.file, "rb") as fh:
        att = frames.extract_att_frames(frames.parse_btsnoop(fh.read()))
    limits = codec.limits_from_att(att)
    page_streams = codec.pages_from_att(att)
    if not page_streams:
        print("error: no offline page data (0x87 packets) found in capture", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)
    date_tag = datetime.date.today().strftime("%d-%m")
    for i, packets in enumerate(page_streams):
        page = codec.decode_page(packets, limits, index=i)
        base = _write_page(page, args.out, date_tag)
        print(f"page {i + 1}: {len(page.strokes)} strokes -> {base}.{{svg,png,json}}")
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

    async def _run() -> int:
        t = BleTransport(mac)
        try:
            session = DumpSession(t, pin=args.pin)
            pages = await session.run()
            if not pages:
                print("no offline pages found on the device.")
                return 0
            os.makedirs(args.out, exist_ok=True)
            date_tag = datetime.date.today().strftime("%d-%m")
            exported = []  # page indices saved to disk AND complete -> safe to delete
            for page in pages:
                base = _write_page(page, args.out, date_tag)
                if not _page_saved(base):
                    tag = "  (NOT saved -> kept on device)"
                elif page.index in session.incomplete:
                    tag = "  (incomplete -> kept on device)"
                else:
                    exported.append(page.index)
                    tag = ""
                print(f"page {page.index + 1}: {len(page.strokes)} strokes -> {base}.{{svg,png,json}}{tag}")
            # Clear the device only for pages verified on disk, unless --keep.
            if args.keep:
                print(f"--keep: all {len(pages)} page(s) left on the device.")
            elif exported:
                deleted = await session.delete_pages(exported)
                await session.clear_cache()
                kept = len(pages) - len(exported)
                msg = f"cleared {deleted}/{len(exported)} exported page(s) from the device."
                if kept:
                    msg += f" ({kept} kept - not safely exported)"
                print(msg)
            return 0
        finally:
            await t.close()

    try:
        return asyncio.run(_run())
    except Exception as e:  # actionable message, not a traceback
        print(f"error: dump failed: {e}", file=sys.stderr)
        return 1


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
    u.add_argument("--keep", action="store_true",
                   help="keep pages on the device (default: delete each page after it is safely exported)")
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
