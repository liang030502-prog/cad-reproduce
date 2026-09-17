# -*- coding: utf-8 -*-
"""A faithful rasteriser built from the geometry itself.

Why this exists.  Checking whether a reproduction matches its source needs an
image of each, and both easy ways to get one are wrong for the job:

* the library renderer (ezdxf + matplotlib) does not draw lineweights to physical
  scale.  Measured: 0.18 and 0.25 mm both come out 1 px, 0.50 mm comes out 2 px and
  1.00 mm comes out 4 px, against 2.1, 3.0, 5.9 and 11.8 px at 300 dpi.  Every line
  in the drawing is therefore thinner than it should be, and an ink comparison
  reports the difference as a geometry error.
* AutoCAD's own plot is faithful but clips to a viewport it chooses, so it cannot
  be put into the same pixel grid as the source.

This module takes the third route: draw the geometry from the numbers.  The
coordinates are already known exactly - they were extracted from the source, scaled
and written into the DXF - so rasterising them needs no DXF parser and no
third-party renderer, and the pen width is applied in millimetres with nothing in
between.

The decisive property is that the SAME rasteriser draws both the source and the
reproduction.  Any difference between the two images then comes from the drawings,
not from two renderers measuring ink differently.  That is what makes the comparison
mean something.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402

PAPER = (255, 255, 255)
RGB = {
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
    "green": (0, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
}

# Supersampling factor.  The source PDF is anti-aliased, so a hard-edged raster of
# the reproduction would disagree along every edge for a reason that has nothing to
# do with the drawing.  Drawing large and averaging down gives both images the same
# kind of soft edge.
SUPERSAMPLE = 2


class Tracer:
    """Draws geometry into a pixel grid defined by a source-point window."""

    def __init__(self, window_pt, dpi: int, origin_pt):
        """`window_pt` is (x0, y0, x1, y1) in source points, y downward.

        `origin_pt` is the point that maps to (0, 0) in the same space the
        coordinates are given in, so a caller can draw the source and the
        reproduction through one identity mapping.
        """
        from PIL import Image
        self.dpi = dpi
        self.origin = origin_pt
        self.window = window_pt
        scale = dpi / 72.0
        w = int(round((window_pt[2] - window_pt[0]) * scale)) * SUPERSAMPLE
        h = int(round((window_pt[3] - window_pt[1]) * scale)) * SUPERSAMPLE
        if w <= 0 or h <= 0:
            raise ValueError(f"degenerate window: {window_pt}")
        self.scale = scale * SUPERSAMPLE
        self.image = Image.new("RGB", (w, h), PAPER)
        self.size_px = (w, h)
        self.stats = {"line": 0, "circle": 0, "poly": 0, "curve": 0, "text": 0, "hatch": 0}

    # ---------------------------------------------------------------- mapping
    def to_px(self, x: float, y: float) -> tuple[float, float]:
        """Source point -> pixel, with y flipped (points grow down, pixels too)."""
        ox, oy = self.origin
        return ((x - self.window[0]) * self.scale,
                (y - self.window[1]) * self.scale)

    def mm_to_px(self, mm: float) -> float:
        """A physical width in millimetres -> pixels.  No cap, no policy."""
        return mm / 25.4 * self.dpi * SUPERSAMPLE

    # ---------------------------------------------------------------- primitives
    def line(self, a, b, color, width_mm: float) -> None:
        from PIL import ImageDraw
        d = ImageDraw.Draw(self.image)
        w = max(1, int(round(self.mm_to_px(width_mm))))
        d.line([self.to_px(*a), self.to_px(*b)], fill=RGB[color], width=w)
        self.stats["line"] += 1

    def polyline(self, pts, color, width_mm: float, closed: bool = False) -> None:
        from PIL import ImageDraw
        if len(pts) < 2:
            return
        d = ImageDraw.Draw(self.image)
        w = max(1, int(round(self.mm_to_px(width_mm))))
        seq = [self.to_px(*p) for p in pts]
        if closed:
            seq = seq + [seq[0]]
        d.line(seq, fill=RGB[color], width=w, joint="curve")
        self.stats["poly"] += 1

    def bezier(self, p0, p1, p2, p3, color, width_mm: float,
               steps: int = 24) -> None:
        """Flatten a cubic to a polyline.

        A DXF spline is written from the same four control points, so flattening
        here reproduces the curve without needing a spline evaluator.
        """
        pts = []
        for i in range(steps + 1):
            t = i / steps
            u = 1.0 - t
            x = (u * u * u * p0[0] + 3 * u * u * t * p1[0]
                 + 3 * u * t * t * p2[0] + t * t * t * p3[0])
            y = (u * u * u * p0[1] + 3 * u * u * t * p1[1]
                 + 3 * u * t * t * p2[1] + t * t * t * p3[1])
            pts.append((x, y))
        self.polyline(pts, color, width_mm)
        self.stats["curve"] += 1

    def disc(self, centre, radius_pt: float, color,
             outline_width_mm: float = 0.0) -> None:
        """A filled circle, optionally with an outline of its own."""
        from PIL import ImageDraw
        d = ImageDraw.Draw(self.image)
        cx, cy = self.to_px(*centre)
        r = radius_pt * self.scale
        box = [cx - r, cy - r, cx + r, cy + r]
        d.ellipse(box, fill=RGB[color])
        if outline_width_mm > 0:
            d.ellipse(box, outline=RGB[color],
                      width=max(1, int(round(self.mm_to_px(outline_width_mm)))))
        self.stats["circle"] += 1

    def filled_polygon(self, pts, color) -> None:
        from PIL import ImageDraw
        if len(pts) < 3:
            return
        d = ImageDraw.Draw(self.image)
        d.polygon([self.to_px(*p) for p in pts], fill=RGB[color])
        self.stats["hatch"] += 1

    def dimension(self, axis: str, lo: float, hi: float, line_pos: float,
                  offset_pt: float, colour: str, arrow_pt: float,
                  line_width_mm: float, value: str, text_height_pt: float,
                  text_gap_pt: float) -> None:
        """Draw a linear dimension the way CAD draws one.

        The generator hands its dimension geometry to a live DIMENSION entity, which
        AutoCAD renders as extension lines, a dimension line, arrowheads and the
        value.  A tracer that skips all of that reports those pieces as missing:
        measured, it accounted for 13923 of the 17233 pixels the source had and the
        trace did not - which looked like a large geometry defect and was really a
        gap in the ruler.

        `lo`/`hi` are positions ALONG the axis, `line_pos` is where the dimension
        line sits across it, and `offset_pt` is how far the feature is from that line
        (its sign says which side the feature is on).
        """
        feature = line_pos + offset_pt
        sign = 1.0 if offset_pt >= 0 else -1.0

        def at(along, across):
            return (along, across) if axis == "h" else (across, along)

        # Extension lines, from the feature to just past the dimension line.
        for along in (lo, hi):
            self.line(at(along, feature), at(along, line_pos - sign * arrow_pt * 0.3),
                      colour, line_width_mm)

        # The dimension line, broken where the value text sits on it.
        clear = text_height_pt + 2.0 * text_gap_pt
        if abs(hi - lo) > clear * 1.4:
            self.line(at(lo, line_pos), at((lo + hi) / 2 - clear / 2, line_pos),
                      colour, line_width_mm)
            self.line(at((lo + hi) / 2 + clear / 2, line_pos), at(hi, line_pos),
                      colour, line_width_mm)
        else:
            self.line(at(lo, line_pos), at(hi, line_pos), colour, line_width_mm)

        # Arrowheads: a filled sliver `arrow_pt` long and a third of that wide, which
        # is the shape this drawing's dimension lines carry.
        half = arrow_pt / 6.0
        for along, direction in ((lo, 1.0), (hi, -1.0)):
            tip = at(along, line_pos)
            corner_a = at(along + direction * arrow_pt, line_pos + half)
            corner_b = at(along + direction * arrow_pt, line_pos - half)
            self._triangle(tip, corner_a, corner_b, colour)

        # The value, off the line on the side away from the feature.
        if value:
            side = -sign
            mid = (lo + hi) / 2.0
            if axis == "h":
                self.text(value, (mid, line_pos + side * text_gap_pt), text_height_pt,
                          colour, centre=True)
            else:
                self.text(value, (line_pos + side * text_gap_pt, mid), text_height_pt,
                          colour, centre=True)

    def _triangle(self, a, b, c, colour) -> None:
        from PIL import ImageDraw
        ImageDraw.Draw(self.image).polygon(
            [self.to_px(*a), self.to_px(*b), self.to_px(*c)], fill=RGB[colour])

    def text(self, s: str, origin, height_pt: float, color, rotation: float = 0.0,
             centre: bool = False):
        from PIL import Image, ImageDraw, ImageFont
        px = height_pt * self.scale
        if px < 1:
            return
        font = _load_font(int(round(px)))
        if font is None:
            return
        d = ImageDraw.Draw(self.image)
        anchor = "ms" if centre else "ls"
        if abs(rotation) < 1e-6:
            d.text(self.to_px(*origin), s, fill=RGB[color], font=font, anchor=anchor)
        else:
            # Rotated text is drawn on its own tile and pasted, because PIL cannot
            # rotate text in place.
            bbox = d.textbbox((0, 0), s, font=font)
            tw, th = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
            tile = Image.new("RGBA", (tw + 4, th + 8), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((2, 2), s, fill=RGB[color] + (255,), font=font)
            tile = tile.rotate(rotation, expand=True, resample=Image.BICUBIC)
            self.image.paste(tile, (int(self.to_px(*origin)[0]),
                                    int(self.to_px(*origin)[1]) - tile.height), tile)
        self.stats["text"] += 1

    # ---------------------------------------------------------------- output
    def save(self, path: str) -> dict:
        from PIL import Image
        cadkit.ensure_dirs(os.path.dirname(os.path.abspath(path)))
        out = self.image
        if SUPERSAMPLE != 1:
            out = out.resize((self.size_px[0] // SUPERSAMPLE,
                              self.size_px[1] // SUPERSAMPLE), Image.LANCZOS)
        out.save(path)
        return {"png": os.path.abspath(path), "size_px": list(out.size),
                "dpi": self.dpi, "window_pt": [round(v, 4) for v in self.window],
                "primitives": dict(self.stats)}


_FONT_CACHE: dict = {}


def _load_font(px: int):
    """A CJK-capable TrueType face, cached per size."""
    from PIL import ImageFont
    if px in _FONT_CACHE:
        return _FONT_CACHE[px]
    candidates = [
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "DejaVuSans.ttf",
    ]
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, px)
            break
        except Exception:
            continue
    _FONT_CACHE[px] = font
    return font


# --------------------------------------------------------------------------
# drawing a full drawing
# --------------------------------------------------------------------------
GLYPH_MAX_THIN = 3.5
GLYPH_MAX_LONG = 6.5


def trace_source(data: dict, out_png: str, dpi: int = 300,
                 margin_pt: float = 6.0) -> dict:
    """Rasterise the SOURCE geometry from its extracted numbers."""
    extent = data["content_extent_pt"]
    window = (extent[0] - margin_pt, extent[1] - margin_pt,
              extent[2] + margin_pt, extent[3] + margin_pt)
    t = Tracer(window, dpi, (0.0, 0.0))

    for p in data["paths"]:
        colour = p["color"] or p["fill"] or "black"
        width_mm = (p["width"] or 0.0) * cadkit.PT2MM
        items = p["items"]

        # A path whose pen is wider than the shape itself prints as a solid mark.
        if _reads_as_solid(p):
            _draw_solid(t, p, colour)
            continue

        for it in items:
            kind = it[0]
            if kind == "l":
                t.line(it[1], it[2], colour, width_mm)
            elif kind == "re":
                x0, y0, x1, y1 = it[1]
                t.polyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], colour,
                           width_mm, closed=True)
            elif kind == "qu":
                t.polyline(it[1], colour, width_mm, closed=True)
            elif kind == "c":
                t.bezier(it[1], it[2], it[3], it[4], colour, width_mm)

    for s in data["spans"]:
        if not s["text"].strip():
            continue
        x0, y0, x1, y1 = s["bbox"]
        vertical = abs(s["dir"][1]) > 0.5
        colour = _span_colour(s)
        if vertical:
            t.text(s["text"], (x0, y1), s["size"], colour, rotation=90)
        else:
            t.text(s["text"], (x0, y1), s["size"], colour)

    info = t.save(out_png)
    info["role"] = "source geometry traced from its own numbers"
    return info


def trace_generated(data: dict, dims: dict, out_png: str, cfg: dict,
                    dpi: int = 300, margin_pt: float = 6.0,
                    min_confidence: str = "medium") -> dict:
    """Rasterise what the GENERATOR produced.

    The same source numbers pass through the same transforms and the same
    suppression rules the generator applies, so this image shows the drawing the
    generator builds - rendered by the one rasteriser whose pen width is exact.
    """
    extent = data["content_extent_pt"]
    window = (extent[0] - margin_pt, extent[1] - margin_pt,
              extent[2] + margin_pt, extent[3] + margin_pt)
    t = Tracer(window, dpi, (0.0, 0.0))

    from infer_dims import _is_glyph_path
    kept_dims = [p for p in dims["proposals"] if p["value"] is not None
                 and _rank(p["confidence"]) >= _rank(min_confidence)]

    # The band a live dimension replaces, per piece - the same rule the generator
    # uses, deliberately duplicated here in source-point space.
    bands = []
    for p in kept_dims:
        a, b = p["p1"], p["p2"]
        lo = (min(a[0], b[0]), min(a[1], b[1]))
        hi = (max(a[0], b[0]), max(a[1], b[1]))
        if p["axis"] == "h":
            bands.append((lo[0] - 1.5, p["line_pos"] - 3.0, hi[0] + 1.5, p["line_pos"] + 3.0))
        else:
            bands.append((p["line_pos"] - 3.0, lo[1] - 1.5, p["line_pos"] + 3.0, hi[1] + 1.5))
        if p.get("label_bbox"):
            lx0, ly0, lx1, ly1 = p["label_bbox"]
            bands.append((lx0 - 1.0, ly0 - 1.0, lx1 + 1.0, ly1 + 1.0))

    def in_band(x, y):
        return any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in bands)

    def item_in_band(it):
        k = it[0]
        if k == "l":
            return in_band(*it[1]) and in_band(*it[2])
        if k == "re":
            x0, y0, x1, y1 = it[1]
            return in_band(x0, y0) and in_band(x1, y1)
        if k == "qu":
            return all(in_band(px, py) for px, py in it[1])
        if k == "c":
            return all(in_band(px, py) for px, py in it[1:5])
        return False

    for p in data["paths"]:
        if _is_glyph_path(p):
            continue
        colour = p["color"] or p["fill"] or "black"
        width_mm = (p["width"] or 0.0) * cadkit.PT2MM
        items = [it for it in p["items"] if not item_in_band(it)]
        if not items:
            continue
        if _reads_as_solid(p):
            _draw_solid(t, p, colour)
            continue
        for it in items:
            kind = it[0]
            if kind == "l":
                t.line(it[1], it[2], colour, width_mm)
            elif kind == "re":
                x0, y0, x1, y1 = it[1]
                t.polyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], colour,
                           width_mm, closed=True)
            elif kind == "qu":
                t.polyline(it[1], colour, width_mm, closed=True)
            elif kind == "c":
                t.bezier(it[1], it[2], it[3], it[4], colour, width_mm)

    # Text the generator emits, minus the dimension values a live dimension draws.
    for s in data["spans"]:
        if not s["text"].strip():
            continue
        x0, y0, x1, y1 = s["bbox"]
        if in_band(x0 + 0.01, y0 + 0.01):
            continue
        vertical = abs(s["dir"][1]) > 0.5
        colour = _span_colour(s)
        if vertical:
            t.text(s["text"], (x0, y1), s["size"], colour, rotation=90)
        else:
            t.text(s["text"], (x0, y1), s["size"], colour)

    # The dimensions that replaced the drawn dimension geometry.  Without this the
    # trace is missing every dimension line, extension line and arrowhead it
    # suppressed, which reads as a defect in the drawing rather than in the ruler.
    # Every one of these is read from the same configuration the generator reads, so
    # the trace and the drawing cannot drift apart.  A value duplicated as a literal
    # here would silently stop matching the moment the generator's default changed,
    # and the disagreement would show up as a fidelity defect that is really a
    # difference between two copies of the same constant.
    offset_mm = float(cfg.get("dim_extension_mm") or 4.0)
    arrow_pt = float(cfg.get("arrow_size_pt") or 5.4)
    text_pt = float(cfg.get("dim_text_pt") or 5.49)
    gap_pt = float(cfg.get("dim_text_gap_pt") or 0.5)
    line_width_mm = float(cfg.get("dim_line_width_mm") or 0.48 * cadkit.PT2MM)
    scale = float(cfg.get("scale_mm_per_pt") or cadkit.PT2MM)
    for p in kept_dims:
        a, b = p["p1"], p["p2"]
        sign = 1.0 if p["offset_sign"] >= 0 else -1.0
        offset_pt = (offset_mm / scale) * sign
        lo = a[0] if p["axis"] == "h" else a[1]
        hi = b[0] if p["axis"] == "h" else b[1]
        t.dimension(p["axis"], lo, hi, p["line_pos"], offset_pt, "green",
                    arrow_pt, line_width_mm, p["value"], text_pt, gap_pt)

    info = t.save(out_png)
    info["role"] = "generated geometry traced through the generator's own rules"
    info["dimensions_drawn"] = len(kept_dims)
    return info


def _draw_solid(t: Tracer, p: dict, colour: str) -> None:
    items = p["items"]
    kinds = {it[0] for it in items}
    rect = p["rect"]
    width = p.get("width") or 0.0
    if kinds == {"c"} and len(items) == 4:
        cx = (rect[0] + rect[2]) / 2.0
        cy = (rect[1] + rect[3]) / 2.0
        radius = max(rect[2] - rect[0], rect[3] - rect[1]) / 2.0 + width / 2.0
        t.disc((cx, cy), radius, colour)
        return
    for it in items:
        if it[0] == "l":
            t.line(it[1], it[2], colour, max(width, 0.1))
        elif it[0] == "qu":
            t.filled_polygon(it[1], colour)
        elif it[0] == "re":
            x0, y0, x1, y1 = it[1]
            t.filled_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], colour)


def _rank(conf: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(conf, 0)


def _reads_as_solid(p: dict) -> bool:
    """Same rule as the generator: a pen wider than the shape fills it."""
    if p["fill"] is not None and (p["color"] is None or (p["width"] or 0.0) <= 0.0):
        return True
    width = p["width"] or 0.0
    if width <= 0.0 or p["color"] is None:
        return False
    if len(p["items"]) < 3:
        return False
    x0, y0, x1, y1 = p["rect"]
    w, h = x1 - x0, y1 - y0
    if len(p["items"]) > 48 or min(w, h) <= 0.05:
        return False
    return max(w, h) / 2.0 <= width


def _span_colour(span: dict) -> str:
    r = int(span["color"][1:3], 16) / 255.0
    g = int(span["color"][3:5], 16) / 255.0
    b = int(span["color"][5:7], 16) / 255.0
    return cadkit.rgb_to_key((r, g, b)) or "black"


def main() -> int:
    import argparse, json
    ap = argparse.ArgumentParser(description="rasterise geometry from its own numbers")
    ap.add_argument("extract_json")
    ap.add_argument("--dims", help="dims.json; omit to trace the source only")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--margin-pt", type=float, default=6.0)
    ap.add_argument("--min-confidence", default="medium",
                    choices=("low", "medium", "high"),
                    help="must match the generator's own threshold, or the trace "
                         "draws a different set of dimensions than the DXF")
    ap.add_argument("--json")
    args = ap.parse_args()

    with open(args.extract_json, encoding="utf-8") as fh:
        data = json.load(fh)
    cfg = cadkit.load_config(None)
    cadkit.ensure_dirs(args.out_dir)
    results = {}
    results["source"] = trace_source(data, os.path.join(args.out_dir, "trace_source.png"),
                                     dpi=args.dpi, margin_pt=args.margin_pt)
    if args.dims:
        with open(args.dims, encoding="utf-8") as fh:
            dims = json.load(fh)
        results["generated"] = trace_generated(
            data, dims, os.path.join(args.out_dir, "trace_generated.png"), cfg,
            dpi=args.dpi, margin_pt=args.margin_pt,
            min_confidence=args.min_confidence)

    cadkit.banner("TRACED FROM GEOMETRY")
    for name, info in results.items():
        cadkit.table([(k, v) for k, v in info.items() if not isinstance(v, dict)],
                     (name, "value"))
        print("   primitives:", info["primitives"])
        print()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
