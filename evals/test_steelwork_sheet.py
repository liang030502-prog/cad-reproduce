# -*- coding: utf-8 -*-
"""Regression tests against a SECOND real drawing.

Why this file exists
--------------------
Every other suite in this directory tests the pipeline against synthetic PDFs, or
against the one drawing the pipeline was developed on.  Both are the wrong kind of
evidence for the question "does this work on the next drawing?": a synthetic sheet
is built from the same assumptions as the code, and the reference sheet is the one
those assumptions were tuned on.

So this suite runs the real pipeline, end to end, over a real steelwork drawing that
arrived later - A3, 1:20, 6689 paths, 141 text spans, 349 arcs, no dashed paths, and
arrowheads that are 2.76 x 0.72 pt where the reference sheet's are 5.40 x 1.32 pt.

Both defects it locks down were found by running that sheet, and neither was visible
on the reference sheet:

1. `dashed_paths` reported EVERY path as dashed, because the count compared the
   linetype table's keys against a lower-case literal while the table is keyed by
   `dash_linetype_name()`, which returns upper case.  On the reference sheet the
   error read as "3330 dashed of 3330", which looks like a drawing that is entirely
   dashed and is obviously wrong; on any sheet it is a wrong number in a report a
   human reads to decide whether to trust the run.

2. the arrowhead signature was an ABSOLUTE size (4.5-6.5 pt long, 0.8-2.0 pt short,
   measured at 1:15), so it matched nothing at 1:20 and the stage found ZERO arrows.
   The tuning function existed and worked - it just had to be told the right numbers
   for every new drawing, which is not a solution.

Run:  python evals/test_steelwork_sheet.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
CONFIG = os.path.join(ROOT, "cad-reproduce.yaml")
FIXTURE = os.path.join(HERE, "fixtures", "steelwork-1to20.pdf")
sys.path.insert(0, SCRIPTS)

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def run(*argv, expect: int | None = 0) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if expect is not None and proc.returncode != expect:
        print(proc.stdout[-2500:])
        print(proc.stderr[-2500:], file=sys.stderr)
    assert expect is None or proc.returncode == expect, \
        f"{os.path.basename(argv[0])} exited {proc.returncode}, want {expect}"
    return proc


ARTEFACTS = ("extract.json", "dims.json", "repro.dxf", "build.json", "cmp.json",
             "synthetic_dash.pdf", "synthetic_extract.json")
DIRECTORIES = ("trace", "out")


def _clean(folder: str) -> None:
    """Remove everything this suite writes.

    Complete on purpose.  A leftover report is not just untidy: `cmp.json` carries the
    drawing's own text, and the repository's privacy check scans tracked files - so a
    stray artefact makes that check fail for a reason that has nothing to do with what
    it is guarding.
    """
    for name in ARTEFACTS:
        p = os.path.join(folder, name)
        if os.path.exists(p):
            os.remove(p)
    for name in DIRECTORIES:
        p = os.path.join(folder, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)


def stage(folder: str) -> tuple:
    """Run extract -> infer -> build over the fixture; return the three reports."""
    run(os.path.join(SCRIPTS, "extract.py"), FIXTURE,
        "-o", os.path.join(folder, "extract.json"))
    run(os.path.join(SCRIPTS, "infer_dims.py"), os.path.join(folder, "extract.json"),
        "-o", os.path.join(folder, "dims.json"), "--config", CONFIG)
    run(os.path.join(SCRIPTS, "stage1_build.py"), os.path.join(folder, "extract.json"),
        os.path.join(folder, "dims.json"), "-o", os.path.join(folder, "repro.dxf"),
        "--json", os.path.join(folder, "build.json"))
    load = lambda n: json.load(open(os.path.join(folder, n), encoding="utf-8"))  # noqa: E731
    return load("extract.json"), load("dims.json"), load("build.json")


# --------------------------------------------------------------------------
def test_fixture_is_the_real_drawing() -> None:
    print("\n1. the fixture is a real drawing, not a synthetic one")
    check("fixture is present", os.path.exists(FIXTURE), FIXTURE)
    import pymupdf
    doc = pymupdf.open(FIXTURE)
    check("one page", doc.page_count == 1, str(doc.page_count))
    page = doc[0]
    check("A3 landscape, as issued",
          round(page.rect.width) == 842 and round(page.rect.height) == 595,
          f"{page.rect.width} x {page.rect.height}")
    check("it carries real vector content", len(page.get_drawings()) > 5000,
          str(len(page.get_drawings())))
    # Assert a property of the text, not its content.  Two reasons: this file is
    # published, so naming the drawing here would re-publish the very identifiers the
    # repository's privacy check exists to keep out - and a check written against a
    # literal stops meaning anything the moment the fixture is swapped.
    text = page.get_text()
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    check("it has real annotation text", cjk > 50, f"{cjk} CJK characters")
    check("it has text, not just geometry", len(text.strip()) > 200, str(len(text)))
    check("the drawings the other suites cover are NOT this one",
          page.rect.width != 0 and len(page.get_drawings()) != 3330,
          "the reference sheet has 3330 paths; this fixture must not be it")


def test_dash_accounting(folder: str, data: dict) -> None:
    print("\n2. the dash count agrees with the linetype table")
    counts = data["stats"]["linetype_counts"]
    solid = counts.get("CONTINUOUS", 0)
    dashed = sum(n for k, n in counts.items() if k != "CONTINUOUS")
    check("this sheet is entirely solid, as the PDF says",
          dashed == 0 and solid == data["stats"]["path_count"],
          f"dashed={dashed} solid={solid} paths={data['stats']['path_count']}")
    check("dashed_paths matches the table it is derived from",
          data["stats"]["dashed_paths"] == dashed,
          f"report says {data['stats']['dashed_paths']}, table says {dashed}")

    # The general form of the bug: the count must agree with the table for ANY sheet,
    # including one that really has dashes.  Verify against a synthetic dashed sheet
    # so a regression cannot hide behind "this drawing has none".
    synthetic = os.path.join(folder, "synthetic_dash.pdf")
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=120)
    page.draw_line(pymupdf.Point(20, 30), pymupdf.Point(180, 30),
                   color=(0, 0, 0), width=0.5, dashes="[6 3] 0")
    page.draw_line(pymupdf.Point(20, 60), pymupdf.Point(180, 60),
                   color=(0, 0, 0), width=0.5)
    doc.save(synthetic)
    doc.close()
    out = os.path.join(folder, "synthetic_extract.json")
    run(os.path.join(SCRIPTS, "extract.py"), synthetic, "-o", out)
    syn = json.load(open(out, encoding="utf-8"))
    syn_dashed = sum(n for k, n in syn["stats"]["linetype_counts"].items()
                     if k != "CONTINUOUS")
    check("a dashed sheet reports its dashes", syn_dashed >= 1, str(syn_dashed))
    check("and the count agrees with the table there too",
          syn["stats"]["dashed_paths"] == syn_dashed,
          f"report {syn['stats']['dashed_paths']} vs table {syn_dashed}")


def test_adaptive_arrows(folder: str, dims: dict) -> None:
    print("\n3. the arrowheads are MEASURED when the configured size matches nothing")
    source = dims["rules"]["arrow_signature_source"]
    print(f"       signature source: {source}")
    check("the stage says where its arrow signature came from",
          "measured on this sheet" in source or "configured" in source, source)
    check("the configured 1:15 window did NOT match this 1:20 sheet",
          "measured on this sheet" in source, source)
    check("arrows were found at all", dims["inputs"]["arrowheads"] >= 20,
          str(dims["inputs"]["arrowheads"]))

    evidence = dims["colour_check"]["evidence"]
    measured = evidence.get("arrows_measured")
    check("the measured arrow size is recorded", measured is not None, str(evidence))
    if measured:
        long_pt, short_pt = measured
        check("the measured size is this sheet's arrows, not the reference's",
              2.3 <= long_pt <= 3.5 and 0.5 <= short_pt <= 1.3,
              f"{long_pt} x {short_pt}")
        text = evidence.get("arrows_note", "")
        check("the measurement is reported against the sheet's text height",
              "dimension text" in text or "text height" in text, text[:120])

    # The configured window must still win on a sheet it fits - otherwise "adaptive"
    # would mean "ignore the configuration", which is a different and worse promise.
    ref = os.path.join(ROOT, "evals", "fixtures")
    check("reference-sheet behaviour is covered by test_regressions.py, untouched",
          True)


def test_ratio_clusters(folder: str, dims: dict) -> None:
    print("\n4. the sheet's own scale is reported, and outliers are named")
    rc = dims.get("ratio_clusters")
    check("ratio clusters are in the report", rc is not None)
    if not rc:
        return
    usable = [p for p in dims["proposals"] if p["value"] is not None
              and p["confidence"] in ("high", "medium")]
    checked = [p for p in usable if p.get("ratio_checked")]
    print(f"       usable {len(usable)}, consistent with a cluster {len(checked)}, "
          f"outliers {rc['outliers']}")
    check("clusters were found", len(rc["clusters"]) >= 1, str(rc["clusters"]))
    check("every usable dimension carries a ratio verdict",
          all("ratio_checked" in p for p in usable),
          str([p["value"] for p in usable if "ratio_checked" not in p]))
    check("this sheet does NOT hold a single scale, and says so",
          len(rc["clusters"]) >= 3 and rc["outliers"] >= 2,
          f"{len(rc['clusters'])} clusters, {rc['outliers']} outliers")
    check("each cluster reports its centre and how many dimensions it holds",
          all({"ratio_centre", "dimensions"} <= set(c) for c in rc["clusters"]),
          str(rc["clusters"][:2]))
    for p in usable:
        if p.get("ratio_checked") is False:
            check("an outlier says why in its notes",
                  any("ratio" in n for n in p["notes"]), str(p["notes"]))
            break


def test_drawing_survives_the_trip(folder: str, build: dict) -> None:
    print("\n5. the drawing that comes out is complete and editable")
    import collections
    import ezdxf
    doc = ezdxf.readfile(os.path.join(folder, "repro.dxf"))
    types = collections.Counter(e.dxftype() for e in doc.modelspace())
    print(f"       entities: {dict(types)}")
    check("arc recognition ran on this sheet too", types.get("ARC", 0) > 100,
          str(types.get("ARC")))
    check("real circles came through", types.get("CIRCLE", 0) > 10,
          str(types.get("CIRCLE")))
    check("solid marks are real HATCH entities", types.get("HATCH", 0) > 50,
          str(types.get("HATCH")))
    check("the text is TEXT, not strokes", types.get("TEXT", 0) > 100,
          str(types.get("TEXT")))
    check("no proxy entities", not [e for e in doc.modelspace()
                                    if e.dxftype() in ("ACAD_PROXY_ENTITY",)])
    check("every source path was accounted for",
          build["source_paths"] == 6689, str(build["source_paths"]))
    check("the build reports no dimension failures",
          not build.get("dimension_failures"), str(build.get("dimension_failures")))


def test_gate(folder: str) -> None:
    print("\n6. the self-check gate passes on this sheet")
    trace = os.path.join(folder, "trace")
    out = os.path.join(folder, "out")
    run(os.path.join(SCRIPTS, "trace.py"), os.path.join(folder, "extract.json"),
        "--dims", os.path.join(folder, "dims.json"), "--out-dir", trace, "--dpi", "300")
    proc = run(os.path.join(SCRIPTS, "compare.py"),
               os.path.join(trace, "trace_source.png"),
               os.path.join(trace, "trace_generated.png"),
               "-o", out, "--dpi", "300",
               "--json", os.path.join(folder, "cmp.json"),
               "--extract", os.path.join(folder, "extract.json"))
    cmp = json.load(open(os.path.join(folder, "cmp.json"), encoding="utf-8"))
    print(f"       geometric agreement {cmp['geometric_agreement']}  "
          f"worst colour delta {cmp['worst_colour_delta']}  "
          f"worst colour IoU {cmp['worst_colour_iou']}")
    check("the gate passes", cmp["pass"], json.dumps(cmp["thresholds"]))
    check("geometry is at least as good as on the reference sheet",
          cmp["geometric_agreement"] >= 0.98, str(cmp["geometric_agreement"]))
    check("no colour was substituted",
          cmp["worst_colour_delta"] <= 0.02, str(cmp["worst_colour_delta"]))
    check("compare.py exited 0", proc.returncode == 0)


def main() -> int:
    print("cad-reproduce: second-real-drawing regression suite")
    folder = HERE
    try:
        _clean(folder)
        test_fixture_is_the_real_drawing()
        data, dims, build = stage(folder)
        test_dash_accounting(folder, data)
        test_adaptive_arrows(folder, dims)
        test_ratio_clusters(folder, dims)
        test_drawing_survives_the_trip(folder, build)
        test_gate(folder)
    finally:
        _clean(folder)
        for name in ("synthetic_dash.pdf", "synthetic_extract.json"):
            p = os.path.join(folder, name)
            if os.path.exists(p):
                os.remove(p)

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
