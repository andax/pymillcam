#!/usr/bin/env python
"""Generate ``tests/fixtures/dxf/gear_profile.dxf`` — a 12-tooth test gear.

Run::

    uv run python scripts/generate_gear_dxf.py

The gear is a simplified spur-gear outline, hand-designed to stress-test
the analytical offsetter's tangent-arc logic. **Not** a functional
involute gear — flanks are straight lines. Per tooth, CCW around the
boundary:

    1. Tip arc (on Ra, convex, centred at origin)
    2. Right tip fillet (convex, small radius)
    3. Right flank (straight line)
    4. Right root fillet (concave)
    5. Root arc on Rf (in the gap after this tooth)
    6. Left root fillet of next tooth (concave)
    7. Left flank of next tooth (straight line)
    8. Left tip fillet of next tooth (convex)

Every join is tangent. The whole boundary is a closed chain of 96
entities (72 ARCs + 24 LINEs). Import with ``auto_stitch_on_import``
enabled, or run ``Operations > Join paths`` after DXF import to
consolidate into a single closed ``GeometryEntity``.

Tweakables (top of file): gear parameters. Dimensions are in mm.
"""
from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing

# ---------------------------------------------------------------- parameters

NUM_TEETH = 12
RA = 17.5            # tip (addendum) radius
RF = 12.5            # root (dedendum) radius
TIP_HALF_DEG = 3.0   # nominal half-angle of tip arc on Ra before filleting
GAP_HALF_DEG = 3.0   # nominal half-angle of root arc on Rf before filleting
TIP_FILLET = 0.5     # radius of tip-flank fillet (convex)
ROOT_FILLET = 0.5    # radius of flank-root fillet (concave)
LAYER_NAME = "Profile_Outside"

OUTPUT = Path(__file__).resolve().parent.parent / "tests/fixtures/dxf/gear_profile.dxf"


# ---------------------------------------------------------------- math


def polar(r: float, angle_deg: float) -> tuple[float, float]:
    a = math.radians(angle_deg)
    return r * math.cos(a), r * math.sin(a)


def rotate_point(p: tuple[float, float], angle_deg: float) -> tuple[float, float]:
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return c * p[0] - s * p[1], s * p[0] + c * p[1]


def reflect_x(p: tuple[float, float]) -> tuple[float, float]:
    """Reflect across the X axis: (x, y) → (x, -y)."""
    return p[0], -p[1]


def normalize_180(a_deg: float) -> float:
    """Wrap to (-180, 180]."""
    return ((a_deg + 180.0) % 360.0) - 180.0


def solve_fillet_tangent(
    ref_radius: float,
    seed_angle_deg: float,
    flank_far_end: tuple[float, float],
    r_fillet: float,
    *,
    internal: bool,
) -> tuple[float, tuple[float, float], tuple[float, float]]:
    """Find where a fillet tangent to both a circle (centred at origin,
    radius ``ref_radius``) and a flank line can sit.

    The flank line passes through ``polar(ref_radius, seed_angle_deg)``
    and ``flank_far_end``. ``seed_angle_deg`` is the angle of the
    unfilleted corner on the reference circle — the solver picks the
    tangent-angle solution closest to that seed.

    When ``internal=True`` the fillet sits **inside** the reference
    circle (convex tip fillet, centre at radius ``ref_radius - r_fillet``).
    When ``internal=False`` it sits **outside** (concave root fillet,
    centre at radius ``ref_radius + r_fillet``).

    Returns ``(tangent_angle_on_ref_circle_deg, fillet_center,
    tangent_point_on_flank)``.
    """
    p_ref = polar(ref_radius, seed_angle_deg)
    p_far = flank_far_end
    dx, dy = p_far[0] - p_ref[0], p_far[1] - p_ref[1]
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("Degenerate flank (zero length)")
    # Unit flank direction and left-perpendicular normal.
    dhx, dhy = dx / length, dy / length
    nx, ny = -dhy, dhx
    # Pick the sign of n such that the fillet centre ends up on the
    # correct side of the flank (toward origin for internal, away for
    # external).
    toward_origin = nx * (-p_ref[0]) + ny * (-p_ref[1]) > 0
    if toward_origin != internal:
        nx, ny = -nx, -ny
    # Centre of the fillet is at distance (ref_radius ∓ r_fillet) from
    # origin (depending on internal/external tangency to the big circle)
    # and at perpendicular distance r_fillet from the flank line.
    c_radius = ref_radius - r_fillet if internal else ref_radius + r_fillet
    # n · (C − P_ref) = r_fillet → n · C = r_fillet + n · P_ref
    # With C = c_radius × (cos α, sin α):
    #   c_radius × (nx cos α + ny sin α) = r_fillet + (nx x_ref + ny y_ref)
    rhs = r_fillet + nx * p_ref[0] + ny * p_ref[1]
    target = rhs / c_radius
    if abs(target) > 1.0 + 1e-12:
        raise ValueError(
            f"No tangent fillet exists for ref={ref_radius} seed={seed_angle_deg}°"
            f" r_fillet={r_fillet} — flank too close / fillet too large"
        )
    target = max(-1.0, min(1.0, target))
    # Standard decomposition: nx cos α + ny sin α = cos(α − θ_n)
    theta_n = math.degrees(math.atan2(ny, nx))
    off = math.degrees(math.acos(target))
    candidates = (
        normalize_180(theta_n + off),
        normalize_180(theta_n - off),
    )
    # Pick the solution nearer to the unfilleted seed angle — the other
    # root is the diametrically-opposite spurious solution.
    alpha = min(
        candidates,
        key=lambda a: abs(normalize_180(a - seed_angle_deg)),
    )
    center = polar(c_radius, alpha)
    # Tangent point on the flank is the foot of the perpendicular from
    # the fillet centre to the flank line.
    t = (center[0] - p_ref[0]) * dhx + (center[1] - p_ref[1]) * dhy
    flank_tangent = (p_ref[0] + t * dhx, p_ref[1] + t * dhy)
    return alpha, center, flank_tangent


# ---------------------------------------------------------------- geometry


def compute_right_side_geom() -> dict[str, object]:
    """Compute the right-side geometry of tooth 0 (tooth centreline on +X).

    All other tooth geometry follows by rotation + X-axis reflection.
    """
    # Nominal (unfilleted) corners.
    alpha_tip_right = TIP_HALF_DEG
    gap_center = 360.0 / NUM_TEETH / 2.0  # 15° for 12 teeth
    beta_root_right = gap_center - GAP_HALF_DEG
    p_ra_seed = polar(RA, alpha_tip_right)
    p_rf_seed = polar(RF, beta_root_right)

    # Right tip fillet: internally tangent to Ra, tangent to the flank
    # line connecting the unfilleted tip corner to the unfilleted root corner.
    alpha_tip_f, c_tip, flank_top = solve_fillet_tangent(
        RA, alpha_tip_right, p_rf_seed, TIP_FILLET, internal=True,
    )
    # Right root fillet: externally tangent to Rf.
    beta_root_f, c_root, flank_bot = solve_fillet_tangent(
        RF, beta_root_right, p_ra_seed, ROOT_FILLET, internal=False,
    )
    return {
        "alpha_tip_f": alpha_tip_f,
        "c_tip": c_tip,
        "flank_top": flank_top,
        "beta_root_f": beta_root_f,
        "c_root": c_root,
        "flank_bot": flank_bot,
    }


# ---------------------------------------------------------------- DXF emit


def short_arc_angles(
    center: tuple[float, float], p_start: tuple[float, float], p_end: tuple[float, float]
) -> tuple[float, float]:
    """Return ``(start_deg, end_deg)`` for a DXF ARC entity (CCW from
    start to end) that draws the short arc between ``p_start`` and
    ``p_end`` around ``center``.

    If the traversal-direction ``p_start → p_end`` happens to be the
    long-way-CCW, the endpoints are swapped — the resulting geometric
    arc is the same set of points, just parameterised from the other
    end. Downstream (PyMillCAM's importer) reads DXF ARCs as CCW and
    stitches on endpoint coincidence, so swap direction is fine.
    """
    sa = math.degrees(math.atan2(p_start[1] - center[1], p_start[0] - center[0]))
    ea = math.degrees(math.atan2(p_end[1] - center[1], p_end[0] - center[0]))
    sweep = (ea - sa) % 360.0
    if sweep > 180.0:
        # The CCW sweep from sa→ea goes the long way; swap so DXF traces
        # the short arc.
        return ea, sa
    return sa, ea


def emit_arc(
    msp: object,
    center: tuple[float, float],
    radius: float,
    start_deg: float,
    end_deg: float,
    attrs: dict,
) -> None:
    msp.add_arc(
        center=center,
        radius=radius,
        start_angle=start_deg,
        end_angle=end_deg,
        dxfattribs=attrs,
    )


def generate_gear() -> Drawing:
    doc = ezdxf.new(setup=True)
    doc.units = 4  # mm
    if LAYER_NAME not in doc.layers:
        doc.layers.new(LAYER_NAME, dxfattribs={"color": 7})
    msp = doc.modelspace()
    attrs = {"layer": LAYER_NAME}

    g = compute_right_side_geom()
    alpha_tip_f = float(g["alpha_tip_f"])  # type: ignore[arg-type]
    beta_root_f = float(g["beta_root_f"])  # type: ignore[arg-type]
    pitch = 360.0 / NUM_TEETH

    # Right-side primitives (tooth 0 frame) — to be rotated per tooth.
    c_tip_r = g["c_tip"]       # type: ignore[assignment]
    flank_top_r = g["flank_top"]  # type: ignore[assignment]
    flank_bot_r = g["flank_bot"]  # type: ignore[assignment]
    c_root_r = g["c_root"]     # type: ignore[assignment]

    # Left-side primitives in tooth 0 frame: reflect across X axis.
    c_tip_l = reflect_x(c_tip_r)
    flank_top_l = reflect_x(flank_top_r)
    flank_bot_l = reflect_x(flank_bot_r)
    c_root_l = reflect_x(c_root_r)

    for k in range(NUM_TEETH):
        theta_k = k * pitch
        next_theta = (k + 1) * pitch

        # 1. Tip arc of tooth k — centred at origin on Ra.
        emit_arc(
            msp,
            center=(0.0, 0.0),
            radius=RA,
            start_deg=theta_k - alpha_tip_f,
            end_deg=theta_k + alpha_tip_f,
            attrs=attrs,
        )

        # Right-side primitives for tooth k (rotate the tooth-0 frame by θ_k).
        c_tip_k = rotate_point(c_tip_r, theta_k)
        ft_k = rotate_point(flank_top_r, theta_k)
        fb_k = rotate_point(flank_bot_r, theta_k)
        c_root_k = rotate_point(c_root_r, theta_k)
        p_ra_tangent_k = polar(RA, theta_k + alpha_tip_f)
        p_rf_tangent_k = polar(RF, theta_k + beta_root_f)

        # 2. Right tip fillet — convex, from Ra-tangent to flank-top.
        sa, ea = short_arc_angles(c_tip_k, p_ra_tangent_k, ft_k)
        emit_arc(msp, c_tip_k, TIP_FILLET, sa, ea, attrs)

        # 3. Right flank — straight line from flank-top to flank-bot.
        msp.add_line(start=ft_k, end=fb_k, dxfattribs=attrs)

        # 4. Right root fillet — concave, from flank-bot to Rf-tangent.
        sa, ea = short_arc_angles(c_root_k, fb_k, p_rf_tangent_k)
        emit_arc(msp, c_root_k, ROOT_FILLET, sa, ea, attrs)

        # 5. Root arc on Rf in gap k (right side of gap = +β_root_f after θ_k,
        #    left side = θ_k + pitch − β_root_f by the gap's reflection symmetry).
        emit_arc(
            msp,
            center=(0.0, 0.0),
            radius=RF,
            start_deg=theta_k + beta_root_f,
            end_deg=next_theta - beta_root_f,
            attrs=attrs,
        )

        # Left-side primitives for tooth (k+1) (rotate the reflected tooth-0
        # frame by next_theta).
        c_tip_l_next = rotate_point(c_tip_l, next_theta)
        ft_l_next = rotate_point(flank_top_l, next_theta)
        fb_l_next = rotate_point(flank_bot_l, next_theta)
        c_root_l_next = rotate_point(c_root_l, next_theta)
        p_ra_tan_l_next = polar(RA, next_theta - alpha_tip_f)
        p_rf_tan_l_next = polar(RF, next_theta - beta_root_f)

        # 6. Left root fillet of tooth (k+1) — concave, from Rf-tangent to flank-bot.
        sa, ea = short_arc_angles(c_root_l_next, p_rf_tan_l_next, fb_l_next)
        emit_arc(msp, c_root_l_next, ROOT_FILLET, sa, ea, attrs)

        # 7. Left flank of tooth (k+1) — straight line from flank-bot to flank-top.
        msp.add_line(start=fb_l_next, end=ft_l_next, dxfattribs=attrs)

        # 8. Left tip fillet of tooth (k+1) — convex, from flank-top to Ra-tangent.
        sa, ea = short_arc_angles(c_tip_l_next, ft_l_next, p_ra_tan_l_next)
        emit_arc(msp, c_tip_l_next, TIP_FILLET, sa, ea, attrs)

    return doc


# ---------------------------------------------------------------- main

def main() -> None:
    doc = generate_gear()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(OUTPUT)

    # Simple summary — count the entities that actually made it into the
    # DXF and cross-check against the expected topology.
    msp = doc.modelspace()
    arcs = sum(1 for e in msp if e.dxftype() == "ARC")
    lines = sum(1 for e in msp if e.dxftype() == "LINE")
    print(f"Wrote {OUTPUT.relative_to(OUTPUT.parent.parent.parent)}")
    print(f"  ARCs : {arcs}  (expected 72 = 12 tips + 24 tip fillets + "
          "24 root fillets + 12 root arcs)")
    print(f"  LINEs: {lines}  (expected 24 = 12 × 2 flanks)")


if __name__ == "__main__":
    main()
