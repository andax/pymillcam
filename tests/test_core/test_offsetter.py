"""Tests for the analytical arc-aware offsetter."""
from __future__ import annotations

import math

import pytest

from pymillcam.core.offsetter import OffsetError, offset_closed_contour
from pymillcam.core.segments import ArcSegment, LineSegment


def _square(size: float = 10.0) -> list[LineSegment]:
    return [
        LineSegment(start=(0, 0), end=(size, 0)),
        LineSegment(start=(size, 0), end=(size, size)),
        LineSegment(start=(size, size), end=(0, size)),
        LineSegment(start=(0, size), end=(0, 0)),
    ]


def test_empty_input_raises() -> None:
    with pytest.raises(OffsetError, match="empty contour"):
        offset_closed_contour([], 1.0, outside=True)


def test_negative_distance_raises() -> None:
    with pytest.raises(OffsetError, match="distance must be positive"):
        offset_closed_contour(_square(), -1.0, outside=True)


# ------------------------------------------------------------ full-circle case


def test_outside_offset_of_full_circle_grows_radius() -> None:
    arc = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=360)
    result = offset_closed_contour([arc], 1.5, outside=True)
    assert len(result) == 1
    assert isinstance(result[0], ArcSegment)
    assert result[0].radius == pytest.approx(11.5)
    assert result[0].center == (0.0, 0.0)
    assert result[0].sweep_deg == 360.0


def test_inside_offset_of_full_circle_shrinks_radius() -> None:
    arc = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=360)
    result = offset_closed_contour([arc], 2.0, outside=False)
    assert isinstance(result[0], ArcSegment)
    assert result[0].radius == pytest.approx(8.0)


def test_inside_offset_collapsing_circle_raises() -> None:
    arc = ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=360)
    with pytest.raises(OffsetError, match="shrinks"):
        offset_closed_contour([arc], 6.0, outside=False)


# ------------------------------------------------------------- square / corners


def test_outside_offset_of_square_rounds_each_corner() -> None:
    result = offset_closed_contour(_square(10.0), 2.0, outside=True)
    # Four lines + four fillet arcs.
    arcs = [s for s in result if isinstance(s, ArcSegment)]
    lines = [s for s in result if isinstance(s, LineSegment)]
    assert len(arcs) == 4
    assert len(lines) == 4
    # Each fillet has radius == offset distance and sits on an original corner.
    expected_centres = {(0, 0), (10, 0), (10, 10), (0, 10)}
    for arc in arcs:
        assert arc.radius == pytest.approx(2.0)
        assert arc.center in expected_centres
        # Quarter-arcs CCW.
        assert arc.sweep_deg == pytest.approx(90.0)


def test_inside_offset_of_square_keeps_sharp_corners_and_shrinks() -> None:
    result = offset_closed_contour(_square(10.0), 2.0, outside=False)
    # Four lines, no fillets — line-line intersection at each corner.
    assert len(result) == 4
    assert all(isinstance(s, LineSegment) for s in result)
    # The trimmed inset square spans from (2,2) to (8,8).
    xs = [s.start[0] for s in result] + [s.end[0] for s in result]
    ys = [s.start[1] for s in result] + [s.end[1] for s in result]
    assert min(xs) == pytest.approx(2.0)
    assert max(xs) == pytest.approx(8.0)
    assert min(ys) == pytest.approx(2.0)
    assert max(ys) == pytest.approx(8.0)


def test_inside_offset_too_large_for_feature_raises() -> None:
    # 50 mm tool radius into a 30-tall rectangle → trim flips orientation /
    # produces a nonsense larger shape; validator catches it.
    rect = [
        LineSegment(start=(0, 0), end=(50, 0)),
        LineSegment(start=(50, 0), end=(50, 30)),
        LineSegment(start=(50, 30), end=(0, 30)),
        LineSegment(start=(0, 30), end=(0, 0)),
    ]
    with pytest.raises(OffsetError):
        offset_closed_contour(rect, 50.0, outside=False)


def test_clockwise_input_is_normalised_to_ccw() -> None:
    # Same square but written CW — should still produce a valid outside offset.
    cw_square = [
        LineSegment(start=(0, 0), end=(0, 10)),
        LineSegment(start=(0, 10), end=(10, 10)),
        LineSegment(start=(10, 10), end=(10, 0)),
        LineSegment(start=(10, 0), end=(0, 0)),
    ]
    result = offset_closed_contour(cw_square, 1.0, outside=True)
    arcs = [s for s in result if isinstance(s, ArcSegment)]
    assert len(arcs) == 4
    assert all(a.radius == pytest.approx(1.0) for a in arcs)


# --------------------------------------------------- mixed line + tangent arc


def test_rounded_rectangle_outside_offset_preserves_arc_centres() -> None:
    # A 20×10 rectangle with 2 mm tangent fillets at every corner. CCW.
    fill = 2.0
    contour = [
        # Bottom edge
        LineSegment(start=(fill, 0), end=(20 - fill, 0)),
        # Bottom-right fillet (centre (20-fill, fill), 270° → 360°)
        ArcSegment(center=(20 - fill, fill), radius=fill, start_angle_deg=270, sweep_deg=90),
        # Right edge
        LineSegment(start=(20, fill), end=(20, 10 - fill)),
        # Top-right fillet
        ArcSegment(center=(20 - fill, 10 - fill), radius=fill, start_angle_deg=0, sweep_deg=90),
        # Top edge
        LineSegment(start=(20 - fill, 10), end=(fill, 10)),
        # Top-left fillet
        ArcSegment(center=(fill, 10 - fill), radius=fill, start_angle_deg=90, sweep_deg=90),
        # Left edge
        LineSegment(start=(0, 10 - fill), end=(0, fill)),
        # Bottom-left fillet
        ArcSegment(center=(fill, fill), radius=fill, start_angle_deg=180, sweep_deg=90),
    ]
    distance = 1.0
    result = offset_closed_contour(contour, distance, outside=True)
    # All four corners are tangent → no extra fillet arcs needed; we should
    # still have exactly four arcs (the original ones, expanded by distance).
    arcs = [s for s in result if isinstance(s, ArcSegment)]
    lines = [s for s in result if isinstance(s, LineSegment)]
    assert len(arcs) == 4
    assert len(lines) == 4
    for arc in arcs:
        assert arc.radius == pytest.approx(fill + distance)


# ------------------------------------------------------------ fillet geometry


def test_outside_corner_fillet_starts_and_ends_on_offset_lines() -> None:
    result = offset_closed_contour(_square(10.0), 2.0, outside=True)
    # Pick the fillet at (10, 0): start angle should be -90° (on the offset
    # of the south edge at y=-2), end angle 0° (on the offset of the east
    # edge at x=12). Quarter-CCW sweep.
    fillet = next(
        s for s in result
        if isinstance(s, ArcSegment) and s.center == (10.0, 0.0)
    )
    assert fillet.start == pytest.approx((10.0, -2.0), abs=1e-9)
    assert fillet.end == pytest.approx((12.0, 0.0), abs=1e-9)
    assert fillet.sweep_deg == pytest.approx(90.0)


def test_offset_polygon_area_matches_expected() -> None:
    # Outside offset of a 10×10 square by 2 mm: original 100 mm² + perimeter
    # band 4·10·2 = 80 + four corner quarter-circles π·2² = 4π ≈ 12.566.
    from shapely.geometry import Polygon

    from pymillcam.core.segments import segments_to_shapely

    result = offset_closed_contour(_square(10.0), 2.0, outside=True)
    shadow = segments_to_shapely(result, closed=True, tolerance=0.001)
    assert isinstance(shadow, Polygon)
    expected = 100.0 + 4 * 10 * 2 + math.pi * 2**2
    assert shadow.area == pytest.approx(expected, rel=0.01)
