# -*- coding: utf-8 -*-
"""AutoCAD stage - plot and audit through the real application.

Why this stage earns its cost: the in-process DXF renderer does NOT draw
lineweights to physical scale.  Measured on this machine, it renders 0.18 mm and
0.25 mm lineweights as 1 px, 0.50 mm as 2 px and 1.00 mm as 4 px, against an
expected 2.1, 3.0, 5.9 and 11.8 px.  A drawing whose lines are all half as thick
as the source passes every look and fails an ink comparison for a reason nobody
can see - so the final fidelity claim is made from AutoCAD's own plot, not from a
library's approximation of one.

Everything here is verified rather than assumed:

* the exit code is not trusted, because accoreconsole returns 0 after a failed
  script; success is established from the output file existing and being
  non-trivially sized, plus a marker the script itself prints;
* the plot style table matters, because `monochrome.ctb` maps every colour to
  black and silently destroys a colour drawing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cadkit  # noqa: E402

MARKER_DONE = "CADKIT_MARKER"


def _accore(cfg: dict) -> str:
    home = cfg.get("acad_home") or r"D:\AutoCAD 2021"
    exe = os.path.join(home, "accoreconsole.exe")
    if not os.path.exists(exe):
        raise FileNotFoundError(f"accoreconsole.exe not found under {home!r}")
    return exe


def _run_script(exe: str, drawing: str, scr_path: str, lang: str = "zh-CN",
                timeout_s: int = 120) -> dict:
    """Run one accoreconsole script and report what actually happened.

    Three guards, because accoreconsole's exit code tells you nothing:

    * a bad keyword does not fail, it HANGS at full CPU, re-asking the same prompt
      forever - so the process is killed on a timeout, as a tree, or a stray
      accoreconsole keeps burning a core;
    * a missing script file exits 0 with no output at all, so the exit code
      cannot distinguish success from "nothing ran";
    * redirected stdout is UTF-16LE, so decoding it as UTF-8 yields mojibake in
      which the marker is unfindable.

    Success is therefore established from the script's own marker plus the
    caller's check that the output file exists and is big enough to be real.
    Standard input is deliberately left alone: attaching a stdin pipe makes even
    a known-good script spin forever.
    """
    creationflags = 0
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    # Output goes to a FILE, not a pipe.  A pipe stays open as long as any process
    # holding it lives, and accoreconsole starts children that inherit the handle -
    # so after a timeout the read never sees end-of-stream and the caller hangs a
    # second time, which is worse than the original timeout because it cannot be
    # escaped.  A file has no such coupling.
    log_path = scr_path + ".log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen([exe, "/i", drawing, "/s", scr_path, "/l", lang],
                                stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                creationflags=creationflags)
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill the whole tree: accoreconsole's children survive a plain
            # terminate and keep asking the same prompt at full CPU.
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:                                       # pragma: no cover
                proc.kill()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:            # pragma: no cover
                cadkit.eprint(f"warning: could not stop accoreconsole pid {proc.pid}")

    if os.path.exists(log_path):
        with open(log_path, "rb") as fh:
            raw = fh.read()
    else:
        raw = b""
    text = _decode_console(raw)
    return {
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "marker_seen": MARKER_DONE in text,
        "log": os.path.abspath(log_path),
        "tail": "\n".join(line for line in text.splitlines() if line.strip())[-4000:],
    }


def _decode_console(raw: bytes | None) -> str:
    """Decode accoreconsole output, which is UTF-16LE with NUL padding."""
    if not raw:
        return ""
    for encoding in ("utf-16-le", "utf-8"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # A wrong guess produces a high proportion of unprintable characters;
        # accept the decoding that yields mostly readable text.
        if text.count("\ufffd") <= len(text) * 0.01:
            return text.replace("\x00", "")
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def _output_ok(path: str, minimum_bytes: int = 4096) -> tuple[bool, int]:
    """A plot or save counts as done only if the file is really there.

    Deleting the target before the run and requiring it to reappear above a size
    floor is the only reliable success signal, because a script can print its
    completion marker after a plot that silently produced nothing.
    """
    if not os.path.exists(path):
        return False, 0
    size = os.path.getsize(path)
    return size >= minimum_bytes, size


def _scr_path(anchor: str, name: str) -> str:
    """Where a .scr and its log live.

    Kept out of the job root: a run leaves one .scr plus one .log per operation, and
    scattering them beside the deliverables makes the output directory hard to read
    at a glance.  They go into a `_scr` subdirectory beside whichever path anchors
    the operation - the drawing for an audit, the output for a conversion or plot.
    """
    return os.path.join(os.path.dirname(os.path.abspath(anchor)), "_scr", name)


def _write_scr(path: str, lines: list[str]) -> None:
    """Write a .scr file the way AutoCAD expects it.

    A script must be ANSI (on this machine: GBK) with CRLF line endings and NO
    BOM.  The same content written as UTF-8 does not fail - it HANGS, which is
    why this ran without a timeout for a while.  Paths inside the script use
    forward slashes so the backslashes are not read as escapes.
    """
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(path)))
    body = "\r\n".join(lines) + "\r\n"
    try:
        data = body.encode("mbcs")
    except UnicodeEncodeError:                      # pragma: no cover
        data = body.encode("gbk", errors="replace")
    with open(path, "wb") as fh:
        fh.write(data)


def _fwd(path: str) -> str:
    return os.path.abspath(path).replace("\\", "/")


def convert_dwg(dxf_path: str, dwg_path: str, cfg: dict, version: str = "2018") -> dict:
    exe = _accore(cfg)
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(dwg_path)))
    scr = _scr_path(dwg_path, "convert.scr")
    # FILEDIA/CMDDIA are already 0 inside accoreconsole, so setting them is
    # redundant; the script only has to issue the command.
    _write_scr(scr, [
        f'(command "_.SAVEAS" "{version}" "{_fwd(dwg_path)}")',
        f'(princ "\\n{MARKER_DONE} DWG\\n")',
        "(princ)",
    ])
    if os.path.exists(dwg_path):
        os.remove(dwg_path)
    result = _run_script(exe, dxf_path, scr)
    ok, size = _output_ok(dwg_path)
    result.update({"dwg": os.path.abspath(dwg_path), "bytes": size,
                   "ok": bool(ok and result["marker_seen"] and not result["timed_out"])})
    return result


# The answers below are POSITIONAL - AutoCAD consumes one token per prompt, in
# order.  Two sources disagree about one slot and the disagreement is unresolved:
#
#   * a verification pass on this machine measured the shading prompt (输入着色打印
#     设置, accepting 按显示(A)/传统线框(W)/传统隐藏(H)/视觉样式(V)/渲染(R)) as the
#     14th answer and recommended "A" there, because the older "N" is not a valid
#     keyword at all;
#   * but the "A" form HANGS on this machine (killed at 180 s, no output), while
#     the "N" form completes in about 6 s and writes the same 93 KB PDF that the
#     verified pass measured as byte-identical between the two variants.
#
# The working sequence is therefore kept, and the discrepancy is recorded rather
# than resolved by preference: an answer sequence that hangs is not an improvement,
# and the observed identity of the outputs means nothing is lost by keeping the
# form that runs.  Revisit only with a prompt-by-prompt transcript.
PLOT_ANSWERS = (
    "Y",       # 1  detailed plot configuration
    "Model",   # 2  layout
    "DWG To PDF.pc3",   # 3  output device
    None,      # 4  paper size, filled in by the caller
    "M",       # 5  paper units (millimetres)
    "L",       # 6  orientation (landscape)
    "N",       # 7  plot upside down
    "E",       # 8  plot area (extents)
    "F",       # 9  fit to paper
    "C",       # 10 plot with offsets, centred
    "Y",       # 11 plot with plot styles
    None,      # 12 plot style table (ctb), filled in by the caller
    "Y",       # 13 plot with lineweights
    "N",       # 14 shading prompt (see the note above)
    "A",       # 15 --
    None,      # 16 output filename, filled in by the caller
    "N",       # 17 save changes to page setup
    "Y",       # 18 proceed with plot
)


def export_pdf(drawing: str, pdf_path: str, cfg: dict) -> dict:
    """Export a PDF with the -EXPORT command.

    This is the alternative to -PLOT and it is the one that keeps working: -PLOT's
    answers are positional and a single wrong token makes it re-ask the same prompt
    forever, which is how one variant of the plot script hung for three minutes with
    no output.  -EXPORT asks five things - format, plot area, save settings, file
    name, finish - so there is far less to get wrong.

    The result is the ground truth for how AutoCAD actually draws the file: fonts,
    fills, lineweights and all, unlike any in-process renderer.
    """
    exe = _accore(cfg)
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(pdf_path)))
    scr = _scr_path(pdf_path, "export.scr")
    # No QUIT at the end.  Quitting asks "abandon all changes?" and any answer the
    # script does not have queued leaves the process waiting - measured, that turned
    # a successful export into a three-minute timeout with a finished 143 KB PDF
    # already on disk.  accoreconsole exits by itself once the script runs out.
    _write_scr(scr, [
        '(command "_.-EXPORT" "PDF" "E" "N" "' + _fwd(pdf_path) + '")',
        f'(princ "\\n{MARKER_DONE} EXPORT\\n")',
        "(princ)",
    ])
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    result = _run_script(exe, drawing, scr, timeout_s=240)
    ok, size = _output_ok(pdf_path)
    result.update({"pdf": os.path.abspath(pdf_path), "bytes": size,
                   "ok": bool(ok and not result["timed_out"])})
    return result


def plot_pdf(drawing: str, pdf_path: str, cfg: dict,
             paper: str = "ISO_full_bleed_A4_(210.00_x_297.00_MM)",
             ctb: str = "acad.ctb") -> dict:
    """Plot a colour PDF through AutoCAD's own plotter.

    `acad.ctb` plots object colours as drawn.  `monochrome.ctb` maps every one of
    the 255 ACI colours to black - verified both by inflating the table and by
    counting colour operators in the resulting PDF - so it turns a colour drawing
    into a black and white one while still reporting a successful plot.
    """
    exe = _accore(cfg)
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(pdf_path)))
    scr = _scr_path(pdf_path, "plot.scr")
    answers = [paper if a is None and i == 3 else
               ctb if a is None and i == 11 else
               _fwd(pdf_path) if a is None else a
               for i, a in enumerate(PLOT_ANSWERS)]
    _write_scr(scr, [
        '(command "_.-PLOT" ' + " ".join(f'"{a}"' for a in answers) + ")",
        f'(princ "\\n{MARKER_DONE} PDF\\n")',
        "(princ)",
    ])
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    result = _run_script(exe, drawing, scr, timeout_s=180)
    ok, size = _output_ok(pdf_path)
    result.update({"pdf": os.path.abspath(pdf_path), "bytes": size, "ctb": ctb,
                   "ok": bool(ok and result["marker_seen"] and not result["timed_out"])})
    return result


def audit(drawing: str, cfg: dict, out_dir: str | None = None) -> dict:
    """Ask AutoCAD what it actually loaded.

    This is the check that catches a file the pipeline wrote happily and AutoCAD
    cannot read: an entity count of zero, a stray entity far outside the intended
    extents, or a missing bigfont all show up here and nowhere else.
    """
    exe = _accore(cfg)
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(drawing)))
    scr = _scr_path(out_dir or drawing, "audit.scr")
    _write_scr(scr, [
        '(princ "\\nCADKIT_AUDIT_BEGIN\\n")',
        f'(princ (strcat "\\nCADKIT_ENTS " (itoa (sslength (ssget "_X")))))',
        '(princ (strcat "\\nCADKIT_EXT "'
        ' (rtos (car (getvar "EXTMIN")) 2 4) " "'
        ' (rtos (cadr (getvar "EXTMIN")) 2 4) " "'
        ' (rtos (car (getvar "EXTMAX")) 2 4) " "'
        ' (rtos (cadr (getvar "EXTMAX")) 2 4)))',
        # STYLE table: report each style's font and bigfont so a missing CJK
        # bigfont is visible here rather than as question marks in the plot.
        '(setq s (tblnext "STYLE" T))',
        '(while s'
        ' (princ (strcat "\\nCADKIT_STYLE " (cdr (assoc 2 s)) "|"'
        ' (cdr (assoc 3 s)) "|" (cdr (assoc 4 s))))'
        ' (setq s (tblnext "STYLE")))',
        f'(princ "\\n{MARKER_DONE} AUDIT\\n")',
        "(princ)",
    ])
    result = _run_script(exe, drawing, scr)
    parsed = {"entities": None, "extents": None, "styles": []}
    # Each value line arrives with a quoted copy of itself stuck on the end, e.g.
    #   CADKIT_ENTS 18"\nCADKIT_ENTS 18"
    # so the value is taken with a number pattern rather than by splitting on
    # whitespace: splitting yields '18"\nCADKIT_ENTS' and every conversion fails.
    number = re.compile(r"-?\d+(?:\.\d+)?")
    for line in result["tail"].splitlines():
        line = line.strip()
        # accoreconsole echoes each script line prefixed with the command prompt
        # ("命令:" in this localisation); a line that starts with the prompt is an
        # echo, anything else is output.
        if line.startswith("命令:") or line.startswith("Command:"):
            continue
        if line.startswith("CADKIT_ENTS ") and parsed["entities"] is None:
            found = number.search(line)
            if found:
                parsed["entities"] = int(float(found.group()))
        elif line.startswith("CADKIT_EXT ") and parsed["extents"] is None:
            found = [float(v) for v in number.findall(line)[:4]]
            if len(found) == 4:
                parsed["extents"] = found
        elif line.startswith("CADKIT_STYLE "):
            entry = line[len("CADKIT_STYLE "):].split('"')[0]
            if "|" in entry and entry not in parsed["styles"]:
                parsed["styles"].append(entry)
    result.update(parsed)
    result["ok"] = bool(result["marker_seen"] and (parsed["entities"] or 0) > 0)
    return result


def rasterise_pdf(pdf_path: str, png_path: str, dpi: int = 300) -> dict:
    import pymupdf
    doc = pymupdf.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
    cadkit.ensure_dirs(os.path.dirname(os.path.abspath(png_path)))
    pix.save(png_path)
    return {"png": os.path.abspath(png_path), "dpi": dpi,
            "size_px": [pix.width, pix.height],
            "page_rect_pt": [round(page.rect.width, 2), round(page.rect.height, 2)]}


def main() -> int:
    ap = argparse.ArgumentParser(description="AutoCAD conversion, plotting and auditing")
    ap.add_argument("drawing", help="DXF or DWG to operate on")
    ap.add_argument("-o", "--out-dir", required=True)
    ap.add_argument("--config")
    ap.add_argument("--dwg", action="store_true", help="convert to DWG")
    ap.add_argument("--plot", action="store_true", help="plot a colour PDF")
    ap.add_argument("--export", action="store_true",
                    help="export a PDF with -EXPORT; the robust path")
    ap.add_argument("--audit", action="store_true", help="report entities, extents, styles")
    ap.add_argument("--ctb", default="acad.ctb")
    ap.add_argument("--paper", default="ISO_full_bleed_A4_(210.00_x_297.00_MM)")
    ap.add_argument("--dpi", type=int, default=300, help="DPI for --rasterise")
    ap.add_argument("--rasterise", metavar="PDF", help="rasterise this PDF to PNG")
    ap.add_argument("--expect-extent-mm", nargs=4, type=float,
                    metavar=("X0", "Y0", "X1", "Y1"),
                    help="the sheet rectangle the drawing should occupy; an entity "
                         "outside it is geometry that landed in the wrong place")
    ap.add_argument("--tolerance-mm", type=float, default=0.5,
                    help="slack when checking --expect-extent-mm")
    ap.add_argument("--json")
    args = ap.parse_args()

    cfg = cadkit.load_config(args.config)
    cadkit.ensure_dirs(args.out_dir)
    stem = os.path.splitext(os.path.basename(args.drawing))[0]
    results = {}

    if args.dwg:
        results["dwg"] = convert_dwg(args.drawing, os.path.join(args.out_dir, stem + ".dwg"), cfg)
    if args.audit:
        results["audit"] = audit(args.drawing, cfg, args.out_dir)
        ext = results["audit"].get("extents")
        if ext and args.expect_extent_mm:
            x0, y0, x1, y1 = args.expect_extent_mm
            tol = args.tolerance_mm
            outside = (ext[0] < x0 - tol or ext[1] < y0 - tol
                       or ext[2] > x1 + tol or ext[3] > y1 + tol)
            results["audit"]["outside_expected_window"] = outside
            results["audit"]["expected_window"] = [x0, y0, x1, y1]
            if outside:
                results["audit"]["ok"] = False
    if args.plot:
        results["plot"] = plot_pdf(args.drawing, os.path.join(args.out_dir, stem + ".pdf"), cfg,
                                   paper=args.paper, ctb=args.ctb)
    if args.export:
        results["export"] = export_pdf(args.drawing,
                                       os.path.join(args.out_dir, stem + "_export.pdf"), cfg)
    if args.rasterise:
        results["raster"] = rasterise_pdf(args.rasterise,
                                         os.path.join(args.out_dir, stem + "_plot.png"),
                                         dpi=args.dpi)
        results["raster"]["ok"] = True

    cadkit.banner("AUTOCAD STAGE")
    for name, res in results.items():
        print(f"\n[{name}] ok={res.get('ok')}  returncode={res.get('returncode', '-')}")
        if name == "audit":
            print(f"   entities : {res.get('entities')}")
            print(f"   extents  : {res.get('extents')}")
            if res.get("outside_expected_window"):
                print(f"   !! geometry outside the expected window "
                      f"{res.get('expected_window')} - something is placed wrong")
            for style in res.get("styles", []):
                print(f"   style    : {style}")
        else:
            for key in ("dwg", "pdf", "png", "bytes", "size_px", "ctb"):
                if res.get(key) is not None:
                    print(f"   {key:9s}: {res[key]}")
        if not res.get("ok"):
            print("   --- output tail ---")
            for line in (res.get("tail") or "").splitlines()[-15:]:
                print("   " + line)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.json}")
    return 0 if all(r.get("ok") for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
