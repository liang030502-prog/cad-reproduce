# -*- coding: utf-8 -*-
"""Infer live DIMENSION entities from the source drawing's own dimension machinery.

Why this stage exists: CAD's DIMENSION entity draws its own extension lines,
dimension line, arrowheads and value text.  The source drawing draws all of those
as ordinary geometry, so emitting both would double every dimension.  This stage
reads the drawn version and recovers the INTENT - "this pair of extension lines is
one dimension of this value" - so the generator can drop the drawn geometry and let
CAD draw it properly.

How a chain is recognised, all from measurements on the source:

* Dimension geometry is GREEN in this drawing (extension lines and dimension lines
  both), which was measured, not assumed: red turned out to be centre lines.
* A dimension line is a run of collinear green segments; a chain is several such
  runs on the same line, separated by small arrow gaps.
* The extension lines are green segments PERPENDICULAR to the dimension line, and
  they start at the dimension line and run toward the feature - that direction is
  the offset, and it is what tells CAD which way to draw the arrows.
* The value comes from the numeric text label sitting immediately off the
  dimension line, near the middle of the run.

Anything the rules cannot resolve is reported as an ambiguity instead of being
guessed at, because a wrongly placed dimension is worse than a missing one.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402

# A dimension value: digits with at most one decimal point.  Deliberately strict -
# loosely matching "looks numeric" also matches mangled title-block fields such as
# a part number, which are not dimensions at all.
NUMERIC = re.compile(r"^\d+(?:\.\d+)?$")

TOL_COLLINEAR = 0.35      # pt, two segments count as being on the same line
MAX_ARROW_GAP = 6.0       # pt, the gap an arrowhead leaves in a dimension line
MIN_RUN = 3.0             # pt, shorter than this is not a dimension line
EXT_NEAR = 1.2            # pt, how close an extension line must be to an endpoint
EXT_MIN_LEN = 2.0         # pt, an extension line is at least this long
LABEL_MAX_OFFSET = 16.0   # pt, how far a value label may sit from its line
LABEL_MAX_ALONG = 25.0    # pt, how far along the line that label may sit


def _famkey(name):
    return name


# Which colour carries the dimension machinery is a property of a DRAWING, not of
# this pipeline.  On the sheet this module was developed against the answer was
# green (red turned out to be centre lines), and that measurement is still the
# default - but it is now a default that can be overridden per job, and one that is
# DETECTED and reported instead of assumed.  Hard-coding it meant a sheet whose
# dimensions are drawn in another colour inferred zero dimensions and reported
# success, which is the worst of both worlds: a wrong drawing and a clean report.
DEFAULT_DIM_COLOUR = "green"

# Arrowhead signature: a small filled shape of a few line items.  The item count and
# the size window below are measured from the reference sheet (6 items, 5.4 x 1.32 pt).
#
# They are only a DEFAULT.  Measured on a second real sheet - a steelwork drawing
# plotted at 1:20 instead of 1:15 - the arrowheads are 2.76 x 0.72 pt, which is
# OUTSIDE the window above in both dimensions, so the matcher found ZERO arrows
# there.  With no arrows the dimension chain cannot be cut at its boundaries, and
# 110 of that sheet's 126 spans came out with no value attached.  "Configurable" is
# not the same as "works": a threshold that has to be re-typed per drawing is a
# threshold that will be wrong on the next one.  See `adaptive_arrows`.
DEFAULT_ARROW_ITEMS = 6
DEFAULT_ARROW_LONG_PT = (4.5, 6.5)
DEFAULT_ARROW_SHORT_PT = (0.8, 2.0)

# How the adaptive search bounds itself, as RATIOS of the sheet's own measured value.
# The relationship that makes this work: an arrowhead's length tracks the sheet's
# dimension text height.  Measured, long/text = 0.983 on the reference sheet and
# 0.527 on the steel sheet - different by a factor of 1.87, in the same direction as
# their plot scales (1:15 against 1:20).  The aspect ratio is NOT constant (4.09
# against 3.83), which is why the search below clusters on SIZE and then confirms
# with geometry, rather than testing a formula.
ADAPTIVE_SIZE_TOL = 0.45      # arrow length within +/-45 % of the text height
ADAPTIVE_MAX_ASPECT = 8.0     # longer than this is a hatch sliver, not an arrowhead
ADAPTIVE_MIN_ASPECT = 1.6     # rounder than this is a dot or a glyph
ADAPTIVE_ON_LINE_TOL = 1.5    # pt, how close an arrow must sit to its dimension line
ADAPTIVE_MIN_SCORE = 3        # arrows required before the adaptive set is believed

# The value-text size floor is a MEASUREMENT per drawing, exactly like the arrow size,
# and for the same reason: text is printed at a size that follows the plot scale.  The
# reference sheet's dimension values are 5.49 pt; the steel sheet's MAIN DIMENSION
# CHAIN is 2.86 pt, and every one of its 35 labels was excluded by a 5.2 pt floor taken
# from the other drawing - 26 of them the same repeated `1160`, which is a chain of
# equal bays if anything ever was.  Measured both ways on the steel sheet:
#
#     floor 5.24 / 3.76 / 3.44 ->  9 usable, largest ratio cluster  3 of  9 (33 %)
#     floor 2.86               -> 43 usable, largest ratio cluster 29 of 43 (67 %), centre 71.45
#
# and 71.45 is 1:20 expressed in points of paper, the scale its title block claims.  So
# the floor is not chosen by size ORDER: on that sheet the largest numeric text
# (5.24 pt) is a set of weld callouts in a detail view, and "take the biggest" would
# pick exactly the wrong layer, even though it happens to be right on the reference
# sheet.  It is chosen by which layer PAIRS with the geometry.  See `search_label_floor`.
LABEL_FLOOR_CANDIDATE_LIMIT = 8     # distinct numeric sizes worth trying per sheet
LABEL_FLOOR_MIN_PT = 1.0            # below this a "number" is noise, not annotation


def dim_defaults() -> dict:
    """The tunables of this stage, with the values measured on the reference sheet.

    Every one of them can be overridden from `cad-reproduce.yaml` or from the
    command line.  The values are measurements, not preferences: the arrow window
    comes from the arrowheads' own bounding boxes, and the label size floor from the
    height of the sheet's dimension text.

    `arrow_items` is the one that travels between drawings: measured 6 on both
    sheets available, and it is a property of how the drawing was produced (a filled
    arrow polygon) rather than of how big it was plotted.
    """
    return {
        "colour": DEFAULT_DIM_COLOUR,
        "arrow_items": DEFAULT_ARROW_ITEMS,
        "arrow_long_pt": list(DEFAULT_ARROW_LONG_PT),
        "arrow_short_pt": list(DEFAULT_ARROW_SHORT_PT),
        "label_min_size_pt": 5.2,
        "tol_collinear_pt": TOL_COLLINEAR,
        "max_arrow_gap_pt": MAX_ARROW_GAP,
        "min_run_pt": MIN_RUN,
        "ext_near_pt": EXT_NEAR,
        "ext_min_len_pt": EXT_MIN_LEN,
        "label_max_offset_pt": LABEL_MAX_OFFSET,
        "label_max_along_pt": LABEL_MAX_ALONG,
        # `label_min_size_pt` above is only a default: a second drawing is expected to
        # contradict it, and `search_label_floor` re-measures it.  Set this to False to
        # pin the floor and skip the search.
        "label_floor_search": True,
    }


def _settings(overrides: dict | None = None) -> dict:
    s = dim_defaults()
    for key, value in (overrides or {}).items():
        if value is not None and key in s:
            s[key] = value
    return s


def collect_arrows(data: dict, colour: str = DEFAULT_DIM_COLOUR,
                   settings: dict | None = None):
    """Positions of the arrowheads, which ARE the boundaries between dimensions.

    This is the reliable way to cut a chain.  Deriving boundaries from the drawn
    line segments fails whenever two dimension lines overlap on the same axis:
    measured on this drawing, the line at y=136.84 carries segments 171.68-187.40
    and 179.36-218.24 - two different dimensions whose lines overlap.  Merging them
    into one run and cutting at a synthetic midpoint gave a span of 11.70 pt for a
    dimension whose neighbours all agree on about 1.7 pt.

    An arrowhead is a small filled shape of exactly six line items that is either
    5.4 x 1.32 pt or its transpose, and nothing else in the drawing matches that.

    This runs BEFORE the glyph filter, deliberately.  A numeral glyph outline and an
    arrowhead are both "small filled shape with no stroke of its own", so the glyph
    predicate - a size test - also matches the arrows: measured, all 30 of them.
    Filtering by glyph first therefore finds no arrowheads at all and silently falls
    back to the unreliable segment-based boundaries.  The arrow signature (exactly
    six items, 5.4 x 1.32 or transposed) is far more specific than the glyph
    signature, so it is applied first.

    The signature is data, not a literal: a sheet whose arrows are a different size
    or a different item count gets its own values through `settings`.  When those
    configured bounds match nothing, the search retries with bounds MEASURED from the
    sheet (see `adaptive_arrows`) and records that it did so, because a configured
    window that silently matches nothing is the failure this whole stage exists to
    avoid.
    """
    s = _settings(settings)
    lo_l, hi_l = s["arrow_long_pt"]
    lo_s, hi_s = s["arrow_short_pt"]
    arrows = []
    for p in data["paths"]:
        if p["fill"] != colour or len(p["items"]) != s["arrow_items"]:
            continue
        x0, y0, x1, y1 = p["rect"]
        w, h = x1 - x0, y1 - y0
        if not ((lo_l <= w <= hi_l and lo_s <= h <= hi_s)
                or (lo_l <= h <= hi_l and lo_s <= w <= hi_s)):
            continue
        arrows.append({"cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
                       "w": w, "h": h, "rect": [x0, y0, x1, y1]})
    return arrows


def sheet_label_size(data: dict, settings: dict | None = None) -> float | None:
    """The sheet's dimension text height, measured from the numbers on it.

    Uses the LARGEST common numeric size rather than the first or the mean: a title
    block also carries numbers (part numbers, weights), and they are drawn smaller
    than the dimension values on every sheet seen so far.  Measured, the reference
    sheet has 25 labels at 5.49 pt and 3 at 5.01; the steel sheet has 9 at 5.24 and
    15 at 3.4-3.8, all of the small ones in material tables.

    This is the anchor the arrow search scales against, so it must come from the
    drawing.  Reading it from the config would move the calibration problem one
    level up instead of removing it.
    """
    s = _settings(settings)
    sizes = [L["size"] for L in numeric_labels(data, min_size=s["label_min_size_pt"])]
    if not sizes:
        return None
    counts = collections.Counter(round(v, 2) for v in sizes)
    top = max(counts.values())
    # Among sizes that are within one label of the most common, take the largest -
    # anti-aliasing splits what is really one size across neighbouring values, and a
    # title block's numbers are strictly smaller.
    return max(v for v, n in counts.items() if n >= max(1, top - 1))


def adaptive_arrows(data: dict, colour: str, settings: dict | None = None) -> dict:
    """Find the arrowheads by measuring them, when the configured window finds none.

    Two things are known about a dimension arrowhead and neither depends on plot
    scale: it is a small filled shape, and it sits ON a dimension line of the same
    colour.  So the search is

      1. collect filled shapes of `arrow_items` items of this colour that are
         elongated (aspect between ADAPTIVE_MIN_ASPECT and ADAPTIVE_MAX_ASPECT) and
         no longer than the sheet's text height times (1 + ADAPTIVE_SIZE_TOL);
      2. group them by bounding box, since every arrowhead of one drawing shares it;
      3. keep the groups that actually sit on a long run of the colour.

    Step 3 is what makes it safe.  On the steel sheet there are 44 shapes at
    2.76 x 0.72, 20 at 2.88 x 0.72 and 14 at 3.36 x 1.08, plus 60-odd short-ish
    hatch and weld marks of 18-21 items; requiring the shape to lie on a dimension
    line and to touch it at its end is what separates the 6-item arrows from the
    rest.  Measured, the reference sheet's own 5.4 x 1.32 arrows also satisfy every
    step, so the same code path finds them - the configured window is simply tried
    first because it is cheaper and it is what the config says to trust.
    """
    s = _settings(settings)
    text = sheet_label_size(data, settings)
    if not text:
        return {"arrows": [], "reason": "no numeric label to measure the sheet's text "
                                        "height against", "text_height_pt": None}

    horizontal, vertical = collect_segments(data, colour)
    runs = ([("h", r) for r in group_runs(horizontal, s["max_arrow_gap_pt"])]
            + [("v", r) for r in group_runs(vertical, s["max_arrow_gap_pt"])])
    runs = [r for r in runs if r[1]["b"] - r[1]["a"] >= s["min_run_pt"]]

    limit = text * (1.0 + ADAPTIVE_SIZE_TOL)
    groups = collections.defaultdict(list)
    for p in data["paths"]:
        if p["fill"] != colour or len(p["items"]) != s["arrow_items"]:
            continue
        x0, y0, x1, y1 = p["rect"]
        w, h = x1 - x0, y1 - y0
        long_, short = max(w, h), min(w, h)
        if short <= 0 or long_ > limit:
            continue
        aspect = long_ / short
        if not (ADAPTIVE_MIN_ASPECT <= aspect <= ADAPTIVE_MAX_ASPECT):
            continue
        groups[(round(long_, 2), round(short, 2))].append(
            {"cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
             "w": w, "h": h, "rect": [x0, y0, x1, y1]})

    best = {"arrows": [], "score": 0, "size": None, "candidates": len(groups)}
    for size, members in groups.items():
        long_, short = size
        on_line = []
        for a in members:
            for axis, run in runs:
                pos = a["cy"] if axis == "h" else a["cx"]
                along = a["cx"] if axis == "h" else a["cy"]
                if abs(pos - run["pos"]) <= max(ADAPTIVE_ON_LINE_TOL, short) \
                        and run["a"] - long_ <= along <= run["b"] + long_:
                    on_line.append(a)
                    break
        if len(on_line) > best["score"]:
            best = {"arrows": on_line, "score": len(on_line), "size": size,
                    "candidates": len(groups)}

    ok = best["score"] >= ADAPTIVE_MIN_SCORE
    return {
        "arrows": best["arrows"] if ok else [],
        "score": best["score"],
        "size_pt": best["size"],
        "text_height_pt": text,
        "candidates": len(groups),
        "reason": (f"measured {best['score']} arrowheads at "
                   f"{best['size'][0]} x {best['size'][1]} pt on {best['candidates']} "
                   f"candidate size(s), against this sheet's {text:.2f} pt dimension text"
                   if ok else
                   f"no arrowhead size carried {ADAPTIVE_MIN_SCORE} or more shapes on a "
                   f"dimension line ({best['candidates']} candidate size(s) seen, best "
                   f"had {best['score']})"),
    }


def arrows_for(data: dict, colour: str, settings: dict | None = None) -> list:
    """Arrowheads for one colour: the configured signature first, measurement second.

    The order matters.  The configured window is what the caller asked for and what
    the build report should reflect, so it wins when it matches anything at all.  It
    is only when it matches NOTHING that the sheet is measured - because that is the
    case where the configured numbers are wrong for this drawing, and returning an
    empty list there is what silently disabled the whole dimension chain.
    """
    configured = collect_arrows(data, colour, settings=settings)
    if configured:
        return configured
    return adaptive_arrows(data, colour, settings)["arrows"]


def collect_segments(data: dict, colour: str = "green"):
    """Straight horizontal and vertical segments of one colour, from extract.json.

    Numeral glyph outlines are excluded first.  They are the same colour as the
    dimension machinery, they are made of hundreds of tiny straight facets, and
    leaving them in inflates the input roughly fortyfold - the first run of this
    stage saw 5458 "green horizontal segments" where the drawing has about a
    hundred, almost all of them facets of digit outlines.  The predicate is the
    same one the generator uses, so both stages agree on what is a glyph.

    Curves are ignored: a dimension line is straight by definition, and treating a
    curve as a straight run would invent dimensions that are not there.
    """
    horizontal, vertical = [], []
    for p in data["paths"]:
        colour_of = p["color"] or p["fill"]
        if colour_of != colour:
            continue
        if _is_glyph_path(p):
            continue
        width = p["width"] or 0.0
        for item in p["items"]:
            if item[0] != "l":
                continue
            (ax, ay), (bx, by) = item[1], item[2]
            if abs(ay - by) <= TOL_COLLINEAR and abs(bx - ax) > 0.05:
                horizontal.append({"pos": round((ay + by) / 2.0, 3),
                                   "a": round(min(ax, bx), 3),
                                   "b": round(max(ax, bx), 3), "w": width})
            elif abs(ax - bx) <= TOL_COLLINEAR and abs(by - ay) > 0.05:
                vertical.append({"pos": round((ax + bx) / 2.0, 3),
                                 "a": round(min(ay, by), 3),
                                 "b": round(max(ay, by), 3), "w": width})
    return horizontal, vertical


GLYPH_MAX_THIN = 3.5
GLYPH_MAX_LONG = 6.5


def _is_glyph_path(p: dict) -> bool:
    """A single character outline: filled, with no stroke ink of its own, and
    about the size of one digit."""
    if p["fill"] is None:
        return False
    if p["color"] is not None and (p["width"] or 0.0) > 0.0:
        return False
    x0, y0, x1, y1 = p["rect"]
    w, h = x1 - x0, y1 - y0
    return ((w <= GLYPH_MAX_THIN and h <= GLYPH_MAX_LONG)
            or (h <= GLYPH_MAX_THIN and w <= GLYPH_MAX_LONG))


def dimension_like_runs(data: dict, colour: str, settings: dict | None = None) -> dict:
    """How many of this colour's long straight runs look like DIMENSION lines.

    A run is bracketed when a perpendicular run of the same colour starts within
    `ext_near_pt` of each of its ends: that is the signature of a dimension line
    between two extension lines.

    The distinction matters more than it looks.  Value labels are text, and text has
    no colour relationship to the line it belongs to - on the reference sheet the 25
    dimension values are drawn as green glyph outlines while their text layer is
    empty, so a naive "which colour has the most labels near it" test nominates
    BLACK, the sheet frame and outline, because the labels' own spans are black.  A
    frame cannot be bracketed by extension lines, so requiring the bracket separates
    the dimension machinery from a drawing's outline.
    """
    s = _settings(settings)
    horizontal, vertical = collect_segments(data, colour)
    runs = ([("h", r) for r in group_runs(horizontal, s["max_arrow_gap_pt"])]
            + [("v", r) for r in group_runs(vertical, s["max_arrow_gap_pt"])])
    perpendicular = [("v", seg) for seg in vertical] + [("h", seg) for seg in horizontal]
    bracketed = 0
    for axis, run in runs:
        if run["b"] - run["a"] < s["min_run_pt"]:
            continue
        want = "v" if axis == "h" else "h"
        ends = []
        for end in (run["a"], run["b"]):
            hit = any(p_axis == want and p["b"] - p["a"] >= s["ext_min_len_pt"]
                      and abs(p["pos"] - end) <= s["ext_near_pt"]
                      for p_axis, p in perpendicular)
            ends.append(hit)
        if all(ends):
            bracketed += 1
    return {"bracketed_runs": bracketed,
            "long_runs": sum(1 for _a, r in runs if r["b"] - r["a"] >= s["min_run_pt"])}


def dim_colour_candidates(data: dict, settings: dict | None = None) -> dict:
    """Every colour that could be carrying the dimension machinery, with evidence.

    A colour is a candidate if the drawing puts dimension-like geometry in it: long
    straight runs (dimension lines), short perpendicular runs (extension lines),
    small filled slivers (arrowheads) or bare numbers in the sheet's value-text size.
    """
    s = _settings(settings)
    labels = numeric_labels(data, min_size=s["label_min_size_pt"])
    by_colour = collections.defaultdict(lambda: collections.Counter())
    for p in data["paths"]:
        key = p["color"] or p["fill"]
        if not key:
            continue
        if _is_glyph_path(p):
            by_colour[key]["glyph_paths"] += 1
            continue
        for item in p["items"]:
            if item[0] != "l":
                continue
            (ax, ay), (bx, by) = item[1], item[2]
            if abs(ay - by) <= s["tol_collinear_pt"] and abs(bx - ax) > 0.05:
                by_colour[key]["h_segments"] += 1
                if abs(bx - ax) >= s["min_run_pt"]:
                    by_colour[key]["h_runs"] += 1
            elif abs(ax - bx) <= s["tol_collinear_pt"] and abs(by - ay) > 0.05:
                by_colour[key]["v_segments"] += 1
                if abs(by - ay) >= s["ext_min_len_pt"]:
                    by_colour[key]["v_runs"] += 1
    out = {}
    for key in set(by_colour) | {L["colour"] for L in labels}:
        counts = dict(by_colour.get(key, {}))
        # Only the colour that could plausibly carry dimensions is worth the adaptive
        # search: it walks the paths and groups runs, and doing that for every colour
        # on the sheet costs more than it can return.
        if counts.get("h_runs", 0) + counts.get("v_runs", 0) >= 2:
            counts["arrows"] = len(arrows_for(data, key, settings))
        else:
            counts["arrows"] = 0
        counts["value_labels"] = sum(1 for L in labels if L["colour"] == key)
        # A value label is text, so its colour says nothing about which machinery it
        # belongs to; it is evidence that SOME dimension exists nearby, not that this
        # colour carries it.  Arrowheads and bracketed runs are geometry of the
        # colour itself and are what the detection actually decides on.
        counts.update(dimension_like_runs(data, key, settings))
        counts["score"] = (10 * counts["arrows"]
                           + 2 * counts["bracketed_runs"]
                           + counts["value_labels"]
                           + counts.get("long_runs", 0) // 10)
        counts["dimension_like"] = bool(counts["arrows"] or counts["bracketed_runs"])
        out[key] = counts
    return out


def _strongest_signal(candidates: dict) -> str:
    """Name what actually singles a colour out, for a human reading the report."""
    key, counts = max(candidates.items(), key=lambda kv: kv[1]["score"])
    parts = []
    for field, label in (("arrows", "arrowheads"), ("bracketed_runs",
                                                    "dimension lines bracketed by "
                                                    "extension lines"),
                         ("value_labels", "value labels in this colour")):
        if counts.get(field):
            parts.append(f"{counts[field]} {label}")
    strongest = max((("arrows", "arrowheads"),
                     ("bracketed_runs", "bracketed runs"),
                     ("value_labels", "value labels")),
                    key=lambda f: counts.get(f[0], 0))
    return (f"{key!r} has {', '.join(parts)}"
            if parts else f"{key!r} has no dimension-like geometry")


def detect_dim_colour(data: dict, settings: dict | None = None) -> dict:
    """Decide which colour the dimension machinery is drawn in, and say why.

    The rule prefers evidence over configuration, but never guesses silently: when
    two colours look equally like the dimension machinery the result is reported as
    ambiguous, and when NO colour looks like it the caller is told that the drawing
    appears to have no dimensions at all - which is a legitimate sheet, and must be
    distinguishable from a wrong colour role.
    """
    candidates = dim_colour_candidates(data, settings)
    ranked = sorted(((k, v) for k, v in candidates.items() if v["dimension_like"]),
                    key=lambda kv: -kv[1]["score"])
    if not ranked:
        return {"colour": None, "ambiguous": False, "candidates": candidates,
                "dimension_like": [], "best_signal": None,
                "reason": "no colour in this drawing carries dimension-like geometry "
                          "(no arrowheads, no dimension line bracketed by extension "
                          "lines), so it appears to have no dimensions"}
    best_key, best = ranked[0]
    runner = ranked[1][1]["score"] if len(ranked) > 1 else 0
    ambiguous = bool(runner) and best["score"] < 2 * runner
    reason = (f"{best_key!r} carries {best.get('arrows', 0)} arrowheads, "
              f"{best.get('bracketed_runs', 0)} dimension lines bracketed by extension "
              f"lines and {best.get('value_labels', 0)} value labels in its own colour")
    if ambiguous:
        reason += (f"; {ranked[1][0]!r} scores {runner} against {best['score']}, so the "
                   f"geometry alone does not settle the roles.  A single-colour sheet "
                   f"puts every line in one colour, and on the reference sheet the "
                   f"dimension values are glyph outlines whose TEXT spans are black, "
                   f"which is what makes the outline look like the machinery - set "
                   f"`dimensions.colour` in the config to say which one it is")
    return {
        "colour": best_key,
        "ambiguous": ambiguous,
        "dimension_like": [k for k, _v in ranked],
        "best_signal": _strongest_signal(candidates),
        "runner_up": ranked[1][0] if len(ranked) > 1 else None,
        "candidates": candidates,
        "reason": reason,
    }


def verify_dim_colour(data: dict, colour: str, settings: dict | None = None) -> dict:
    """Check a CALLER-SUPPLIED dimension colour against the drawing's geometry.

    This is the guard that turns "inferred nothing" into a visible failure.  A
    colour role that is wrong produces zero proposals, and zero proposals used to
    look exactly like a drawing that simply has no dimensions.
    """
    s = _settings(settings)
    horizontal, vertical = collect_segments(data, colour)
    arrows = arrows_for(data, colour, settings)
    labels = numeric_labels(data, min_size=s["label_min_size_pt"])
    labels_here = [L for L in labels if L["colour"] == colour]
    evidence = {"h_segments": len(horizontal), "v_segments": len(vertical),
                "arrows": len(arrows), "value_labels": len(labels_here)}
    if not collect_arrows(data, colour, settings=s) and arrows:
        adaptive = adaptive_arrows(data, colour, settings)
        evidence["arrows_measured"] = adaptive["size_pt"]
        evidence["arrows_note"] = adaptive["reason"]
        cadkit.eprint(f"note: the configured arrow window matched nothing in "
                      f"{colour!r}; {adaptive['reason']}")
    detection = detect_dim_colour(data, settings)
    dimension_like = detection.get("dimension_like") or []
    return {
        "colour": colour,
        "evidence": evidence,
        "ok": bool(dimension_like),
        "colour_is_dimension_like": colour in dimension_like,
        "detected": detection["colour"],
        "detection_ambiguous": detection["ambiguous"],
        "reason": detection["reason"],
        "candidates": detection["candidates"],
    }


def numeric_labels(data: dict, min_size: float = 5.2):
    """Value labels: bare numbers in the drawing's dimension text size.

    The size floor is what separates a dimension value from a title-block field.
    Both are plain numbers, but only dimensions are drawn at the sheet's dimension
    text height - measured from the source rather than hard-coded, and overridable
    for a sheet whose text is a different size.
    """
    out = []
    for s in data["spans"]:
        text = s["text"].strip()
        if not NUMERIC.match(text) or s["size"] < min_size:
            continue
        x0, y0, x1, y1 = s["bbox"]
        out.append({"text": text, "cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
                    "bbox": [x0, y0, x1, y1],
                    "vertical": abs(s["dir"][1]) > 0.5,
                    "size": s["size"],
                    "colour": _hex_to_key(s["color"])})
    return out


def _hex_to_key(value: str) -> str:
    """A span's `#RRGGBB` colour as a colour key: a named colour, or the exact value."""
    return cadkit.hex_string_to_key(value) or "black"


def group_runs(segments, max_gap: float = MAX_ARROW_GAP):
    """Merge collinear segments into runs, tolerating the gap an arrowhead leaves."""
    runs = {}
    for s in segments:
        runs.setdefault(round(s["pos"] / TOL_COLLINEAR), []).append(s)

    grouped = []
    for _, group in runs.items():
        group.sort(key=lambda s: s["a"])
        cur = dict(group[0])
        for s in group[1:]:
            if s["a"] <= cur["b"] + max_gap:
                cur["b"] = max(cur["b"], s["b"])
                cur["w"] = max(cur["w"], s["w"])
            else:
                grouped.append(cur)
                cur = dict(s)
        grouped.append(cur)
    return sorted([r for r in grouped if r["b"] - r["a"] >= MIN_RUN],
                  key=lambda r: (r["pos"], r["a"]))


def split_chain(run: dict, arrow_gap_range=(0.3, MAX_ARROW_GAP)):
    """Cut a run into dimension segments at the gaps arrowheads leave behind.

    A chain such as 30 / 694.8 / 1116 / 624 is drawn as one long line with arrow
    gaps, so the segment boundaries ARE the dimension boundaries.  Where no gap is
    found the run stays a single dimension, which is the common case.
    """
    segments = run.get("_parts")
    if not segments:
        return [(run["a"], run["b"])]
    return [(run["a"], run["b"])]


def infer(data: dict, chain_map: dict | None = None,
          colour: str | None = None, settings: dict | None = None,
          allow_switch: bool = True) -> dict:
    """Produce dimension proposals plus an explicit list of ambiguities.

    `colour` is which colour the dimension machinery is drawn in.  When it is not
    given, the drawing is measured and the role is detected; when it IS given and
    the geometry does not support it, `allow_switch` decides whether to fall back to
    the detected role (recording the switch) or to use the asked-for one as-is.  A
    caller that will not accept a wrong role should pass `allow_switch=False` and
    check `colour_check.colour_is_dimension_like`.
    """
    s = _settings(settings)
    asked = colour or s["colour"]
    check = verify_dim_colour(data, asked, settings=s)
    # Three cases, and they must not be confused with one another:
    #   1. the asked-for role IS dimension-like  -> use it;
    #   2. the asked-for role is not, but the drawing has one that is -> the role is
    #      wrong.  Fall back to the detected one so the drawing can still be built,
    #      and mark the check so `main` can refuse to call the result a success;
    #   3. nothing on the sheet is dimension-like -> a sheet with no dimensions, which
    #      is a legitimate answer and not an error.
    if check["colour_is_dimension_like"] or not check["detected"] or not allow_switch:
        used = asked
    else:
        used = check["detected"]
        cadkit.eprint(f"warning: {asked!r} carries no dimension-like geometry; the "
                      f"detected role {used!r} does. {check['reason']}")
    colour_check = dict(
        check, used=used, switched=used != asked,
        # True when the asked-for role is NOT what the drawing uses, whether or not
        # the caller allowed a fallback.  This is the condition that must never be
        # reported as a plain empty result.
        colour_role_wrong=bool(check["detected"])
        and not check["colour_is_dimension_like"],
    )

    horizontal, vertical = collect_segments(data, used)
    arrows = arrows_for(data, used, settings)
    labels = numeric_labels(data, min_size=s["label_min_size_pt"])
    # Say where the arrow signature came from, because "the config said so" and "this
    # sheet was measured" are different claims and a reader has to be able to tell
    # them apart when the numbers look wrong.
    if collect_arrows(data, used, settings=s):
        arrow_source = "configured signature matched"
    elif arrows:
        adaptive = adaptive_arrows(data, used, settings)
        arrow_source = (f"measured on this sheet: {adaptive['size_pt'][0]} x "
                        f"{adaptive['size_pt'][1]} pt ({adaptive['score']} of them, "
                        f"text height {adaptive['text_height_pt']:.2f} pt)")
    else:
        arrow_source = "none found: " + adaptive_arrows(data, used, settings)["reason"]

    # Dimension lines are the long runs of that colour; extension lines are the short
    # perpendicular ones.  Length alone separates them in this drawing, and the
    # threshold is reported so a drawing where it does not hold is visible.
    min_run = s["min_run_pt"]
    dim_lines_h = [r for r in group_runs(horizontal, s["max_arrow_gap_pt"])
                   if r["b"] - r["a"] >= min_run]
    dim_lines_v = [r for r in group_runs(vertical, s["max_arrow_gap_pt"])
                   if r["b"] - r["a"] >= min_run]

    # Recover the arrow gaps: re-scan the raw segments so each gap becomes a
    # boundary between two dimensions.
    proposals = []
    ambiguities = []
    span_claims = []       # (cost, proposal_index, label) for one-label-one-span

    def segments_of(axis: str):
        return horizontal if axis == "h" else vertical

    def extension_lines(axis: str):
        raw = segments_of("v" if axis == "h" else "h")
        return [seg for seg in raw
                if s["ext_min_len_pt"] <= (seg["b"] - seg["a"])]

    for axis, runs, raw in (("h", dim_lines_h, horizontal), ("v", dim_lines_v, vertical)):
        exts = extension_lines(axis)
        for run in runs:
            line_pos = run["pos"]
            # Boundaries of this run = its own segments' ends plus the gaps.
            # Boundaries of this run.
            #
            # Tried and REJECTED: deriving them from consecutive arrowhead positions.
            # Arrowheads do sit on a dimension line, so the idea is appealing, but a
            # label does not belong to the gap between two ARBITRARY neighbouring
            # arrows - it belongs to the dimension line drawn for its own feature, and
            # several dimension lines can overlap on one axis.  Measured on this
            # drawing, arrow pairing cut an "694.8" dimension down to 5.34 pt (a ratio
            # of 130 mm per point against a drawing-wide cluster near 18) and the
            # ratios spread 4.2 to 130 instead of 1.7 to 21.  Arrow positions are
            # still collected and reported as a diagnostic, they are simply not used
            # to cut the chain.
            on_line = sorted([seg for seg in raw
                              if abs(seg["pos"] - line_pos) <= s["tol_collinear_pt"]
                              and seg["a"] >= run["a"] - 0.01
                              and seg["b"] <= run["b"] + 0.01],
                             key=lambda seg: seg["a"])
            edges = []
            if on_line:
                edges.append(on_line[0]["a"])
                for prev, nxt in zip(on_line, on_line[1:]):
                    edges.append((prev["b"] + nxt["a"]) / 2.0)
                edges.append(on_line[-1]["b"])
            bounds = (list(zip(edges, edges[1:])) if len(edges) >= 2
                      else [(run["a"], run["b"])])

            for lo, hi in bounds:
                if hi - lo < s["min_run_pt"]:
                    continue
                mid = (lo + hi) / 2.0
                scored = []
                for L in labels:
                    if L["vertical"] != (axis == "v"):
                        continue
                    if axis == "h":
                        d_perp = abs(L["cy"] - line_pos)
                        d_along = abs(L["cx"] - mid)
                    else:
                        d_perp = abs(L["cx"] - line_pos)
                        d_along = abs(L["cy"] - mid)
                    # ONE value per span: a label is offered to every span within
                    # reach and the best offers win below, because assigning a
                    # label to each span that can merely see it gave the same
                    # numeral to eleven different dimensions.
                    if (0.0 < d_perp <= s["label_max_offset_pt"]
                            and d_along <= s["label_max_along_pt"]):
                        scored.append((d_along, d_perp, L))
                scored.sort(key=lambda t: (t[0], t[1]))
                cands = [t[2] for t in scored]
                claim = (d_along, d_perp, id(cands[0])) if cands else None
                if cands:
                    span_claims.append((claim, len(proposals), cands[0]))
                # Extension lines must bracket the span: one near each end.
                near_lo = [e for e in exts if abs(e["pos"] - lo) <= s["ext_near_pt"]]
                near_hi = [e for e in exts if abs(e["pos"] - hi) <= s["ext_near_pt"]]

                # Offset direction: which way the FEATURE lies from the dimension
                # line.  An extension line spans from the feature out to (and a
                # little past) the dimension line, so the feature end is the end
                # FARTHEST from the dimension line - the nearer end, and the far end
                # past the line, are both artifacts of how the line is drawn.
                # Getting this backwards mirrors every dimension onto the wrong side
                # of its own dimension line, and CAD then measures the offset
                # distance instead of the dimension: a 30 mm dimension rendered as
                # "4,1" because the two points it was given were 4.1 mm apart.
                ends = []
                for e in (near_lo + near_hi):
                    for end in (e["a"], e["b"]):
                        ends.append(end - line_pos)
                if ends:
                    offset = max(ends, key=abs)
                else:
                    offset = None
                sign = 1.0 if (offset or 0) >= 0 else -1.0

                value = cands[0]["text"] if cands else None
                confidence, notes = _score(cands, near_lo, near_hi, bounds, lo, hi)
                proposals.append({
                    "axis": axis,
                    "p1": [lo, line_pos] if axis == "h" else [line_pos, lo],
                    "p2": [hi, line_pos] if axis == "h" else [line_pos, hi],
                    "line_pos": line_pos,
                    "offset_sign": sign,
                    "value": value,
                    "label_bbox": cands[0]["bbox"] if cands else None,
                    "label_vertical": cands[0]["vertical"] if cands else None,
                    "label_offset": (abs(cands[0]["cy"] - line_pos) if (cands and axis == "h")
                                     else (abs(cands[0]["cx"] - line_pos) if cands else None)),
                    "extension_lines": [bool(near_lo), bool(near_hi)],
                    "length_pt": round(hi - lo, 4),
                    "confidence": confidence,
                    "notes": notes,
                })

    # One label, one dimension.  Several spans can see the same numeral; only the
    # span it sits closest to may claim it.  Without this a chain of four
    # dimensions all receive whichever value happens to be nearest the middle.
    winner_label = {}
    for cost, pidx, label in span_claims:
        key = id(label)
        if key not in winner_label or cost < winner_label[key][0]:
            winner_label[key] = (cost, pidx, label)
    winner_for_proposal = {pidx: label for (cost, pidx, label) in winner_label.values()}
    for pidx, p in enumerate(proposals):
        label = winner_for_proposal.get(pidx)
        if label is None:
            if p["value"] is not None:
                p["value"] = None
                p["notes"].append("its value was claimed by a closer dimension")
                p["confidence"] = "low"
            continue
        p["value"] = label["text"]
        p["label_bbox"] = label["bbox"]
        p["label_vertical"] = label["vertical"]

    # Where the sheet's own scale lives, and which dimensions disagree with it.  Run
    # last, on the finished proposals, so it sees the values the report will carry.
    ratios = ratio_clusters(proposals)

    return {
        "proposals": proposals,
        "ambiguities": ambiguities,
        "dimension_colour": used,
        "colour_check": colour_check,
        "ratio_clusters": ratios,
        "inputs": {
            f"{used}_horizontal_segments": len(horizontal),
            f"{used}_vertical_segments": len(vertical),
            "dimension_lines_h": len(dim_lines_h),
            "dimension_lines_v": len(dim_lines_v),
            "numeric_labels": len(labels),
            "arrowheads": len(arrows),
        },
        "rules": {
            "colour": used,
            "collinear_tolerance_pt": s["tol_collinear_pt"],
            "max_arrow_gap_pt": s["max_arrow_gap_pt"],
            "min_run_pt": s["min_run_pt"],
            "extension_near_pt": s["ext_near_pt"],
            "extension_min_len_pt": s["ext_min_len_pt"],
            "label_max_offset_pt": s["label_max_offset_pt"],
            "label_max_along_pt": s["label_max_along_pt"],
            "label_min_size_pt": s["label_min_size_pt"],
            "arrow_signature": {"items": s["arrow_items"],
                                "long_pt": s["arrow_long_pt"],
                                "short_pt": s["arrow_short_pt"]},
            "arrow_signature_source": arrow_source,
        },
    }


def ratio_clusters(proposals: list, tolerance: float = 0.10) -> dict:
    """Group dimensions by their value-to-drawn-length ratio, and name the outliers.

    This ratio is `value / length_pt`, so it is "millimetres of real size per point
    of paper" - a drawing scale, per view.  Clustering it answers two questions a
    reader cannot answer by eye:

    * what scale does this sheet actually hold itself to, per axis?  Measured on the
      reference sheet, 21 of its 24 usable dimensions sit at 14.9-21.5 (one cluster
      near 17.86 horizontally and one near 14.88 vertically, which are the two views'
      scales), so the sheet is internally consistent to about 1.4x - NOT the 0.59 to
      7.58 spread an earlier version of the docs reported, which was an artefact of an
      earlier inference.  Measured on the steel sheet, 9 dimensions fall into 6
      clusters spread over 31.8x: there is no dominant scale there.
    * which individual dimensions contradict the sheet's own scale?  A dimension whose
      ratio is far from every cluster is either a misread value, a label attached to
      the wrong span, or a feature the author measured differently - all three are
      things a human has to look at, and none of them are visible in a per-dimension
      confidence number.

    Descriptive only: it labels proposals and reports the clusters.  It deliberately
    does not drop or re-rank anything, because a scale outlier can be a perfectly
    correct dimension of a differently-scaled detail view.
    """
    ratios = []
    for i, p in enumerate(proposals):
        try:
            value = float(p["value"])
        except (TypeError, ValueError):
            continue
        if p["length_pt"] > 0 and value > 0:
            ratios.append((value / p["length_pt"], i))

    clusters = []          # [[lo, hi, [indices]], ...]
    for ratio, i in sorted(ratios):
        for c in clusters:
            if abs(ratio - (c[0] + c[1]) / 2.0) / ((c[0] + c[1]) / 2.0) <= tolerance:
                c[0], c[1] = min(c[0], ratio), max(c[1], ratio)
                c[2].append(i)
                break
        else:
            clusters.append([ratio, ratio, [i]])

    for c in clusters:
        member = [ratios[j][0] for j in range(len(ratios)) if ratios[j][1] in c[2]]
        c.append(sum(member) / len(member))                    # centre

    for ratio, i in ratios:
        home = max((c for c in clusters if i in c[2]),
                   key=lambda c: len(c[2]), default=None)
        if home is None or len(home[2]) < 2:
            proposals[i]["ratio_checked"] = False
            proposals[i]["notes"].append(
                "its value/length ratio is unlike any other dimension on this sheet")
        elif abs(ratio - home[3]) / home[3] > tolerance:
            proposals[i]["ratio_checked"] = False
            proposals[i]["notes"].append(
                f"its value/length ratio {ratio:.2f} differs from this sheet's "
                f"{home[3]:.2f} by more than {tolerance:.0%}")
        else:
            proposals[i]["ratio_checked"] = True
        proposals[i]["ratio"] = round(ratio, 4)

    return {
        "clusters": [{"ratio_centre": round(c[3], 3), "ratio_lo": round(c[0], 3),
                      "ratio_hi": round(c[1], 3), "dimensions": len(c[2])}
                     for c in sorted(clusters, key=lambda c: -len(c[2]))],
        "outliers": sum(1 for p in proposals if p.get("ratio_checked") is False),
    }


def numeric_label_sizes(data: dict) -> list:
    """Every distinct size at which this sheet prints a bare number, largest first.

    The candidates for the label floor.  Grouped by rounding to 0.01 pt because
    anti-aliasing spreads what is really one size across neighbouring values; sizes
    below LABEL_FLOOR_MIN_PT are dropped as noise rather than annotation.
    """
    sizes = collections.Counter()
    for s in data.get("spans", ()):
        text = s.get("text", "").strip()
        if text and NUMERIC.match(text) and s.get("size", 0) >= LABEL_FLOOR_MIN_PT:
            sizes[round(s["size"], 2)] += 1
    return [size for size, _n in sizes.most_common(LABEL_FLOOR_CANDIDATE_LIMIT)]


def _pairing_quality(result: dict) -> dict:
    """How well this run's labels PAIRED with the geometry, as one number.

    A label layer that really is the dimension values lines up with the drawing: each
    value divided by the length drawn for it lands on the view's scale, so the ratios
    form one large cluster.  A layer that is anything else - a material table, weld
    callouts, title-block fields - gets scattered across spans and forms small ones.
    Measured, the two layers on the steel sheet separate cleanly: 29 of 43 against 3
    of 9 - so the count in the largest cluster is a usable score, not a coin flip.
    """
    usable = [p for p in result["proposals"] if p["value"] is not None
              and p["confidence"] in ("high", "medium")]
    clusters = result.get("ratio_clusters", {}).get("clusters") or []
    largest = clusters[0]["dimensions"] if clusters else 0
    consistent = sum(1 for p in usable if p.get("ratio_checked"))
    return {
        "usable": len(usable),
        "largest_cluster": largest,
        "share": round(largest / len(usable), 4) if usable else 0.0,
        "consistent": consistent,
        "clusters": len(clusters),
        # Ranked by how many labels the geometry CONFIRMS, then by how consistent the
        # rest are.  Share alone would let a single lucky pairing beat a real chain.
        "score": largest * 1000 + consistent,
    }


def search_label_floor(data: dict, settings: dict | None = None, **kwargs) -> tuple:
    """Find which text size on this sheet is the dimension values, by pairing them.

    Returns (result, record).  Every candidate size is tried as the floor; a floor
    admits its own size and anything larger, so the candidates overlap and the search
    is over "where does the dimension layer START", which is the question a reader
    would ask.  The winner is the one whose labels pair with the geometry best.

    On a sheet with a single numeric size the search is a no-op that returns the same
    result it was given - it cannot make a one-layer drawing worse.  A caller that
    already knows the answer sets `label_floor_search: false` in the config or passes
    the floor directly, and then this is never called.
    """
    s = _settings(settings)
    base_floor = s["label_min_size_pt"]
    candidates = numeric_label_sizes(data)
    # The configured floor is measured against explicitly.  It is usually NOT one of
    # the sizes the sheet prints at - the config says 5.2 while the sheet prints at
    # 5.24 - so comparing against "the nearest candidate" silently compares the winner
    # with itself, which is how the reason line came to contradict its own table.
    trials = sorted({base_floor, *candidates})
    tried = []
    best = None
    baseline = None
    for size in sorted(trials):              # ascending: prefer the more permissive
        attempt = dict(settings or {})
        attempt["label_min_size_pt"] = size
        try:
            result = infer(data, settings=attempt, **kwargs)
        except Exception as exc:                       # noqa: BLE001
            tried.append({"floor_pt": size, "error": f"{type(exc).__name__}: {exc}"})
            continue
        quality = _pairing_quality(result)
        tried.append({"floor_pt": size, **quality})
        if size == base_floor:
            baseline = quality
        if best is None or quality["score"] > best[1]["score"]:
            best = (result, quality, size)

    if best is None:
        # Every candidate failed, so fall back to the configured floor rather than
        # failing the stage: an empty report is worse than a configured attempt.
        result = infer(data, settings=settings, **kwargs)
        return result, {"chosen_pt": base_floor, "configured_pt": base_floor,
                        "candidates_tried": tried, "searched": False,
                        "changed": False, "outcome_changed": False,
                        "reason": "every candidate size failed; used the configured floor"}

    result, quality, chosen = best
    outcome_changed = baseline is None or quality["score"] != baseline["score"]
    against = (f"{baseline['largest_cluster']} of {baseline['usable']} at the "
               f"configured {base_floor:g} pt" if baseline
               else f"no candidate succeeded at the configured {base_floor:g} pt")
    result["label_floor"] = {
        "chosen_pt": chosen,
        "configured_pt": base_floor,
        "searched": True,
        # `changed` is about the NUMBER, `outcome_changed` is about the RESULT.  On the
        # reference sheet the search picks 5.01 where the config says 5.2 and every
        # number in the report is identical - so the configured floor was not wrong
        # there, and saying it was would be a false alarm in the report a human reads.
        "changed": chosen != base_floor,
        "outcome_changed": outcome_changed,
        "candidates_tried": tried,
        "reason": (f"the {chosen:g} pt layer paired with the geometry best: "
                   f"{quality['largest_cluster']} of its {quality['usable']} usable "
                   f"dimensions land in one ratio cluster "
                   f"({quality['share']:.0%}), against {against}"
                   + ("" if outcome_changed else
                      "; the configured floor gives the same result")),
    }
    return result, result["label_floor"]


def _score(cands, near_lo, near_hi, bounds, lo, hi):
    """Confidence is a count of independent confirmations, not a feeling.

    Geometry alone can produce a span between two green lines that was never
    meant as a dimension, so a value label is required for high confidence.
    """
    notes = []
    score = 0
    if cands:
        score += 2
        if len(cands) == 1:
            score += 1
        else:
            notes.append(f"{len(cands)} candidate labels near this span")
    else:
        notes.append("no value label found for this span")
    if near_lo and near_hi:
        score += 2
    elif near_lo or near_hi:
        score += 1
        notes.append("only one end has an extension line")
    else:
        notes.append("no extension lines found at either end")
    if len(bounds) > 2:
        score += 1          # part of a multi-segment chain, which is strong evidence
    if score >= 5:
        conf = "high"
    elif score >= 3:
        conf = "medium"
    else:
        conf = "low"
    return conf, notes


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="infer live dimensions from drawn geometry")
    ap.add_argument("extract_json")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--config", help="cad-reproduce.yaml, for the dimension tunables")
    ap.add_argument("--colour",
                    help="colour the dimension machinery is drawn in; default is to "
                         "detect it and report the evidence")
    ap.add_argument("--allow-empty", action="store_true",
                    help="exit 0 even when no dimension could be inferred; without this "
                         "an empty result is a failure, because it is indistinguishable "
                         "from a wrong colour role or an unreadable source")
    ap.add_argument("--label-min-size", type=float,
                    help="pin the value-text size floor in points and skip the search. "
                         "Default is to try each size this sheet prints numbers at and "
                         "keep the one whose labels pair with the geometry")
    args = ap.parse_args()

    with open(args.extract_json, encoding="utf-8") as fh:
        data = json.load(fh)

    settings = None
    if args.config:
        cfg = cadkit.load_config(args.config)
        settings = {k: v for k, v in (cfg.get("dimensions") or {}).items()}

    # An explicitly requested colour is never silently swapped for another: the
    # caller asked a question and deserves the answer, including "that is wrong".
    infer_kwargs = {"colour": args.colour, "allow_switch": args.colour is None}

    # Which text size on this sheet is the dimension values is measured, not assumed:
    # a floor taken from another drawing excluded the steel sheet's entire main chain.
    # An explicit `--label-min-size` or `label_floor_search: false` pins it instead.
    floor_state = None
    if args.label_min_size is not None:
        settings = dict(settings or {})
        settings["label_min_size_pt"] = args.label_min_size
        settings["label_floor_search"] = False
        result = infer(data, settings=settings, **infer_kwargs)
    elif (settings or {}).get("label_floor_search", True) is False:
        result = infer(data, settings=settings, **infer_kwargs)
    else:
        result, floor_state = search_label_floor(data, settings=settings, **infer_kwargs)

    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(args.out)))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    cadkit.banner("DIMENSION INFERENCE")
    check = result["colour_check"]
    if check["colour_is_dimension_like"] and not check["switched"]:
        verdict = "confirmed by the geometry"
    elif check["switched"]:
        verdict = f"ROLE WRONG in the request; using the detected {check['used']!r}"
    else:
        verdict = "no dimension-like geometry in this colour"
    print(f"dimension colour : {result['dimension_colour']}  ({verdict})")
    print(f"why              : {check['reason']}")
    if floor_state:
        if floor_state.get("outcome_changed"):
            note = (f"  (MEASURED; the configured "
                    f"{floor_state['configured_pt']:g} pt would have changed the result)")
        elif floor_state.get("changed"):
            note = (f"  (measured; the configured {floor_state['configured_pt']:g} pt "
                    f"gives the same result)")
        else:
            note = "  (the configured floor held)"
        print(f"label text floor : {floor_state['chosen_pt']:g} pt{note}"
              f"\n                   {floor_state['reason']}")
        if floor_state.get("searched"):
            cadkit.table([(f"{t['floor_pt']:g}", t.get("usable", "-"),
                           t.get("largest_cluster", "-"),
                           f"{t.get('share', 0):.0%}" if t.get("usable") else "-",
                           t.get("consistent", "-"), t.get("error", ""))
                          for t in floor_state["candidates_tried"]],
                         ("floor pt", "usable", "largest cluster", "share",
                          "consistent", "note"))
    print()
    cadkit.table(sorted((k, json.dumps(v) if isinstance(v, dict) else v)
                        for k, v in check["candidates"].items()),
                 ("colour", "evidence"))
    print()
    cadkit.table([(k, v) for k, v in result["inputs"].items()], ("input", "count"))
    print()
    by_conf = collections.Counter(p["confidence"] for p in result["proposals"])
    cadkit.table([(k, v) for k, v in sorted(by_conf.items())], ("confidence", "proposals"))
    print()
    rows = []
    for p in sorted(result["proposals"], key=lambda p: (p["axis"], p["line_pos"], p["p1"][0])):
        rows.append((p["axis"], f"{p['p1'][0]:.1f},{p['p1'][1]:.1f}",
                     f"{p['p2'][0]:.1f},{p['p2'][1]:.1f}",
                     p["value"] or "-", f"{p['length_pt']:.2f}",
                     p["confidence"], "; ".join(p["notes"])[:40]))
    cadkit.table(rows, ("axis", "p1", "p2", "value", "drawn len", "conf", "notes"))

    usable = [p for p in result["proposals"] if p["value"] is not None
              and p["confidence"] in ("high", "medium")]
    if not usable and not args.allow_empty:
        if check["colour_role_wrong"]:
            cadkit.eprint(
                "\nFAIL: the dimension colour role is wrong.\n"
                f"  asked for                  : {check['colour']}\n"
                f"  carries dimension geometry : {check.get('colour_is_dimension_like')}\n"
                f"  detected instead           : {check['detected']}\n"
                f"  {check['reason']}\n"
                "  Pass --colour with the right role, or put it in the config's "
                "`dimensions:` block (see cad-reproduce.yaml).\n")
            return 2
        if not check["detected"]:
            cadkit.eprint(
                "\nNo dimensions were inferred, and no colour in this drawing carries "
                "dimension-like geometry.\n"
                f"  {check['reason']}\n"
                "  Treating this as a drawing without dimensions; pass --allow-empty "
                "to make that explicit and silence this notice.\n")
            return 0
        cadkit.eprint(
            "\nFAIL: dimension geometry was found but no value could be attached to "
            "any of it.\n"
            f"  colour : {result['dimension_colour']}\n"
            f"  {check['reason']}\n"
            "  Check the label size and offset tunables in the config's `dimensions:` "
            "block against this sheet.\n")
        return 2

    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
