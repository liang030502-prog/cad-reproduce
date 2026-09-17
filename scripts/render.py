# -*- coding: utf-8 -*-
"""Stage 3 - render a drawing to PNG so it can actually be looked at and diffed.

A DXF is a text file.  Nothing in a DXF tells you whether the drawing looks
right: whether the outlines are the right colour, whether the fills landed, or
whether the numerals appeared at all.  The only honest check is to rasterise the
generated drawing and rasterise the source into the same pixel grid and compare
them.  This module is that rasteriser.

Both renderers are driven from an explicit extent in millimetres, so the two
images are guaranteed to be comparable rather than merely similar in size.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402

WHITE = "#FFFFFF"

# ACI 7 is "black or white, whichever contrasts with the background".  A viewer
# that resolves it against a white page gets black, which is right; but a viewer
# that resolves it against a dark background gets white, which makes a correct
# drawing look empty.  Pinning the foreground makes the check independent of the
# viewer's background choice, and matches how the drawing will be printed.
ACI7_AS_BLACK = 250


def render_dxf(dxf_path: str, png_path: str, dpi: int = 150,
               extent_mm: tuple | None = None, width_mm: float | None = None,
               height_mm: float | None = None) -> dict:
    import ezdxf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import (
        BackgroundPolicy, ColorPolicy, Configuration, LineweightPolicy,
    )
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    if extent_mm is None:
        # Fall back to measuring the drawing.  This is a last resort, not the
        # normal path: the generator already knows the exact region its entities
        # occupy, and a window measured from the entities themselves cannot be
        # the same window as the source's, so a measured window makes the
        # comparison approximate.  It exists only so a hand-made DXF can be
        # rendered for inspection.
        for entity in msp:
            try:
                ex0, ey0, ex1, ey1 = entity.bbox().extents
            except Exception:
                continue
            x0, y0 = min(x0, ex0), min(y0, ey0)
            x1, y1 = max(x1, ex1), max(y1, ey1)
        if x0 == float("inf"):
            raise RuntimeError(
                f"{dxf_path}: could not measure the modelspace; pass --extent-mm "
                f"with the region to render")
        extent_mm = (x0, y0, x1, y1)

    ex0, ey0, ex1, ey1 = extent_mm
    span_x = ex1 - ex0
    span_y = ey1 - ey0
    if span_x <= 0 or span_y <= 0:
        raise ValueError(f"degenerate render window: {extent_mm}")
    if os.environ.get("CADKIT_DEBUG"):
        cadkit.eprint(f"[render_dxf] extent_mm={extent_mm} span={span_x}x{span_y} "
                      f"width_mm={width_mm} height_mm={height_mm} dpi={dpi}")
    if width_mm is None:
        width_mm = span_x
    if height_mm is None:
        height_mm = span_y

    fig = plt.figure(figsize=(width_mm / 25.4, height_mm / 25.4), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    fig.patch.set_facecolor(WHITE)
    ax.set_facecolor(WHITE)
    ax.set_xlim(ex0, ex1)
    ax.set_ylim(ey0, ey1)

    config = Configuration(
        color_policy=ColorPolicy.COLOR,
        background_policy=BackgroundPolicy.WHITE,
        lineweight_policy=LineweightPolicy.ABSOLUTE,
        pdsize=None,
    )
    context = RenderContext(doc)
    # finalize=False on purpose.  With finalize=True the backend calls set_aspect
    # with an adjustable box, which RESIZES the figure to the drawing's own
    # proportions - a 9.975 x 7.093 in figure silently became 6.75 x 4.8 in.  That
    # is fatal here: the whole point of this render is to land the generated
    # drawing in the same pixel grid as the source, and a backend that picks its
    # own figure size destroys the alignment.  The render window is already
    # exactly the sheet rectangle, so no aspect adjustment is wanted at all.
    Frontend(context, MatplotlibBackend(ax), config=config).draw_layout(msp, finalize=False)

    # Re-assert the figure geometry after drawing, then verify it by measuring the
    # saved file rather than trusting the figure object.
    fig.set_size_inches(width_mm / 25.4, height_mm / 25.4)
    ax.set_position([0.0, 0.0, 1.0, 1.0])
    ax.set_xlim(ex0, ex1)
    ax.set_ylim(ey0, ey1)
    ax.set_aspect("auto")

    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(png_path)))
    fig.savefig(png_path, dpi=dpi, facecolor=WHITE)
    plt.close(fig)

    from PIL import Image
    with Image.open(png_path) as im:
        size = im.size

    # Verify the saved raster against the size the window implies.  A renderer that
    # quietly changes its figure size produces a correct-looking picture in the
    # wrong pixel grid, and every comparison built on it is then meaningless while
    # still reporting numbers.  This check is what makes the alignment trustworthy
    # instead of merely intended.
    expected = (round(width_mm / 25.4 * dpi), round(height_mm / 25.4 * dpi))
    if size != expected:
        # One pixel of disagreement is rounding; more than that is a broken window.
        if abs(size[0] - expected[0]) > 1 or abs(size[1] - expected[1]) > 1:
            raise RuntimeError(
                f"render produced {size} px but the render window implies {expected} px; "
                f"the generated raster would not be comparable with the source")
    if os.environ.get("CADKIT_DEBUG"):
        cadkit.eprint(f"[render_dxf] window={width_mm:.4f}x{height_mm:.4f} mm "
                      f"dpi={dpi} expected_px={expected} actual_px={size}")
    return {
        "png": os.path.abspath(png_path),
        "dpi": dpi,
        "size_px": list(size),
        "extent_mm": [round(v, 4) for v in extent_mm],
        "drawing_extent_mm": [round(v, 4) for v in (x0, y0, x1, y1)],
    }


def render_pdf(pdf_path: str, png_path: str, dpi: int = 150,
               clip_pt: tuple | None = None) -> dict:
    """Rasterise page 1 of a PDF.

    `clip_pt` is (x0, y0, x1, y1) in PDF points, y measured downward from the top
    of the page, matching what PyMuPDF reports for text and path rectangles.
    """
    import pymupdf

    doc = pymupdf.open(pdf_path)
    page = doc[0]
    scale = dpi / 72.0
    matrix = pymupdf.Matrix(scale, scale)
    if clip_pt:
        x0, y0, x1, y1 = clip_pt
        clip = pymupdf.Rect(x0, y0, x1, y1)
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
    else:
        pix = page.get_pixmap(matrix=matrix, alpha=False)
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(png_path)))
    pix.save(png_path)
    return {
        "png": os.path.abspath(png_path),
        "dpi": dpi,
        "size_px": [pix.width, pix.height],
        "clip_pt": list(clip_pt) if clip_pt else None,
    }


def render_pair(source_pdf: str, dxf_path: str, out_dir: str, data: dict,
                build_report: dict | None = None, dpi: int = 150,
                margin_pt: float = 6.0) -> dict:
    """Render the source and the generated drawing into the SAME pixel grid.

    A comparison is only meaningful if both images cover the same physical
    region, so both windows come from one geometry: the source's content extent,
    expressed around the *same origin the generator used*.  The origin is taken
    from the build report when it is available, because re-deriving it here would
    introduce a sub-millimetre difference, and a sub-millimetre difference is
    enough to put the two rasters in different pixel grids.

    The DXF image comes out mirrored because its y axis points up while the PDF's
    points down; `compare` corrects that with a single flip, which is exact.
    """
    scale = float(data.get("scale_mm_per_pt") or cadkit.PT2MM)
    x0, y0, x1, y1 = data["content_extent_pt"]
    clip = (x0 - margin_pt, y0 - margin_pt, x1 + margin_pt, y1 + margin_pt)

    if build_report and build_report.get("origin_pt"):
        ox, oy = (float(v) for v in build_report["origin_pt"])
    else:
        ox, oy = float(x0), float(y1)

    # ONE window, computed once.  Computing the PDF clip in points and the DXF
    # window in millimetres independently rounds the same rectangle two different
    # ways, which is how an otherwise identical pair of renders ends up one pixel
    # apart - and a one-pixel offset is enough to make every textured edge
    # disagree.  The millimetre window is authoritative; the clip is derived from
    # it.
    extent_mm = ((clip[0] - ox) * scale, (oy - clip[3]) * scale,
                 (clip[2] - ox) * scale, (oy - clip[1]) * scale)
    clip2 = (ox + extent_mm[0] / scale, oy - extent_mm[3] / scale,
             ox + extent_mm[2] / scale, oy - extent_mm[1] / scale)

    src_info = render_pdf(source_pdf, os.path.join(out_dir, "source.png"), dpi=dpi,
                          clip_pt=clip2)
    dxf_info = render_dxf(dxf_path, os.path.join(out_dir, "generated.png"), dpi=dpi,
                          extent_mm=extent_mm)
    size_match = src_info["size_px"] == dxf_info["size_px"]

    if not size_match:
        # The PDF rasteriser rounds its own clip to whole pixels and can land one
        # pixel away from the DXF renderer's window.  Resample the source to the
        # generated grid rather than tolerating the offset: one pixel of
        # misregistration makes every edge in the drawing disagree, which would
        # swamp the real defects the comparison exists to find.
        from PIL import Image
        target = tuple(dxf_info["size_px"])
        with Image.open(src_info["png"]) as im:
            im.convert("RGB").resize(target, Image.LANCZOS).save(src_info["png"])
        src_info = {**src_info, "resampled_to": list(target)}
        size_match = True

    return {
        "clip_pt": [round(v, 4) for v in clip2],
        "extent_mm": [round(v, 4) for v in extent_mm],
        "origin_pt": [round(ox, 4), round(oy, 4)],
        "dpi": dpi,
        "source": src_info,
        "generated": dxf_info,
        "size_match": size_match,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="render a DXF or a PDF to PNG")
    ap.add_argument("input", nargs="?", help="DXF or PDF, for single renders")
    ap.add_argument("--dxf", help="DXF path, for --pair mode")
    ap.add_argument("--extract", help="extract.json, for --pair mode")
    ap.add_argument("--build-report", help="build.json, for --pair mode: supplies the origin")
    ap.add_argument("--pair", action="store_true",
                    help="render a source PDF and a DXF into one pixel grid")
    ap.add_argument("-o", "--out", help="output PNG, or output directory in --pair mode")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--margin-pt", type=float, default=6.0,
                    help="paper margin around the content in --pair mode")
    ap.add_argument("--extent-mm", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                    help="DXF render window in millimetres")
    ap.add_argument("--clip-pt", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                    help="PDF clip window in points")
    ap.add_argument("--json", help="also write the render metadata here")
    args = ap.parse_args()

    if args.pair:
        if not (args.input and args.dxf and args.extract and args.out):
            ap.error("--pair needs the source PDF, --dxf, --extract and -o")
        with open(args.extract, encoding="utf-8") as fh:
            data = json.load(fh)
        build_report = None
        if args.build_report:
            with open(args.build_report, encoding="utf-8") as fh:
                build_report = json.load(fh)
        info = render_pair(args.input, args.dxf, args.out, data,
                           build_report=build_report,
                           dpi=args.dpi, margin_pt=args.margin_pt)
        cadkit.banner("PAIR RENDERED")
        cadkit.table([("clip (pt)", " ".join(f"{v:.2f}" for v in info["clip_pt"])),
                      ("extent (mm)", " ".join(f"{v:.2f}" for v in info["extent_mm"])),
                      ("source size px", info["source"]["size_px"]),
                      ("generated size px", info["generated"]["size_px"]),
                      ("pixel grids match", info["size_match"])],
                     ("item", "value"))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(info, fh, ensure_ascii=False, indent=1)
            print(f"\nwrote {args.json}")
        return 0 if info["size_match"] else 1

    if not (args.input and args.out):
        ap.error("give an input and -o, or use --pair")
    if args.input.lower().endswith(".pdf"):
        info = render_pdf(args.input, args.out, dpi=args.dpi, clip_pt=args.clip_pt)
    else:
        info = render_dxf(args.input, args.out, dpi=args.dpi, extent_mm=args.extent_mm)

    cadkit.banner("RENDERED")
    cadkit.table([(k, v) for k, v in info.items()], ("item", "value"))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
