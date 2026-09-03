#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lensmap2lbx.py — Generate a Brother P-touch .lbx scale label from a lens-map XML.

Reads a <lensMap> XML (as exported for FIZ motor systems: <lensDetails> +
<lensData>/<iris|focus>/<lensMark>/<position>+<encoded>) and produces:

  1. A .lbx file (P-touch Editor project) containing the scale rendered as a
     1:1 monochrome bitmap at 180 dpi — the native P-touch print resolution —
     so every tick mark lands on the exact printer dot for its motor position.
  2. A .png preview that is the *identical* raster that will print
     (tagged 180 dpi, so image viewers show it at true physical size).

Geometry
--------
* <position> is a 16-bit motor value. By default 0..65535 maps linearly onto
  the scale span (default 195.0 mm). Use --fit to map min..max marks instead.
* Total label length defaults to 200.0 mm with the scale centered, leaving
  (length - scale)/2 at each end.
* Ticks rise from the BOTTOM edge of the printable band, ending on the last
  dot row the print head covers, so the scale sits as close to the physical
  tape edge as the hardware allows (the unprintable side margin — ~1.1 mm on
  18 mm tape — is a print-head limit). Numbers sit above the ticks, and the
  remaining tape height above them carries an info line
  (brand, model, focal length, serial, squeeze) built from <lensDetails>,
  or your own --info text.

Iris value decoding
-------------------
<encoded> for the iris channel is an index into an f-number table:
    f = (encoded + 7) / 10      for encoded <= 73   (0.1-stop steps, T0.7–T8)
    f = encoded - 65            for encoded >= 74   (whole stops T9, T10, T11 …)
Because a lens ring may be engraved with a value the table can't represent
exactly (e.g. a T2.2 lens stored as index 16 = 2.3), you can override any
mark's printed text with --map ENC=TEXT (repeatable) or --key key.json
({"16": "2.2", ...}). Overrides are printed verbatim.

Focus channel (--channel focus) decodes <encoded> as distance: 65535 = INF,
otherwise inches (default) or cm (--focus-units cm).

Iris values print as tenths of a stop BY DEFAULT (--no-iris-tenths for
plain f-numbers). --iris-tenths reformats iris values the way a light meter
reads: tenths of
a stop above the last nominal full stop (1, 1.4, 2, 2.8, 4, 5.6, 8, 11 ...).
T2.2 -> '2 3/10', T2.4 -> '2 5/10'; full stops print plain. Nominal
third-stop engravings (1.1 1.2 1.6 1.8 2.2 2.5 3.2 3.5 4.5 5 6.3 7.1 9 10
13 14 18 20 25 28/29 36 40 51 57) snap to 3/10 and 7/10 like a meter reads;
everything else uses exact photometric tenths. Fractions render stacked —
numerator over a bar over the denominator, one digit tall and about one
character wide (falls back to inline text if the band is too small for
legible fraction digits). Numeric override values are reformatted too;
non-numeric override text stays verbatim.

Mark numbers print rotated 90° BY DEFAULT (reading bottom-to-top), each
stacked above a short ruler-style tick that points at the middle of the
character height. A number's footprint along the scale is then one text
height, so crowded scales keep full-size numbers. --no-rotate-marks gives
the horizontal style with numbers beside taller ticks.

End stops: motor 0 and 65535 are the mechanical travel limits. If a barrel
travels past its outermost mark (the mark sits > 1.5 mm inside the stop on
the printed scale), the stop is drawn as an unlabeled bracket — a full-height
tick with a short arm pointing inward over the travel range. Disable with
--no-endstops. Stops that coincide with a mark are skipped automatically.

The .lbx internals mirror a file saved by P-touch Editor itself
(24mm/PT-P710BT reference), so the editor opens it as a normal project with
one full-bleed image object. Supported tape widths: 6, 9, 12, 18, 24 mm.
"""

import argparse
import datetime
import io
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("This tool needs Pillow:  pip install pillow")

DPI = 180.0                    # P-touch print resolution (fixed, all TZe models)
DPMM = DPI / 25.4              # dots per mm  (7.0866)
PT_PER_PX = 72.0 / DPI         # 0.4 pt per dot
MM_TO_PT = 72.0 / 25.4         # 2.83465
END_MARGIN_PT = 5.6            # leader/trailer margin along the length (both ends)

# Tape geometry taken from P-touch Editor's own files (values in pt).
# band_pt is the printable height across the tape; all convert to whole
# 180-dpi dots (px = pt / 0.4): 24mm->128, 18mm->112, 12mm->70, 9mm->50, 6mm->32.
TAPES = {
    6:  dict(width_pt=16.8, side_margin_pt=2.0, fmt="257", band_pt=12.8),
    9:  dict(width_pt=25.6, side_margin_pt=2.8, fmt="258", band_pt=20.0),
    12: dict(width_pt=33.6, side_margin_pt=2.8, fmt="259", band_pt=28.0),
    18: dict(width_pt=51.2, side_margin_pt=3.2, fmt="260", band_pt=44.8),
    24: dict(width_pt=68.0, side_margin_pt=8.4, fmt="261", band_pt=51.2),
}

__version__ = "1.3.0"


def app_dir():
    """Folder holding bundled resources: the PyInstaller extraction dir when
    frozen, else the directory of this source file."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


FONT_DIRS = [
    str(app_dir() / "fonts"),           # bundled — identical metrics everywhere
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/Library/Fonts", "/System/Library/Fonts", "C:/Windows/Fonts",
]
FONT_BOLD = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"]
FONT_REG = ["DejaVuSansCondensed.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf", "arial.ttf"]


# ----------------------------------------------------------------------------
# lens-map parsing & decoding
# ----------------------------------------------------------------------------

def parse_lensmap(path):
    root = ET.parse(path).getroot()
    details = {}
    d = root.find("lensDetails")
    if d is not None:
        details = {c.tag: (c.text or "").strip() for c in d}
    data = root.find("lensData")
    channels = {}
    if data is not None:
        for section in data:
            marks = []
            for m in section.findall("lensMark"):
                pos = int(m.findtext("position"))
                enc = int(m.findtext("encoded"))
                marks.append((pos, enc))
            channels[section.tag] = sorted(marks)
    return details, channels


def decode_iris(enc):
    """encoded -> f-number (float). Table: 0.1 steps to f/8.0, whole stops above."""
    return (enc + 7) / 10.0 if enc <= 73 else float(enc - 65)


def fmt_fnum(f):
    s = f"{f:.1f}"
    return s[:-2] if s.endswith(".0") else s


def fmt_focus(enc, units="in"):
    if enc >= 65535:
        return "\u221e"                       # infinity
    if units == "cm":
        return f"{enc / 100:g}m" if enc >= 100 else f"{enc}cm"
    ft, inches = divmod(enc, 12)
    if ft == 0:
        return f'{inches}"'
    return f"{ft}'" + (f"{inches}" if inches else "")


# nominal full-stop ladder by index n = whole stops above T1
FULL_STOPS = {-2: "0.5", -1: "0.7", 0: "1", 1: "1.4", 2: "2", 3: "2.8", 4: "4",
              5: "5.6", 6: "8", 7: "11", 8: "16", 9: "22", 10: "32", 11: "45",
              12: "64", 13: "90", 14: "128"}


# nominal 1/3-stop engravings -> (full-stop index, tenths): these snap to
# 3/10 and 7/10 like a meter, instead of their exact photometric tenths
THIRD_STOPS = {80: (-1, 3), 90: (-1, 7), 110: (0, 3), 120: (0, 7),
               160: (1, 3), 180: (1, 7), 220: (2, 3), 250: (2, 7),
               320: (3, 3), 350: (3, 7), 450: (4, 3), 500: (4, 7),
               630: (5, 3), 710: (5, 7), 900: (6, 3), 1000: (6, 7),
               1300: (7, 3), 1400: (7, 7), 1800: (8, 3), 2000: (8, 7),
               2500: (9, 3), 2800: (9, 7), 2900: (9, 7),
               3600: (10, 3), 4000: (10, 7), 5100: (11, 3), 5700: (11, 7)}


def fstop_tenths(f):
    """f-number -> light-meter style 'FULL n/10' (tenths of a stop above the
    last full stop).  2.2 -> '2 3/10', 2.4 -> '2 5/10', 2.8 -> '2.8'.
    Nominal engravings (11, 22, 45 ...) snap to their full stop and nominal
    thirds (2.2, 3.2, 9, 13 ...) snap to 3/10 and 7/10."""
    n_t = THIRD_STOPS.get(round(f * 100))
    if n_t:
        return f"{FULL_STOPS[n_t[0]]} {n_t[1]}/10"
    stops = 2.0 * math.log2(f)                  # exact stops above T1
    if abs(stops - round(stops)) <= 0.12:       # nominal full stop (11 = 6.92 ...)
        n, tenths = round(stops), 0
    else:
        n = math.floor(stops)
        tenths = int((stops - n) * 10 + 0.5)
        if tenths == 10:
            n, tenths = n + 1, 0
    base = FULL_STOPS.get(n)
    if base is None:
        return fmt_fnum(f)                      # off the ladder — leave as-is
    return base if tenths == 0 else f"{base} {tenths}/10"


def mark_labels(marks, channel, overrides, focus_units, iris_tenths=False,
                t_prefix=False):
    """[(pos, enc)] -> [(pos, enc, text)] applying overrides > formula.
    iris_tenths reformats iris values (incl. numeric overrides) as tenths of
    a stop; non-numeric override text is kept verbatim. t_prefix prepends 'T'
    to iris labels that don't already start with T/t."""
    out = []
    for pos, enc in marks:
        if str(enc) in overrides:
            text = overrides[str(enc)]
            if iris_tenths and channel == "iris":
                try:
                    text = fstop_tenths(float(text.lstrip("TtFf")))
                except ValueError:
                    pass                        # verbatim
        elif channel == "iris":
            f = decode_iris(enc)
            text = fstop_tenths(f) if iris_tenths else fmt_fnum(f)
        else:
            text = fmt_focus(enc, focus_units)
        if t_prefix and channel == "iris" and not text[:1] in ("T", "t"):
            text = "T" + text
        out.append((pos, enc, text))
    return out


# ----------------------------------------------------------------------------
# raster rendering
# ----------------------------------------------------------------------------

def find_font(candidates):
    for d in FONT_DIRS:
        for name in candidates:
            p = Path(d) / name
            if p.exists():
                return str(p)
    return None


def font_for_height(path, target_px, sample="0123456789."):
    """Return an ImageFont whose digit glyphs are ~target_px tall."""
    size = max(4, int(target_px * 1.2))
    for _ in range(24):
        f = ImageFont.truetype(path, size)
        box = ImageDraw.Draw(Image.new("L", (8, 8))).textbbox((0, 0), sample, font=f)
        h = box[3] - box[1]
        if abs(h - target_px) <= 1:
            return f
        size = max(4, size + (1 if h < target_px else -1) * max(1, abs(h - target_px) // 2))
    return ImageFont.truetype(path, size)


FRAC_RE = re.compile(r"^(.+?) (\d+)/(\d+)$")


def label_metrics(draw, text, f_num, f_frac):
    """Measure a mark label. 'MAIN d/10' becomes main text plus a stacked
    fraction cell (numerator over bar over denominator, one digit-height tall,
    roughly one character wide). f_frac None -> plain text fallback."""
    m = FRAC_RE.match(text) if f_frac else None
    if not m:
        b = draw.textbbox((0, 0), text, font=f_num)
        return dict(kind="plain", text=text, main=text, mb=b, font=f_num,
                    w=b[2] - b[0])
    main, num, den = m.groups()
    mb = draw.textbbox((0, 0), main, font=f_num)
    nb = draw.textbbox((0, 0), num, font=f_frac)
    db = draw.textbbox((0, 0), den, font=f_frac)
    fw = max(nb[2] - nb[0], db[2] - db[0])
    fgap = max(2, round((mb[3] - mb[1]) * 0.10))
    return dict(kind="frac", text=text, main=main, num=num, den=den,
                mb=mb, nb=nb, db=db, font=f_num, ffont=f_frac,
                fw=fw, fgap=fgap, H=mb[3] - mb[1],
                w=(mb[2] - mb[0]) + fgap + fw)


def draw_label(draw, x_left, y_bot, met):
    """Draw a measured label: ink left edge at x_left, ink bottom at y_bot."""
    mb = met["mb"]
    draw.text((x_left - mb[0], y_bot - mb[3]), met["main"], font=met["font"], fill=0)
    if met["kind"] == "plain":
        return
    H, nb, db = met["H"], met["nb"], met["db"]
    fx = x_left + (mb[2] - mb[0]) + met["fgap"]
    top = y_bot - H
    draw.text((fx + (met["fw"] - (nb[2] - nb[0])) / 2.0 - nb[0], top - nb[1]),
              met["num"], font=met["ffont"], fill=0)
    draw.text((fx + (met["fw"] - (db[2] - db[0])) / 2.0 - db[0], y_bot - db[3]),
              met["den"], font=met["ffont"], fill=0)
    bar_h = max(1, round(H * 0.05))
    ybar = round(y_bot - H / 2.0 - bar_h / 2.0)
    draw.rectangle([round(fx), ybar, round(fx + met["fw"] - 1), ybar + bar_h - 1],
                   fill=0)


def render(marks, info_text, tape_mm, label_mm, scale_mm, reverse, rotate180,
           fit, baseline, font_bold, font_reg, tick_w=3, endstops=True,
           num_h_override=None, rotate_marks=False):
    """Returns (PIL image, layout dict, list of placed-label x-extents in px)."""
    band_px = round(TAPES[tape_mm]["band_pt"] / PT_PER_PX)          # image height
    end_margin_mm = END_MARGIN_PT / MM_TO_PT                        # 1.976 mm
    printable_mm = label_mm - 2 * end_margin_mm
    if scale_mm > printable_mm + 1e-6:
        sys.exit(f"scale {scale_mm} mm does not fit: label {label_mm} mm has "
                 f"{printable_mm:.2f} mm printable (leader/trailer {end_margin_mm:.2f} mm each end)")
    img_w = round(printable_mm * DPMM)
    img = Image.new("L", (img_w, band_px), 255)
    draw = ImageDraw.Draw(img)

    scale_x0_mm = (label_mm - scale_mm) / 2.0 - end_margin_mm       # rel. to printable origin
    positions = [p for p, _, _ in marks]
    lo, hi = (min(positions), max(positions)) if fit else (0, 65535)
    span = max(1, hi - lo)

    def x_f(pos):
        """Exact (float) pixel coordinate of a motor position."""
        frac = (pos - lo) / span
        if reverse:
            frac = 1.0 - frac
        return (scale_x0_mm + frac * scale_mm) * DPMM

    def stroke(c):
        """Leftmost column of a tick_w-wide stroke centered on float coord c."""
        return round(c - (tick_w - 1) / 2.0)

    # vertical layout (px), proportional to band height
    tick_h = max(14, round(band_px * 0.30))
    if rotate_marks:
        tick_h = max(10, round(band_px * 0.14))
    num_h = max(11, min(round(band_px * 0.32), 36))
    gap = max(2, round(band_px * 0.06))
    info_h = max(9, min(round(band_px * 0.17), 19))
    base_h = 1 if baseline else 0

    f_num = font_for_height(font_bold, num_h)
    f_info = font_for_height(font_reg, info_h, sample="ABCXYZ0123456789")

    if baseline:
        a, b = sorted((stroke(x_f(lo)), stroke(x_f(hi))))
        draw.rectangle([a, band_px - base_h, b + tick_w - 1, band_px - 1], fill=0)

    for pos, _, _ in marks:                                         # ticks first — these carry accuracy
        x0 = stroke(x_f(pos))
        draw.rectangle([x0, band_px - (base_h + tick_h), x0 + tick_w - 1, band_px - 1], fill=0)

    # end stops: motor 0 / 65535 are the mechanical travel limits. When the
    # outermost lens mark sits clearly inside a stop (barrel travels past its
    # last mark), draw the stop as a bracket: a full-height tick plus a short
    # arm along the tick top pointing inward over the travel range.
    ES_CLEAR_MM = 1.5                       # skip a stop if a mark is this close
    drawn_stops = []
    if endstops:
        xs_marks = [x_f(p) for p, _, _ in marks]
        x_stop_a, x_stop_b = x_f(0), x_f(65535)
        mid = (x_stop_a + x_stop_b) / 2.0
        for stop_pos, x_stop in ((0, x_stop_a), (65535, x_stop_b)):
            dist = min(abs(x_stop - xm) for xm in xs_marks)
            if dist < ES_CLEAR_MM * DPMM:
                continue                                            # mark is (at) the stop
            x0 = stroke(x_stop)
            if x0 < 0 or x0 + tick_w > img_w:
                print(f"warning: end stop (motor {stop_pos}) falls outside the "
                      f"printable area — not drawn", file=sys.stderr)
                continue
            top = band_px - (base_h + tick_h)
            draw.rectangle([x0, top, x0 + tick_w - 1, band_px - 1], fill=0)
            arm = max(3, min(6, int(dist) - tick_w - 3))
            if x_stop <= mid:
                draw.rectangle([x0 + tick_w, top, x0 + tick_w - 1 + arm, top + tick_w - 1], fill=0)
            else:
                draw.rectangle([x0 - arm, top, x0 - 1, top + tick_w - 1], fill=0)
            drawn_stops.append(stop_pos)

    # labels
    label_boxes = []
    trials = ((num_h_override,) if num_h_override else
              (num_h, round(num_h * 0.88), round(num_h * 0.78), round(num_h * 0.68)))
    rot_info_ok = rotate_marks and bool(info_text) and band_px >= 60
    if rotate_marks:
        # vertical numbers: each label is rendered horizontally, rotated 90°
        # CCW (reads bottom-to-top) and stacked above its (shortened) tick,
        # centered so the tick line points at the middle of the character
        # height. Footprint along the scale is one text HEIGHT, so dense
        # scales keep full-size numbers. Edge marks clamp inward to stay on
        # the label, like horizontal mode.
        GAP_V = 3                                                   # tick top -> number
        reserve = (min(round(band_px * 0.17), 19) + 6) if rot_info_ok else 2
        anchor = band_px - base_h - tick_h - GAP_V                  # number bottoms
        avail = anchor - reserve                                    # max number length

        def measure(th):
            f_n = font_for_height(font_bold, max(9, th))
            fh = round(max(9, th) * 0.42)
            f_f = font_for_height(font_bold, fh) if fh >= 8 else None
            ms = [label_metrics(draw, text, f_n, f_f) for _, _, text in marks]
            fp = max(m.get("H", m["mb"][3] - m["mb"][1]) for m in ms)
            xs = sorted(x_f(p) for p, _, _ in marks)
            ok_p = all(b - a >= fp + 4 for a, b in zip(xs, xs[1:]))
            return ms, ok_p, max(m["w"] for m in ms)

        # priority: keep the info line, shrinking marks to coexist with it;
        # drop it only if even the smallest tier cannot fit alongside it
        chosen = None
        for trial_h in trials:
            mets, pitch_ok, maxw = measure(trial_h)
            if num_h_override or (pitch_ok and maxw <= avail):
                chosen = (trial_h, mets)
                break
        if chosen is None:
            if rot_info_ok:
                rot_info_ok, info_text = False, ""
                avail = anchor - 2
                print("note: info line omitted to fit vertical mark numbers",
                      file=sys.stderr)
            for trial_h in trials:
                mets, pitch_ok, maxw = measure(trial_h)
                if (pitch_ok and maxw <= avail) or trial_h == trials[-1]:
                    chosen = (trial_h, mets)
                    break
        trial_h, mets = chosen
        for (pos, enc, _), met in zip(marks, mets):
            hh = met.get("H", met["mb"][3] - met["mb"][1])
            tile = Image.new("L", (math.ceil(met["w"]) + 2, hh + 2), 255)
            draw_label(ImageDraw.Draw(tile), 1, hh + 1, met)
            rot = tile.rotate(90, expand=True)
            x_num = round(x_f(pos) - rot.width / 2.0)               # center on tick
            x_num = min(max(x_num, 2), img_w - rot.width - 2)       # clamp inside
            y_num = max(2, anchor - rot.height)
            img.paste(rot, (int(x_num), int(y_num)))
            label_boxes.append((enc, met["text"], x_num, x_num + rot.width))
    else:
        # center on ticks, then resolve collisions by nudging apart; shrink the
        # font globally only if labels would drift too far from their ticks
        for trial_h in trials:
            MIN_GAP = max(4, round(trial_h * 0.22))                 # digit pairs need real air
            f_num = font_for_height(font_bold, max(9, trial_h))
            frac_h = round(max(9, trial_h) * 0.42)
            f_frac = font_for_height(font_bold, frac_h) if frac_h >= 8 else None
            items = []                                              # [ink_center, half_w, metrics]
            for pos, enc, text in marks:
                met = label_metrics(draw, text, f_num, f_frac)
                items.append([x_f(pos), met["w"] / 2.0, met])
            # ideal = tick center clamped to the printable area: edge labels are
            # expected to sit shifted inward, so boundary clamping is not drift —
            # only collision-driven displacement should trigger a font shrink
            ideal = [min(max(it[0], it[1] + 2), img_w - it[1] - 2) for it in items]
            order = sorted(range(len(items)), key=lambda i: items[i][0])
            for _ in range(200):
                moved = False
                for i in order:                                     # clamp to image
                    c, h = items[i][0], items[i][1]
                    nc = min(max(c, h + 2), img_w - h - 2)
                    if nc != c:
                        items[i][0], moved = nc, True
                for a, b in zip(order, order[1:]):                  # push apart
                    need = items[a][1] + items[b][1] + MIN_GAP
                    d = items[b][0] - items[a][0]
                    if d < need:
                        shift = (need - d) / 2.0
                        items[a][0] -= shift
                        items[b][0] += shift
                        moved = True
                if not moved:
                    break
            max_drift = max(abs(it[0] - i0) for it, i0 in zip(items, ideal))
            if num_h_override or max_drift <= max(6, trial_h * 0.7):
                break
        y_bot = band_px - (base_h + tick_h + gap)                   # label ink bottom
        for (c, hw, met), (_, enc, _) in zip(items, marks):
            draw_label(draw, c - hw, y_bot, met)
            label_boxes.append((enc, met["text"], c - hw, c + hw))

    if info_text:
        box = draw.textbbox((0, 0), info_text, font=f_info)
        ih = box[3] - box[1]
        bottom_used = base_h + tick_h + gap + num_h
        if rot_info_ok if rotate_marks else (band_px - bottom_used >= ih + 4):
            ix = max(2, round(scale_x0_mm * DPMM))
            draw.text((ix, 2 - box[1]), info_text, font=f_info, fill=0)
        else:
            print("warning: tape too narrow for info line — omitted", file=sys.stderr)

    img = img.point(lambda v: 255 if v >= 128 else 0)               # hard 1-bit
    if rotate180:
        img = img.rotate(180)
    layout = dict(img_w=img_w, band_px=band_px, printable_mm=printable_mm,
                  end_margin_mm=end_margin_mm, scale_x0_mm=scale_x0_mm,
                  endstops=drawn_stops, num_h=max(9, trial_h))
    return img, layout, label_boxes


# ----------------------------------------------------------------------------
# .lbx container (mirrors a genuine P-touch Editor save)
# ----------------------------------------------------------------------------

LABEL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<pt:document xmlns:pt="http://schemas.brother.info/ptouch/2007/lbx/main"'
    ' xmlns:style="http://schemas.brother.info/ptouch/2007/lbx/style"'
    ' xmlns:text="http://schemas.brother.info/ptouch/2007/lbx/text"'
    ' xmlns:draw="http://schemas.brother.info/ptouch/2007/lbx/draw"'
    ' xmlns:image="http://schemas.brother.info/ptouch/2007/lbx/image"'
    ' xmlns:barcode="http://schemas.brother.info/ptouch/2007/lbx/barcode"'
    ' xmlns:database="http://schemas.brother.info/ptouch/2007/lbx/database"'
    ' xmlns:table="http://schemas.brother.info/ptouch/2007/lbx/table"'
    ' xmlns:cable="http://schemas.brother.info/ptouch/2007/lbx/cable"'
    ' version="1.9" generator="com.brother.PtouchEditor">'
    '<pt:body currentSheet="Sheet 1" direction="LTR">'
    '<style:sheet name="Sheet 1">'
    '<style:paper media="0" width="{tape_w}pt" height="{label_pt}pt"'
    ' marginLeft="{side_m}pt" marginTop="5.6pt" marginRight="{side_m}pt" marginBottom="5.6pt"'
    ' orientation="landscape" autoLength="false" monochromeDisplay="true"'
    ' printColorDisplay="false" printColorsID="0" paperColor="#FFFFFF" paperInk="#000000"'
    ' split="1" format="{fmt}" backgroundTheme="0" printerID="30256" printerName="Brother PT-P710BT">'
    '</style:paper>'
    '<style:cutLine regularCut="0pt" freeCut=""></style:cutLine>'
    '<style:backGround x="5.6pt" y="{side_m}pt" width="{img_w_pt}pt" height="{band_pt}pt"'
    ' brushStyle="NULL" brushId="0" userPattern="NONE" userPatternId="0" color="#000000"'
    ' printColorNumber="1" backColor="#FFFFFF" backPrintColorNumber="0"></style:backGround>'
    '<pt:objects>'
    '<image:image>'
    '<pt:objectStyle x="5.6pt" y="{side_m}pt" width="{img_w_pt}pt" height="{band_pt}pt"'
    ' backColor="#FFFFFF" backPrintColorNumber="0" ropMode="COPYPEN" angle="0" anchor="TOPLEFT" flip="NONE">'
    '<pt:pen style="NULL" widthX="0.5pt" widthY="0.5pt" color="#000000" printColorNumber="1"></pt:pen>'
    '<pt:brush style="NULL" color="#000000" printColorNumber="1" id="0"></pt:brush>'
    '<pt:expanded objectName="ScaleBitmap" ID="0" lock="2" templateMergeTarget="LABELLIST"'
    ' templateMergeType="NONE" templateMergeID="0" linkStatus="NONE" linkID="0"></pt:expanded>'
    '</pt:objectStyle>'
    '<image:imageStyle originalName="{orig_name}" alignInText="NONE" firstMerge="true" IpName="" fileName="Object0.bmp">'
    '<image:transparent flag="false" color="#FFFFFF"></image:transparent>'
    '<image:trimming flag="false" shape="RECTANGLE" trimOrgX="0pt" trimOrgY="0pt"'
    ' trimOrgWidth="0pt" trimOrgHeight="0pt"></image:trimming>'
    '<image:orgPos x="5.6pt" y="{side_m}pt" width="{img_w_pt}pt" height="{band_pt}pt"></image:orgPos>'
    '<image:effect effect="MONO" brightness="50" contrast="50" photoIndex="4"></image:effect>'
    '<image:mono operationKind="ERRORDIFFUSION" reverse="0" ditherKind="MESH" threshold="128"'
    ' gamma="100" ditherEdge="0" rgbconvProportionRed="30" rgbconvProportionGreen="59"'
    ' rgbconvProportionBlue="11" rgbconvProportionReversed="0"></image:mono>'
    '</image:imageStyle>'
    '</image:image>'
    '</pt:objects>'
    '</style:sheet>'
    '</pt:body>'
    '</pt:document>'
)

PROP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<meta:properties xmlns:meta="http://schemas.brother.info/ptouch/2007/lbx/meta"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
    '<meta:appName>com.brother.PtouchEditor</meta:appName>'
    '<dc:title>{title}</dc:title><dc:subject></dc:subject><dc:creator>lensmap2lbx</dc:creator>'
    '<meta:keyword></meta:keyword><dc:description></dc:description><meta:template></meta:template>'
    '<dcterms:created>{ts}</dcterms:created><dcterms:modified>{ts}</dcterms:modified>'
    '<meta:lastPrinted></meta:lastPrinted><meta:modifiedBy></meta:modifiedBy>'
    '<meta:revision>1</meta:revision><meta:editTime>0</meta:editTime><meta:numPages>1</meta:numPages>'
    '<meta:numWords>0</meta:numWords><meta:numChars>0</meta:numChars><meta:security>0</meta:security>'
    '<meta:transferScript></meta:transferScript></meta:properties>'
)


def fmt_pt(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def write_lbx(out_path, img, tape_mm, label_mm, title, orig_name):
    t = TAPES[tape_mm]
    img_w_pt = img.width * PT_PER_PX
    label_xml = LABEL_XML.format(
        tape_w=fmt_pt(t["width_pt"]), label_pt=fmt_pt(label_mm * MM_TO_PT),
        side_m=fmt_pt(t["side_margin_pt"]), fmt=t["fmt"],
        img_w_pt=fmt_pt(img_w_pt), band_pt=fmt_pt(t["band_pt"]),
        orig_name=orig_name,
    )
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prop_xml = PROP_XML.format(title=title, ts=ts)
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, format="BMP")                      # 24-bit like the editor's own
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (("label.xml", label_xml.encode("utf-8")),
                           ("Object0.bmp", bmp.getvalue()),
                           ("prop.xml", prop_xml.encode("utf-8"))):
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def build_info(details, channel, scale_mm):
    parts = []
    bits = " ".join(x for x in (details.get("brand"), details.get("name")) if x)
    if bits:
        parts.append(bits)
    if details.get("focalLength"):
        parts[-1:] = [f"{parts[-1]} {details['focalLength']}mm"] if parts else [f"{details['focalLength']}mm"]
    if details.get("serial"):
        parts.append(f"#{details['serial']}")
    sq = details.get("squeeze")
    if sq and sq.isdigit() and int(sq) > 100:
        parts.append(f"{int(sq) / 100:.2f}x")
    parts.append("IRIS (T)" if channel == "iris" else "FOCUS")
    parts.append(f"scale {scale_mm:g} mm")
    return "  \u00b7  ".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Lens-map XML -> Brother P-touch .lbx scale label")
    ap.add_argument("lensfile", help="lens map XML (e.g. XA100845.XML)")
    ap.add_argument("-o", "--out", help="output .lbx path (default: <lensfile>_<channel>.lbx)")
    ap.add_argument("--channel", choices=["iris", "focus"], default="iris")
    ap.add_argument("--tape", type=int, choices=sorted(TAPES), default=18, help="tape width mm (default 18)")
    ap.add_argument("--length", type=float, default=200.0, help="total label length mm (default 200)")
    ap.add_argument("--scale", type=float, default=195.0, help="scale span mm, motor 0..65535 (default 195)")
    ap.add_argument("--key", help="JSON file of overrides {\"encoded\": \"printed text\"}")
    ap.add_argument("--map", action="append", default=[], metavar="ENC=TEXT",
                    help="single override, e.g. --map 16=2.2 (repeatable)")
    ap.add_argument("--info", help="override the info line ('' to omit)")
    ap.add_argument("--t-prefix", action="store_true", help="prefix iris numbers with 'T'")
    ap.add_argument("--iris-tenths", action="store_true", default=True,
                    help="show iris values as tenths of a stop above the last full "
                         "stop (2.2 -> '2 3/10'); ON by default")
    ap.add_argument("--no-iris-tenths", dest="iris_tenths", action="store_false",
                    help="print iris values as plain f-numbers (2.2, 3.2, 11)")
    ap.add_argument("--rotate-marks", action="store_true", default=True,
                    help="rotate mark numbers 90°, centered above their ticks; "
                         "ON by default")
    ap.add_argument("--no-rotate-marks", dest="rotate_marks", action="store_false",
                    help="horizontal mark numbers next to the ticks")
    ap.add_argument("--label-size", type=int, default=None, metavar="PX",
                    help="pin mark-number height in pixels instead of auto-fitting "
                         "(use one value across a lens set for a uniform look)")
    ap.add_argument("--fit", action="store_true", help="map min..max marks to the scale instead of 0..65535")
    ap.add_argument("--reverse", action="store_true", help="mirror the scale left<->right")
    ap.add_argument("--rotate180", action="store_true", help="rotate the whole label 180\u00b0")
    ap.add_argument("--no-baseline", action="store_true", help="omit the edge baseline between scale ends")
    ap.add_argument("--no-endstops", dest="endstops", action="store_false",
                    help="don't mark the motor end stops (0/65535) when the outermost "
                         "lens marks sit clear of them")
    ap.add_argument("--tick-width", type=int, default=3, choices=range(1, 7),
                    help="tick stroke width in printer dots (default 3 = 0.42 mm)")
    ap.add_argument("--focus-units", choices=["in", "cm"], default="in")
    ap.add_argument("--preview", help="preview PNG path (default: alongside the .lbx)")
    args = ap.parse_args()

    details, channels = parse_lensmap(args.lensfile)
    if args.channel not in channels or not channels[args.channel]:
        sys.exit(f"no <{args.channel}> marks found in {args.lensfile} "
                 f"(has: {', '.join(channels) or 'none'})")

    overrides = {}
    if args.key:
        overrides.update({str(k): str(v) for k, v in json.load(open(args.key)).items()})
    for m in args.map:
        k, _, v = m.partition("=")
        if not v:
            sys.exit(f"bad --map value: {m!r} (expected ENC=TEXT)")
        overrides[k.strip()] = v.strip()

    marks = mark_labels(channels[args.channel], args.channel, overrides,
                        args.focus_units, iris_tenths=args.iris_tenths,
                        t_prefix=args.t_prefix)

    fb, fr = find_font(FONT_BOLD), find_font(FONT_REG)
    if not fb or not fr:
        sys.exit("no usable TTF font found — install DejaVu/Liberation or edit FONT_DIRS")

    info = build_info(details, args.channel, args.scale) if args.info is None else args.info
    img, layout, _ = render(marks, info, args.tape, args.length, args.scale,
                            args.reverse, args.rotate180, args.fit,
                            not args.no_baseline, fb, fr, tick_w=args.tick_width,
                            endstops=args.endstops, num_h_override=args.label_size,
                            rotate_marks=args.rotate_marks)

    stem = Path(args.lensfile).stem
    out = Path(args.out) if args.out else Path(f"{stem}_{args.channel}.lbx")
    title = f"{details.get('brand', '')} {details.get('name', '')} {details.get('focalLength', '')}mm {args.channel} scale".strip()
    write_lbx(out, img, args.tape, args.length, title, f"{stem}_{args.channel}.png")
    preview = Path(args.preview) if args.preview else out.with_suffix(".png")
    img.save(preview, dpi=(DPI, DPI))

    lo, hi = (min(p for p, _, _ in marks), max(p for p, _, _ in marks)) if args.fit else (0, 65535)
    print(f"{args.channel} marks  (motor {lo}..{hi} -> {args.scale} mm, "
          f"label {args.length} mm on {args.tape} mm tape, {img.width}x{img.height} "
          f"px @180dpi, mark size {layout['num_h']} px)")
    print(f"{'pos':>7}  {'enc':>5}  {'text':>6}  {'mm':>9}")
    rows = [(p, str(e), t, "key" if str(e) in overrides else "formula") for p, e, t in marks]
    rows += [(p, "-", "STOP", "end stop") for p in layout["endstops"]]
    for p, e, t, src in sorted(rows):
        frac = (p - lo) / max(1, hi - lo)
        if args.reverse:
            frac = 1 - frac
        print(f"{p:>7}  {e:>5}  {t:>6}  {frac * args.scale:>9.3f}   [{src}]")
    if args.channel == "iris" and any(str(e) not in overrides for _, e, _ in marks):
        print("note: formula-decoded values — check them against the physical ring; "
              "override any mismatch with --map ENC=TEXT")
    print(f"\nwrote {out}  and  {preview}")


if __name__ == "__main__":
    main()
