"""Render decoded pages to SVG / JSON / PNG (spec §7).

SVG is the pure-Python vector master; JSON is the ordered, lossless point dump
(the high-accuracy input for AI handwriting recognition); PNG is produced by
shelling out to ImageMagick and degrades gracefully (returns False) if absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from huion_notes.codec import Page


def render_svg(page: Page, width: int = 900, height: int = 1190, pad: int = 15) -> str:
    """strokes -> SVG paths. Origin top-left, no axis flip (non-A4 device)."""
    def sx(x: int) -> float:
        return pad + (x / page.max_x) * (width - 2 * pad)

    def sy(y: int) -> float:
        return pad + (y / page.max_y) * (height - 2 * pad)

    paths = []
    for s in page.strokes:
        d = " ".join(
            f"{'M' if i == 0 else 'L'}{sx(p.x):.1f},{sy(p.y):.1f}"
            for i, p in enumerate(s)
        )
        paths.append(f'<path d="{d}" fill="none" stroke="#111" stroke-width="2.5"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="background:#fff">' + "".join(paths) + "</svg>"
    )


def render_json(page: Page) -> str:
    """Ordered, lossless point dump (spec §7 schema)."""
    return json.dumps(
        {
            "page": page.index,
            "max_x": page.max_x,
            "max_y": page.max_y,
            "max_press": page.max_press,
            "strokes": [
                [{"x": p.x, "y": p.y, "press": p.press, "pen_down": p.pen_down} for p in s]
                for s in page.strokes
            ],
        }
    )


def render_png(svg_path: str, png_path: str, *, which=shutil.which, runner=subprocess.run) -> bool:
    """Convert an SVG file to PNG via ImageMagick. Returns False if unavailable."""
    exe = which("magick") or which("convert")
    if not exe:
        return False
    runner([exe, svg_path, png_path], check=True)
    return True
