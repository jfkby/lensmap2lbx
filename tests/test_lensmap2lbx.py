"""Tests for lensmap2lbx.  Run:  python -m unittest discover -s tests"""

import io
import sys
import tempfile
import unittest
import xml.dom.minidom
import zipfile
from pathlib import Path

from PIL import ImageOps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lensmap2lbx as core                                  # noqa: E402

EXAMPLE = ROOT / "examples" / "XA100845.XML"
EXAMPLE_KEY = ROOT / "examples" / "XA100845.key.json"
FONTS = (core.find_font(core.FONT_BOLD), core.find_font(core.FONT_REG))


def render(marks, info="", **kw):
    """core.render with the CLI defaults: 18 mm tape, 200 mm label, 195 mm scale."""
    opts = dict(tape_mm=18, label_mm=200.0, scale_mm=195.0, reverse=False,
                rotate180=False, fit=False, baseline=True)
    opts.update(kw)
    extra = {k: opts.pop(k) for k in list(opts)
             if k in ("tick_w", "endstops", "num_h_override", "rotate_marks")}
    return core.render(marks, info, opts["tape_mm"], opts["label_mm"], opts["scale_mm"],
                       opts["reverse"], opts["rotate180"], opts["fit"], opts["baseline"],
                       *FONTS, **extra)


def ink(img):
    """Set of black pixel coordinates."""
    w = img.width
    return {(i % w, i // w) for i, v in enumerate(img.tobytes()) if v == 0}


class Decoding(unittest.TestCase):
    def test_decode_iris(self):
        self.assertEqual(core.decode_iris(16), 2.3)
        self.assertEqual(core.decode_iris(73), 8.0)
        self.assertEqual(core.decode_iris(76), 11.0)
        self.assertEqual(core.decode_iris(81), 16.0)

    def test_fstop_tenths(self):
        cases = {2.0: "2", 2.8: "2.8", 11: "11", 22: "22",
                 2.2: "2 3/10", 2.5: "2 7/10", 5.0: "4 7/10", 9: "8 3/10",
                 2.4: "2 5/10"}                              # 2.4 is photometric
        for f, want in cases.items():
            self.assertEqual(core.fstop_tenths(f), want, f)

    def test_fmt_focus(self):
        self.assertEqual(core.fmt_focus(65535), "∞")
        self.assertEqual(core.fmt_focus(42), "3'6")
        self.assertEqual(core.fmt_focus(36), "3'")
        self.assertEqual(core.fmt_focus(11), '11"')
        self.assertEqual(core.fmt_focus(480, "cm"), "4.8m")
        self.assertEqual(core.fmt_focus(50, "cm"), "50cm")

    def test_mark_labels_overrides(self):
        marks = [(0, 16), (100, 21), (200, 33), (300, 49)]
        out = core.mark_labels(marks, "iris",
                               {"16": "2.2", "21": "f/2.8", "33": "inf", "49": "open"},
                               "in", iris_tenths=True)
        self.assertEqual([t for _, _, t in out], ["2 3/10", "2.8", "inf", "open"])

    def test_t_prefix(self):
        out = core.mark_labels([(0, 21)], "iris", {}, "in", t_prefix=True)
        self.assertEqual(out[0][2], "T2.8")


class Parsing(unittest.TestCase):
    def test_example(self):
        details, channels = core.parse_lensmap(EXAMPLE)
        self.assertEqual(details["brand"], "XELMUS")
        self.assertEqual(len(channels["iris"]), 7)
        self.assertEqual(len(channels["focus"]), 19)
        self.assertEqual(channels["iris"][0], (0, 16))

    def _parse(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "lens.XML"
            p.write_text(text)
            return core.parse_lensmap(p)

    def test_malformed(self):
        with self.assertRaisesRegex(ValueError, "not valid XML"):
            self._parse("<lensMap><lensData")
        with self.assertRaisesRegex(ValueError, "lensMark 1"):
            self._parse("<lensMap><lensData><iris><lensMark><position>1</position>"
                        "</lensMark></iris></lensData></lensMap>")

    def test_load_key(self):
        self.assertEqual(core.load_key(EXAMPLE_KEY)["16"], "2.2")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k.json"
            p.write_text("[1, 2]")
            with self.assertRaisesRegex(ValueError, "expected an object"):
                core.load_key(p)


class Rendering(unittest.TestCase):
    def test_ticks_land_on_exact_dots(self):
        """Each 1-dot tick sits in the column its motor position maps to."""
        positions = [0, 12022, 32043, 45394, 65535]
        marks = [(p, i, str(i)) for i, p in enumerate(positions)]
        img, layout, _ = render(marks, tick_w=1, baseline=False, endstops=False,
                                rotate_marks=False)
        x0_mm = (200.0 - 195.0) / 2 - core.END_MARGIN_PT / core.MM_TO_PT
        want = {round((x0_mm + p / 65535 * 195.0) * core.DPMM) for p in positions}
        bottom = {x for x in range(img.width) if img.getpixel((x, img.height - 1)) == 0}
        self.assertEqual(bottom, want)

    def test_image_size(self):
        img, layout, _ = render([(0, 0, "1"), (65535, 1, "2")])
        self.assertEqual(img.height, 112)                   # 18 mm band at 180 dpi
        self.assertEqual(img.width, round(layout["printable_mm"] * core.DPMM))
        self.assertEqual(set(img.tobytes()) - {0, 255}, set())   # hard 1-bit

    def test_overlapping_vertical_labels_keep_their_ink(self):
        """A crowded label must not erase its neighbour's ink."""
        a, b = (30000, 1, "1000"), (30150, 2, "3000")
        ink_a = ink(render([a], num_h_override=28, endstops=False, rotate_marks=True)[0])
        img_ab, layout, _ = render([a, b], num_h_override=28, endstops=False,
                                   rotate_marks=True)
        self.assertTrue(ink_a <= ink(img_ab))
        self.assertTrue(any("too close" in w for w in layout["warnings"]))

    def test_infinity_stays_upright(self):
        """Rotated 90 degrees an infinity sign reads as an 8: it must stay wide."""
        img, _, boxes = render([(767, 65535, "∞"), (30000, 36, "3'")],
                               rotate_marks=True, baseline=False, endstops=False)
        (_, _, x0, x1), = [bx for bx in boxes if bx[1] == "∞"]
        glyph = img.crop((int(x0), 0, int(x1), img.height - 20))   # above the tick
        left, top, right, bottom = ImageOps.invert(glyph).getbbox()
        self.assertGreater(right - left, bottom - top)

    def test_warnings_are_returned_not_printed(self):
        _, channels = core.parse_lensmap(EXAMPLE)
        marks = core.mark_labels(channels["focus"], "focus", {}, "in")
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            _, layout, _ = render(marks, info="some info line", rotate_marks=True)
        finally:
            sys.stderr = old
        self.assertEqual(err.getvalue(), "")
        self.assertTrue(any("info line omitted" in w for w in layout["warnings"]))

    def test_bad_scale_exits(self):
        with self.assertRaises(SystemExit):
            render([(0, 0, "1")], scale_mm=0)
        with self.assertRaises(SystemExit):
            render([(0, 0, "1")], scale_mm=199)             # wider than printable


class LbxFile(unittest.TestCase):
    def test_container_and_escaping(self):
        img, _, _ = render([(0, 0, "1"), (65535, 1, "2")])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "t.lbx"
            core.write_lbx(out, img, 18, 200.0, 'A&B "Lens" <100mm>', 'a&b "x".png')
            with zipfile.ZipFile(out) as z:
                self.assertEqual(sorted(z.namelist()),
                                 ["Object0.bmp", "label.xml", "prop.xml"])
                for name in ("label.xml", "prop.xml"):
                    xml.dom.minidom.parseString(z.read(name))        # must be well-formed
                title = xml.dom.minidom.parseString(z.read("prop.xml")) \
                    .getElementsByTagName("dc:title")[0].firstChild.data
                self.assertEqual(title, 'A&B "Lens" <100mm>')


try:
    import lensmap2lbx_gui as gui
except ImportError:                                          # no tkinter available
    gui = None


@unittest.skipIf(gui is None, "tkinter not available")
class GuiHelpers(unittest.TestCase):
    def test_parse_manual_maps(self):
        self.assertEqual(gui.parse_manual_maps("16=2.2, 21=2.8"),
                         {"16": "2.2", "21": "2.8"})
        with self.assertRaises(ValueError):
            gui.parse_manual_maps("=2.2")

    def test_warning_text(self):
        layout = {"warnings": ["note: info line omitted", "warning: marks too close"]}
        self.assertEqual(gui.warning_text(layout), "info line omitted; marks too close")


if __name__ == "__main__":
    unittest.main()
