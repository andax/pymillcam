"""Tests for pymillcam.core.segments."""
from __future__ import annotations

import math
from itertools import pairwise

import pytest
from shapely.geometry import LineString, Polygon

from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    _sample_arc,
    segments_to_shapely,
    split_full_circle,
)

# ---------- LineSegment ----------------------------------------------------

def test_line_length() -> None:
    seg = LineSegment(start=(0.0, 0.0), end=(3.0, 4.0))
    assert seg.length == 5.0


def test_line_json_round_trip() -> None:
    original = LineSegment(start=(1.0, 2.0), end=(3.0, 4.0))
    restored = LineSegment.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.type == "line"


# ---------- ArcSegment -----------------------------------------------------

def test_arc_start_and_end_derived() -> None:
    arc = ArcSegment(center=(0.0, 0.0), radius=10.0, start_angle_deg=0.0, sweep_deg=90.0)
    sx, sy = arc.start
    ex, ey = arc.end
    assert math.isclose(sx, 10.0) and math.isclose(sy, 0.0, abs_tol=1e-9)
    assert math.isclose(ex, 0.0, abs_tol=1e-9) and math.isclose(ey, 10.0)


def test_arc_ccw_sign() -> None:
    ccw = ArcSegment(center=(0, 0), radius=1, start_angle_deg=0, sweep_deg=90)
    cw = ArcSegment(center=(0, 0), radius=1, start_angle_deg=0, sweep_deg=-90)
    assert ccw.ccw is True
    assert cw.ccw is False


def test_arc_full_circle_flag() -> None:
    full_ccw = ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=360)
    full_cw = ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=-360)
    quarter = ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=90)
    assert full_ccw.is_full_circle is True
    assert full_cw.is_full_circle is True
    assert quarter.is_full_circle is False


def test_arc_length_matches_analytical() -> None:
    arc = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=90)
    assert math.isclose(arc.length, math.pi * 10 / 2)


def test_arc_json_round_trip() -> None:
    original = ArcSegment(
        center=(1.0, 2.0), radius=3.0, start_angle_deg=45.0, sweep_deg=-180.0
    )
    restored = ArcSegment.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.type == "arc"


# ---------- segments_to_shapely -------------------------------------------

def test_discretize_requires_segments() -> None:
    with pytest.raises(ValueError, match="at least one segment"):
        segments_to_shapely([], closed=False)


def test_discretize_requires_positive_tolerance() -> None:
    seg = LineSegment(start=(0, 0), end=(1, 1))
    with pytest.raises(ValueError, match="tolerance must be positive"):
        segments_to_shapely([seg], closed=False, tolerance=0.0)


def test_single_line_becomes_linestring() -> None:
    seg = LineSegment(start=(0, 0), end=(10, 0))
    geom = segments_to_shapely([seg], closed=False)
    assert isinstance(geom, LineString)
    assert list(geom.coords) == [(0.0, 0.0), (10.0, 0.0)]


def test_line_chain_preserves_all_vertices() -> None:
    segs = [
        LineSegment(start=(0, 0), end=(10, 0)),
        LineSegment(start=(10, 0), end=(10, 5)),
        LineSegment(start=(10, 5), end=(0, 5)),
    ]
    geom = segments_to_shapely(segs, closed=False)
    assert list(geom.coords) == [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0)]


def test_closed_line_chain_becomes_polygon() -> None:
    segs = [
        LineSegment(start=(0, 0), end=(10, 0)),
        LineSegment(start=(10, 0), end=(10, 10)),
        LineSegment(start=(10, 10), end=(0, 10)),
        LineSegment(start=(0, 10), end=(0, 0)),
    ]
    geom = segments_to_shapely(segs, closed=True)
    assert isinstance(geom, Polygon)
    assert math.isclose(geom.area, 100.0)


def test_full_circle_arc_becomes_closed_polygon() -> None:
    arc = ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=360)
    geom = segments_to_shapely([arc], closed=True, tolerance=0.001)
    assert isinstance(geom, Polygon)
    # π·r² = 25π ≈ 78.54; tight tolerance should keep us within 0.1%.
    assert math.isclose(geom.area, math.pi * 25, rel_tol=1e-3)


def test_arc_chord_sag_respects_tolerance() -> None:
    arc = ArcSegment(center=(0, 0), radius=100, start_angle_deg=0, sweep_deg=360)
    tolerance = 0.01
    points = _sample_arc(arc, tolerance)
    # For every adjacent pair, midpoint distance from (0,0) must be ≥ r - tol.
    for (x1, y1), (x2, y2) in pairwise(points):
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        midpoint_radius = math.hypot(mx, my)
        sag = arc.radius - midpoint_radius
        assert sag <= tolerance + 1e-9, f"chord sag {sag} exceeds tolerance {tolerance}"


def test_tighter_tolerance_produces_more_vertices() -> None:
    arc = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=360)
    coarse = _sample_arc(arc, 0.1)
    fine = _sample_arc(arc, 0.001)
    assert len(fine) > len(coarse)


def test_split_full_circle_into_two_semicircles() -> None:
    arc = ArcSegment(center=(0, 0), radius=10.0, start_angle_deg=0.0, sweep_deg=360.0)
    a, b = split_full_circle(arc)
    assert a.sweep_deg == pytest.approx(180.0)
    assert b.sweep_deg == pytest.approx(180.0)
    assert a.start == pytest.approx(arc.start)
    assert a.end == pytest.approx(b.start)
    assert b.end == pytest.approx(arc.start)
    # Distinct endpoints — that's the whole point.
    assert a.end != pytest.approx(a.start)


def test_split_full_circle_preserves_cw_orientation() -> None:
    arc = ArcSegment(center=(0, 0), radius=10.0, start_angle_deg=0.0, sweep_deg=-360.0)
    a, b = split_full_circle(arc)
    assert a.sweep_deg == pytest.approx(-180.0)
    assert b.sweep_deg == pytest.approx(-180.0)
    assert a.ccw is False and b.ccw is False


def test_split_full_circle_rejects_partial_arc() -> None:
    arc = ArcSegment(center=(0, 0), radius=10.0, start_angle_deg=0.0, sweep_deg=90.0)
    with pytest.raises(ValueError, match="full-circle"):
        split_full_circle(arc)


def test_mixed_line_and_arc_chain() -> None:
    # Slot: two 50 mm lines joined by two 180° arcs of radius 5.
    segs = [
        LineSegment(start=(0, -5), end=(50, -5)),
        ArcSegment(center=(50, 0), radius=5, start_angle_deg=-90, sweep_deg=180),
        LineSegment(start=(50, 5), end=(0, 5)),
        ArcSegment(center=(0, 0), radius=5, start_angle_deg=90, sweep_deg=180),
    ]
    geom = segments_to_shapely(segs, closed=True, tolerance=0.001)
    assert isinstance(geom, Polygon)
    # Area = 50 × 10 + π·25 ≈ 578.54 within discretization error.
    assert math.isclose(geom.area, 50 * 10 + math.pi * 25, rel_tol=1e-3)
