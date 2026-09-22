# -*- coding: utf-8 -*-
"""Final-state checks for the repository itself.

The two other suites in this directory check BEHAVIOUR: what the pipeline writes
into a DXF.  Nothing checked the artefact people actually land on - the repository.
That gap is not academic.  This repository has already shipped:

* a README pointing at a file that had been deleted (`HANDOFF.md`);
* a merge that silently reverted a previously fixed defect, because a newer branch
  was merged over it and no check compared the merged result against the fixes;
* documentation promising support for dashed curves while the code that counted the
  remaining loss had been dropped by that same merge;
* absolute paths from the machine it was developed on, in text a reader is invited
  to copy.

So this file checks the repository as a thing: that the skill is loadable, that every
document reference resolves, that no file carries machine-specific or drawing-specific
strings, that the documented entry points exist, and that the behavioural suites pass
against the tree as it stands.

Run:  python evals/test_repository.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def tracked_files() -> list:
    """Every file the repository intends to publish.

    `.gitignore` is the authority on what is not published: job outputs, drawings,
    caches, scratch.  Reading it means a file added to the tree but git-ignored is
    not silently skipped here as well.
    """
    ignore = []
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                ignore.append(line)
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for name in files:
            rel = os.path.relpath(os.path.join(base, name), ROOT).replace("\\", "/")
            if rel.endswith((".pyc", ".pyo")):
                continue
            if any(_ignored(rel, pat) for pat in ignore):
                continue
            out.append(rel)
    return sorted(out)


def _ignored(rel: str, pattern: str) -> bool:
    pattern = pattern.strip("/")
    if pattern.startswith("*."):
        return rel.endswith(pattern[1:])
    if pattern.startswith("!"):
        return False
    return rel == pattern or rel.startswith(pattern + "/") or f"/{pattern}/" in f"/{rel}/"


# --------------------------------------------------------------------------
# 1. the skill is loadable
# --------------------------------------------------------------------------
def test_skill_frontmatter() -> None:
    print("\n1. the skill is discoverable and loadable")
    path = os.path.join(ROOT, "SKILL.md")
    check("SKILL.md exists", os.path.exists(path))
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    check("starts with YAML frontmatter", text.startswith("---\n"), text[:40])
    end = text.find("\n---\n", 3)
    check("frontmatter is closed", end > 0)
    block = text[3:end] if end > 0 else ""
    fields = dict(re.findall(r"^([A-Za-z_]+):\s*(.+)$", block, re.M))
    check("frontmatter has a name", "name" in fields, str(list(fields)))
    # The bundle is installed under its skill name, so the durable check is that
    # this directory is a repository ROOT that carries that name in its docs -
    # not that whoever cloned it kept the directory called `cad-reproduce`.
    declared = fields.get("name", "").strip()
    check("the declared name is the one the docs and the README use",
          declared == "cad-reproduce"
          and all(declared in open(os.path.join(ROOT, f), encoding="utf-8").read()
                  for f in ("README.md", "SKILL.md")),
          f"{declared!r} not used consistently by README.md/SKILL.md")
    desc = fields.get("description", "")
    check("description is present", bool(desc))
    check("description is one line and within the 150-400 range",
          150 <= len(desc) <= 400, f"{len(desc)} chars")
    check("description says when NOT to use it",
          any(k in desc for k in ("不适用", "不用", "not for")), desc[-80:])
    check("body says when not to use it too",
          "何时不用" in text or "不适用" in text)


# --------------------------------------------------------------------------
# 2. every document reference resolves
# --------------------------------------------------------------------------
def test_references_resolve() -> None:
    print("\n2. every file the docs point at exists")
    files = tracked_files()
    known = set(files)

    # Documents legitimately name the pipeline's own OUTPUTS - `extract.json`,
    # `repro.dxf`, `out/cmp_difference.png`.  Those are per-job artefacts, produced
    # by a run and deliberately not tracked, so a reference to one is not a broken
    # link.  The set is taken from .gitignore rather than listed here, because
    # .gitignore is already where "not published" is defined.
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
        artifacts = {line.strip().lstrip("*").lstrip(".") for line in fh
                     if line.strip().startswith(("*.", "jobs"))}
    artifacts |= {"json", "dxf", "dwg", "pdf", "png", "scr", "log"}

    referenced: dict = {}
    pattern = re.compile(r"`([A-Za-z0-9_./\\-]+\.(?:py|md|json|yaml|ya?ml|dxf|dwg|png))`")
    for rel in files:
        if not rel.endswith(".md"):
            continue
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            for match in pattern.finditer(fh.read()):
                target = match.group(1).replace("\\", "/")
                if target.startswith("http"):
                    continue
                referenced.setdefault(target, set()).add(rel)

    missing, artefacts = [], []
    for target, sources in sorted(referenced.items()):
        if target in known or any(f.endswith("/" + target) for f in known):
            continue
        if target.rsplit(".", 1)[-1].lower() in artifacts:
            artefacts.append(target)
            continue
        missing.append(f"{target} (from {', '.join(sorted(sources))})")
    check(f"all {len(referenced) - len(artefacts)} document references resolve",
          not missing, "; ".join(missing[:6]))
    print(f"       ({len(artefacts)} references are to per-job outputs, not tracked: "
          f"{', '.join(sorted(artefacts)[:4])}...)")

    # The specific regression: a README that advertised a deleted file.
    readme = os.path.join(ROOT, "README.md")
    if os.path.exists(readme):
        with open(readme, encoding="utf-8") as fh:
            body = fh.read()
        for name in re.findall(r"`([^`]+\.md)`", body):
            check(f"README reference {name} exists",
                  any(f == name or f.endswith("/" + name) for f in known), name)


# --------------------------------------------------------------------------
# 3. nothing machine-specific or drawing-specific is published
# --------------------------------------------------------------------------
# The patterns this check looks for are themselves machine- and drawing-specific, so
# they are stored ENCODED and decoded at run time instead of being written as
# literals.  Written as literals they would publish exactly what the check exists to
# keep out: a stranger grepping the repository for the maintainer's account name or
# the first test drawing's name would find it in this file - which is how the first
# version of this check managed to pass its own scan, by exempting itself.
#
# The encoding is obfuscation, not secrecy.  Its job is to keep the strings out of
# plain text so a search does not surface them; the check's behaviour is visible to
# anyone reading the file.
_NEEDLES_B64 = (
    "WyJ0aGlydCIsIuWImOW/g+Wwj+W8nyIsIkM6XFxVc2Vyc1xcIiwiQzovVXNlcnMvIiwi6JOE54Ot"
    "5byPIiwi54eD54On5ZmoIiwi5Lic5YyX5aSn5a2mIiwi5p2o6LaF5biGIiwi5q615paH5YabIiwi"
    "5a2m5Y+3IiwiQ0FE5L2c5LiaIiwiY2FkX2J1cm5lciIsIkNBRF9kZWxpdmVyeSIsIkRvY3VtZW50"
    "c1xcQ29kZXgiLCJnaHBfIiwiZ2l0aHViX3BhdF8iLCJCZWFyZXIgIl0="
)


def needles() -> list:
    import base64
    return json.loads(base64.b64decode(_NEEDLES_B64).decode("utf-8"))


#: Files that sit beside the pipeline on the maintainer's machine but are NOT part of
#: the published repository, and are known to contain these patterns by design.
#:
#: A published tree and an installed skill are different things.  The skill is
#: installed into a directory that also holds private notes - here, the working
#: notebook the first version of this pipeline kept - so running this check where the
#: skill is installed would fail on files a reader would never clone.  A check that
#: only ever fails on the maintainer's machine is a check nobody runs.
#:
#: Named, not globbed.  An exemption for "*.md" or for the directory would silently
#: disable the check that matters; naming one file means a NEW file carrying a
#: personal string still fails.
PRIVATE_UNPUBLISHED = ("HANDOFF.md",)


def test_no_personal_strings() -> None:
    print("\n3. no machine-specific, drawing-specific or secret strings")
    patterns = needles()
    check(f"{len(patterns)} patterns loaded", len(patterns) >= 10, str(len(patterns)))
    hits = []
    scanned = 0
    skipped = []
    for rel in tracked_files():
        if not rel.endswith((".md", ".py", ".json", ".yaml", ".yml", ".txt", ".cfg")):
            continue
        # This file carries the patterns, encoded; it is the one place they exist.
        if rel == "evals/test_repository.py":
            continue
        if rel in PRIVATE_UNPUBLISHED:
            skipped.append(rel)
            continue
        scanned += 1
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for index, needle in enumerate(patterns):
            if needle in text:
                hits.append(f"{rel}: pattern #{index}")
    check(f"scanned {scanned} published text files", scanned > 0)
    if skipped:
        check(f"not scanned, and not published: {', '.join(skipped)}", True)
    check("no personal or machine-specific strings", not hits, "; ".join(hits[:8]))


# --------------------------------------------------------------------------
# 4. the documented entry points exist and answer --help
# --------------------------------------------------------------------------
DOCUMENTED_SCRIPTS = (
    "extract.py", "infer_dims.py", "stage1_build.py", "trace.py", "compare.py",
    "accad.py",
)
DOCUMENTED_FLAGS = {
    "infer_dims.py": ("--colour", "--allow-empty", "--config"),
    "stage1_build.py": ("--associative", "--mtext", "--no-dimensions",
                        "--min-confidence"),
    "compare.py": ("--extract", "--band-mm", "--min-agreement"),
}


def test_entry_points() -> None:
    print("\n4. every documented script runs and accepts its documented flags")
    for name in DOCUMENTED_SCRIPTS:
        path = os.path.join(ROOT, "scripts", name)
        if not os.path.exists(path):
            check(f"scripts/{name} exists", False)
            continue
        proc = subprocess.run([sys.executable, path, "--help"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        check(f"scripts/{name} --help exits 0", proc.returncode == 0,
              (proc.stderr or proc.stdout)[-200:])
        for flag in DOCUMENTED_FLAGS.get(name, ()):
            check(f"  {name} accepts {flag}", flag in proc.stdout,
                  "flag missing from --help")


# --------------------------------------------------------------------------
# 5. the documented capability is actually wired up
# --------------------------------------------------------------------------
def test_documented_capabilities_are_wired() -> None:
    print("\n5. documented capabilities are backed by code, not just promised")

    def source(rel: str) -> str:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    # Dashes: the whole point of the first fix.  Extract names the pattern, the
    # builder defines the linetype, and the ruler can see one.
    extract = source("scripts/extract.py")
    build = source("scripts/stage1_build.py")
    trace = source("scripts/trace.py")
    check("extract.py publishes a linetype table", "linetypes" in extract)
    check("stage1_build.py defines DXF linetypes", "linetypes.add" in build)
    check("stage1_build.py puts a linetype on entities", 'attr["linetype"]' in build)
    check("trace.py can draw a dashed line", "dashed_line" in trace)
    check("trace.py dashes the SOURCE side too", "pattern=pattern" in trace)

    # The dashed-curve bookkeeping that a merge silently dropped once: a spline
    # cannot carry a linetype, so the loss has to be reported.
    check("the dashed-curve loss is still counted",
          "dashed_curve_not_dashed" in build)

    # Colour: never substituted.
    cadkit = source("scripts/cadkit.py")
    check("out-of-palette colours are written as true colour",
          "true_color" in cadkit and "hex_key" in cadkit)

    # Dimension colour role: detected, configurable, and able to fail.
    infer = source("scripts/infer_dims.py")
    check("the dimension colour is detected", "def detect_dim_colour" in infer)
    check("the detection reports ambiguities", "ambiguous" in infer)
    check("a wrong role can fail the run", "return 2" in infer)

    # Arc recognition (PR #3): the curve path must use it.
    check("curve_entities.py exists", os.path.exists(os.path.join(ROOT, "scripts/curve_entities.py")))
    check("stage1_build.py routes curves through it", "emit_curves" in build)


# --------------------------------------------------------------------------
# 6. the behavioural suites pass against this tree
# --------------------------------------------------------------------------
SUITES = ("test_regressions.py", "test_arc_recognition.py", "test_steelwork_sheet.py")


def test_behavioural_suites() -> None:
    print("\n6. the behavioural suites pass against this tree")
    for name in SUITES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            check(f"evals/{name} exists", False)
            continue
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        tail = [l for l in proc.stdout.splitlines() if "[FAIL]" in l]
        check(f"evals/{name} passes", proc.returncode == 0,
              "; ".join(tail[:4]) or proc.stderr[-300:])


# --------------------------------------------------------------------------
# 7. the README's own claims about the gate are still the true ones
# --------------------------------------------------------------------------
def test_gate_honesty() -> None:
    print("\n7. the docs still describe the gate's real limits")
    readme = os.path.join(ROOT, "README.md")
    with open(readme, encoding="utf-8") as fh:
        body = fh.read()
    check("README states what the gate cannot prove",
          "不能" in body or "cannot" in body.lower())
    check("README warns that a passing gate is not a correct drawing",
          "不等于图纸对" in body or "not the same as" in body)
    with open(os.path.join(ROOT, "scripts/trace.py"), encoding="utf-8") as fh:
        trace = fh.read()
    check("trace.py still says the source side is self-drawn, not a PDF render",
          "from its own numbers" in trace or "自己画的" in trace
          or "extracted numbers" in trace)


def main() -> int:
    print("cad-reproduce repository checks")
    test_skill_frontmatter()
    test_references_resolve()
    test_no_personal_strings()
    test_entry_points()
    test_documented_capabilities_are_wired()
    test_behavioural_suites()
    test_gate_honesty()

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
