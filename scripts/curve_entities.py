# -*- coding: utf-8 -*-
"""curve_entities - turn PDF cubic Beziers into real CAD curve entities.

WHY THIS EXISTS
---------------
A PDF has no arc primitive.  Its content stream has only `m`/`l`/`c`/`re`/`h`, so
every circle and every arc in a PDF is written as cubic Beziers (a full circle is
usually four, one per quadrant).  The DXF input path already receives real
`ARC`/`CIRCLE` entities and carries them through unchanged; the PDF path cannot,
because the information is simply not in the source.

This module recovers it.  It does not invent geometry - it decides, with a
measured residual, whether a run of Beziers *is* a circular arc, and only then
re-expresses it as `ARC`/`CIRCLE`.  When it is not an arc the curve is kept as a
`SPLINE` built from the source control points, which is exact.

THE FOUR LAYERS
---------------
L1  read    - every cubic is already an exact item from extract.py.  No work here.
L2  group   - join consecutive cubics that share an endpoint AND are tangent
              continuous.  This is exact: the comparison uses the source numbers.
L3  classify- fit a circle, then accept it only if the measured radial residual is
              below tolerance.  Otherwise keep a SPLINE.
L4  merge   - a full circle that the author cut into N arcs becomes one CIRCLE.

WHAT IS EXACT AND WHAT IS MEASURED
----------------------------------
L1 and L2 introduce no error at all - they only read and compare source values.
L3 and L4 are measurements and therefore carry a tolerance.  Every tolerance below
is justified by a measurement recorded in `references/PIPELINE.md`; none is a guess.
"""
from __future__ import annotations

import math

# --------------------------------------------------------------------------
# Tolerances.  All geometry here is in SOURCE POINTS (pt); millimetres are only
# used at the boundary, where the pipeline's own PT2MM scale is supplied by the
# caller.  Keeping the maths in one unit means each tolerance has one meaning.
# --------------------------------------------------------------------------

#: Two cubic endpoints count as the same point.  The source values are exact (they
#: are read out of the PDF, not sampled), so this can be tight.  Measured on
#: jobs/burner: all 370 consecutive-cubic pairs in the sheet sit at gap 0.0.
TOL_POS = 1e-6

#: Tangent continuity, as 1 - cos(angle).  1e-4 is about 0.81 degrees.  A shared
#: endpoint with a real corner between the tangents is two separate features (the
#: two fillets either side of a sharp corner), so it must not be joined.
TOL_TAN = 1e-4

#: A chain whose ends coincide is a closed curve.
TOL_CLOSE = 1e-6

#: Sagitta / chord below this cannot define a circle at all - the three fit points
#: would be nearly collinear.  At 1e-3 the sweep is about 0.3 degrees.
TOL_OPEN = 1e-3

#: Minimum half-angle of the fitted arc, in radians.  This is the numerical
#: conditioning guard: the centre estimate scales like 1/sin(theta), so a very
#: shallow arc has a meaningless centre.  0.02 rad is about 1.15 degrees.
TOL_THETA = 0.02

#: Radius above which the fit is not a real arc at drawing scale.
R_MAX_MM = 10000.0

#: Radial residual accepted as "this is a circle", relative to the radius.
#:
#: This MUST be relative, not an absolute millimetre figure.  The residual of a
#: circle that a writer expressed as cubic Beziers is dominated by the Bezier
#: approximation itself, which is a fixed FRACTION of the radius (about 2.7e-4 with
#: the classic kappa, 2.0e-4 with the error-minimising one) and does not shrink with
#: size.  Measured with an absolute 0.01 mm gate: r=0.83 mm -> 2.3e-4 mm (pass),
#: r=30 mm -> 8.2e-3 mm (pass), r=100 mm -> 2.7e-2 mm (WRONGLY rejected as a
#: spline).  A relative gate is scale free and gives the same answer at every size.
TOL_RESID_REL = 0.01

#: Floor for the relative gate, so that numerical noise on a very small arc cannot
#: by itself trigger a rejection.  The generator rounds coordinates to 1e-4 mm, so
#: anything below that is below the precision of what gets written anyway.
TOL_RESID_ABS_MM = 1e-4

#: How closely two arcs must agree on centre and radius to be judged the same circle,
#: as a fraction of the radius.
#:
#: This has to be as large as the FIT's own uncertainty, not as small as float
#: precision.  An arc written as Beziers can only be located to about 1.5e-4 * r, so
#: four 90 degree arcs of one circle come out with centres up to 6e-3 pt apart at
#: r=30 - measured.  Grouping at 1e-6 * r therefore failed to rejoin them, which is
#: how this number was arrived at.
EPS_GROUP_REL = 5e-3

#: Floor for the grouping tolerance, in millimetres on paper.  Two arcs this close
#: together are the same feature at any drawing scale worth caring about.
EPS_GROUP_MM = 0.01

#: A group of arcs is a whole circle when the sweeps add up to one turn.  The
#: tolerance is not tight because each arc's sweep is itself estimated from a Bezier
#: approximation of that arc, which over-reports the angle slightly: measured, four
#: 90 degree arcs written with the classic kappa sum to 360.046 degrees rather than
#: 360.  A half degree would sit right on top of that systematic bias.
TOL_SWEEP_SUM_DEG = 2.0

#: Samples per cubic when measuring.  20 is enough that the maximum deviation of a
#: quarter-circle Bezier from its chords stays far below TOL_RESID_MM.
SAMPLES_PER_CUBIC = 20


# --------------------------------------------------------------------------
# L1  read
# --------------------------------------------------------------------------

def bezier_point(cubic, t: float):
    """Exact point on a cubic Bezier (de Casteljau, expanded).

    `cubic` is (p0, cp1, cp2, p3), each a 2-tuple, in source points.
    """
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = cubic
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3.0 * mt * mt * t
    c = 3.0 * mt * t * t
    d = t * t * t
    return (a * x0 + b * x1 + c * x2 + d * x3,
            a * y0 + b * y1 + c * y2 + d * y3)


def item_cubic(item):
    """extract.py item ["c", p0, cp1, cp2, p3] -> (p0, cp1, cp2, p3)."""
    return (tuple(item[1]), tuple(item[2]), tuple(item[3]), tuple(item[4]))


def beziers_of_items(items):
    """The cubic items of one PDF path, in order, as cubics."""
    return [item_cubic(it) for it in items if it and it[0] == "c"]


# --------------------------------------------------------------------------
# L2  group - exact, uses only source values
# --------------------------------------------------------------------------

def _continuous(a, b) -> bool:
    """Do cubics `a` and `b` form one smooth curve?

    Both halves are required.  Position alone is not enough: a sharp corner has
    coincident endpoints too, and joining across it would erase the corner and
    fabricate a curve the drawing does not have.
    """
    p3 = a[3]
    t_in = (p3[0] - a[2][0], p3[1] - a[2][1])
    t_out = (b[1][0] - b[0][0], b[1][1] - b[0][1])
    # `b` must start where `a` ended.
    gap = math.hypot(p3[0] - b[0][0], p3[1] - b[0][1])
    if gap > TOL_POS:
        return False
    n_in = math.hypot(*t_in)
    n_out = math.hypot(*t_out)
    if n_in < 1e-12 or n_out < 1e-12:
        # A zero-length tangent carries no direction; treat it as continuous rather
        # than guessing, because the position match is already exact.
        return True
    cos = (t_in[0] * t_out[0] + t_in[1] * t_out[1]) / (n_in * n_out)
    return cos > 1.0 - TOL_TAN


def group_chains(cubics):
    """Split a list of cubics into maximal smooth chains."""
    if not cubics:
        return []
    chains = [[cubics[0]]]
    for prev, cur in zip(cubics, cubics[1:]):
        if _continuous(prev, cur):
            chains[-1].append(cur)
        else:
            chains.append([cur])
    return chains


def chain_is_closed(chain) -> bool:
    p0 = chain[0][0]
    p3 = chain[-1][3]
    return math.hypot(p0[0] - p3[0], p0[1] - p3[1]) <= TOL_CLOSE


# --------------------------------------------------------------------------
# L3  classify - measured
# --------------------------------------------------------------------------

def sample_chain(chain, per_cubic: int = SAMPLES_PER_CUBIC):
    """Dense points along the chain, in order.  The join point is not repeated."""
    pts = []
    for cubic in chain:
        for i in range(per_cubic):
            pts.append(bezier_point(cubic, i / per_cubic))
    pts.append(chain[-1][3])
    return pts


def fit_circle(points):
    """Algebraic least-squares circle fit (Kasa).

    Returns (cx, cy, r) or None when the points are collinear.

    The 3x3 system is solved with ``numpy.linalg.lstsq`` on purpose.  A hand-rolled
    Gaussian elimination here is what broke the first draft of this module: it
    returned a radius of 215 mm and a 90 mm residual for a 0.83 mm arc, i.e. it was
    quietly wrong rather than failing.  np.linalg.lstsq is rank-revealing and was
    measured stable at this coordinate scale (300 perturbed arcs: worst residual
    1.338e-05 pt, identical to a pre-centred fit).
    """
    import numpy as np

    n = len(points)
    if n < 3:
        return None
    p = np.asarray(points, dtype=float)
    a = np.column_stack((2.0 * p[:, 0], 2.0 * p[:, 1], np.ones(n)))
    b = (p * p).sum(axis=1)
    try:
        sol, residuals, rank, _ = np.linalg.lstsq(a, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if rank < 3:
        return None
    cx, cy, c3 = float(sol[0]), float(sol[1]), float(sol[2])
    r2 = c3 + cx * cx + cy * cy
    if not math.isfinite(r2) or r2 <= 0.0:
        return None
    return cx, cy, math.sqrt(r2)


def max_radial_residual(points, cx, cy, r) -> float:
    return max(abs(math.hypot(x - cx, y - cy) - r) for x, y in points)


def _sagitta_over_chord(points):
    """How "open" the chain is.  0 is a straight line, 0.5 is a semicircle."""
    p0, p3 = points[0], points[-1]
    chord = math.hypot(p3[0] - p0[0], p3[1] - p0[1])
    if chord <= 1e-12:
        return 0.0, chord
    nx, ny = -(p3[1] - p0[1]) / chord, (p3[0] - p0[0]) / chord
    sag = 0.0
    for x, y in points:
        d = abs((x - p0[0]) * nx + (y - p0[1]) * ny)
        if d > sag:
            sag = d
    return sag / chord, chord


def sweep_degrees(points, cx, cy) -> float:
    """Signed total turn about the centre, by accumulation.

    Accumulation, not the difference of the two end angles: a chain that closes on
    itself has identical end angles, so a difference would report 0 for a full
    circle.
    """
    total = 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        a0 = math.atan2(ay - cy, ax - cx)
        a1 = math.atan2(by - cy, bx - cx)
        d = (a1 - a0 + math.pi) % (2.0 * math.pi) - math.pi
        total += d
    return math.degrees(total)


#: Smallest circle worth emitting as a CIRCLE, in millimetres.  Below this the
#: entity carries no useful information and the rounding of the coordinates is a
#: significant fraction of the radius.
R_MIN_MM = 1e-3


def classify_chain(chain, scale_mm_per_pt: float):
    """Decide what ONE chain is.

    Returns ("CIRCLE", {cx, cy, r, resid}) | ("ARC", {..., a0, a1, sweep})
            | ("SPLINE", None).  Distances are in source points.
    """
    pts = sample_chain(chain)
    closed = chain_is_closed(chain)

    # The "openness" and centre-vs-chord guards are defined on the CHORD, and a
    # closed chain has none: its ends coincide, so chord is 0 and sagitta/chord is
    # 0/0.  Applying the openness test to a closed chain read that as "perfectly
    # straight" and rejected every full circle - measured on a synthetic circle
    # written as 4 Beziers.  So closed chains take the other branch outright.
    if not closed:
        openness, chord = _sagitta_over_chord(pts)
        if openness < TOL_OPEN:
            return "SPLINE", None
        theta = math.atan(2.0 * openness)
        if theta < TOL_THETA:
            return "SPLINE", None
    else:
        chord = 0.0

    fit = fit_circle(pts)
    if fit is None:
        return "SPLINE", None
    cx, cy, r = fit
    if not (math.isfinite(r) and r > 0.0):
        return "SPLINE", None
    r_mm = r * scale_mm_per_pt
    if r_mm > R_MAX_MM or r_mm < R_MIN_MM:
        return "SPLINE", None

    # A centre far outside an OPEN curve means the fit has degenerated.  There is
    # no such test to make on a closed one.
    if not closed:
        mid = ((pts[0][0] + pts[-1][0]) / 2.0, (pts[0][1] + pts[-1][1]) / 2.0)
        if math.hypot(cx - mid[0], cy - mid[1]) > 50.0 * max(chord, 1e-12):
            return "SPLINE", None

    resid = max_radial_residual(pts, cx, cy, r)
    # Relative gate: scale free, so a large circle is judged by the same rule as a
    # small one.  See TOL_RESID_REL for the measurement that forced this.
    limit = max(TOL_RESID_REL * r, TOL_RESID_ABS_MM / max(scale_mm_per_pt, 1e-12))
    if resid > limit:
        return "SPLINE", None

    if closed:
        return "CIRCLE", {"cx": cx, "cy": cy, "r": r, "resid": resid}
    a0 = math.degrees(math.atan2(pts[0][1] - cy, pts[0][0] - cx))
    a1 = math.degrees(math.atan2(pts[-1][1] - cy, pts[-1][0] - cx))
    return "ARC", {"cx": cx, "cy": cy, "r": r, "resid": resid,
                   "a0": a0, "a1": a1, "sweep": sweep_degrees(pts, cx, cy),
                   "first": pts[0], "last": pts[-1]}


# --------------------------------------------------------------------------
# SPLINE control points and knots - exact piecewise Bezier
# --------------------------------------------------------------------------

def chain_control_points(chain):
    """3N+1 control points of the chain: p0, then (cp1, cp2, p3) of every cubic."""
    pts = [chain[0][0]]
    for cubic in chain:
        pts.append(cubic[1])
        pts.append(cubic[2])
        pts.append(cubic[3])
    return pts


def chain_knots(n_cubics: int):
    """Knot vector for a piecewise-Bezier degree-3 B-spline.

    M = 3N+1 control points, so K = M + 4 = 3N+5 knots, of which 3N-3 are interior:
    that is multiplicity 3 at each of the N-1 interior knots, i.e. the chain is only
    C0 there, which is exactly what the PDF drew.

    This MUST be written explicitly.  ezdxf's automatic knots turn the chain into a
    C2 spline that no longer passes through the junction points - measured, that
    moves the curve by 0.0234 mm at the join.
    """
    knots = [0.0] * 4
    for j in range(1, n_cubics):
        knots.extend([float(j)] * 3)
    knots.extend([float(n_cubics)] * 4)
    return knots


# --------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------

def _attr_without(attr, *keys):
    return {k: v for k, v in attr.items() if k not in keys}


def _radius_mm(r_pt, X):
    """Convert a radius to the drawing's units.

    A radius is a LENGTH, so it must be scaled exactly as the coordinates are.  The
    caller's X() already carries the sheet's scale, so the mapping is measured from
    X itself rather than applying the scale a second time - doing both was the first
    draft's bug: it emitted a 30 mm circle with radius 10.58 mm.
    """
    return abs(X(r_pt) - X(0.0))


def emit_curves(msp, cubics, attr, X, Y, scale_mm_per_pt, counts=None):
    """Emit every curve of ONE pdf path as single curve entities.

    `cubics` is a list of (p0, cp1, cp2, p3) in source points.
    `X`, `Y` map source points to drawing millimetres; a RADIUS is therefore
    already in millimetres once X() has been applied to a length, so it must NOT be
    multiplied by `scale_mm_per_pt` again.
    """
    if counts is None:
        counts = {}
    fitted = []
    for chain in group_chains(cubics):
        kind, info = classify_chain(chain, scale_mm_per_pt)
        fitted.append((chain, kind, info))

    # L4: a whole circle that the author cut into several arcs becomes one CIRCLE.
    # Curves are grouped per (spatial chain, attribute set): merging across a layer
    # or colour change would silently repaint the drawing, and re-joining across a
    # linetype change would merge a dashed curve into a solid one.
    merged = _merge_full_circles(fitted, scale_mm_per_pt)

    for chain, kind, info in merged:
        if kind == "CIRCLE":
            msp.add_circle(center=(X(info["cx"]), Y(info["cy"])),
                           radius=_radius_mm(info["r"], X), dxfattribs=attr)
            counts["curve_circle"] = counts.get("curve_circle", 0) + 1
        elif kind == "ARC":
            # Angles must be computed in the FINAL (y-up) coordinate system: going
            # through the point transform keeps the arc on the same physical side,
            # whereas reflecting the source angles mirrors it.
            cx, cy = info["cx"], info["cy"]
            cxm, cym = X(cx), Y(cy)
            a0 = math.degrees(math.atan2(Y(info["first"][1]) - cym,
                                         X(info["first"][0]) - cxm))
            a1 = math.degrees(math.atan2(Y(info["last"][1]) - cym,
                                         X(info["last"][0]) - cxm))
            # A DXF ARC is always counter-clockwise from its start angle to its end
            # angle, so those two numbers describe the MINOR sweep.  Emitting them
            # in the order the source curve runs therefore flips the arc to the
            # other side whenever the source runs the other way: measured, a 90
            # degree source arc came out as a 270 degree arc the long way round.
            # Order them so the sweep stays short, i.e. follows the source.
            if info["sweep"] > 0.0:
                start_angle, end_angle = a0, a1
            else:
                start_angle, end_angle = a1, a0
            msp.add_arc(center=(cxm, cym), radius=_radius_mm(info["r"], X),
                        start_angle=start_angle, end_angle=end_angle,
                        dxfattribs=attr)
            counts["curve_arc"] = counts.get("curve_arc", 0) + 1
        else:
            ctrl = [(X(px), Y(py)) for px, py in chain_control_points(chain)]
            sp = msp.add_open_spline(ctrl, degree=3, dxfattribs=attr)
            knots = chain_knots(len(chain))
            if [round(float(k), 9) for k in sp.knots] != [round(k, 9) for k in knots]:
                sp.knots = knots
            counts["curve_spline"] = counts.get("curve_spline", 0) + 1
            if len(chain) > 1:
                counts["curve_spline_joined"] = counts.get("curve_spline_joined", 0) + 1
    counts["cubics_in"] = counts.get("cubics_in", 0) + len(cubics)
    counts["curves_out"] = counts.get("curves_out", 0) + len(merged)
    return counts


def _merge_full_circles(fitted, scale_mm_per_pt: float):
    """Replace a set of arcs that together make a full circle with one CIRCLE.

    A full circle is often written as several arcs (four 90 degree ones, or three
    120 degree ones), so recognising the circle means recognising the SET.

    Grouping is by PROXIMITY, not by a bucketed key.  Hashing the centre and radius
    into `round(v / (eps * r))` buckets looks equivalent but is not: two centres a
    nanometre apart can straddle a bucket edge and land in different groups, so the
    merge would silently depend on where the origin happens to be.  Comparing
    distances does not have that failure mode.
    """
    passthrough = []
    arcs = []
    for chain, kind, info in fitted:
        if kind == "ARC":
            arcs.append((chain, kind, info))
        else:
            passthrough.append((chain, kind, info))

    groups = []
    for item in arcs:
        info = item[2]
        for g in groups:
            head = g[0][2]
            dr = abs(head["r"] - info["r"])
            dc = math.hypot(head["cx"] - info["cx"], head["cy"] - info["cy"])
            tol = max(EPS_GROUP_REL * max(head["r"], info["r"]),
                      EPS_GROUP_MM / max(scale_mm_per_pt, 1e-12))
            if dr <= tol and dc <= tol:
                g.append(item)
                break
        else:
            groups.append([item])

    out = list(passthrough)
    for members in groups:
        if len(members) < 2:
            out.extend(members)
            continue
        total = sum(abs(m[2]["sweep"]) for m in members)
        if abs(total - 360.0) > TOL_SWEEP_SUM_DEG:
            out.extend(members)
            continue
        n = len(members)
        cx = sum(m[2]["cx"] for m in members) / n
        cy = sum(m[2]["cy"] for m in members) / n
        r = sum(m[2]["r"] for m in members) / n
        out.append((None, "CIRCLE", {"cx": cx, "cy": cy, "r": r, "resid": 0.0,
                                     "merged_from": n}))
    return out
