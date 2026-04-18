"""Tests for the viewport's chord-polyline arc sampler.

These guard against two classes of regression:

1. **Junction gaps.** The sampler must return ``arc.start`` as its first
   point and ``arc.end`` as its last, bit-for-bit. If this invariant
   breaks, adjacent arcs drawn at a shared mathematical point won't
   share a pixel — exactly the ~0.01 mm tooth-tip gap we saw with
   ``QPainter.drawArc`` (1/16° quantisation) and ~0.003 mm with
   ``QPainterPath.arcTo`` (Bézier approximation).

2. **Chord sag.** Every intermediate chord must lie within 0.5 px of
   the true circle. Asserting sag as a property means the test
   generalises to any arc / any zoom, not a specific sample count.
"""
from __future__ import annotations

import math

import pytest

from pymillcam.core.segments import ArcSegment
from pymillcam.ui.viewport import arc_polyline_world_points

# Slightly loose — floating-point evaluation of cos / sin around the sag
# bound can tip a chord a few ULPs past 0.5 px. 0.51 keeps the test
# stable while still failing if a regression pushes sag noticeably past
# sub-pixel.
_MAX_SAG_PX_ASSERTION = 0.51


def _chord_sag_px(
    chord_start: tuple[float, float],
    chord_end: tuple[float, float],
    arc: ArcSegment,
    scale: float,
) -> float:
    """Worst-case sag between a chord and the true arc, in widget px.

    For a chord whose endpoints lie on the arc, the maximum deviation
    from the true circle is at the midpoint along the shorter arc
    between them. Sag h = r · (1 − cos(Δθ/2)) where Δθ is the chord's
    subtended angle.
    """
    cx, cy = arc.center
    sx, sy = chord_start
    ex, ey = chord_end
    ang_s = math.atan2(sy - cy, sx - cx)
    ang_e = math.atan2(ey - cy, ex - cx)
    d = abs(ang_e - ang_s)
    d = min(d, 2 * math.pi - d)  # take the short way round
    sag_world = arc.radius * (1 - math.cos(d / 2))
    return sag_world * scale


# ------------------------------------------------------------- endpoint contract


def _make_arc(
    *,
    center: tuple[float, float] = (0.0, 0.0),
    radius: float = 10.0,
    start_angle_deg: float = 0.0,
    sweep_deg: float = 90.0,
) -> ArcSegment:
    return ArcSegment(
        center=center,
        radius=radius,
        start_angle_deg=start_angle_deg,
        sweep_deg=sweep_deg,
    )


def test_first_point_equals_arc_start_exactly() -> None:
    arc = _make_arc(start_angle_deg=37.0, sweep_deg=23.0)
    pts = arc_polyline_world_points(arc, scale_px_per_mm=100.0)
    assert pts[0] == arc.start


def test_last_point_equals_arc_end_exactly() -> None:
    arc = _make_arc(start_angle_deg=37.0, sweep_deg=23.0)
    pts = arc_polyline_world_points(arc, scale_px_per_mm=100.0)
    assert pts[-1] == arc.end


def test_adjacent_arcs_share_endpoint_bit_for_bit() -> None:
    """Tooth-tip regression: two arcs meeting at a shared mathematical
    point must render with byte-identical widget coordinates, so the
    boundary shows no gap.
    """
    # Tip arc and tip fillet from the generated gear: both pass through
    # (17.4909635539, 0.5623112635).
    tip_arc = _make_arc(
        center=(0.0, 0.0),
        radius=17.5,
        start_angle_deg=-1.8413490731469437,
        sweep_deg=3.6826981462938874,
    )
    tip_fillet = _make_arc(
        center=(16.99122173804846, 0.5462452274112426),
        radius=0.5,
        start_angle_deg=1.8413490731469537,
        sweep_deg=70.38149000209614,
    )
    # Shared mathematical point up to floating-point noise.
    assert math.isclose(tip_arc.end[0], tip_fillet.start[0], abs_tol=1e-12)
    assert math.isclose(tip_arc.end[1], tip_fillet.start[1], abs_tol=1e-12)

    a = arc_polyline_world_points(tip_arc, scale_px_per_mm=100.0)
    b = arc_polyline_world_points(tip_fillet, scale_px_per_mm=100.0)
    # The sampler preserves the underlying endpoints, so the rendered
    # junction inherits whatever precision the source data has.
    assert a[-1] == tip_arc.end
    assert b[0] == tip_fillet.start
    gap = math.hypot(a[-1][0] - b[0][0], a[-1][1] - b[0][1])
    assert gap < 1e-12  # == source precision


# ------------------------------------------------------------- chord sag bound


@pytest.mark.parametrize("sweep", [10.0, 90.0, 180.0, 270.0, -45.0, -180.0])
@pytest.mark.parametrize("scale", [1.0, 10.0, 100.0, 1000.0])
def test_chord_sag_stays_subpixel(sweep: float, scale: float) -> None:
    """At every zoom / sweep, the polyline stays within 0.5 px of the arc."""
    arc = _make_arc(radius=17.5, start_angle_deg=-20.0, sweep_deg=sweep)
    pts = arc_polyline_world_points(arc, scale_px_per_mm=scale)
    assert len(pts) >= 2
    for a, b in zip(pts[:-1], pts[1:], strict=True):
        sag = _chord_sag_px(a, b, arc, scale)
        assert sag <= _MAX_SAG_PX_ASSERTION, (
            f"chord sag {sag:.4f} px exceeds 0.5 px at scale={scale}, sweep={sweep}"
        )


def test_subpixel_radius_collapses_to_chord() -> None:
    """A tiny arc (under a pixel at the current zoom) becomes one chord —
    no point drawing dozens of sub-pixel samples.
    """
    arc = _make_arc(radius=0.001, start_angle_deg=0.0, sweep_deg=90.0)
    pts = arc_polyline_world_points(arc, scale_px_per_mm=100.0)
    # 0.001 mm × 100 px/mm = 0.1 px → under the 1-px threshold.
    assert len(pts) == 2
    assert pts[0] == arc.start
    assert pts[1] == arc.end


def test_sample_count_is_bounded_for_huge_arcs() -> None:
    """At extreme zoom on a large arc, the sampler still caps output."""
    arc = _make_arc(radius=1_000_000.0, start_angle_deg=0.0, sweep_deg=360.0)
    pts = arc_polyline_world_points(arc, scale_px_per_mm=10.0)
    # Far fewer than the naïve count (arc length / sub-pixel step), but
    # enough to maintain sub-pixel sag given the cap.
    assert len(pts) < 10_000


def test_reversed_sweep_is_sampled_the_same_way() -> None:
    """A negative sweep (CW arc) samples symmetrically to the positive case."""
    ccw = _make_arc(start_angle_deg=0.0, sweep_deg=60.0)
    cw = _make_arc(start_angle_deg=60.0, sweep_deg=-60.0)
    ccw_pts = arc_polyline_world_points(ccw, scale_px_per_mm=100.0)
    cw_pts = arc_polyline_world_points(cw, scale_px_per_mm=100.0)
    # Same number of samples, start/end swapped.
    assert len(ccw_pts) == len(cw_pts)
    assert ccw_pts[0] == cw_pts[-1]
    assert ccw_pts[-1] == cw_pts[0]
