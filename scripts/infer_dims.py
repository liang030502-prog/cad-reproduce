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


def collect_arrows(data: dict, colour: str = "green"):
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
    """
    arrows = []
    for p in data["paths"]:
        if p["fill"] != colour or len(p["items"]) != 6:
            continue
        x0, y0, x1, y1 = p["rect"]
        w, h = x1 - x0, y1 - y0
        if not ((4.5 <= w <= 6.5 and 0.8 <= h <= 2.0)
                or (4.5 <= h <= 6.5 and 0.8 <= w <= 2.0)):
            continue
        arrows.append({"cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
                       "w": w, "h": h, "rect": [x0, y0, x1, y1]})
    return arrows


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


def numeric_labels(data: dict, min_size: float = 5.2):
    """Value labels: bare numbers in the drawing's dimension text size.

    The size floor is what separates a dimension value from a title-block field.
    Both are plain numbers, but only dimensions are drawn at the sheet's dimension
    text height, which is measured from the source rather than hard-coded.
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
    r = int(value[1:3], 16) / 255.0
    g = int(value[3:5], 16) / 255.0
    b = int(value[5:7], 16) / 255.0
    return cadkit.rgb_to_key((r, g, b)) or "black"


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


def infer(data: dict, chain_map: dict | None = None) -> dict:
    """Produce dimension proposals plus an explicit list of ambiguities."""
    horizontal, vertical = collect_segments(data, "green")
    arrows = collect_arrows(data, "green")
    labels = numeric_labels(data)

    # Dimension lines are the long green runs; extension lines are the short
    # perpendicular ones.  Length alone separates them in this drawing, and the
    # threshold is reported so a drawing where it does not hold is visible.
    dim_lines_h = [r for r in group_runs(horizontal) if r["b"] - r["a"] >= 8.0]
    dim_lines_v = [r for r in group_runs(vertical) if r["b"] - r["a"] >= 8.0]

    # Recover the arrow gaps: re-scan the raw segments so each gap becomes a
    # boundary between two dimensions.
    proposals = []
    ambiguities = []
    span_claims = []       # (cost, proposal_index, label) for one-label-one-span

    def segments_of(axis: str):
        return horizontal if axis == "h" else vertical

    def extension_lines(axis: str):
        raw = segments_of("v" if axis == "h" else "h")
        return [s for s in raw if EXT_MIN_LEN <= (s["b"] - s["a"])]

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
            on_line = sorted([s for s in raw if abs(s["pos"] - line_pos) <= TOL_COLLINEAR
                              and s["a"] >= run["a"] - 0.01 and s["b"] <= run["b"] + 0.01],
                             key=lambda s: s["a"])
            edges = []
            if on_line:
                edges.append(on_line[0]["a"])
                for prev, nxt in zip(on_line, on_line[1:]):
                    edges.append((prev["b"] + nxt["a"]) / 2.0)
                edges.append(on_line[-1]["b"])
            bounds = (list(zip(edges, edges[1:])) if len(edges) >= 2
                      else [(run["a"], run["b"])])

            for lo, hi in bounds:
                if hi - lo < MIN_RUN:
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
                    if 0.0 < d_perp <= LABEL_MAX_OFFSET and d_along <= LABEL_MAX_ALONG:
                        scored.append((d_along, d_perp, L))
                scored.sort(key=lambda t: (t[0], t[1]))
                cands = [t[2] for t in scored]
                claim = (d_along, d_perp, id(cands[0])) if cands else None
                if cands:
                    span_claims.append((claim, len(proposals), cands[0]))
                # Extension lines must bracket the span: one near each end.
                near_lo = [e for e in exts if abs(e["pos"] - lo) <= EXT_NEAR]
                near_hi = [e for e in exts if abs(e["pos"] - hi) <= EXT_NEAR]

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

    return {
        "proposals": proposals,
        "ambiguities": ambiguities,
        "inputs": {
            "green_horizontal_segments": len(horizontal),
            "green_vertical_segments": len(vertical),
            "dimension_lines_h": len(dim_lines_h),
            "dimension_lines_v": len(dim_lines_v),
            "numeric_labels": len(labels),
            "arrowheads": len(arrows),
        },
        "rules": {
            "collinear_tolerance_pt": TOL_COLLINEAR,
            "max_arrow_gap_pt": MAX_ARROW_GAP,
            "min_run_pt": MIN_RUN,
            "extension_near_pt": EXT_NEAR,
            "label_max_offset_pt": LABEL_MAX_OFFSET,
        },
    }


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
    args = ap.parse_args()

    with open(args.extract_json, encoding="utf-8") as fh:
        data = json.load(fh)
    result = infer(data)

    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(args.out)))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    cadkit.banner("DIMENSION INFERENCE")
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
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
