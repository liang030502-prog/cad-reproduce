# -*- coding: utf-8 -*-
"""Regression tests for arc recognition (the L1-L4 curve path).

WHY THIS IS A SEPARATE FILE
---------------------------
`test_regressions.py` covers defects that were already visible in the pipeline's
own output.  This one covers a capability the PDF path did not have: a PDF has no
arc primitive, so every circle and arc in it is written as cubic Beziers, and the
PDF path used to emit those as SPLINEs.  A 90 degree arc came out as four separate
splines, so nothing in CAD could be selected or edited as one curve.

Nothing here changes geometry - every check is about the ENTITY TYPE and about the
arc's measured centre, radius and DIRECTION being the ones the source drew.

Run:  python evals/test_arc_recognition.py
"""
from __future__ import annotations

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import curve_entities as ce  # noqa: E402
import ezdxf  # noqa: E402
from ezdxf.math import BSpline  # noqa: E402

PT2MM = 25.4 / 72
KAPPA = 4 * (math.sqrt(2) - 1) / 3

#: Identity mapping, so every number in a failure message can be read directly
#: against the synthetic source.
X = lambda x: x          # noqa: E731
Y = lambda y: y          # noqa: E731

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def circle_as_cubics(cx, cy, r, n=4, start_deg=0.0):
    """Write a circle (or arc) as `n` cubic Beziers, y-down source coordinates."""
    out = []
    for i in range(n):
        a0 = math.radians(start_deg) + 2 * math.pi * i / n
        a1 = math.radians(start_deg) + 2 * math.pi * (i + 1) / n
        k = KAPPA * (a1 - a0) / (math.pi / 2)      # scale kappa for non-90 arcs
        p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
        p3 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        t0 = (-math.sin(a0), math.cos(a0))
        t1 = (-math.sin(a1), math.cos(a1))
        out.append((p0,
                    (p0[0] + k * r * t0[0], p0[1] + k * r * t0[1]),
                    (p3[0] - k * r * t1[0], p3[1] - k * r * t1[1]),
                    p3))
    return out


def arc_polyline(arc, n=181):
    """Sample a DXF ARC the way CAD draws it: CCW from 50 to 51, minor sweep."""
    a0 = math.radians(arc.dxf.start_angle)
    sweep = (math.radians(arc.dxf.end_angle) - a0) % (2 * math.pi)
    return [(arc.dxf.center.x + arc.dxf.radius * math.cos(a0 + sweep * i / (n - 1)),
             arc.dxf.center.y + arc.dxf.radius * math.sin(a0 + sweep * i / (n - 1)))
            for i in range(n)]


def emit(cubics):
    msp = ezdxf.new("R2010", setup=False).modelspace()
    counts: dict = {}
    ce.emit_curves(msp, cubics, {}, X, Y, PT2MM, counts)
    return msp, counts


# --------------------------------------------------------------------------
print("1. a circle written as 4 Beziers becomes ONE CIRCLE")
# --------------------------------------------------------------------------
msp, _ = emit(circle_as_cubics(100.0, 200.0, 30.0, n=4))
circles = list(msp.query("CIRCLE"))
check("exactly one entity, and it is a CIRCLE",
      len(circles) == 1 and not list(msp.query("ARC")) and not list(msp.query("SPLINE")),
      f"CIRCLE={len(circles)} ARC={len(list(msp.query('ARC')))} "
      f"SPLINE={len(list(msp.query('SPLINE')))}")
if circles:
    e = circles[0]
    dc = math.hypot(e.dxf.center.x - 100.0, e.dxf.center.y - 200.0)
    check("centre is the source centre", dc < 1e-3, f"{dc:.3e}")
    # The source is itself only a Bezier APPROXIMATION of a circle: with the classic
    # kappa its radial error is 2.7e-4 * r = 8.2e-3 here, so the fitted radius cannot
    # be closer to 30 than the source is.
    check("radius is the source radius (within the source's own 8e-3 error)",
          abs(e.dxf.radius - 30.0) < 0.02, f"{e.dxf.radius:.6f}")

# --------------------------------------------------------------------------
print("2. a quarter arc becomes ONE ARC, on the correct side")
# --------------------------------------------------------------------------
for seg in range(4):
    one = [circle_as_cubics(100.0, 200.0, 30.0, n=4)[seg]]
    msp, _ = emit(one)
    arcs = list(msp.query("ARC"))
    if not arcs:
        check(f"segment {seg} produced an ARC", False)
        continue
    arc = arcs[0]
    span = (arc.dxf.end_angle - arc.dxf.start_angle) % 360.0
    check(f"segment {seg}: sweep is 90 degrees", abs(span - 90.0) < 1.0, f"span={span:.3f}")
    src = [ce.bezier_point(one[0], i / 180) for i in range(181)]
    got = arc_polyline(arc)
    dev = max(min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in got) for p in src)
    # Source Bezier and emitted ARC approximate the same ideal circle from opposite
    # sides, so their separation is a few times 1e-4 * r.  A mirrored arc, or one
    # that took the 270 degree way round, would be off by tens of millimetres.
    check(f"segment {seg}: lies on the source curve", dev < 0.5, f"dev={dev:.3e}")

# --------------------------------------------------------------------------
print("3. two Beziers that are one smooth curve become ONE entity")
# --------------------------------------------------------------------------
half = circle_as_cubics(0.0, 0.0, 10.0, n=4)[:2]
check("grouped into a single chain", len(ce.group_chains(half)) == 1,
      f"{len(ce.group_chains(half))}")
msp, _ = emit(half)
check("exactly one entity emitted", len(list(msp)) == 1, f"{len(list(msp))}")

# --------------------------------------------------------------------------
print("4. a corner is not joined, and a free-form curve is not forced into an arc")
# --------------------------------------------------------------------------
corner = [((0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (30.0, 0.0)),
          ((30.0, 0.0), (30.0, 10.0), (30.0, 20.0), (30.0, 30.0))]
check("90 degree corner stays two chains", len(ce.group_chains(corner)) == 2,
      f"{len(ce.group_chains(corner))}")
msp, _ = emit(corner)
check("corner emits two entities", len(list(msp)) == 2, f"{len(list(msp))}")

free = [((0.0, 0.0), (10.0, 40.0), (40.0, 10.0), (50.0, 50.0))]
msp, _ = emit(free)
check("free-form curve stays a SPLINE",
      len(list(msp.query("SPLINE"))) == 1
      and not list(msp.query("ARC")) and not list(msp.query("CIRCLE")),
      f"SPLINE={len(list(msp.query('SPLINE')))} ARC={len(list(msp.query('ARC')))}")

# --------------------------------------------------------------------------
print("5. a joined SPLINE is exact, not an approximation")
# --------------------------------------------------------------------------
chain = ce.group_chains(half)[0]
ctrl = ce.chain_control_points(chain)
knots = ce.chain_knots(len(chain))
check("control points are 3N+1", len(ctrl) == 3 * len(chain) + 1, f"{len(ctrl)}")
check("knots are 3N+5", len(knots) == 3 * len(chain) + 5, f"{len(knots)}")
bs = BSpline(control_points=ctrl, order=4, knots=knots)
worst = 0.0
for k, cubic in enumerate(chain):
    for i in range(101):
        u = i / 100.0
        q = ce.bezier_point(cubic, u)
        p = bs.point(k + u)
        worst = max(worst, math.hypot(p[0] - q[0], p[1] - q[1]))
check("joined spline reproduces the source Beziers to 1e-9", worst < 1e-9, f"{worst:.3e}")

# --------------------------------------------------------------------------
print("6. the residual gate is scale free, and still rejects an ellipse")
# --------------------------------------------------------------------------
# An ABSOLUTE millimetre gate fails here: the Bezier approximation error is a fixed
# FRACTION of the radius (2.7e-4), so at r=100 mm an absolute 0.01 mm gate rejected
# a genuine circle.  Measured before the gate was made relative.
msp, _ = emit(circle_as_cubics(0.0, 0.0, 100.0, n=4))
big = list(msp.query("CIRCLE"))
check("a 100 mm circle is still recognised", len(big) == 1,
      f"CIRCLE={len(big)} SPLINE={len(list(msp.query('SPLINE')))}")
if big:
    check("its radius is right", abs(big[0].dxf.radius - 100.0) < 0.05,
          f"{big[0].dxf.radius:.4f}")

ellipse = []
for i in range(4):
    a0 = 2 * math.pi * i / 4
    a1 = 2 * math.pi * (i + 1) / 4
    pt = lambda a: (100.0 * math.cos(a), 80.0 * math.sin(a))       # noqa: E731
    d0 = (-100.0 * math.sin(a0), 80.0 * math.cos(a0))
    d1 = (-100.0 * math.sin(a1), 80.0 * math.cos(a1))
    k = KAPPA * (a1 - a0) / (math.pi / 2)
    p0, p3 = pt(a0), pt(a1)
    ellipse.append((p0, (p0[0] + k * d0[0], p0[1] + k * d0[1]),
                    (p3[0] - k * d1[0], p3[1] - k * d1[1]), p3))
msp, _ = emit(ellipse)
check("an ellipse 20 percent off round is NOT called a circle",
      len(list(msp.query("CIRCLE"))) == 0,
      f"CIRCLE={len(list(msp.query('CIRCLE')))}")

# --------------------------------------------------------------------------
print("7. a circle cut into three arcs is merged back into one CIRCLE")
# --------------------------------------------------------------------------
# Three 120 degree arcs written as Beziers carry about 1% radial error, which is the
# WRITER's approximation and above the gate on purpose: laundering that into a
# "perfect" circle would move the geometry.  So the merge path is exercised with the
# arcs the gate does accept, grouped directly.
fitted = []
for seg in range(4):
    cub = [circle_as_cubics(100.0, 200.0, 30.0, n=4)[seg]]
    chain = ce.group_chains(cub)[0]
    kind, info = ce.classify_chain(chain, PT2MM)
    if kind == "ARC":
        fitted.append((chain, kind, info))
merged = ce._merge_full_circles(fitted, PT2MM)
kinds = [k for _, k, _ in merged]
check("four 90 degree arcs merge into a single CIRCLE", kinds == ["CIRCLE"],
      f"{kinds}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
