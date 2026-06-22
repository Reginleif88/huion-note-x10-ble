"""Tests for rendering (SVG/JSON/PNG). Stdlib unittest.

Run: python3 -m unittest huion_notes.test_render -v
"""
import json
import unittest

from huion_notes.codec import Page, StylusPoint
from huion_notes.render import render_svg, render_json, render_png


def _page():
    s1 = [StylusPoint(10, 10, 100, True), StylusPoint(20, 30, 120, True)]
    return Page(index=0, max_x=100.0, max_y=200.0, max_press=8191.0, strokes=[s1])


class SvgTests(unittest.TestCase):
    def test_svg_has_path_and_dimensions(self):
        svg = render_svg(_page())
        self.assertIn("<path", svg)
        self.assertIn('width="900"', svg)
        self.assertIn('height="1190"', svg)
        self.assertTrue(svg.startswith("<svg"))


class JsonTests(unittest.TestCase):
    def test_json_schema_roundtrips(self):
        obj = json.loads(render_json(_page()))
        self.assertEqual(obj["page"], 0)
        self.assertEqual(obj["max_x"], 100.0)
        self.assertEqual(len(obj["strokes"]), 1)
        self.assertEqual(obj["strokes"][0][0], {"x": 10, "y": 10, "press": 100, "pen_down": True})


class PngTests(unittest.TestCase):
    def test_png_returns_false_when_magick_absent(self):
        self.assertFalse(render_png("in.svg", "out.png", which=lambda name: None))

    def test_png_invokes_runner_with_correct_argv(self):
        calls = []

        def fake_runner(argv, **kw):
            calls.append((argv, kw))

        ok = render_png(
            "in.svg", "out.png",
            which=lambda name: "/usr/bin/magick" if name == "magick" else None,
            runner=fake_runner,
        )
        self.assertTrue(ok)
        self.assertEqual(calls, [(["/usr/bin/magick", "in.svg", "out.png"], {"check": True})])


if __name__ == "__main__":
    unittest.main()
