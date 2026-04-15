"""Unit tests for pymillcam.engine.tabs (placement + Z modulation)."""
from __future__ import annotations

import math

import pytest

from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.engine.ir import IRInstruction, MoveType
from pymillcam.engine.tabs import (
    TabPlacementError,
    compute_tab_intervals,
    effective_z_at,
    emit_pass_with_tabs,
    split_chain_at_lengths,
)

# ---------- compute_tab_intervals ----------------------------------------

def test_compute_tab_intervals_evenly_spaced_centers() -> None:
    intervals = compute_tab_intervals(
        contour_length=160.0, count=4, tab_width=5.0, ramp_length=1.5
    )
    centers = [(s + e) / 2 for s, e in intervals]
    assert centers == pytest.approx([20.0, 60.0, 100.0, 140.0])


def test_compute_tab_intervals_full_footprint_includes_ramps() -> None:
    intervals = compute_tab_intervals(
        contour_length=80.0, count=2, tab_width=6.0, ramp_length=2.0
    )
    # Footprint = 6 + 2*2 = 10; centers at 20 and 60.
    assert intervals[0] == pytest.approx((15.0, 25.0))
    assert intervals[1] == pytest.approx((55.0, 65.0))


def test_compute_tab_intervals_zero_count_returns_empty() -> None:
    assert compute_tab_intervals(100.0, 0, 5.0, 1.0) == []


def test_compute_tab_intervals_raises_when_footprint_overlaps() -> None:
    # Spacing 25, footprint 30 → overlap.
    with pytest.raises(TabPlacementError):
        compute_tab_intervals(100.0, 4, tab_width=20.0, ramp_length=5.0)


# ---------- split_chain_at_lengths --------------------------------------

def test_split_chain_at_lengths_no_cuts_returns_input() -> None:
    chain = [LineSegment(start=(0, 0), end=(10, 0))]
    result = split_chain_at_lengths(chain, [])
    assert result == chain


def test_split_chain_at_lengths_single_cut_inside_segment() -> None:
    chain = [LineSegment(start=(0, 0), end=(10, 0))]
    result = split_chain_at_lengths(chain, [3.0])
    assert len(result) == 2
    assert result[0].end == pytest.approx((3.0, 0.0))
    assert result[1].start == pytest.approx((3.0, 0.0))
    assert result[1].end == pytest.approx((10.0, 0.0))


def test_split_chain_at_lengths_multiple_cuts_inside_one_segment() -> None:
    chain = [LineSegment(start=(0, 0), end=(10, 0))]
    result = split_chain_at_lengths(chain, [2.0, 5.0, 8.0])
    assert len(result) == 4
    ends = [s.end for s in result]
    assert ends == [
        pytest.approx((2.0, 0.0)),
        pytest.approx((5.0, 0.0)),
        pytest.approx((8.0, 0.0)),
        pytest.approx((10.0, 0.0)),
    ]


def test_split_chain_at_lengths_cuts_spanning_multiple_segments() -> None:
    chain = [
        LineSegment(start=(0, 0), end=(10, 0)),
        LineSegment(start=(10, 0), end=(20, 0)),
    ]
    result = split_chain_at_lengths(chain, [3.0, 15.0])
    assert len(result) == 4
    assert result[0].end == pytest.approx((3.0, 0.0))
    assert result[1].end == pytest.approx((10.0, 0.0))
    assert result[2].end == pytest.approx((15.0, 0.0))
    assert result[3].end == pytest.approx((20.0, 0.0))


def test_split_chain_at_lengths_ignores_out_of_range_cuts() -> None:
    chain = [LineSegment(start=(0, 0), end=(10, 0))]
    # Cuts at 0, 10, and beyond — all should be ignored.
    result = split_chain_at_lengths(chain, [-1.0, 0.0, 10.0, 99.0])
    assert result == chain


def test_split_chain_at_lengths_preserves_arc_segments() -> None:
    arc = ArcSegment(
        center=(0, 0), radius=10.0, start_angle_deg=0.0, sweep_deg=90.0,
    )
    full_len = arc.length
    result = split_chain_at_lengths([arc], [full_len / 2.0])
    assert len(result) == 2
    assert isinstance(result[0], ArcSegment)
    assert isinstance(result[1], ArcSegment)
    assert result[0].sweep_deg == pytest.approx(45.0)
    assert result[1].sweep_deg == pytest.approx(45.0)


# ---------- effective_z_at ----------------------------------------------

def test_effective_z_at_outside_intervals_is_pass_z() -> None:
    intervals = [(10.0, 20.0)]
    assert effective_z_at(
        5.0, pass_z=-6.0, tab_top_z=-5.5, intervals=intervals, ramp_length=1.5,
    ) == pytest.approx(-6.0)
    assert effective_z_at(
        25.0, pass_z=-6.0, tab_top_z=-5.5, intervals=intervals, ramp_length=1.5,
    ) == pytest.approx(-6.0)


def test_effective_z_at_plateau_is_tab_top() -> None:
    intervals = [(10.0, 20.0)]
    assert effective_z_at(
        15.0, pass_z=-6.0, tab_top_z=-5.5, intervals=intervals, ramp_length=1.5,
    ) == pytest.approx(-5.5)


def test_effective_z_at_entry_ramp_interpolates() -> None:
    intervals = [(10.0, 20.0)]
    # Entry ramp [10, 11.5]: midpoint 10.75 → halfway from -6 to -5.5 = -5.75.
    assert effective_z_at(
        10.75, pass_z=-6.0, tab_top_z=-5.5, intervals=intervals, ramp_length=1.5,
    ) == pytest.approx(-5.75)


def test_effective_z_at_exit_ramp_interpolates() -> None:
    intervals = [(10.0, 20.0)]
    # Exit ramp [18.5, 20]: midpoint 19.25 → halfway back from -5.5 to -6 = -5.75.
    assert effective_z_at(
        19.25, pass_z=-6.0, tab_top_z=-5.5, intervals=intervals, ramp_length=1.5,
    ) == pytest.approx(-5.75)


def test_effective_z_at_zero_ramp_is_step_function() -> None:
    intervals = [(10.0, 20.0)]
    assert effective_z_at(
        10.0, pass_z=-6.0, tab_top_z=-5.5, intervals=intervals, ramp_length=0.0,
    ) == pytest.approx(-5.5)


# ---------- emit_pass_with_tabs -----------------------------------------

def _line_chain(length: float) -> list[LineSegment]:
    return [LineSegment(start=(0.0, 0.0), end=(length, 0.0))]


def test_emit_pass_with_tabs_plunges_to_pass_z_first() -> None:
    instructions: list[IRInstruction] = []
    emit_pass_with_tabs(
        instructions,
        _line_chain(40.0),
        pass_z=-6.0,
        tab_top_z=-5.5,
        intervals=[(10.0, 20.0)],
        ramp_length=1.5,
        feed_xy=1200.0,
        feed_z=300.0,
    )
    assert instructions[0].type is MoveType.FEED
    assert instructions[0].z == pytest.approx(-6.0)
    assert instructions[0].f == pytest.approx(300.0)


def test_emit_pass_with_tabs_walks_through_plateau_at_tab_top() -> None:
    instructions: list[IRInstruction] = []
    emit_pass_with_tabs(
        instructions,
        _line_chain(40.0),
        pass_z=-6.0,
        tab_top_z=-5.5,
        intervals=[(10.0, 20.0)],
        ramp_length=1.5,
        feed_xy=1200.0,
        feed_z=300.0,
    )
    feeds = [i for i in instructions if i.type is MoveType.FEED and i.x is not None]
    # Cuts at 10, 11.5, 18.5, 20 → pieces ending at those + final 40.
    end_points = [(round(f.x, 4), round(f.z, 4)) for f in feeds]
    assert (10.0, -6.0) in end_points       # boundary into entry ramp
    assert (11.5, -5.5) in end_points       # entry ramp top → plateau start
    assert (18.5, -5.5) in end_points       # plateau end → exit ramp start
    assert (20.0, -6.0) in end_points       # exit ramp end
    assert (40.0, -6.0) in end_points       # final piece


def test_emit_pass_with_tabs_two_intervals() -> None:
    instructions: list[IRInstruction] = []
    emit_pass_with_tabs(
        instructions,
        _line_chain(80.0),
        pass_z=-6.0,
        tab_top_z=-5.5,
        intervals=[(10.0, 20.0), (60.0, 70.0)],
        ramp_length=1.5,
        feed_xy=1200.0,
        feed_z=300.0,
    )
    feeds = [i for i in instructions if i.type is MoveType.FEED and i.x is not None]
    plateau_pts = [round(f.x, 4) for f in feeds if f.z is not None and abs(f.z - -5.5) < 1e-6]
    # Each tab plateau bookend: 11.5/18.5 and 61.5/68.5.
    assert 11.5 in plateau_pts
    assert 18.5 in plateau_pts
    assert 61.5 in plateau_pts
    assert 68.5 in plateau_pts


def test_emit_pass_with_tabs_emits_arcs_with_z() -> None:
    arc = ArcSegment(
        center=(0, 0), radius=10.0, start_angle_deg=0.0, sweep_deg=180.0,
    )
    arc_len = arc.length
    instructions: list[IRInstruction] = []
    emit_pass_with_tabs(
        instructions,
        [arc],
        pass_z=-6.0,
        tab_top_z=-5.5,
        # Place a tab around the midpoint of the arc.
        intervals=[(arc_len / 2 - 5.0, arc_len / 2 + 5.0)],
        ramp_length=1.5,
        feed_xy=1200.0,
        feed_z=300.0,
    )
    arc_moves = [i for i in instructions if i.type in (MoveType.ARC_CCW, MoveType.ARC_CW)]
    # Pre-split into 5 arc pieces (4 cuts) — first/last at pass_z, middle 3 modulated.
    assert len(arc_moves) == 5
    plateau_arcs = [
        a for a in arc_moves
        if a.z is not None and math.isclose(a.z, -5.5, abs_tol=1e-6)
    ]
    assert len(plateau_arcs) >= 1
