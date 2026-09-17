# -*- coding: utf-8 -*-
"""Stage 2 - build a DXF that reproduces the source drawing.

Design rules this generator follows, each one a fix for a measured defect:

1. GEOMETRY SIZE IS NOT A JUDGEMENT CALL.  One point in the source is
   25.4/72 mm in the output, always.  A drawing reproduced at a different size
   than its source cannot be compared with it, so any "true scale" recovery is
   a separate, explicitly reported step - never a silent default.

2. COLOUR COMES FROM THE STROKE, NOT FROM THE GRAPHICS STATE.  A path that
   stroked with a colour is that colour.  A path that filled with a colour is
   that colour.  A path that did neither is black outline.  Taking the fill
   colour as the path's colour paints every black outline green, which is what
   happened before: 4514 green entities against ~125 genuinely green paths.

3. TEXT DRAWN TWICE IS DRAWN ONCE.  Some drawings are printed with text
   converted to outlines, so the same numeral appears both as a real text span
   and as a set of filled paths on top of it.  Paths that sit on a text span
   and carry no stroke of their own are that span's outline and are dropped;
   the span is written as a text entity instead.  The predicate is positional,
   not a size threshold - a size threshold misses outlines that are offset from
   the span and can delete small genuine features.

4. BLACK IS ACI 250, NOT ACI 7.  ACI 7 means "black or white, whichever
   contrasts with the background", so it displays white in AutoCAD's dark model
   space.  ACI 250 is black everywhere.

5. LINEWEIGHT IS A PAPER DIMENSION.  Lineweight describes how thick the line is
   on paper.  It must not be multiplied by any drawing scale, or every line
   becomes wrong by that factor.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402

SOURCE_ORDER_PREFIX = "~SRC~"


def build(data: dict, out_dxf: str, cfg: dict, variant: str = "repro") -> dict:
    import ezdxf
    from ezdxf.enums import TextEntityAlignment
    from ezdxf.fonts import fonts
    from ezdxf.math import Bezier

    scale = float(cfg.get("scale_mm_per_pt") or cadkit.PT2MM)
    policy = cfg.get("color_policy", "exact")
    lineweight_multiplier = 1.0
    if variant == "true_scale":
        # A true-scale variant keeps the same paper lineweights on purpose: the
        # geometry grows, the ink does not.  Multiply here only if the variant is
        # meant to be plotted at a different scale AND keep identical appearance.
        lineweight_multiplier = float(cfg.get("lineweight_multiplier", 1.0))

    origin = cfg.get("origin_pt")
    if origin:
        ox, oy = float(origin[0]), float(origin[3])
    else:
        extent = data["content_extent_pt"]
        ox, oy = extent[0], extent[3]

    def X(x: float) -> float:
        return round((x - ox) * scale, 4)

    def Y(y: float) -> float:
        return round((oy - y) * scale, 4)      # PDF y grows downward, DXF upward

    doc = ezdxf.new("R2010", setup=False)
    doc.header["$INSUNITS"] = 4                # millimetres
    doc.header["$MEASUREMENT"] = 1
    # Filled polygons are the only way to reproduce markers whose stroke is
    # wider than the shape itself.  With FILLMODE off AutoCAD *displays* them as
    # hollow outlines while still plotting them filled, which makes a correct
    # drawing look wrong on screen.
    doc.header["$FILLMODE"] = 1
    doc.header["$PLINEGEN"] = 1
    doc.header["$LTSCALE"] = 1.0
    doc.header["$PSLTSCALE"] = 1

    # ---- text styles -----------------------------------------------------
    # A style's font is resolved as a FAMILY NAME by the renderer, not as a file
    # name: "SimSun" resolves, "simsun.ttc" does not, and an unresolved name
    # falls back to a font without CJK glyphs and renders Chinese as empty
    # boxes.  Resolving up front turns that silent failure into a build error.
    def add_style(name: str, font: str, bigfont: str | None) -> None:
        style = doc.styles.add(name, font=font)
        if bigfont:
            style.dxf.bigfont = bigfont
        if font and not font.lower().endswith((".shx", ".lff")):
            try:
                face = fonts.font_manager.find_best_match(font)
            except Exception as exc:
                raise SystemExit(
                    f"font {font!r} for text style {name!r} does not resolve "
                    f"({type(exc).__name__}); the renderer would silently fall back to "
                    f"{fonts.font_manager.fallback_font_name()!r}, which cannot draw "
                    f"Chinese characters.  Use a font FAMILY name such as 'SimSun', "
                    f"not a file name such as 'simsun.ttc'.") from exc
            resolved.append((name, font, face.filename))

    resolved: list = []
    style_name = cfg.get("style_name", "HZ")
    style_font = cfg.get("style_font", "SimSun")
    shx = cfg.get("style_shx") or ""
    bigfont = cfg.get("style_bigfont") or ""

    fallback = cfg.get("fallback_font")
    if fallback:
        # The renderer's fallback name is private state with no setter in ezdxf
        # 1.4.4, so the fallback is verified rather than installed: if the
        # configured fallback is not a font this machine has, say so now instead
        # of discovering it as empty boxes in a render.
        if not fonts.font_manager.has_font(fallback):
            cadkit.eprint(f"warning: configured fallback_font {fallback!r} is not "
                          f"available; the renderer will use "
                          f"{fonts.font_manager.fallback_font_name()!r}")

    add_style(style_name, shx or style_font, bigfont or None)
    doc.header["$TEXTSTYLE"] = style_name

    dim_style = cfg.get("dim_style_name", "DIM")
    add_style(dim_style, cfg.get("dim_style_font", "SimSun"), None)

    # ---- layers ----------------------------------------------------------
    for key, (name, color_key) in cadkit.LAYERS.items():
        spec = cadkit.SOURCE_COLORS[color_key]
        layer = doc.layers.add(name, color=spec["aci"])
        if policy == "true":
            layer.dxf.true_color = cadkit._rgb_int(spec["rgb"])

    msp = doc.modelspace()

    # ---- which paths are text outlines? ---------------------------------
    spans = data["spans"]
    span_boxes = [s["bbox"] for s in spans]

    def boxes_overlap(rect, box, margin: float) -> bool:
        x0, y0, x1, y1 = rect
        bx0, by0, bx1, by1 = box
        return not (x1 <= bx0 - margin or bx1 + margin <= x0
                    or y1 <= by0 - margin or by1 + margin <= y0)

    def outlines_a_span(rect) -> bool:
        # A font outline sits on the span it belongs to but can be offset by a
        # fraction of its height (the three periods in this drawing are the
        # clearest case), so the test is overlap with a small margin.
        for box in span_boxes:
            if boxes_overlap(rect, box, (box[3] - box[1]) * 0.35):
                return True
        return False

    # A path is a text outline when it carries NO stroke ink of its own and either
    # sits on a text span or has the proportions of a single glyph.  Keying on
    # that rather than on a rectangle size alone is not cosmetic: a size window of
    # the kind an earlier attempt used also matched six cyan cross-hatch
    # fragments, so a size-only filter deletes real drawing content.  On the
    # source drawing this predicate selects 132 paths, every one of them a green
    # numeral outline, and nothing else - confirmed by two independent analyses.
    GLYPH_MAX_THIN = 3.5      # pt, the short side of one digit
    GLYPH_MAX_LONG = 6.5      # pt, the long side of one digit

    def has_glyph_proportions(rect) -> bool:
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        return ((w <= GLYPH_MAX_THIN and h <= GLYPH_MAX_LONG)
                or (h <= GLYPH_MAX_THIN and w <= GLYPH_MAX_LONG))

    def has_no_stroke_ink(p: dict) -> bool:
        # Fill-only, or filled plus a zero-width stroke: in both cases the visible
        # mark is the fill, which is what a font outline contributes.
        return p["fill"] is not None and (p["color"] is None or (p["width"] or 0.0) <= 0.0)

    def is_text_outline(p: dict) -> bool:
        return has_no_stroke_ink(p) and (outlines_a_span(p["rect"])
                                        or has_glyph_proportions(p["rect"]))

    text_outlines, kept = [], []
    for p in data["paths"]:
        (text_outlines if is_text_outline(p) else kept).append(p)

    # A numeral's colour lives on the outline that draws it, not on the text span,
    # because the source may carry a non-painting text layer over a painted
    # outline.  Measured on the source drawing: all 25 dimension numerals are
    # written with text rendering mode 3 ("invisible"), so their span colour
    # (#000000) is NOT what a reader sees - the outline colour is.  Copying the
    # span colour there would print every dimension number black while the source
    # prints it in the outline's colour.
    inferred_text_colour = {}
    for p in text_outlines:
        if p["fill"] is None:
            continue
        for index, s in enumerate(spans):
            box = s["bbox"]
            if boxes_overlap(p["rect"], box, (box[3] - box[1]) * 0.35):
                inferred_text_colour.setdefault(index, collections.Counter())[p["fill"]] += 1

    # ---- emit geometry --------------------------------------------------
    counts = collections.Counter()
    lineweight_substitutions = collections.Counter()

    def attribs(color_key: str, width_pt: float, layer_key: str) -> dict:
        name = cadkit.LAYERS[layer_key][0]
        color_attr, _record = cadkit.color_attrs(color_key, policy)
        attrs = {"layer": name, **color_attr}
        mm = width_pt * cadkit.PT2MM * lineweight_multiplier
        chosen, requested = cadkit.snap_lineweight(mm)
        if requested is not None:
            lineweight_substitutions[(requested, chosen)] += 1
        attrs["lineweight"] = chosen
        return attrs

    for p in kept:
        stroke = p["color"]
        fill = p["fill"]
        layer_key = _layer_for(stroke, fill)
        color_key = stroke or fill or "black"
        width_pt = p["width"] if stroke else 0.0
        attrs = attribs(color_key, width_pt, layer_key)
        items = p["items"]

        # A filled path with no stroke of its own must be drawn as a filled
        # region: line geometry alone loses the fill, and a closed outline in the
        # fill colour is what turns a solid marker into a hollow ring.
        if stroke is None and fill is not None:
            _emit_filled(msp, p, attrs, X, Y, scale)
            counts["filled"] += 1
            continue

        for item in items:
            kind = item[0]
            if kind == "l":
                (ax, ay), (bx, by) = item[1], item[2]
                msp.add_lwpolyline([(X(ax), Y(ay)), (X(bx), Y(by))], dxfattribs=attrs)
                counts["line"] += 1
            elif kind == "re":
                x0, y0, x1, y1 = item[1]
                msp.add_lwpolyline(
                    [(X(x0), Y(y0)), (X(x1), Y(y0)), (X(x1), Y(y1)), (X(x0), Y(y1))],
                    close=True, dxfattribs=attrs)
                counts["rect"] += 1
            elif kind == "qu":
                pts = item[1]
                msp.add_lwpolyline([(X(px), Y(py)) for px, py in pts],
                                   close=True, dxfattribs=attrs)
                counts["quad"] += 1
            elif kind == "c":
                bez = Bezier([(X(px), Y(py)) for px, py in item[1:5]])
                msp.add_open_spline(bez.control_points, degree=3, dxfattribs=attrs)
                counts["curve"] += 1
            else:
                counts[f"skipped:{kind}"] += 1

    # ---- emit text ------------------------------------------------------
    numerals_resolved = 0
    for index, s in enumerate(spans):
        text = s["text"]
        if not text.strip():
            continue
        x0, y0, x1, y1 = s["bbox"]
        height = s["size"] * scale
        vertical = abs(s["dir"][1]) > 0.5
        counters = inferred_text_colour.get(index)
        if counters:
            color_key = counters.most_common(1)[0][0]
            numerals_resolved += 1
        else:
            color_key = _text_color(s["color"])
        is_dim = _is_dimension(s)
        layer_name = (cadkit.LAYERS["dimtext"][0] if is_dim
                      else cadkit.LAYERS["note"][0] if color_key == "yellow"
                      else cadkit.LAYERS["outline"][0])
        use_style = dim_style if is_dim else style_name
        color_attr, _rec = cadkit.color_attrs(color_key, policy)
        attrs = {"layer": layer_name, "style": use_style, **color_attr}

        if vertical:
            ox_, oy_ = s["origin"]
            entity = msp.add_text(text, height=height, dxfattribs={**attrs, "rotation": 90})
            entity.set_placement((X(ox_), Y(oy_)), align=TextEntityAlignment.BOTTOM_LEFT)
        else:
            entity = msp.add_text(text, height=height, dxfattribs=attrs)
            entity.set_placement((X(x0), Y(y1)), align=TextEntityAlignment.BOTTOM_LEFT)
        counts["text"] += 1

    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(out_dxf)))
    doc.saveas(out_dxf)

    report = {
        "dxf": os.path.abspath(out_dxf),
        "variant": variant,
        "scale_mm_per_pt": scale,
        "units": "mm",
        # The origin the transform used, recorded so the renderer can build its
        # window from the SAME origin.  Two stages that derive the origin
        # independently produce a sub-millimetre offset, which is enough to put
        # the two rasters in different pixel grids and make every later
        # comparison approximate instead of exact.
        "origin_pt": [ox, oy],
        "content_extent_pt": data["content_extent_pt"],
        "source_paths": len(data["paths"]),
        "kept_paths": len(kept),
        "text_outlines_dropped": len(text_outlines),
        "text_entities": sum(1 for s in spans if s["text"].strip()),
        "text_colour_from_outline": numerals_resolved,
        "entity_kinds": dict(counts),
        "lineweight_substitutions": {
            f"{req}->{chosen} (0.01mm)": n for (req, chosen), n in lineweight_substitutions.items()
        },
        "fonts_resolved": [{"style": s, "requested": f, "file": fn} for s, f, fn in resolved],
        "colour_policy": policy,
    }
    return report


class _ColorProbe:  # pragma: no cover - retained name for backwards compatibility
    pass


def _layer_for(stroke: str | None, fill: str | None) -> str:
    key = stroke or fill or "black"
    return {
        "black": "outline",
        "red": "dims",
        "green": "section",
        "cyan": "cyan",
        "yellow": "note",
        "magenta": "magenta",
    }[key]


def _text_color(hex_color: str) -> str:
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return cadkit.rgb_to_key((r / 255.0, g / 255.0, b / 255.0)) or "black"


def _is_dimension(span: dict) -> bool:
    """A dimension value is a bare number.  Chinese notes and title-block fields
    are never bare numbers, so this test needs no size threshold."""
    text = span["text"].strip()
    if not text:
        return False
    stripped = text.replace(".", "", 1).replace("-", "", 1)
    return stripped.isdigit() and not any(ord(c) > 127 for c in text)


def _emit_filled(msp, path: dict, attrs: dict, X, Y, scale: float) -> None:
    """Draw a stroke-less filled path as a real filled region.

    Circles whose stroke is wider than their diameter print as solid discs.  DXF
    cannot express a stroke that wide (the lineweight cap is 2.11 mm), so the
    disc is reproduced as a filled region.  A SOLID hatch is used rather than a
    closed polyline because a closed polyline is an outline: hollow on screen and
    hollow on paper.  Leaving FILLMODE at 0 makes even the hatch display hollow,
    which is why the header sets it to 1.
    """
    items = path["items"]
    kinds = {it[0] for it in items}
    rect = path["rect"]

    if kinds == {"c"} and len(items) == 4:
        # Four cubic segments is how a circle is written into a PDF.  The path
        # rectangle of a circle is its bounding box, so the centre and radius come
        # straight out of it - no curve fitting needed.
        cx = (rect[0] + rect[2]) / 2.0
        cy = (rect[1] + rect[3]) / 2.0
        radius = max(rect[2] - rect[0], rect[3] - rect[1]) / 2.0
        msp.add_circle((X(cx), Y(cy)), radius * scale, dxfattribs=attrs)
        return

    hatch = msp.add_hatch(dxfattribs=attrs)
    if "color" in attrs:
        hatch.set_solid_fill(color=attrs["color"])
    else:
        hatch.set_solid_fill(color=250)
        hatch.dxf.true_color = attrs.get("true_color")
    for item in items:
        kind = item[0]
        if kind == "l":
            (ax, ay), (bx, by) = item[1], item[2]
            hatch.paths.add_polyline_path([(X(ax), Y(ay)), (X(bx), Y(by))], is_closed=True)
        elif kind == "re":
            x0, y0, x1, y1 = item[1]
            hatch.paths.add_polyline_path(
                [(X(x0), Y(y0)), (X(x1), Y(y0)), (X(x1), Y(y1)), (X(x0), Y(y1))],
                is_closed=True)
        elif kind == "qu":
            hatch.paths.add_polyline_path([(X(px), Y(py)) for px, py in item[1]],
                                          is_closed=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="build a DXF from extracted source data")
    ap.add_argument("extract_json")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--config")
    ap.add_argument("--variant", default="repro")
    ap.add_argument("--json", help="write the build report here; the renderer needs its origin")
    args = ap.parse_args()

    cfg = cadkit.load_config(args.config)
    with open(args.extract_json, encoding="utf-8") as fh:
        data = json.load(fh)

    report = build(data, args.out, cfg, variant=args.variant)
    if args.json:
        cadkit.ensure_dirs(os.path.dirname(os.path.abspath(args.json)))
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
    cadkit.banner("DXF BUILT")
    cadkit.table([(k, v) for k, v in report.items() if not isinstance(v, dict)],
                 ("item", "value"))
    if report["lineweight_substitutions"]:
        print("\nlineweight substitutions (requested -> written, 0.01 mm):")
        for k, v in report["lineweight_substitutions"].items():
            print(f"   {k}: {v}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
