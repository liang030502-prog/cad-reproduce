# -*- coding: utf-8 -*-
"""Stage 4 - the self-check gate.

This is the stage that decides whether a drawing is finished, and it decides by
measurement rather than by looking.  Two numbers matter and they are independent:

* COLOUR.  The share of ink of each colour in the generated plot must match the
  share in the source.  An earlier attempt reported "colour verified" while the
  drawing carried 4514 green entities against ~125 green paths in the source, so
  a check that only confirms the expected colours are present is worthless: it
  has to compare proportions, and it has to use the rendered page as the
  reference rather than the path metadata.

* GEOMETRY.  Ink that exists in one image and not the other is a defect, and it
  is located - a mismatch is reported as a bounding box in drawing millimetres
  so the next iteration knows where to look instead of re-rendering the sheet.

The gate fails loudly.  A pipeline that reports success on a drawing it has not
measured is worse than one that reports nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402

PAPER = 245          # above this on every channel is paper, not ink


def _load_rgb(path: str):
    import numpy as np
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def _classify(rgb, palette=None):
    """(family_index, ink_mask, shares) using the shared raster classifier.

    Sharing the classifier with the extraction stage matters: if the two stages
    bucketed pixels differently, a colour difference between them would be an
    artifact of the bucketing rather than a fact about the drawings.  The palette
    comes from the source's own colours where one is supplied, so a sheet drawn in
    a colour outside the default eight is still classified exactly.
    """
    import numpy as np
    family, ink = cadkit.classify_raster(rgb, palette)
    total = int(ink.sum())
    names = [n for n, _rgb in palette] if palette else list(cadkit.RASTER_FAMILIES)
    if total == 0:
        return family, ink, {}
    counts = np.bincount(family[ink].ravel(), minlength=len(names))
    shares = {names[i]: round(float(counts[i]) / total, 6)
              for i in range(1, len(names)) if counts[i]}
    return family, ink, shares


def compare(source_png: str, dxf_png: str, out_dir: str,
            dpi: int | None = None,
            tolerance: float = 0.03, band_mm: float = 0.12,
            min_geometric_agreement: float = 0.97,
            min_colour_iou: float = 0.70,
            palette=None) -> dict:
    import numpy as np
    from PIL import Image

    src = _load_rgb(source_png)
    gen = _load_rgb(dxf_png)

    # NO mirroring.  PDF user space and DXF model space both have y increasing
    # upward, and the renderer already turned the source rectangle into the same
    # millimetre window the generator used, so the two rasters come out in the
    # same orientation.  Flipping the source here is tempting and wrong: measured
    # on a real sheet it drops ink overlap from 0.29 to 0.14 and shifts every
    # asymmetric feature by the full sheet height.  If a future renderer changes
    # orientation, the `orientation` check below catches it instead of silently
    # comparing a mirrored drawing.
    if src.shape != gen.shape:
        src = np.asarray(Image.fromarray(src).resize((gen.shape[1], gen.shape[0]), Image.LANCZOS))

    # Verify the orientation instead of assuming it.  A mirrored drawing still
    # produces a full set of plausible numbers, so the check has to be explicit:
    # if flipping the source matches the drawing better than not flipping it, the
    # two rasters are not in the same space and every number below would be about
    # a mirrored drawing.
    _s_fam, s_ink = cadkit.classify_raster(src, palette)
    _g_fam, g_ink = cadkit.classify_raster(gen, palette)
    _same = float((s_ink & g_ink).sum()) / max(1, int((s_ink | g_ink).sum()))
    _flip = float((np.flipud(s_ink) & g_ink).sum()) / max(1, int((np.flipud(s_ink) | g_ink).sum()))
    orientation = {
        "ink_iou_direct": round(_same, 6),
        "ink_iou_mirrored": round(_flip, 6),
        "mirrored_fits_better": bool(_flip > _same * 1.15),
    }
    if orientation["mirrored_fits_better"]:
        raise RuntimeError(
            f"the source raster fits the drawing only when mirrored "
            f"(direct IoU {_same:.4f} vs mirrored {_flip:.4f}); the two renders are "
            f"not in the same coordinate space, so no comparison is valid")

    h, w, _ = gen.shape
    src_family, src_mask, src_shares = _classify(src, palette)
    gen_family, gen_mask, gen_shares = _classify(gen, palette)
    src_ink = int(src_mask.sum())
    gen_ink = int(gen_mask.sum())

    families = sorted(set(src_shares) | set(gen_shares))
    rows = []
    worst = (0.0, None)
    for fam in families:
        s, g = float(src_shares.get(fam, 0.0)), float(gen_shares.get(fam, 0.0))
        delta = abs(s - g)
        rows.append((fam, f"{s:.4f}", f"{g:.4f}", f"{delta:+.4f}",
                     "OK" if delta <= tolerance else "FAIL"))
        if delta > worst[0]:
            worst = (delta, fam)

    ink_delta = abs(src_ink - gen_ink) / max(src_ink, 1)

    # Ink AREA is deliberately reported but not gated on.  Two rasterisers never
    # agree pixel-for-pixel on line thickness: a vector source is anti-aliased
    # across a wider band than cleanly stroked geometry, so the source holds ~25%
    # more ink at 150 dpi even when every line is nominally the same width.  A
    # gate on raw area therefore fails a correct drawing, which is worse than no
    # gate at all.
    #
    # What is gated instead is geometric agreement inside a tolerance band: ink in
    # the drawing must lie within `band_px` of ink in the source, and vice versa.
    # That is a statement about where the drawing is, which is what actually has
    # to be right, and it is insensitive to how thick each rasteriser draws.
    #
    # The band is expressed in millimetres and converted to pixels, not fixed in
    # pixels.  A pixel band that is generous at 150 dpi becomes impossible at
    # 300 dpi, where the same physical misalignment covers twice as many pixels; a
    # fixed pixel band would make the gate depend on the DPI it happened to run at.
    h, w, _ = gen.shape
    dpi = dpi or 300
    band_px = max(1, int(round(band_mm / 25.4 * dpi)))

    def _dilate(mask, radius: int):
        if radius <= 0 or not mask.any():
            return mask
        out = mask.copy()
        for _ in range(radius):
            p = np.pad(out, 1, mode="constant", constant_values=False)
            out = (p[0:-2, 0:-2] | p[0:-2, 1:-1] | p[0:-2, 2:] |
                   p[1:-1, 0:-2] | p[1:-1, 1:-1] | p[1:-1, 2:] |
                   p[2:, 0:-2] | p[2:, 1:-1] | p[2:, 2:])
        return out

    src_band = _dilate(src_mask, band_px)
    gen_band = _dilate(gen_mask, band_px)
    extra = gen_mask & ~src_band          # in the drawing, nowhere near the source
    missing = src_mask & ~gen_band        # in the source, nowhere near the drawing
    geometric_agreement = 1.0 - (
        float(extra.sum()) + float(missing.sum())) / max(1, 2 * max(src_ink, gen_ink))

    # Where does the ink disagree?  These are the pixels that fall outside the
    # tolerance band on both sides, so the difference map shows real defects
    # rather than the one-pixel disagreement every anti-aliased edge produces.
    def _clean(mask, minimum_neighbours: int = 3):
        """Drop specks: keep a pixel only if enough of its 3x3 neighbourhood agrees.

        Implemented with shifts rather than a scipy dependency, so the gate runs
        anywhere the rest of the pipeline runs.
        """
        if not mask.any():
            return mask
        padded = np.pad(mask, 1, mode="constant", constant_values=False)
        votes = np.zeros(mask.shape, dtype=np.uint8)
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                votes += padded[dy:dy + mask.shape[0], dx:dx + mask.shape[1]]
        return mask & (votes >= minimum_neighbours)

    extra_c, missing_c = _clean(extra), _clean(missing)

    def _bbox(mask):
        if not mask.any():
            return None
        ys, xs = np.nonzero(mask)
        return [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]

    # A colour-level defect is more useful than a pixel-level one: it says which
    # colour is in the wrong place.  Comparing family indices rather than RGB
    # keeps anti-aliased edges from counting as disagreement.
    palette_names = [n for n, _rgb in palette] if palette else list(cadkit.RASTER_FAMILIES)
    colour_confusion = {}
    for idx, fam in enumerate(palette_names):
        if idx == 0:
            continue
        src_fam = (src_family == idx) & gen_mask
        gen_fam = (gen_family == idx) & src_mask
        if not src_fam.any() and not gen_fam.any():
            continue
        inter = int((src_fam & gen_fam).sum())
        union = int((src_fam | gen_fam).sum())
        if union:
            colour_confusion[fam] = {"iou": round(inter / union, 4), "pixels": union}

    worst_iou = min((v["iou"] for v in colour_confusion.values()), default=1.0)

    os.makedirs(out_dir, exist_ok=True)
    _write_visuals(src, gen, src_mask, gen_mask, extra_c, missing_c, out_dir)

    report = {
        "source_png": os.path.abspath(source_png),
        "dxf_png": os.path.abspath(dxf_png),
        "size_px": [w, h],
        "dpi": dpi,
        "orientation": orientation,
        "ink_pixels": {"source": src_ink, "generated": gen_ink,
                       "relative_delta": round(ink_delta, 6)},
        "colour_shares": [
            {"colour": fam,
             "source": round(float(src_shares.get(fam, 0.0)), 6),
             "generated": round(float(gen_shares.get(fam, 0.0)), 6),
             "delta": round(abs(float(src_shares.get(fam, 0.0))
                                - float(gen_shares.get(fam, 0.0))), 6),
             "verdict": verdict}
            for fam, _s, _g, _d, verdict in rows
        ],
        "colour_iou": colour_confusion,
        "worst_colour": worst[1],
        "worst_colour_delta": round(worst[0], 6),
        "worst_colour_iou": round(worst_iou, 6),
        "geometric_agreement": round(geometric_agreement, 6),
        "unmatched_pixels": {"in_drawing_only": int(extra.sum()),
                             "in_source_only": int(missing.sum())},
        "extra_ink_bbox_px": _bbox(extra_c),
        "missing_ink_bbox_px": _bbox(missing_c),
        "thresholds": {"colour_share": tolerance, "band_mm": band_mm, "band_px": band_px,
                       "min_geometric_agreement": min_geometric_agreement,
                       "min_colour_iou": min_colour_iou},
        "pass": bool(worst[0] <= tolerance
                     and geometric_agreement >= min_geometric_agreement
                     and worst_iou >= min_colour_iou),
    }
    report["visuals"] = {
        "side_by_side": os.path.abspath(os.path.join(out_dir, "cmp_side_by_side.png")),
        "difference_map": os.path.abspath(os.path.join(out_dir, "cmp_difference.png")),
    }
    return report


def _write_visuals(src, gen, src_mask, gen_mask, extra, missing, out_dir) -> None:
    import numpy as np
    from PIL import Image

    def _ink_on_white(mask):
        out = np.full(mask.shape + (3,), 255, dtype=np.uint8)
        out[mask] = (0, 0, 0)
        return out

    left = np.full(src.shape, 255, dtype=np.uint8)
    left[src_mask] = src[src_mask]
    right = np.full(gen.shape, 255, dtype=np.uint8)
    right[gen_mask] = gen[gen_mask]

    gap = np.full((src.shape[0], 24, 3), 200, dtype=np.uint8)
    side = np.concatenate([left, gap, right], axis=1)
    Image.fromarray(side).save(os.path.join(out_dir, "cmp_side_by_side.png"))

    diff = np.full(src.shape, 255, dtype=np.uint8)
    both = src_mask & gen_mask
    diff[both] = (190, 190, 190)              # agreement, faded
    diff[extra] = (0, 90, 255)                # blue: in the drawing, not the source
    diff[missing] = (255, 0, 0)               # red: in the source, not the drawing
    Image.fromarray(diff).save(os.path.join(out_dir, "cmp_difference.png"))


def main() -> int:
    ap = argparse.ArgumentParser(description="compare a generated drawing against its source")
    ap.add_argument("source_png")
    ap.add_argument("dxf_png")
    ap.add_argument("-o", "--out-dir", required=True)
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--tolerance", type=float, default=0.03,
                    help="permitted per-colour share difference (default 3%% of ink)")
    ap.add_argument("--band-mm", type=float, default=0.12,
                    help="geometric tolerance band in millimetres (default 0.12)")
    ap.add_argument("--dpi", type=int, default=300,
                    help="resolution the renders were made at; converts the band to pixels")
    ap.add_argument("--min-agreement", type=float, default=0.97,
                    help="minimum fraction of ink that must land inside the band")
    ap.add_argument("--min-colour-iou", type=float, default=0.70,
                    help="minimum placement IoU per colour")
    ap.add_argument("--extract", dest="extract_json",
                    help="extract.json, to classify colours against the SOURCE's own "
                         "palette instead of the default eight (recommended: without it "
                         "a colour outside those eight is reported as its nearest "
                         "neighbour and the colour gate measures the palette, not the drawing)")
    args = ap.parse_args()

    palette = None
    if args.extract_json:
        with open(args.extract_json, encoding="utf-8") as fh:
            palette = cadkit.raster_palette(json.load(fh))

    report = compare(args.source_png, args.dxf_png, args.out_dir,
                     dpi=args.dpi, tolerance=args.tolerance, band_mm=args.band_mm,
                     min_geometric_agreement=args.min_agreement,
                     min_colour_iou=args.min_colour_iou, palette=palette)

    cadkit.banner("SELF-CHECK")
    cadkit.table(
        [(k, report["ink_pixels"][k]) for k in ("source", "generated")]
        + [("relative delta", report["ink_pixels"]["relative_delta"])],
        ("ink", "pixels"))
    print()
    cadkit.table([(r["colour"], f"{r['source']:.4f}", f"{r['generated']:.4f}",
                   f"{r['delta']:+.4f}", r["verdict"]) for r in report["colour_shares"]],
                 ("colour", "source share", "generated share", "delta", "verdict"))
    print()
    cadkit.table([(k, v["iou"], v["pixels"]) for k, v in report["colour_iou"].items()],
                 ("colour", "placement IoU", "overlap pixels"))
    print()
    print(f"worst colour delta  : {report['worst_colour_delta']:.4f} ({report['worst_colour']})")
    print(f"worst colour IoU    : {report['worst_colour_iou']:.4f}")
    print(f"geometric agreement : {report['geometric_agreement']:.4f} "
          f"(band {report['thresholds']['band_mm']} mm = "
          f"{report['thresholds']['band_px']} px at {report['dpi']} dpi, need "
          f"{report['thresholds']['min_geometric_agreement']})")
    print(f"unmatched in drawing: {report['unmatched_pixels']['in_drawing_only']} px  "
          f"bbox {report['extra_ink_bbox_px']}")
    print(f"unmatched in source : {report['unmatched_pixels']['in_source_only']} px  "
          f"bbox {report['missing_ink_bbox_px']}")
    print()
    print(f"VERDICT: {'PASS' if report['pass'] else 'FAIL'}")
    print(f"  see {report['visuals']['side_by_side']}")
    print(f"  see {report['visuals']['difference_map']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print(f"  report: {args.json}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
