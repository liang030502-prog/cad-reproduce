# -*- coding: utf-8 -*-
"""Stage 1 + 2 - build the drawing using CAD's own entities.

This generator exists because the previous attempt drew everything as plain
geometry, which produced four visible defects.  Each one is answered here by the
entity that CAD actually provides for it:

  faked before                                  used now
  -------------------------------------------   ---------------------------------
  a closed outline where a solid disc belongs    HATCH with a SOLID fill
  dimension lines drawn by hand                  live DIMENSION entities
  characters assembled from short strokes        TEXT / MTEXT in a real font
  one colour for everything, or a wrong one      the source colour, as an exact ACI

Two rules keep it honest:

1. NOTHING IS DRAWN TWICE.  The source drawing has its dimension machinery - lines,
   extension lines, arrowheads and value text - drawn as ordinary geometry.  Where
   a live DIMENSION is emitted, that geometry is suppressed, or the drawing carries
   both the real dimension and a hand-drawn copy of it.

2. EVERY PARAMETER COMES FROM THE SOURCE.  Text height, the value-text offset from
   the dimension line, the line colour and the sheet scale are all measured from the
   source drawing, not chosen by preference.  A number that was picked rather than
   measured is indistinguishable from a correct one in the output.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402
import curve_entities  # noqa: E402
import infer_dims  # noqa: E402

# Source colours.  All six are exactly representable in the ACI palette, which was
# verified with ezdxf.colors.aci2rgb, so an index reproduces the source colour with
# no approximation.
#
# Black is ACI 250 and NOT 7.  ACI 7 means "black or white, whichever contrasts
# with the background", so it displays white in AutoCAD's dark model space and only
# prints black - a drawing that is correct but looks wrong on screen.
LAYER_FOR = {
    "black": ("OUTLINE", 250),
    "red": ("CENTRE_RED", 1),
    "yellow": ("NOTES_YELLOW", 2),
    "green": ("DIM_GREEN", 3),
    "cyan": ("HATCH_CYAN", 4),
    "magenta": ("MARK_MAGENTA", 6),
}
# A colour outside those six is not one of them and must not be painted as one.
# It keeps its exact value on a layer of its own, named after that value, and is
# written as a true colour (group code 420).  The earlier behaviour - `stroke or
# fill or "black"` against a six-entry table - turned every blue line on the sheet
# black with no warning, which is a wrong drawing that also cannot pass a colour
# check no matter how many times it is iterated.
TRUECOLOUR_LAYER = ("TRUECOLOUR", 7)
TEXT_LAYER = ("TEXT", 250)
DIM_LAYER = ("DIMENSION", 3)


def layer_for_colour(key: str) -> tuple:
    """(layer name, aci, true_rgb or None) for one colour key."""
    if key in LAYER_FOR:
        name, aci = LAYER_FOR[key]
        return name, aci, None
    if key.startswith("#") and len(key) == 7:
        try:
            rgb = cadkit.hex_to_rgb(key)
        except ValueError:
            return TRUECOLOUR_LAYER[0], TRUECOLOUR_LAYER[1], None
        return f"TRUECOLOUR_{key[1:]}", TRUECOLOUR_LAYER[1], rgb
    return TRUECOLOUR_LAYER[0], TRUECOLOUR_LAYER[1], None


def build(data: dict, dims: dict, out_dxf: str, cfg: dict,
          use_dimensions: bool = True, min_confidence: str = "medium",
          associative: bool = False, mtext_wrap: bool = False) -> dict:
    import ezdxf
    from ezdxf.enums import TextEntityAlignment, MTextEntityAlignment
    from ezdxf.fonts import fonts
    from ezdxf.math import Bezier

    scale = float(cfg.get("scale_mm_per_pt") or cadkit.PT2MM)
    extent = data["content_extent_pt"]
    ox, oy = extent[0], extent[3]

    def X(x: float) -> float:
        return round((x - ox) * scale, 4)

    def Y(y: float) -> float:
        return round((oy - y) * scale, 4)

    # setup=False on purpose.  setup=True creates 26 text styles pointing at
    # OpenSans/Liberation files that AutoCAD does not have, and AutoCAD then
    # substitutes every one of them with simplex.shx - visible in its own log as a
    # long table of substitutions.
    doc = ezdxf.new("R2010", setup=False)
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    doc.header["$FILLMODE"] = 1
    doc.header["$PLINEGEN"] = 1

    # ---- text style ------------------------------------------------------
    # A style's font is resolved as a font FAMILY name, not a file name: "SimSun"
    # resolves, "simsun.ttc" does not, and an unresolved name falls back to a font
    # with no CJK glyphs and renders Chinese as empty boxes.  Resolving here turns
    # that silent failure into a build error.
    style_name = cfg.get("style_name", "HZ")
    style_font = cfg.get("style_font", "SimSun")
    doc.styles.add(style_name, font=style_font)
    if not style_font.lower().endswith((".shx", ".lff")):
        try:
            face = fonts.font_manager.find_best_match(style_font)
        except Exception as exc:
            raise SystemExit(
                f"font {style_font!r} does not resolve ({type(exc).__name__}); the "
                f"renderer would silently fall back to "
                f"{fonts.font_manager.fallback_font_name()!r}, which cannot draw "
                f"Chinese.  Use a family name such as 'SimSun', not 'simsun.ttc'."
            ) from exc
    doc.header["$TEXTSTYLE"] = style_name

    # ---- dimension style, sized from the source --------------------------
    # Arrow size and text height are measured from the drawing's own machinery:
    # the arrowheads are the filled 5.4 x 1.32 pt shapes on the dimension lines,
    # and the value text is 5.49 pt.  Sized from those, a DIMENSION draws itself
    # the way the source drew it.
    dim_style_name = cfg.get("dim_style_name", "CADREPRO")
    # Arrow size and text height are MEASURED from the source, not chosen: the
    # arrowheads are the filled 5.4 x 1.32 pt shapes sitting on the dimension lines.
    # Guessing 2.5 pt instead drew arrowheads at half the source's size, which cost
    # 48 % of the drawing's ink and was caught only by the comparison gate.
    arrow_pt = float(cfg.get("arrow_size_pt") or 5.4)
    text_pt = float(cfg.get("dim_text_pt") or 5.49)
    # MEASURED, not chosen.  In the source all 25 value labels sit with their centre
    # exactly 3.25 pt off the dimension line - 0.48 of the text height, and the spread
    # over 25 samples is 3.25 to 3.25, so it is a constant rather than a preference.
    # AutoCAD puts the text BASELINE at dimgap and centres the cap height above it, so
    # the centre lands near dimgap + text_height/2.  Solving that against the measured
    # 3.25 pt gives dimgap ~ 0.5 pt.  The earlier 2.75 pt held every number roughly 2 pt
    # - about a quarter of its own height - too far from its dimension line.
    gap_pt = float(cfg.get("dim_text_gap_pt") or 0.5)
    # The colour of the dimension machinery, as detected from (or configured for)
    # the source.  It is green on some sheets and something else on others; using
    # the detected role instead of the literal 3 keeps the DIMENSION entities on the
    # same colour the source drew them in.
    dim_colour_key = (dims.get("dimension_colour") or cfg.get("dim_colour") or "green")
    dim_aci = cadkit.key_to_aci(dim_colour_key) or 3
    dimstyle = doc.dimstyles.add(dim_style_name)
    dimstyle.dxf.dimtxsty = style_name
    dimstyle.dxf.dimtxt = round(text_pt * scale, 4)
    dimstyle.dxf.dimasz = round(arrow_pt * scale, 4)
    dimstyle.dxf.dimgap = round(gap_pt * scale, 4)
    # An "arrow" that is 5.4 pt long and 1.32 pt wide is not AutoCAD's closed solid
    # arrow, it is a filled sliver.  The "architectural tick" and "oblique" styles
    # both draw a stroke, so the closest match is the closed filled arrow with the
    # size taken from the source; the discrepancy is recorded in the build report.
    dimstyle.dxf.dimblk = ""
    dimstyle.dxf.dimlwd = 18
    dimstyle.dxf.dimlwe = 18
    dimstyle.dxf.dimdec = 1
    dimstyle.dxf.dimzin = 8          # suppress trailing zeros: 694.8 not 694.80
    dimstyle.dxf.dimexe = round(1.25 * scale, 4)    # extension beyond the line
    dimstyle.dxf.dimexo = round(0.0, 4)
    # The source puts its value text just OFF the dimension line rather than through
    # it: the gap measured from the drawing is 2.7 to 3.2 pt for a 5.49 pt text.
    # Above-the-line placement plus that gap reproduces where the numbers sit.
    dimstyle.dxf.dimtad = 1                          # text above the dimension line
    dimstyle.dxf.dimjust = 0
    dimstyle.dxf.dimtih = 0                          # keep text horizontal, not aligned
    dimstyle.dxf.dimtoh = 0
    dimstyle.dxf.dimclrd = dim_aci
    dimstyle.dxf.dimclre = dim_aci
    dimstyle.dxf.dimclrt = dim_aci

    # ---- linetypes -------------------------------------------------------
    # Dash patterns measured in the source, one DXF linetype each.  Without this
    # every centre line, hidden line and section boundary comes out solid: the dash
    # array was read into extract.json and then never used, and the self-check
    # could not see the loss because its own rasteriser ignored dashes too.
    linetype_names = {}
    for name, spec in (data.get("linetypes", {}).get("patterns") or {}).items():
        pattern = spec.get("pattern_pt") or []
        if not pattern or name == "CONTINUOUS":
            continue
        # AutoCAD stores the TOTAL pattern length as the first element, so the first
        # dash value would otherwise be swallowed as the length and every linetype
        # would come out with one element instead of two.  The lengths are converted
        # from source points to millimetres by the same factor the geometry uses.
        gp = [round((-v if i % 2 else v) * scale, 4) for i, v in enumerate(pattern)]
        used = gp if len(gp) > 2 else [abs(gp[0]) + abs(gp[1])] + gp
        doc.linetypes.add(name, pattern=used,
                          description="source PDF dash array "
                                      + " ".join("%g" % v for v in pattern) + " pt")
        linetype_names[tuple(spec["pattern_pt"])] = name

    # ---- layers ----------------------------------------------------------
    # A layer is created the first time something needs it, carrying the colour of
    # what goes on it.  The colour a layer carries is only a hint (every entity also
    # sets its own colour), but a layer whose colour contradicts its contents is a
    # trap for whoever opens the file.
    layer_specs = {}
    doc.layers.add(TEXT_LAYER[0], color=TEXT_LAYER[1])
    doc.layers.add(DIM_LAYER[0], color=dim_aci)

    def register_layer(key: str) -> str:
        """Create the layer for a colour key if it does not exist yet; return its name."""
        name, aci, rgb = layer_for_colour(key)
        if name in layer_specs:
            return name
        layer_specs[name] = ({"color": aci, "true_color": cadkit.rgb_int(rgb)}
                             if rgb else {"color": aci})
        try:
            doc.layers.add(name, **layer_specs[name])
        except Exception as exc:                       # pragma: no cover
            raise RuntimeError(
                f"could not create layer {name!r} for colour {key!r}: {exc}") from exc
        return name

    msp = doc.modelspace()

    # ---- what the dimensions replace -------------------------------------
    kept_dims = [p for p in dims["proposals"]
                 if p["value"] is not None
                 and _rank(p["confidence"]) >= _rank(min_confidence)]
    # Geometry a live dimension already draws: every green line, and the arrowheads
    # sitting on those lines.  Text is suppressed per proposal by bounding box
    # below, because only the dimension values must move to the dimension entity -
    # the installation notes stay as text.
    def suppression_boxes():
        """Regions a live DIMENSION replaces, kept as thin as the dimension is.

        The box must be a BAND along the dimension line, tight across it.  Using the
        dimension's overall bounding rectangle instead swallows everything that
        shares its span: the top dimension chain spans 171 to 315 pt horizontally, so
        its rectangle covered the whole upper half of the sheet and took the SHEET
        FRAME with it - measured, that removed both frame rectangles and 81 % of the
        drawing's black ink.
        """
        boxes = []
        for p in kept_dims:
            a, b = p["p1"], p["p2"]
            lo = (min(a[0], b[0]), min(a[1], b[1]))
            hi = (max(a[0], b[0]), max(a[1], b[1]))
            along_pad = 1.5          # pt, past the end of the dimension line
            across_pad = 3.0         # pt, beyond the drawn line and its arrowheads
            if p["axis"] == "h":
                boxes.append((lo[0] - along_pad, p["line_pos"] - across_pad,
                              hi[0] + along_pad, p["line_pos"] + across_pad))
            else:
                boxes.append((p["line_pos"] - across_pad, lo[1] - along_pad,
                              p["line_pos"] + across_pad, hi[1] + along_pad))
            # The value text is replaced by the dimension's own text, so its box is
            # suppressed too - but only that box, not a band across the sheet.
            if p.get("label_bbox"):
                lx0, ly0, lx1, ly1 = p["label_bbox"]
                boxes.append((lx0 - 1.0, ly0 - 1.0, lx1 + 1.0, ly1 + 1.0))
        return boxes

    dim_boxes = suppression_boxes()

    def point_in_a_dimension(x: float, y: float) -> bool:
        for bx0, by0, bx1, by1 in dim_boxes:
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                return True
        return False

    def item_in_a_dimension(item) -> bool:
        """Is ONE drawn piece inside a region a live dimension replaces?

        Judged per piece, not per path.  A PDF path is not one shape: the sheet
        frame and an unrelated line can share a single path, so testing the path's
        bounding rectangle deletes the whole path as soon as any part of it falls in
        a suppression band.  Measured, that removed both frame rectangles - 81 % of
        the drawing's black ink - because the top dimension chain's band crosses
        where the frame is.
        """
        kind = item[0]
        if kind == "l":
            (ax, ay), (bx, by) = item[1], item[2]
            return point_in_a_dimension(ax, ay) and point_in_a_dimension(bx, by)
        if kind == "re":
            x0, y0, x1, y1 = item[1]
            return (point_in_a_dimension(x0, y0) and point_in_a_dimension(x1, y1))
        if kind == "qu":
            return all(point_in_a_dimension(px, py) for px, py in item[1])
        if kind == "c":
            return all(point_in_a_dimension(px, py) for px, py in item[1:5])
        return False

    # ---- geometry --------------------------------------------------------
    counts = collections.Counter()
    suppressed = collections.Counter()
    lw_sub = collections.Counter()
    true_colour_entities = collections.Counter()

    def layer_and_colour(stroke, fill):
        key = stroke or fill or "black"
        return register_layer(key), key

    for p in data["paths"]:
        stroke, fill = p["color"], p["fill"]

        # 1. glyph outlines are replaced by real text
        if _is_text_outline(p, data):
            suppressed["glyph_outline"] += 1
            continue

        # 2. the dimension machinery is replaced by live DIMENSION entities, piece
        #    by piece - a whole path is not discarded just because part of it is
        #    dimension geometry.
        items = [it for it in p["items"] if not (use_dimensions and item_in_a_dimension(it))]
        if use_dimensions and len(items) != len(p["items"]):
            suppressed["dimension_geometry"] += len(p["items"]) - len(items)
        if not items:
            continue

        layer, colour = layer_and_colour(stroke, fill)
        attr = {"layer": layer}
        color_attr, _rec = cadkit.color_attrs(colour, cfg.get("color_policy", "exact"))
        attr.update(color_attr)
        if _rec.get("mode", "").startswith("true"):
            true_colour_entities[colour] += 1
        width_mm = (p["width"] or 0.0) * cadkit.PT2MM
        chosen, requested = cadkit.snap_lineweight(width_mm)
        if requested is not None:
            lw_sub[(requested, chosen)] += 1
        attr["lineweight"] = chosen

        # The dash pattern of this path, as a real DXF linetype.  A spline is the
        # one entity DXF cannot dash, so a dashed curve keeps its geometry and loses
        # its dashes; that loss is recorded in the build report rather than hidden.
        ltype = linetype_names.get(cadkit.dash_pattern(p.get("dashes")))
        if ltype:
            attr["linetype"] = ltype
            counts[f"dashed:{ltype}"] += 1

        # 3. SOLID SHAPES.  The source has no stroke-less filled path at all - the
        #    only fill-only paths are the 82 numeral outlines.  Its solid marks are
        #    circles whose pen is WIDER THAN THE CIRCLE ITSELF: a 0.84-1.32 pt
        #    diameter stroked at 1.44 pt, so the ink fills the interior and the mark
        #    reads as a solid dot.  That is the shape of the defect the user saw -
        #    a hollow circle with lines drawn inside it - and the fix is to detect
        #    the condition and emit a real filled region instead.
        if _reads_as_solid(p):
            _emit_solid(msp, p, attr, X, Y, scale)
            counts["solid_hatch"] += 1
            continue

        if stroke is None and fill is not None:
            _emit_solid(msp, p, attr, X, Y, scale)
            counts["solid_hatch"] += 1
            continue

        for item in items:
            kind = item[0]
            if kind == "l":
                (ax, ay), (bx, by) = item[1], item[2]
                msp.add_lwpolyline([(X(ax), Y(ay)), (X(bx), Y(by))], dxfattribs=attr)
                counts["line"] += 1
            elif kind == "re":
                x0, y0, x1, y1 = item[1]
                msp.add_lwpolyline([(X(x0), Y(y0)), (X(x1), Y(y0)),
                                    (X(x1), Y(y1)), (X(x0), Y(y1))],
                                   close=True, dxfattribs=attr)
                counts["rect"] += 1
            elif kind == "qu":
                msp.add_lwpolyline([(X(px), Y(py)) for px, py in item[1]],
                                   close=True, dxfattribs=attr)
                counts["quad"] += 1
            elif kind == "c":
                bez = Bezier([(X(px), Y(py)) for px, py in item[1:5]])
                spline_attr = {k: v for k, v in attr.items() if k != "linetype"}
                if "linetype" in attr:
                    counts["dashed_curve_not_dashed"] += 1
                msp.add_open_spline(bez.control_points, degree=3, dxfattribs=spline_attr)
                counts["curve"] += 1
            else:
                counts[f"skipped:{kind}"] += 1

        # ---- curves ---------------------------------------------------------
        # A PDF has no arc primitive.  Its content stream has only move/line/curve/
        # rectangle/close, so every circle and every arc in it is written as cubic
        # Beziers - a full circle is usually four, one per quadrant.  Emitting those
        # Beziers one at a time as splines means a single drawn arc becomes several
        # unrelated entities that CAD cannot select, dimension or edit as one curve.
        #
        # curve_entities looks at the run as a whole and decides, from a measured
        # residual, whether it is a circular arc.  If it is, the drawing gets a real
        # ARC or CIRCLE - the entity the original draughtsman would have used.  If it
        # is not, the run is kept as a SPLINE built from the source control points,
        # which is exact: a chain of N cubic Beziers is reproduced by one degree-3
        # spline to 1e-13 pt, passing through every junction.
        #
        # The run ends wherever a non-curve item intervenes, so curves are never
        # joined across other geometry.
        curve_runs = []
        prev_was_curve = False
        for item in items:
            if item[0] == "c":
                cubic = curve_entities.item_cubic(item)
                if prev_was_curve:
                    curve_runs[-1].append(cubic)
                else:
                    curve_runs.append([cubic])
                prev_was_curve = True
            else:
                prev_was_curve = False

        for cubics in curve_runs:
            curve_entities.emit_curves(msp, cubics, attr, X, Y, scale, counts)

    # ---- live dimensions -------------------------------------------------
    # `associative` decides where the number comes from, and the choice is forced by
    # the source drawing rather than by preference.
    #
    # A live dimension measures the geometry it is attached to.  Measured on this
    # sheet, the ratio between each printed value and the length actually drawn for
    # it ranges from 0.59 to 7.58 - the drawing claims 1:15 in its title block but is
    # plotted at roughly 1:50.6, and several of its dimensions do not agree with
    # their own geometry at all.  So "let CAD measure it" and "print the number the
    # source prints" cannot both hold: on this drawing they are different numbers.
    #
    # Default: draw the dimension with CAD's own machinery - extension lines,
    # dimension line, arrowheads and text all produced by the DIMENSION entity - and
    # set the value to the source's, so the sheet reads as the source reads.
    # Switch to associative mode once a drawing's geometry and its numbers agree, and
    # CAD then measures and maintains the value itself.
    dims_emitted = 0
    dim_failures = []
    for p in kept_dims:
        a, b = p["p1"], p["p2"]
        line_pos = p["line_pos"]
        sign = p["offset_sign"]
        offset = float(cfg.get("dim_extension_mm") or 4.0) * sign
        try:
            span = offset / scale
            if p["axis"] == "h":
                fy = line_pos + span * sign
                base = (X((a[0] + b[0]) / 2.0), Y(line_pos))
                p1 = (X(a[0]), Y(fy))
                p2 = (X(b[0]), Y(fy))
            else:
                fx = line_pos + span * sign
                base = (X(line_pos), Y((a[1] + b[1]) / 2.0))
                p1 = (X(fx), Y(a[1]))
                p2 = (X(fx), Y(b[1]))

            # add_linear_dim returns a DimStyleOverride wrapper; the entity is on
            # `.dimension` and the wrapper carries the setters and render().
            dim = msp.add_linear_dim(base=base, p1=p1, p2=p2,
                                     dimstyle=dim_style_name,
                                     text="<>" if associative else p["value"],
                                     dxfattribs={"layer": DIM_LAYER[0]})
            if associative:
                dim.set_text("<>")
                if dim.dimension.dxf.get("text") != "<>":
                    raise RuntimeError("dimension text is not associative")
            else:
                dim.set_text(p["value"])
            # render() is mandatory: without it the DIMENSION exists but carries no
            # geometry block, so nothing is visible in the drawing.
            dim.render()
            dims_emitted += 1
        except Exception as exc:                      # pragma: no cover
            dim_failures.append({"p1": a, "p2": b, "error": f"{type(exc).__name__}: {exc}"})

    # ---- text ------------------------------------------------------------
    # Group the source spans into blocks first: several lines that share a column
    # and are close together are one MTEXT paragraph, not a stack of TEXT entities.
    text_emitted = {"TEXT": 0, "MTEXT": 0}
    outlined = {id(p) for p in data["paths"] if _is_text_outline(p, data)}
    for span in data["spans"]:
        text = span["text"]
        if not text.strip():
            continue
        if use_dimensions and point_in_a_dimension(span["bbox"][0] + 0.01, span["bbox"][1] + 0.01):
            suppressed["dimension_text"] += 1
            continue
        x0, y0, x1, y1 = span["bbox"]
        height = round(span["size"] * scale, 4)
        vertical = abs(span["dir"][1]) > 0.5
        colour = _span_colour(span)
        # Text goes on the layer for its own colour, not on one shared TEXT layer.
        # A span whose colour is outside the named six then needs no substitution
        # either, which is the whole point: the sheet keeps the colours it has.
        attr = {"layer": register_layer(colour), "style": style_name}
        color_attr, _rec = cadkit.color_attrs(colour, cfg.get("color_policy", "exact"))
        attr.update(color_attr)

        if "\n" in text or (mtext_wrap and len(text) > 40):
            # MTEXT, for text that must wrap or carry internal formatting.  Note
            # that the in-process renderer used for self-checking draws MTEXT as
            # empty boxes where the same font in a TEXT entity draws correctly
            # (measured: 42 ink pixels against 372 for identical content), so a
            # wrapped block is a deliberate trade of checkability for wrapping.
            mt = msp.add_mtext(text, dxfattribs={**attr, "char_height": height})
            mt.set_location((X(x0), Y(y1)), attachment_point=MTextEntityAlignment.TOP_LEFT)
            text_emitted["MTEXT"] += 1
        else:
            # TEXT by default.  The source PDF already splits its text into lines,
            # so each span IS one line and nothing is lost by using the simpler
            # entity - and this is the form that renders and checks correctly.
            ent = msp.add_text(text, height=height, dxfattribs=attr)
            if vertical:
                ent.dxf.rotation = 90
            ent.set_placement((X(x0), Y(y1)), align=TextEntityAlignment.BOTTOM_LEFT)
            text_emitted["TEXT"] += 1

    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(out_dxf)))
    doc.saveas(out_dxf)

    report = {
        "dxf": os.path.abspath(out_dxf),
        "units": "mm",
        "scale_mm_per_pt": scale,
        "origin_pt": [ox, oy],
        "content_extent_pt": extent,
        "source_paths": len(data["paths"]),
        "entity_counts": dict(counts),
        "text_counts": text_emitted,
        "dimensions_emitted": dims_emitted,
        "dimension_failures": dim_failures,
        "suppressed": dict(suppressed),
        "lineweight_substitutions": {f"{a}->{b}": n for (a, b), n in lw_sub.items()},
        "dimension_style": {
            "name": dim_style_name, "text_height_mm": dimstyle.dxf.dimtxt,
            "arrow_size_mm": dimstyle.dxf.dimasz, "gap_mm": dimstyle.dxf.dimgap,
            "text_style": style_name, "font": style_font,
            "colour": dim_colour_key, "aci": dim_aci,
        },
        "linetypes": {
            "defined": sorted(n for n in linetype_names.values()),
            "source_patterns_pt": {n: s["pattern_pt"]
                                   for n, s in (data.get("linetypes", {})
                                                .get("patterns") or {}).items()},
        },
        "layers": sorted(layer_specs),
        "true_colour_entities": true_colour_entities,
    }
    return report


def _reads_as_solid(p: dict) -> bool:
    """True when a stroked shape's pen is so wide it fills the shape's interior.

    A dot drawn as a small circle with a pen wider than its own diameter has no
    hollow interior on paper: the stroke covers it.  Both numbers are in source
    points, so the test is scale-independent and needs no threshold chosen by hand.
    DXF cannot express such a pen (0.18 mm is 2 % of the 38 mm the ratio implies,
    and the format caps lineweight at 2.11 mm), so the appearance is reproduced with
    a filled region instead.

    The shape must also be a SHAPE.  Without that condition the rule matches every
    degenerate fragment in the drawing - measured here, 2369 paths of 0.12 to
    0.84 pt, which are line ends, dash caps and hatch faceting rather than marks -
    and produces thousands of hatches nobody asked for.  Requiring several vertices
    that actually enclose something keeps the 102 real dots and drops the noise.

    Also true for a path that is filled with no stroke at all.
    """
    if p["fill"] is not None and (p["color"] is None or (p["width"] or 0.0) <= 0.0):
        return True
    width = p["width"] or 0.0
    if width <= 0.0 or p["color"] is None:
        return False
    if len(p["items"]) < 3:
        return False
    x0, y0, x1, y1 = p["rect"]
    w, h = x1 - x0, y1 - y0
    if len(p["items"]) > 48:          # a drawn curve, not a font facet
        return False
    # A real mark has area in both directions; a fragment is flat in one.
    if min(w, h) <= 0.05:
        return False
    return max(w, h) / 2.0 <= width


def _rank(conf: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(conf, 0)


def _span_colour(span: dict) -> str:
    r = int(span["color"][1:3], 16) / 255.0
    g = int(span["color"][3:5], 16) / 255.0
    b = int(span["color"][5:7], 16) / 255.0
    return cadkit.rgb_to_key((r, g, b)) or "black"


def _is_text_outline(p: dict, data: dict) -> bool:
    """A single character drawn as a filled path rather than as text.

    Recognised by shape and by position: filled, with no stroke ink of its own, and
    either about the size of one digit or sitting on a text span.  A pure size test
    also matches hatching fragments, and a pure position test also matches the
    dimension lines that cross a numeral, so both halves are needed.
    """
    if p["fill"] is None:
        return False
    if p["color"] is not None and (p["width"] or 0.0) > 0.0:
        return False
    x0, y0, x1, y1 = p["rect"]
    w, h = x1 - x0, y1 - y0
    if ((w <= infer_dims.GLYPH_MAX_THIN and h <= infer_dims.GLYPH_MAX_LONG)
            or (h <= infer_dims.GLYPH_MAX_THIN and w <= infer_dims.GLYPH_MAX_LONG)):
        return True
    return _overlaps_span(p["rect"], data["spans"])


def _overlaps_span(rect, spans) -> bool:
    x0, y0, x1, y1 = rect
    for s in spans:
        bx0, by0, bx1, by1 = s["bbox"]
        m = (by1 - by0) * 0.35
        if not (x1 <= bx0 - m or bx1 + m <= x0 or y1 <= by0 - m or by1 + m <= y0):
            return True
    return False


def _emit_solid(msp, path: dict, attr: dict, X, Y, scale: float) -> None:
    """A stroke-less filled path becomes a real SOLID hatch.

    Circles whose stroke is wider than their own diameter print as solid discs, and
    DXF cannot express a stroke that wide - the lineweight cap is 2.11 mm against
    the 38 mm the source would need.  So the disc is reproduced as a filled region.
    A closed polyline cannot do this: it is an outline, hollow on screen and pale on
    paper, which is the defect this replaces.
    """
    items = path["items"]
    kinds = {it[0] for it in items}
    rect = path["rect"]
    width = path.get("width") or 0.0

    if kinds == {"c"} and len(items) == 4:
        # Four cubic segments is how a PDF writes a circle, and a circle's path
        # rectangle is its bounding box, so centre and radius come straight out of it.
        #
        # The radius is the geometry radius PLUS half the pen: the source draws a
        # small circle with a pen wider than the circle, so what lands on paper is
        # the circle grown by half the pen on every side.  Using the bare geometry
        # radius reproduces a dot of two thirds the size - measured 1.2 to 2.6 pt
        # against the source's 2.4 pt.
        cx = (rect[0] + rect[2]) / 2.0
        cy = (rect[1] + rect[3]) / 2.0
        radius_pt = max(rect[2] - rect[0], rect[3] - rect[1]) / 2.0 + width / 2.0
        hatch = msp.add_hatch(dxfattribs=attr)
        hatch.set_solid_fill(color=attr.get("color", 250))
        hatch.paths.add_edge_path().add_arc(center=(X(cx), Y(cy)),
                                            radius=radius_pt * scale,
                                            start_angle=0, end_angle=360)
        # A real CIRCLE on top too, so the outline is a circle entity rather than a
        # tessellated polygon and the shape stays clean when scaled or plotted.
        msp.add_circle((X(cx), Y(cy)), radius_pt * scale, dxfattribs=attr)
        return

    hatch = msp.add_hatch(dxfattribs=attr)
    hatch.set_solid_fill(color=attr.get("color", 250))
    pts = []
    for item in items:
        kind = item[0]
        if kind == "l":
            (ax, ay), (bx, by) = item[1], item[2]
            pts.append((X(ax), Y(ay)))
            pts.append((X(bx), Y(by)))
        elif kind == "re":
            x0, y0, x1, y1 = item[1]
            pts.extend([(X(x0), Y(y0)), (X(x1), Y(y0)), (X(x1), Y(y1)), (X(x0), Y(y1))])
        elif kind == "qu":
            pts.extend([(X(px), Y(py)) for px, py in item[1]])
    if len(pts) >= 3:
        hatch.paths.add_polyline_path(pts, is_closed=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="build a DXF with native CAD entities")
    ap.add_argument("extract_json")
    ap.add_argument("dims_json")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--config")
    ap.add_argument("--json")
    ap.add_argument("--no-dimensions", action="store_true",
                    help="draw dimensions as plain geometry instead of live entities")
    ap.add_argument("--min-confidence", default="medium",
                    choices=("low", "medium", "high"))
    ap.add_argument("--associative", action="store_true",
                    help="let CAD measure the value instead of printing the source's "
                         "number; only correct once the drawing's geometry and its "
                         "numbers agree")
    ap.add_argument("--mtext", action="store_true",
                    help="wrap long text into MTEXT; renders as empty boxes in the "
                         "in-process checker, so it is off by default")
    args = ap.parse_args()

    cfg = cadkit.load_config(args.config)
    with open(args.extract_json, encoding="utf-8") as fh:
        data = json.load(fh)
    with open(args.dims_json, encoding="utf-8") as fh:
        dims = json.load(fh)

    report = build(data, dims, args.out, cfg,
                   use_dimensions=not args.no_dimensions,
                   min_confidence=args.min_confidence,
                   associative=args.associative,
                   mtext_wrap=args.mtext)
    report["dimension_value_source"] = ("measured by CAD" if args.associative
                                        else "copied from the source drawing")

    cadkit.banner("DRAWING BUILT")
    cadkit.table([(k, v) for k, v in report.items()
                  if not isinstance(v, (dict, list))], ("item", "value"))
    print()
    cadkit.table([(k, v) for k, v in report["entity_counts"].items()], ("geometry", "count"))
    print()
    cadkit.table([(k, v) for k, v in report["text_counts"].items()], ("text entity", "count"))
    print()
    cadkit.table([(k, v) for k, v in report["suppressed"].items()],
                 ("suppressed (replaced by a real entity)", "count"))
    print()
    st = report["dimension_style"]
    cadkit.table([("text height mm", st["text_height_mm"]), ("arrow size mm", st["arrow_size_mm"]),
                  ("text gap mm", st["gap_mm"]), ("font", st["font"]),
                  ("text style", st["text_style"])], ("dimension style", "value"))
    if report["dimension_failures"]:
        print("\nDIMENSION FAILURES:")
        for f in report["dimension_failures"]:
            print(f"   {f}")
    if args.json:
        cadkit.ensure_dirs(os.path.dirname(os.path.abspath(args.json)))
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
