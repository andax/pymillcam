"""Tests for the path-stitching helper."""
from __future__ import annotations

import math

import pytest

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.path_stitching import stitch_entities
from pymillcam.core.segments import ArcSegment, LineSegment


def _line(start: tuple[float, float], end: tuple[float, float]) -> GeometryEntity:
    return GeometryEntity(segments=[LineSegment(start=start, end=end)])


def test_empty_input_returns_empty_list() -> None:
    assert stitch_entities([], 0.01) == []


def test_negative_tolerance_raises() -> None:
    with pytest.raises(ValueError, match="tolerance must be positive"):
        stitch_entities([_line((0, 0), (1, 0))], -0.01)


def test_closed_entities_pass_through_unchanged() -> None:
    closed = GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(1, 0)),
            LineSegment(start=(1, 0), end=(1, 1)),
            LineSegment(start=(1, 1), end=(0, 1)),
            LineSegment(start=(0, 1), end=(0, 0)),
        ],
        closed=True,
    )
    result = stitch_entities([closed], 0.01)
    assert result == [closed]


def test_point_entities_pass_through_unchanged() -> None:
    point = GeometryEntity(point=(5.0, 5.0))
    assert stitch_entities([point], 0.01) == [point]


def test_two_lines_meeting_at_a_point_become_one_chain() -> None:
    a = _line((0, 0), (10, 0))
    b = _line((10, 0), (10, 10))
    result = stitch_entities([a, b], 0.001)
    assert len(result) == 1
    chain = result[0].segments
    assert len(chain) == 2
    assert chain[0].start == (0.0, 0.0)
    assert chain[-1].end == (10.0, 10.0)
    assert result[0].closed is False


def test_four_lines_form_a_closed_square() -> None:
    a = _line((0, 0), (10, 0))
    b = _line((10, 0), (10, 10))
    c = _line((10, 10), (0, 10))
    d = _line((0, 10), (0, 0))
    result = stitch_entities([a, b, c, d], 0.001)
    assert len(result) == 1
    assert result[0].closed is True
    assert len(result[0].segments) == 4


def test_reverses_segment_when_endpoints_meet_end_to_end() -> None:
    # Both lines have endpoint (10, 0) — `b` must be reversed to chain.
    a = _line((0, 0), (10, 0))
    b = _line((20, 0), (10, 0))
    result = stitch_entities([a, b], 0.001)
    assert len(result) == 1
    chain = result[0].segments
    assert chain[0].start == (0.0, 0.0)
    assert chain[-1].end == (20.0, 0.0)


def test_ambiguous_three_way_junction_is_left_unstitched() -> None:
    # Three lines all meet at (10, 0) — joining is ambiguous, so leave alone.
    a = _line((0, 0), (10, 0))
    b = _line((10, 0), (20, 0))
    c = _line((10, 0), (10, 10))
    result = stitch_entities([a, b, c], 0.001)
    # No ambiguous merge → all three remain separate, just under fresh ids.
    assert len(result) == 3


def test_tolerance_window_decides_membership() -> None:
    a = _line((0, 0), (10, 0))
    b = _line((10.05, 0), (20, 0))  # 0.05 mm gap
    assert len(stitch_entities([a, b], 0.01)) == 2  # too tight, no merge
    assert len(stitch_entities([a, b], 0.1)) == 1   # generous, merges


def test_arc_reversal_negates_sweep_and_shifts_start() -> None:
    # Force end-to-end-of-arc reversal: arc's *end* lands on the line's end.
    # Arc walks CCW from (20, 0) to (10, 0) via north — start_angle 0°,
    # sweep 180°, centred at (15, 0) radius 5.
    line = _line((0, 0), (10, 0))
    arc = GeometryEntity(
        segments=[
            ArcSegment(center=(15, 0), radius=5, start_angle_deg=0, sweep_deg=180),
        ],
    )
    result = stitch_entities([line, arc], 0.001)
    assert len(result) == 1
    chain = result[0].segments
    assert chain[0].start == (0.0, 0.0)
    arc_seg = chain[1]
    assert isinstance(arc_seg, ArcSegment)
    # Reversed arc: same circle, sweep negated, new start angle is the old end.
    assert arc_seg.start_angle_deg == pytest.approx(180.0)
    assert arc_seg.sweep_deg == pytest.approx(-180.0)
    assert arc_seg.start == pytest.approx((10.0, 0.0), abs=1e-9)
    assert arc_seg.end == pytest.approx((20.0, 0.0), abs=1e-9)


def test_closed_chain_snaps_last_endpoint_to_first() -> None:
    # Last endpoint a hair off — closure snap should land it exactly on start.
    a = _line((0, 0), (10, 0))
    b = _line((10, 0), (0, 1e-12))  # near-but-not-exact closure
    result = stitch_entities([a, b], 0.001)
    assert len(result) == 1
    assert result[0].closed is True
    assert result[0].segments[-1].end == (0.0, 0.0)


def test_mixed_closed_and_open_stitched_separately() -> None:
    closed = GeometryEntity(
        segments=[
            LineSegment(start=(50, 50), end=(60, 50)),
            LineSegment(start=(60, 50), end=(60, 60)),
            LineSegment(start=(60, 60), end=(50, 50)),
        ],
        closed=True,
    )
    a = _line((0, 0), (10, 0))
    b = _line((10, 0), (20, 0))
    result = stitch_entities([closed, a, b], 0.001)
    assert len(result) == 2  # closed unchanged, two open lines merged
    assert any(e.closed for e in result)
    assert any(
        not e.closed and len(e.segments) == 2 and e.segments[0].start == (0, 0)
        for e in result
    )


def test_stitched_chain_is_relabelled_path() -> None:
    a = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))],
        dxf_entity_type="line",
    )
    b = GeometryEntity(
        segments=[LineSegment(start=(10, 0), end=(10, 10))],
        dxf_entity_type="line",
    )
    result = stitch_entities([a, b], 0.001)
    assert result[0].dxf_entity_type == "path"


def test_lone_entity_keeps_its_original_dxf_type() -> None:
    # No neighbour to stitch with — label must not change.
    e = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))],
        dxf_entity_type="line",
    )
    result = stitch_entities([e], 0.001)
    assert result[0].dxf_entity_type == "line"


def test_arc_endpoints_are_polar_evaluated_within_tolerance() -> None:
    # Sanity: the helper compares the *evaluated* endpoints, which for arcs
    # come out of the polar property. Make sure that path actually works.
    arc = GeometryEntity(
        segments=[
            ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=90),
        ],
    )
    line = _line((0.0, 5.0), (5.0, 5.0))
    result = stitch_entities([arc, line], 0.001)
    assert len(result) == 1
    chain = result[0].segments
    # Start from (5, 0), then arc to (0, 5), then line to (5, 5).
    assert chain[0].start == (5.0, 0.0)
    assert chain[-1].end == (5.0, 5.0)
    assert _approx_pair(chain[0].end, (0.0, 5.0))
    assert chain[1].start == pytest.approx((0.0, 5.0), abs=1e-9)


def _approx_pair(a: tuple[float, float], b: tuple[float, float], tol: float = 1e-9) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tol
