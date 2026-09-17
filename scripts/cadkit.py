# -*- coding: utf-8 -*-
"""cadkit - shared helpers for the cad-reproduce pipeline.

Every number this module produces is either read from the source drawing or
reported as an explicit measurement.  Nothing is guessed and nothing is
silently defaulted: a helper that cannot determine a value raises, because a
defaulted value is indistinguishable from a correct one in the output.
"""
from __future__ import annotations

import os
import re
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
# and makes any comparison meaningless.  Nearest-neighbour against the colours the
# drawing can actually contain keeps anti-aliased edge pixels with the ink they
# came from, so a colour share means what it says.
PAPER_THRESHOLD = 245

# The colour families a raster is bucketed into when the caller names none.
# "paper" is always slot 0, so `ink = family != 0` holds for every palette.
RASTER_FAMILIES = ("paper", "black", "red", "green", "blue", "cyan", "magenta", "yellow")

_DEFAULT_RASTER_PALETTE = (
    ("paper", (255, 255, 255)),
    ("black", (0, 0, 0)),
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("blue", (0, 0, 255)),
    ("cyan", (0, 255, 255)),
    ("magenta", (255, 0, 255)),
    ("yellow", (255, 255, 0)),
)


def raster_palette(data: dict) -> list:
    """The colour families THIS drawing can contain, as [(name, rgb), ...].

    A fixed palette is only correct for a drawing that uses those six saturated
    colours.  A sheet drawn in #1E90FF has no exact entry in the default palette,
    so every one of its pixels is forced into "blue" and the per-colour shares the
    gate compares are then about the palette rather than about the drawing.  Taking
    the palette from the source's own path colours keeps the classification exact
    for any drawing, and leaves the default palette in place for a source that
    names no colour at all.
    """
    palette, seen = [], set()
    for name, rgb in _DEFAULT_RASTER_PALETTE + (("white", (255, 255, 255)),):
        seen.add(name)
        palette.append((name, rgb))
    for p in data.get("paths", ()):
        for key in (p.get("color"), p.get("fill")):
            rgb = key_to_rgb(key)
            if rgb is None or key in seen:
                continue
            seen.add(key)
            palette.append((key, rgb))
    for s in data.get("spans", ()):
        key = s.get("color")
        rgb = key_to_rgb(key)
        if rgb is None or key in seen:
            continue
        seen.add(key)
        palette.append((key, rgb))
    return palette


def classify_raster(arr, palette=None):
    """Classify every pixel of an RGB array into a colour family.

    Returns (family, ink_mask) where family is an int array of indices into the
    palette and ink_mask marks the pixels that are not paper.  `palette` is a
    sequence of (name, rgb); the default is RASTER_FAMILIES.
    """
    import numpy as np

    if palette is None:
        palette = _DEFAULT_RASTER_PALETTE
    lut = np.array([rgb for _name, rgb in palette], dtype=np.int16)

    flat = arr.astype(np.int16).reshape(-1, 3)
    # Squared Euclidean distance to each palette entry, then take the nearest.
    # The array is processed in blocks so a 300 dpi A4 page does not need a
    # multi-gigabyte intermediate.
    out = np.empty(flat.shape[0], dtype=np.uint8)
    block = 1 << 20
    for start in range(0, flat.shape[0], block):
        chunk = flat[start:start + block]
        delta = chunk[:, None, :] - lut[None, :, :]
        dist = (delta.astype(np.int32) ** 2).sum(axis=2)
        out[start:start + block] = dist.argmin(axis=1).astype(np.uint8)

    family = out.reshape(arr.shape[0], arr.shape[1])
    ink = family != 0
    return family, ink


def family_shares(arr, palette=None) -> tuple[dict, int]:
    """Share of ink pixels per colour family, excluding paper."""
    import numpy as np

    if palette is None:
        palette = _DEFAULT_RASTER_PALETTE
    family, ink = classify_raster(arr, palette)
    total = int(ink.sum())
    if total == 0:
        return {}, 0
    counts = np.bincount(family[ink].ravel(), minlength=len(palette))
    return ({palette[i][0]: round(float(counts[i]) / total, 6)
             for i in range(1, len(palette)) if counts[i]},
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
    """Map a PDF colour tuple to a colour key.

    A key is either one of the named source colours or a `#RRGGBB` string.  It is
    NEVER silently mapped to a different colour: an earlier version snapped every
    colour to the nearest of six named ones, so a sheet drawn in blue came out
    black, with no error and no report entry - and the colour gate then failed
    forever, because the fault was in the palette rather than in the geometry.

    The mapping is by exact value, not by a per-channel threshold.  Thresholding was
    tried and is wrong: #1E90FF has all three channels above the midpoint, so it was
    silently reported as "cyan", which is both a different colour and the wrong ACI.
    A name is only used when the source colour IS that name's colour.
    """
    if rgb is None:
        return None
    try:
        r, g, b = (float(c) for c in rgb[:3])
    except (TypeError, ValueError):
        return None
    if max(r, g, b) > 1.0:                      # 0..255 input
        r, g, b = r / 255.0, g / 255.0, b / 255.0
    got = tuple(int(round(v * 255)) for v in (r, g, b))
    if got == (255, 255, 255):
        return None                             # white: paper, never ink
    for name, spec in SOURCE_COLORS.items():
        if tuple(spec["rgb"]) == got:
            return name
    return hex_key((r, g, b))


def hex_key(rgb) -> str:
    r, g, b = rgb
    return "#%02X%02X%02X" % (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def hex_string_to_key(value: str) -> str | None:
    """A `#RRGGBB` string as a colour key, going through the same mapping as a PDF
    colour tuple so a span and a path of the same colour always agree."""
    if not value or not isinstance(value, str) or not value.startswith("#") \
            or len(value) != 7:
        return None
    return rgb_to_key((int(value[1:3], 16) / 255.0,
                       int(value[3:5], 16) / 255.0,
                       int(value[5:7], 16) / 255.0))


def hex_to_rgb(value: str) -> tuple:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def key_to_rgb(key) -> tuple | None:
    """Colour key -> (r, g, b) in 0..255, or None if the key is not a colour."""
    if not key or key == "paper":
        return None
    if key in SOURCE_COLORS:
        return SOURCE_COLORS[key]["rgb"]
    if isinstance(key, str) and key.startswith("#") and len(key) == 7:
        return hex_to_rgb(key)
    if key == "white":
        return (255, 255, 255)
    return None


def key_to_aci(key) -> int | None:
    """Exact ACI index for a colour key, or None when only a true colour can
    reproduce it.  True colour is group code 420; AutoCAD renders it exactly."""
    spec = SOURCE_COLORS.get(key)
    return spec["aci"] if spec else None


def nearest_aci(rgb) -> int:
    """Closest ACI index to an RGB triple.

    Only used when the caller explicitly asks for the ACI palette.  The distance is
    plain Euclidean RGB, which is a crude perceptual metric but is exactly what
    "snap to the palette" means, and the caller gets told what it was snapped from.
    """
    best, best_d = None, None
    for idx, cand in _aci_rgb_table():
        d = sum((int(a) - int(b)) ** 2 for a, b in zip(rgb, cand))
        if best_d is None or d < best_d:
            best, best_d = idx, d
    if best is None:                               # pragma: no cover
        raise RuntimeError("the ACI palette is empty")
    return best


def aci2rgb(idx: int) -> tuple:
    for cand_idx, rgb in _aci_rgb_table():
        if cand_idx == idx:
            return rgb
    from ezdxf import colors as ezcolors
    return tuple(ezcolors.aci2rgb(idx))


def color_attrs(color_key, policy: str = "exact") -> tuple[dict, dict]:
    """Return (dxfattribs_for_colour, record) describing what will be written.

    The record lets a caller report the colour it actually used instead of
    assuming the requested one took effect.

    Policies:

    * `exact` (default) - an index when one reproduces the colour exactly,
      otherwise a true colour.  Never an approximation, so a colour outside the
      palette survives into the drawing instead of being rounded to something else.
    * `aci` - always an index, snapped to the nearest.  Explicitly lossy; the
      record says what it was snapped from.
    * `true` - always a true colour (group code 420).
    """
    rgb = key_to_rgb(color_key)
    if rgb is None:
        if color_key in (None, "white", "paper"):
            return {}, {"mode": "none", "rgb": None, "aci": 256}
        raise ValueError(
            f"colour key {color_key!r} is neither a named source colour nor a "
            f"#RRGGBB value; refusing to substitute an arbitrary colour")
    aci = key_to_aci(color_key)
    if policy == "true":
        return {"true_color": _rgb_int(rgb)}, {"mode": "true", "rgb": rgb, "aci": 256}
    if aci is None:
        if policy == "aci":
            snapped = nearest_aci(rgb)
            return ({"color": snapped},
                    {"mode": "aci_snapped", "rgb": rgb, "aci": snapped,
                     "snapped_from": rgb, "snapped_to": list(aci2rgb(snapped))})
        return {"true_color": _rgb_int(rgb)}, {"mode": "true", "rgb": rgb, "aci": 256}
    return {"color": aci}, {"mode": "aci", "rgb": rgb, "aci": aci}


# --------------------------------------------------------------------------
# dash patterns
# --------------------------------------------------------------------------
def dash_pattern(dashes) -> tuple:
    """Normalise a PDF dash array to a tuple of point lengths, or ().

    PyMuPDF reports a dash array as a STRING in PDF syntax - `"[ 6 3 ] 0"` - not as
    a list, and `"[] 0"` for a solid path, while a path with no stroke of its own
    reports `None`.  All three mean something different and only the first is a
    dash.  (Parsing only lists, which an earlier version did, silently found no
    dashes at all: a string is not a list, so `[] 0` and `[ 6 3 ] 0` both came back
    empty, and the drawing's dashes were dropped with no error.)

    The bracket holds the array; the number after it is the phase, which is dropped
    because a DXF linetype carries no phase.  An array may also arrive already
    split, in which case the trailing element is the phase as well.
    """
    if not dashes:
        return ()
    if isinstance(dashes, str):
        body = re.search(r"\[([^\]]*)\]", dashes)
        if body is None:
            return ()
        seq = [float(v) for v in re.findall(r"-?\d*\.?\d+", body.group(1))]
    else:
        try:
            seq = [float(v) for v in dashes]
        except (TypeError, ValueError):
            return ()
    # Drop the phase: PDF writes it after the array, PyMuPDF appends it to a list.
    if seq and seq[-1] == 0.0 and len(seq) % 2 == 1:
        seq = seq[:-1]
    if len(seq) < 2 or len(seq) % 2 or all(abs(v) < 1e-9 for v in seq):
        return ()
    return tuple(round(v, 4) for v in seq)


def dash_linetype_name(pattern) -> str:
    """A stable DXF linetype name for one dash pattern.

    The name encodes the pattern so two runs on the same source always agree, and
    so a drawing opened in AutoCAD shows what the linetype actually is rather than
    an opaque `DASH_1`.
    """
    if not pattern:
        return "CONTINUOUS"
    return "PDF_" + "_".join("%g" % v for v in pattern).replace(".", "p").replace("-", "m")



def _rgb_int(rgb) -> int:
    r, g, b = rgb
    return (int(r) << 16) | (int(g) << 8) | int(b)


# Public spelling of the same conversion: callers outside this module (layer
# creation, tracing) need the packed 24-bit form AutoCAD writes in group code 420.
rgb_int = _rgb_int


def _aci_rgb_table() -> list:
    """(index, rgb) for every ACI index that has a definite colour.

    Built once from ezdxf's own table rather than transcribed, because a
    transcribed palette is a second source of truth that can drift.
    """
    global _ACI_TABLE
    try:
        return _ACI_TABLE
    except NameError:
        pass
    from ezdxf import colors as ezcolors
    table = []
    for idx in range(1, 256):
        if idx in (7, 255):        # 7 is "black or white by background", 255 unused
            continue
        try:
            rgb = tuple(ezcolors.aci2rgb(idx))
        except Exception:          # pragma: no cover - a hole in the table
            continue
        if rgb == (0, 0, 0):
            continue
        table.append((idx, rgb))
    _ACI_TABLE = table
    return table


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
