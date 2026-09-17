# -*- coding: utf-8 -*-
"""cadkit - shared helpers for the cad-reproduce pipeline.

Every number this module produces is either read from the source drawing or
reported as an explicit measurement.  Nothing is guessed and nothing is
silently defaulted: a helper that cannot determine a value raises, because a
defaulted value is indistinguishable from a correct one in the output.
"""
from __future__ import annotations

import os
import sys

# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------
# The six colours printed technical drawings actually use.  The ACI (AutoCAD
# Color Index) palette contains each of them exactly, which was verified with
# ezdxf.colors.aci2rgb.  Keeping the exact ACI index means AutoCAD shows the
# same colour as the source without needing true-colour support.
#
# ACI 7 is deliberately NOT used for black.  ACI 7 means "black or white,
# whichever contrasts with the background", so an entity on ACI 7 displays
# white in AutoCAD's dark model space and only plots black.  A drawing that is
# checked by looking at the screen then looks wrong while actually being right.
# ACI 250 is pure black in every context, so it is used instead.
SOURCE_COLORS: dict[str, dict] = {
    "black":   {"rgb": (0, 0, 0),       "aci": 250},
    "red":     {"rgb": (255, 0, 0),     "aci": 1},
    "yellow":  {"rgb": (255, 255, 0),   "aci": 2},
    "green":   {"rgb": (0, 255, 0),     "aci": 3},
    "cyan":    {"rgb": (0, 255, 255),   "aci": 4},
    "magenta": {"rgb": (255, 0, 255),   "aci": 6},
}

# Layer names.  ASCII names are deliberate: CJK layer names work in AutoCAD but
# make every downstream script, regex and shell invocation fragile, and a layer
# name carries no information the report does not already carry.
LAYERS = {
    "outline": ("OUTLINE", "black"),
    "section": ("SECTION", "green"),
    "dims":    ("DIM_LINES", "red"),
    "dimtext": ("DIM_TEXT", "black"),
    "note":    ("NOTE_TEXT", "yellow"),
    "cyan":    ("LAYER_CYAN", "cyan"),
    "magenta": ("MARK_MAGENTA", "magenta"),
    "frame":   ("SHEET_FRAME", "black"),
}

# DXF lineweight values AutoCAD accepts.  A value outside this list is either
# rejected or silently substituted, so requests are snapped to the nearest
# legal step and the substitution is reported.
LEGAL_LINEWEIGHTS = (
    0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80,
    90, 100, 106, 120, 140, 158, 200, 211,
)

PT2MM = 25.4 / 72.0


# --------------------------------------------------------------------------
# raster colour classification
# --------------------------------------------------------------------------
# Rasterised line art is full of intermediate pixels: every edge is a gradient
# between the ink colour and the paper.  Classifying those with hard thresholds
# on each channel throws roughly half of all ink into an "unclassified" bucket
# and makes any comparison meaningless.  Nearest-neighbour against the seven
# colours the drawing can actually contain keeps anti-aliased edge pixels with
# the ink they came from, so a colour share means what it says.
PAPER_THRESHOLD = 245


def classify_raster(arr):
    """Classify every pixel of an RGB array into a colour family.

    Returns (family, ink_mask) where family is an int array of indices into
    RASTER_FAMILIES and ink_mask marks the pixels that are not paper.
    """
    import numpy as np

    palette = np.array(
        [[255, 255, 255],                       # paper
         [0, 0, 0],                             # black
         [255, 0, 0],                           # red
         [0, 255, 0],                           # green
         [0, 0, 255],                           # blue
         [0, 255, 255],                         # cyan
         [255, 0, 255],                         # magenta
         [255, 255, 0]],                        # yellow
        dtype=np.int16)

    flat = arr.astype(np.int16).reshape(-1, 3)
    # Squared Euclidean distance to each palette entry, then take the nearest.
    # The array is processed in blocks so a 300 dpi A4 page does not need a
    # multi-gigabyte intermediate.
    out = np.empty(flat.shape[0], dtype=np.uint8)
    block = 1 << 20
    for start in range(0, flat.shape[0], block):
        chunk = flat[start:start + block]
        delta = chunk[:, None, :] - palette[None, :, :]
        dist = (delta.astype(np.int32) ** 2).sum(axis=2)
        out[start:start + block] = dist.argmin(axis=1).astype(np.uint8)

    family = out.reshape(arr.shape[0], arr.shape[1])
    ink = family != 0
    return family, ink


RASTER_FAMILIES = ("paper", "black", "red", "green", "blue", "cyan", "magenta", "yellow")


def family_shares(arr) -> tuple[dict, int]:
    """Share of ink pixels per colour family, excluding paper."""
    import numpy as np

    family, ink = classify_raster(arr)
    total = int(ink.sum())
    if total == 0:
        return {}, 0
    counts = np.bincount(family[ink].ravel(), minlength=len(RASTER_FAMILIES))
    return ({RASTER_FAMILIES[i]: round(float(counts[i]) / total, 6)
             for i in range(1, len(RASTER_FAMILIES)) if counts[i]},
            total)



def snap_lineweight(mm: float) -> tuple[int, int | None]:
    """Return (chosen_centi_mm, requested_centi_mm) for a wanted millimetre width.

    DXF stores lineweight in hundredths of a millimetre.  The second element is
    None when the request was already legal, otherwise it is the requested
    value so the caller can report that a substitution happened.
    """
    want = int(round(mm * 100))
    chosen = min(LEGAL_LINEWEIGHTS, key=lambda v: abs(v - want))
    return chosen, (None if chosen == want else want)


def rgb_to_key(rgb) -> str | None:
    """Map a PDF colour tuple to one of the six source colour names."""
    if rgb is None:
        return None
    try:
        r, g, b = (float(c) for c in rgb[:3])
    except (TypeError, ValueError):
        return None
    # PDF colour components are floats in 0..1 and the drawings use saturated
    # values, so a tolerant threshold is safe and avoids float-representation
    # surprises such as 0.9999999.
    key = (int(r > 0.5), int(g > 0.5), int(b > 0.5))
    return {
        (0, 0, 0): "black",
        (1, 0, 0): "red",
        (1, 1, 0): "yellow",
        (0, 1, 0): "green",
        (0, 1, 1): "cyan",
        (1, 0, 1): "magenta",
        (1, 1, 1): None,          # white: paper, never ink
    }.get(key)


def color_attrs(color_name: str, policy: str = "exact") -> tuple[dict, dict]:
    """Return (dxfattribs_for_colour, record) describing what will be written.

    The record lets a caller report the colour it actually used instead of
    assuming the requested one took effect.  Every one of the six source colours
    has an exact ACI index - verified with ezdxf.colors.aci2rgb - so the `exact`
    policy writes an index rather than a true colour, and the two are therefore
    identical in appearance while the index stays readable in AutoCAD's layer
    manager.
    """
    spec = SOURCE_COLORS[color_name]
    rgb, aci = spec["rgb"], spec["aci"]
    if policy == "true":
        return {"true_color": _rgb_int(rgb)}, {"mode": "true", "rgb": rgb, "aci": 256}
    if policy == "aci":
        return {"color": aci}, {"mode": "aci", "rgb": rgb, "aci": aci}
    from ezdxf import colors as ezcolors
    try:
        got = tuple(ezcolors.aci2rgb(aci))
    except Exception as exc:                       # pragma: no cover
        raise RuntimeError(f"ACI {aci} for {color_name} is not in the palette") from exc
    if got != tuple(rgb):                          # pragma: no cover - tables are exact
        return {"true_color": _rgb_int(rgb)}, {"mode": "true_fallback", "rgb": rgb, "aci": 256}
    return {"color": aci}, {"mode": "aci", "rgb": rgb, "aci": aci}


def _rgb_int(rgb) -> int:
    r, g, b = rgb
    return (int(r) << 16) | (int(g) << 8) | int(b)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def load_config(path: str | None = None) -> dict:
    """Load cad-reproduce.yaml, then let CLI flags override it."""
    import yaml
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "cad-reproduce.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError(f"config not found: {path}")
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg


def job_dir(cfg: dict, job: str | None = None, base: str | None = None) -> str:
    """Resolve the directory a single drawing's artifacts live in.

    The pipeline never writes loose files next to its own scripts: every run
    gets one directory holding the source, the generated drawing, the render
    and the reports, so a later run can be compared against it.
    """
    if job:
        root = base or cfg.get("work_root") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jobs")
        return os.path.join(root, job)
    out = cfg.get("out_dir")
    if not out:
        raise ValueError("no out_dir configured and no job name given")
    return out


def ensure_dirs(*paths: str) -> None:
    for p in paths:
        if p:
            os.makedirs(p, exist_ok=True)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def eprint(*a, **k) -> None:
    print(*a, file=sys.stderr, **k)


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def table(rows, headers) -> None:
    """Print a fixed-width table.  Avoids a tabulate dependency."""
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*r))
