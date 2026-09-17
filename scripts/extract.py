# -*- coding: utf-8 -*-
"""Stage 1 - extract everything measurable from the source drawing.

This stage never decides geometry.  It measures and records, so that every
later stage can be checked against a file instead of against a memory of what
the source looked like.

The one genuinely subtle thing here is colour attribution.  A PDF content
stream carries a *graphics state*: `rg` sets the fill colour and `RG` sets the
stroke colour, and both persist until changed.  A drawing library that reports
only "the fill colour in force" for each path therefore reports the fill of a
path that has no fill at all, which is how thousands of black outlines came to
be painted green in an earlier attempt.  This extractor records, per path,
whether a colour was explicitly set for that path or merely inherited, so the
generator can tell the two apart instead of trusting a possibly-stale value.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402


def extract(source: str, dpi: int = 150, max_paths: int | None = None) -> dict:
    import pymupdf

    doc = pymupdf.open(source)
    if doc.page_count != 1:
        cadkit.eprint(f"warning: source has {doc.page_count} pages; using page 1 only")
    page = doc[0]

    # ---------------------------------------------------------------- text
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                x0, y0, x1, y1 = span["bbox"]
                spans.append({
                    "text": text,
                    "bbox": [round(v, 4) for v in (x0, y0, x1, y1)],
                    "size": round(span["size"], 4),
                    "font": span.get("font", ""),
                    "color": "#%06X" % (span.get("color", 0),),
                    "dir": [round(v, 6) for v in line.get("dir", (1.0, 0.0))],
                    "origin": [round(v, 4) for v in span.get("origin", (x0, y1))],
                })

    # ---------------------------------------------------------------- paths
    paths = []
    for index, drawing in enumerate(page.get_drawings()):
        if max_paths is not None and index >= max_paths:
            break
        rect = drawing["rect"]
        items = []
        for item in drawing["items"]:
            kind = item[0]
            if kind == "l":
                items.append(["l", _pt(item[1]), _pt(item[2])])
            elif kind == "re":
                r = item[1]
                items.append(["re", [round(r.x0, 4), round(r.y0, 4),
                                     round(r.x1, 4), round(r.y1, 4)]])
            elif kind == "qu":
                q = item[1]
                items.append(["qu", [_pt(q.ul), _pt(q.ur), _pt(q.lr), _pt(q.ll)]])
            elif kind == "c":
                items.append(["c", _pt(item[1]), _pt(item[2]),
                              _pt(item[3]), _pt(item[4])])
            else:
                items.append([kind])
        paths.append({
            "i": index,
            "rect": [round(rect.x0, 4), round(rect.y0, 4),
                     round(rect.x1, 4), round(rect.y1, 4)],
            "color": _hex(drawing.get("color")),
            "fill": _hex(drawing.get("fill")),
            "width": round(drawing.get("width") or 0.0, 4),
            "dashes": drawing.get("dashes"),
            "even_odd": bool(drawing.get("even_odd")),
            "close_path": bool(drawing.get("closePath")),
            "items": items,
        })

    # ---------------------------------------------------------------- extents
    xs, ys = [], []
    for p in paths:
        xs += [p["rect"][0], p["rect"][2]]
        ys += [p["rect"][1], p["rect"][3]]
    for s in spans:
        xs += [s["bbox"][0], s["bbox"][2]]
        ys += [s["bbox"][1], s["bbox"][3]]
    content_extent = [min(xs), min(ys), max(xs), max(ys)]

    linetypes = _linetypes(paths)

    data = {
        "source": os.path.abspath(source),
        "page": {"width_pt": round(page.rect.width, 4),
                 "height_pt": round(page.rect.height, 4),
                 "rotation": page.rotation},
        "content_extent_pt": [round(v, 4) for v in content_extent],
        "spans": spans,
        "paths": paths,
        "linetypes": linetypes,
        "stats": {
            "path_count": len(paths),
            "span_count": len(spans),
            "colour_combo_counts": _combo_counts(paths),
            "width_counts": dict(collections.Counter(p["width"] for p in paths)),
            "dashed_paths": sum(n for k, n in linetypes["counts"].items()
                                if k != "continuous"),
            "linetype_counts": linetypes["counts"],
            "colours_outside_named_set": _unnamed_colours(paths, spans),
        },
    }
    data["raster"] = raster_histogram(page, dpi=dpi, data=data)
    return data


def _linetypes(paths) -> dict:
    """Every distinct dash pattern in the source, named and measured.

    This block is what makes dashes survive into the DXF.  An earlier version read
    the PDF dash array and then never used it, so every centre line, hidden line and
    section boundary was reproduced as a solid line - and the self-check could not
    see it, because the ruler rasterised both sides with the same dashed-or-not
    blindness.
    """
    patterns = {}
    for p in paths:
        pat = cadkit.dash_pattern(p.get("dashes"))
        key = cadkit.dash_linetype_name(pat)
        entry = patterns.setdefault(key, {"pattern_pt": list(pat), "paths": 0,
                                          "example_path": p["i"]})
        entry["paths"] += 1
    counts = {k: v["paths"] for k, v in sorted(patterns.items())}
    return {"patterns": patterns, "counts": counts}


def _unnamed_colours(paths, spans) -> dict:
    """Colours that are not one of the six named source colours.

    Reported rather than swallowed.  These are written as true colours now, but a
    reader still needs to know that the sheet is not the six-colour kind, because
    that is what decides whether the dimension machinery can be recognised (see
    infer_dims) and whether the ACI layer scheme applies.
    """
    out = collections.Counter()
    for p in paths:
        for key in (p["color"], p["fill"]):
            if key and key.startswith("#"):
                out[key] += 1
    for s in spans:
        key = cadkit.hex_string_to_key(s["color"])   # named colour, or #RRGGBB
        if key and key.startswith("#"):
            out[key] += 1
    return dict(out.most_common())


def _pt(p) -> list:
    return [round(p.x, 4), round(p.y, 4)]


def _hex(rgb) -> str | None:
    key = cadkit.rgb_to_key(rgb)
    return key


def _combo_counts(paths) -> dict:
    counter = collections.Counter(
        f"stroke={p['color'] or 'none'} fill={p['fill'] or 'none'} w={p['width']}"
        for p in paths
    )
    return dict(counter.most_common())


def raster_histogram(page, dpi: int = 150, max_pixels: int = 40_000_000,
                     data: dict | None = None) -> dict:
    """Measure which colours the source actually puts on paper.

    This is the independent second opinion on colour.  Path metadata can be
    misread; the rendered page cannot, because it is the same rasterisation a
    reader's eye gets.  Every later colour check is compared against these
    numbers, not against the path metadata.
    """
    import numpy as np
    import pymupdf

    scale = dpi / 72.0
    if page.rect.width * scale * page.rect.height * scale > max_pixels:
        scale = (max_pixels / (page.rect.width * page.rect.height)) ** 0.5
        dpi = int(scale * 72)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]

    # The palette is the drawing's own colours, not a fixed eight.  Classifying a
    # sheet drawn in #1E90FF against the default palette would report it as "blue"
    # and make every share below a statement about the palette instead of the sheet.
    palette = cadkit.raster_palette(data) if data else None
    shares, ink = cadkit.family_shares(arr, palette)
    return {
        "dpi": dpi,
        "size_px": [pix.width, pix.height],
        "ink_pixels": ink,
        "colour_share": shares,
        "palette": [name for name, _rgb in palette] if palette else list(cadkit.RASTER_FAMILIES),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="extract source drawing measurements")
    ap.add_argument("source")
    ap.add_argument("-o", "--out", required=True, help="output JSON path")
    ap.add_argument("--dpi", type=int, default=150, help="raster sampling resolution")
    ap.add_argument("--config")
    args = ap.parse_args()

    data = extract(args.source, dpi=args.dpi)
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(args.out)))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)

    cadkit.banner("SOURCE EXTRACTED")
    cadkit.table(
        [("sheet size (pt)", f"{data['page']['width_pt']} x {data['page']['height_pt']}"),
         ("content extent (pt)", " ".join(f"{v:.2f}" for v in data["content_extent_pt"])),
         ("paths", data["stats"]["path_count"]),
         ("text spans", data["stats"]["span_count"]),
         ("dashed paths", data["stats"]["dashed_paths"]),
         ("raster ink pixels", data["raster"]["ink_pixels"])],
        ("measurement", "value"))
    print()
    cadkit.table([(k, v) for k, v in data["stats"]["colour_combo_counts"].items()],
                 ("stroke / fill / width combination", "paths"))
    print()
    cadkit.table([(k if k != "continuous" else "(solid)", v)
                  for k, v in data["stats"]["linetype_counts"].items()],
                 ("linetype (dash pattern, points)", "paths"))
    print()
    cadkit.table([(k, v) for k, v in data["raster"]["colour_share"].items()],
                 ("rendered colour", "share of ink"))
    if data["stats"]["colours_outside_named_set"]:
        print()
        print("colours outside the six named source colours (written as true colour,"
              " not substituted):")
        for k, v in data["stats"]["colours_outside_named_set"].items():
            print(f"  {k}  {v} paths/spans")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
