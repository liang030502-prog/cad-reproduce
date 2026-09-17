# -*- coding: utf-8 -*-
"""Regression tests for the defects fixed in this branch.

Three of the four defects this pipeline had were invisible to its own self-check,
so each one gets a test that fails on the OLD behaviour and passes on the new:

1. **Dashes were extracted and then dropped.**  Every centre line, hidden line and
   section boundary came out solid.  A synthetic sheet with one dashed line is run
   through extract -> infer -> build, and the resulting DXF is read back and checked
   for a real linetype on a real entity.

2. **A colour outside the six named ones was silently replaced by black.**  A
   synthetic sheet with an ACI-5 blue line must come back blue: an exact-ACI entity,
   a true colour only when no index is exact, and never black.

3. **A wrong dimension colour role produced zero dimensions and reported success.**
   `infer_dims` must fail (exit 2) when the colour it is told to use carries no
   dimension-like geometry, unless it is explicitly told to accept an empty result.

4. **The dimension colour was a literal in two places.**  `trace.py` drew every
   generated dimension in green regardless of the detected role.

Run:  python evals/test_regressions.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
CONFIG = os.path.join(os.path.dirname(HERE), "cad-reproduce.yaml")
sys.path.insert(0, SCRIPTS)

import cadkit  # noqa: E402

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def run(*argv, expect: int = 0) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if proc.returncode != expect:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:], file=sys.stderr)
    assert proc.returncode == expect, f"{argv[0]} exited {proc.returncode}, want {expect}"
    return proc


# Everything the tests create starts with one of these, so cleanup can never touch
# a file that belongs to the repository.
SCRATCH_PREFIXES = ("dashed", "dim", "dimred", "gen")


def _clean_scratch(folder: str) -> None:
    for name in os.listdir(folder):
        if not name.startswith(SCRATCH_PREFIXES):
            continue
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# synthetic sources
# --------------------------------------------------------------------------
def make_dashed_blue_pdf(path: str) -> None:
    """A sheet with one dashed #0000FF line, one solid black line, and a box.

    Deliberately tiny: the point is that a named colour and a dash array survive
    the whole pipeline, not that a drawing looks like anything.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=200, height=150)
    page.draw_line(pymupdf.Point(20, 40), pymupdf.Point(180, 40),
                   color=(0, 0, 1), width=0.5, dashes="[6 3] 0")
    page.draw_line(pymupdf.Point(20, 60), pymupdf.Point(180, 60),
                   color=(0, 0, 0), width=0.5)
    page.draw_rect(pymupdf.Rect(20, 80, 180, 130), color=(0, 0, 0), width=0.5)
    doc.save(path)
    doc.close()


def make_dimension_pdf(path: str, dim_colour=(0, 1, 0)) -> None:
    """A sheet with a drawn linear dimension in `dim_colour` and a value label.

    The dimension is drawn as an ordinary dimension looks on paper: a dimension
    line with an arrow gap, two extension lines, and the number as text.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=200, height=150)
    page.draw_rect(pymupdf.Rect(10, 10, 190, 140), color=(0, 0, 0), width=0.5)
    y_line, y_feature = 40.0, 70.0
    # One dimension line with an arrow gap in the middle: the gap is narrower than
    # MAX_ARROW_GAP, so the two pieces merge back into a single span and the value
    # label sits at that span's midpoint - which is how the inference reads a chain.
    page.draw_line(pymupdf.Point(50, y_line), pymupdf.Point(98, y_line),
                   color=dim_colour, width=0.5)
    page.draw_line(pymupdf.Point(102, y_line), pymupdf.Point(150, y_line),
                   color=dim_colour, width=0.5)
    for x in (50.0, 150.0):
        page.draw_line(pymupdf.Point(x, y_line), pymupdf.Point(x, y_feature),
                       color=dim_colour, width=0.5)
    page.insert_text(pymupdf.Point(93, y_line - 5.5), "100", fontsize=8.0,
                     fontname="helv", color=dim_colour)
    doc.save(path)
    doc.close()


def dxf_entities(path: str):
    import ezdxf
    doc = ezdxf.readfile(path)
    return doc, list(doc.modelspace())


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_dashes_survive(tmp: str) -> None:
    print("\n1. dash patterns reach the DXF as real linetypes")
    pdf = os.path.join(tmp, "dashed.pdf")
    make_dashed_blue_pdf(pdf)
    ex = os.path.join(tmp, "dashed_extract.json")
    dims = os.path.join(tmp, "dashed_dims.json")
    out = os.path.join(tmp, "dashed.dxf")

    run(os.path.join(SCRIPTS, "extract.py"), pdf, "-o", ex)
    with open(ex, encoding="utf-8") as fh:
        data = json.load(fh)
    patterns = data["linetypes"]["patterns"]
    named = [k for k in patterns if k != "CONTINUOUS"]
    check("a non-continuous linetype was extracted", bool(named), str(list(patterns)))
    check("the dash pattern is 6/3 pt",
          any(p["pattern_pt"] == [6.0, 3.0] for p in patterns.values()),
          str({k: v["pattern_pt"] for k, v in patterns.items()}))
    check("dashed_paths is counted in stats", data["stats"]["dashed_paths"] >= 1,
          str(data["stats"]["dashed_paths"]))

    run(os.path.join(SCRIPTS, "infer_dims.py"), ex, "-o", dims)
    run(os.path.join(SCRIPTS, "stage1_build.py"), ex, dims, "-o", out)

    doc, entities = dxf_entities(out)
    ltypes = {lt.dxf.name for lt in doc.linetypes}
    dashed_name = named[0]
    check(f"linetype {dashed_name!r} is defined in the DXF", dashed_name in ltypes,
          str(sorted(ltypes)))
    used = [e for e in entities if e.dxf.get("linetype", "BYLAYER") == dashed_name]
    check("at least one entity carries the dashed linetype", bool(used),
          f"{len(used)} entities")
    solid = [e for e in entities if e.dxf.get("linetype", "BYLAYER") == "BYLAYER"]
    check("the undashed lines stay continuous", bool(solid), f"{len(solid)} entities")

    # The linetype must describe the source's pattern, not an approximation of it.
    # A DXF pattern's first element is the total length of the pattern; the dashes
    # and gaps follow as group-code-49 elements, positive for ink and negative for a
    # gap.  (ezdxf also writes a group-code-74 marker after each one.)
    lt = doc.linetypes.get(dashed_name)
    pattern = [round(float(tag.value), 4) for tag in lt.pattern_tags.tags
               if tag.code == 49]
    ink = [v for v in pattern if v > 0]
    gaps = [v for v in pattern if v < 0]
    check("the DXF linetype keeps the source's dash and gap lengths",
          ink == [round(6.0 * cadkit.PT2MM, 4)]
          and gaps == [round(-3.0 * cadkit.PT2MM, 4)],
          f"dash {ink} gap {gaps} in drawing units")


def test_unnamed_colour_is_not_black(tmp: str) -> None:
    print("\n2. a colour outside the named six is not silently replaced")
    pdf = os.path.join(tmp, "dashed.pdf")
    ex = os.path.join(tmp, "dashed_extract.json")
    dims = os.path.join(tmp, "dashed_dims.json")
    out = os.path.join(tmp, "dashed.dxf")

    with open(ex, encoding="utf-8") as fh:
        data = json.load(fh)
    check("blue is a named colour", cadkit.rgb_to_key((0, 0, 1)) == "#0000FF",
          str(cadkit.rgb_to_key((0, 0, 1))))
    check("a near-miss blue is NOT coerced to cyan",
          cadkit.rgb_to_key((0x1E / 255, 0x90 / 255, 1.0)) == "#1E90FF",
          str(cadkit.rgb_to_key((0x1E / 255, 0x90 / 255, 1.0))))
    check("the blue path kept its exact value",
          "#0000FF" in {p["color"] for p in data["paths"]},
          str(sorted({p["color"] for p in data["paths"]})))

    doc, entities = dxf_entities(out)
    true_colour = [e for e in entities if e.dxf.get("true_color", 256) != 256]
    check("the blue geometry is written as a true colour", bool(true_colour),
          f"{len(true_colour)} entities")
    check("no entity was painted black by the palette",
          all(e.dxf.get("true_color", 256) in (256, 0x0000FF) or
              e.dxf.get("color", 256) != 250 or e.dxf.get("true_color") is not None
              for e in entities),
          "an entity carrying ACI 250 with no true colour would be the old defect")
    blues = [e for e in entities if e.dxf.get("true_color") == 0x0000FF]
    check("the blue entities carry exactly #0000FF", bool(blues), f"{len(blues)}")

    # And the raster palette must contain the source's own colour, so the colour
    # gate compares the drawing rather than the palette.
    palette = cadkit.raster_palette(data)
    check("the raster palette includes the source's blue",
          "#0000FF" in [n for n, _rgb in palette],
          str([n for n, _rgb in palette]))


def test_wrong_dimension_role_fails(tmp: str) -> None:
    print("\n3. a wrong dimension colour role fails loudly")
    pdf = os.path.join(tmp, "dim_green.pdf")
    make_dimension_pdf(pdf, dim_colour=(0, 1, 0))
    ex = os.path.join(tmp, "dim_extract.json")
    run(os.path.join(SCRIPTS, "extract.py"), pdf, "-o", ex)

    with open(ex, encoding="utf-8") as fh:
        data = json.load(fh)
    import infer_dims

    detection = infer_dims.detect_dim_colour(data)
    check("the dimension colour is detected as green",
          detection["colour"] == "green", str(detection["colour"]))

    # Asking for a role the geometry does not support must fail, not return nothing.
    proc = run(os.path.join(SCRIPTS, "infer_dims.py"), ex,
               "-o", os.path.join(tmp, "dim_wrong.json"), "--colour", "black",
               expect=2)
    check("a wrong role exits 2", proc.returncode == 2, str(proc.returncode))
    check("the failure says the role is wrong",
          "dimension colour role is wrong" in proc.stderr, proc.stderr[-400:])
    check("the failure names the detected role",
          "green" in proc.stderr, proc.stderr[-400:])

    # And --allow-empty is the documented way to accept a sheet with no dimensions.
    run(os.path.join(SCRIPTS, "infer_dims.py"), ex,
        "-o", os.path.join(tmp, "dim_wrong2.json"), "--colour", "black",
        "--allow-empty")

    # The right role still works, and says which colour it used.
    good = os.path.join(tmp, "dim_good.json")
    run(os.path.join(SCRIPTS, "infer_dims.py"), ex, "-o", good)
    with open(good, encoding="utf-8") as fh:
        dims = json.load(fh)
    check("the detected role is recorded in dims.json",
          dims["dimension_colour"] == "green", str(dims["dimension_colour"]))
    check("one dimension was inferred",
          len([p for p in dims["proposals"] if p["value"] == "100"]) == 1,
          json.dumps(dims["proposals"], ensure_ascii=False)[:400])


def test_dimension_colour_is_not_a_literal(tmp: str) -> None:
    print("\n4. the traced dimension uses the detected colour")
    pdf = os.path.join(tmp, "dim_red.pdf")
    make_dimension_pdf(pdf, dim_colour=(1, 0, 0))
    ex = os.path.join(tmp, "dimred_extract.json")
    dims_path = os.path.join(tmp, "dimred_dims.json")
    run(os.path.join(SCRIPTS, "extract.py"), pdf, "-o", ex)
    run(os.path.join(SCRIPTS, "infer_dims.py"), ex, "-o", dims_path)

    with open(ex, encoding="utf-8") as fh:
        data = json.load(fh)
    with open(dims_path, encoding="utf-8") as fh:
        dims = json.load(fh)
    check("the red dimension machinery is detected as red",
          dims["dimension_colour"] == "red", str(dims["dimension_colour"]))

    import trace as trace_mod
    out_png = os.path.join(tmp, "gen.png")
    trace_mod.trace_generated(data, dims, out_png, cadkit.load_config(CONFIG), dpi=150)
    from PIL import Image
    import numpy as np
    arr = np.asarray(Image.open(out_png).convert("RGB"), dtype=np.uint8)
    red = int(((arr[:, :, 0] > 180) & (arr[:, :, 1] < 100) & (arr[:, :, 2] < 100)).sum())
    green = int(((arr[:, :, 1] > 180) & (arr[:, :, 0] < 100) & (arr[:, :, 2] < 100)).sum())
    check("the traced dimension is red, not green", red > 0 and green == 0,
          f"red={red} green={green}")


def main() -> int:
    print("cad-reproduce regression tests")
    # Scratch files are written next to this script, not into a fresh sub-directory
    # and not into %TEMP%.  On Windows a sandboxed shell cannot always write to
    # %TEMP%, and a directory created by the test itself is not writable either
    # there - a test that fails for either reason teaches nothing.  Everything
    # created here is removed on the way out.
    tmp = HERE
    try:
        _clean_scratch(tmp)
        make_dashed_blue_pdf(os.path.join(tmp, "dashed.pdf"))
        test_dashes_survive(tmp)
        test_unnamed_colour_is_not_black(tmp)
        test_wrong_dimension_role_fails(tmp)
        test_dimension_colour_is_not_a_literal(tmp)
    finally:
        _clean_scratch(tmp)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
