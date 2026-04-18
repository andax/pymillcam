"""Behaviour tests for the time estimator.

Assertions test time-as-a-function-of-distance-and-rate, not specific
second counts — the formulas (distance/rate×60) are the contract, and
staying at that level keeps tests resilient to future changes like
accel/decel correction factors.
"""
from __future__ import annotations

import math

import pytest

from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath
from pymillcam.engine.time_estimate import (
    DEFAULT_TOOL_CHANGE_SECONDS,
    estimate_toolpath_seconds,
    format_seconds,
)


def _toolpath(*instructions: IRInstruction) -> Toolpath:
    return Toolpath(
        operation_name="test", tool_number=1, instructions=list(instructions),
    )


# ---------------------------------------------------------------- rapids


def test_empty_toolpath_is_zero_seconds() -> None:
    assert estimate_toolpath_seconds(_toolpath()) == 0.0


def test_rapid_time_is_distance_over_rapid_rate() -> None:
    # Start at origin (implicit from first positioning move) then rapid
    # 100 mm along X. At the default 5000 mm/min, that's 100/5000 min
    # = 1.2 s.
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=0, z=0),
        IRInstruction(type=MoveType.RAPID, x=100, y=0, z=0),
    )
    assert estimate_toolpath_seconds(tp) == pytest.approx(100 / 5000 * 60)


def test_rapid_time_scales_with_rapid_rate_override() -> None:
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=0, z=0),
        IRInstruction(type=MoveType.RAPID, x=100, y=0, z=0),
    )
    # Halving the rapid rate should double the time.
    slow = estimate_toolpath_seconds(tp, rapid_rate_mm_per_min=2500.0)
    fast = estimate_toolpath_seconds(tp, rapid_rate_mm_per_min=5000.0)
    assert slow == pytest.approx(fast * 2.0)


def test_first_positioning_move_contributes_zero_time() -> None:
    """The first move establishes a starting position — there's no
    "get to the start" motion to charge for."""
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=100, y=200, z=50),
    )
    assert estimate_toolpath_seconds(tp) == 0.0


def test_rapid_uses_3d_distance() -> None:
    # Combined XYZ rapid: 3-4-5 triangle in XY, no Z → len=5.
    # Then 5-12-13 in the XZ plane → len=13.
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=0, z=0),
        IRInstruction(type=MoveType.RAPID, x=3, y=4, z=0),
        IRInstruction(type=MoveType.RAPID, x=3 + 5, y=4, z=12),
    )
    expected = (5 + 13) / 5000 * 60
    assert estimate_toolpath_seconds(tp) == pytest.approx(expected)


# ----------------------------------------------------------------- feeds


def test_feed_time_is_distance_over_instruction_feed_rate() -> None:
    # Move 60 mm at 1200 mm/min → 60/1200 × 60 = 3 s
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=0, z=0),
        IRInstruction(type=MoveType.FEED, x=60, y=0, z=0, f=1200),
    )
    assert estimate_toolpath_seconds(tp) == pytest.approx(60 / 1200 * 60)


def test_feed_with_no_feed_rate_is_skipped() -> None:
    """IR without ``f`` is malformed; the estimator advances position
    but charges zero rather than dividing by zero."""
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=0, z=0),
        IRInstruction(type=MoveType.FEED, x=100, y=0, z=0),  # no f
    )
    assert estimate_toolpath_seconds(tp) == 0.0


def test_feed_zero_rate_is_skipped() -> None:
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=0, z=0),
        IRInstruction(type=MoveType.FEED, x=100, y=0, z=0, f=0),
    )
    assert estimate_toolpath_seconds(tp) == 0.0


def test_helical_feed_uses_3d_length() -> None:
    # XY distance 4, Z distance 3 → 3D length 5.
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=0, z=0),
        IRInstruction(type=MoveType.FEED, x=4, y=0, z=-3, f=600),
    )
    assert estimate_toolpath_seconds(tp) == pytest.approx(5 / 600 * 60)


# ------------------------------------------------------------------ arcs


def test_quarter_circle_arc_length_is_pi_r_over_2() -> None:
    r = 10.0
    feed = 1200.0
    # Quarter-circle CCW, centred at origin, going from +X to +Y.
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=r, y=0, z=0),
        IRInstruction(
            type=MoveType.ARC_CCW,
            x=0, y=r, z=0,
            i=-r, j=0,
            f=feed,
        ),
    )
    expected_arc = math.pi * r / 2
    assert estimate_toolpath_seconds(tp) == pytest.approx(
        expected_arc / feed * 60
    )


def test_cw_and_ccw_arcs_of_equal_sweep_cost_the_same() -> None:
    r = 5.0
    feed = 1000.0
    ccw = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=r, y=0, z=0),
        IRInstruction(type=MoveType.ARC_CCW, x=0, y=r, i=-r, j=0, f=feed),
    )
    cw = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=0, y=r, z=0),
        IRInstruction(type=MoveType.ARC_CW, x=r, y=0, i=0, j=-r, f=feed),
    )
    assert estimate_toolpath_seconds(ccw) == pytest.approx(
        estimate_toolpath_seconds(cw)
    )


def test_full_circle_arc_uses_2pi_sweep() -> None:
    """start == end means a full circle; our generator avoids this but
    external IR might use it."""
    r = 3.0
    feed = 600.0
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=r, y=0, z=0),
        IRInstruction(type=MoveType.ARC_CCW, x=r, y=0, i=-r, j=0, f=feed),
    )
    assert estimate_toolpath_seconds(tp) == pytest.approx(
        (2 * math.pi * r) / feed * 60
    )


def test_helical_arc_uses_hypot_of_arc_and_z_delta() -> None:
    """A G2/G3 with Z change traces a helix; path length is
    hypot(arc, dz)."""
    r = 10.0
    feed = 1000.0
    dz = -5.0
    # Quarter circle → arc length = π r / 2. Combined with |dz|=5:
    # hypot(πr/2, 5).
    tp = _toolpath(
        IRInstruction(type=MoveType.RAPID, x=r, y=0, z=0),
        IRInstruction(
            type=MoveType.ARC_CCW,
            x=0, y=r, z=dz,
            i=-r, j=0,
            f=feed,
        ),
    )
    expected = math.hypot(math.pi * r / 2, abs(dz))
    assert estimate_toolpath_seconds(tp) == pytest.approx(expected / feed * 60)


# ---------------------------------------------------------- tool change


def test_tool_change_adds_fixed_seconds() -> None:
    tp = _toolpath(IRInstruction(type=MoveType.TOOL_CHANGE, tool_number=1))
    assert estimate_toolpath_seconds(tp) == pytest.approx(DEFAULT_TOOL_CHANGE_SECONDS)


def test_tool_change_seconds_override() -> None:
    tp = _toolpath(IRInstruction(type=MoveType.TOOL_CHANGE, tool_number=1))
    assert estimate_toolpath_seconds(
        tp, tool_change_seconds=30.0
    ) == pytest.approx(30.0)


# ---------------------------------------------------------------- dwell


def test_dwell_instruction_adds_its_seconds() -> None:
    tp = _toolpath(IRInstruction(type=MoveType.DWELL, f=2.5))
    assert estimate_toolpath_seconds(tp) == pytest.approx(2.5)


def test_dwell_without_duration_is_zero() -> None:
    tp = _toolpath(IRInstruction(type=MoveType.DWELL))
    assert estimate_toolpath_seconds(tp) == 0.0


# -------------------------------------------------- zero-time instructions


def test_spindle_and_comment_instructions_contribute_zero() -> None:
    tp = _toolpath(
        IRInstruction(type=MoveType.COMMENT, comment="hi"),
        IRInstruction(type=MoveType.SPINDLE_ON, s=18000),
        IRInstruction(type=MoveType.SPINDLE_OFF),
        IRInstruction(type=MoveType.COOLANT_ON),
        IRInstruction(type=MoveType.COOLANT_OFF),
    )
    assert estimate_toolpath_seconds(tp) == 0.0


# ------------------------------------------------------------ validation


def test_non_positive_rapid_rate_raises() -> None:
    tp = _toolpath()
    with pytest.raises(ValueError, match="rapid_rate"):
        estimate_toolpath_seconds(tp, rapid_rate_mm_per_min=0)
    with pytest.raises(ValueError, match="rapid_rate"):
        estimate_toolpath_seconds(tp, rapid_rate_mm_per_min=-100.0)


def test_negative_tool_change_seconds_raises() -> None:
    tp = _toolpath()
    with pytest.raises(ValueError, match="tool_change"):
        estimate_toolpath_seconds(tp, tool_change_seconds=-1.0)


# ------------------------------------------------------------ format helper


def test_format_seconds_under_hour() -> None:
    assert format_seconds(0) == "0:00"
    assert format_seconds(5) == "0:05"
    assert format_seconds(65) == "1:05"
    assert format_seconds(3599) == "59:59"


def test_format_seconds_over_hour() -> None:
    assert format_seconds(3600) == "1:00:00"
    assert format_seconds(3725) == "1:02:05"


def test_format_seconds_rounds_to_nearest() -> None:
    assert format_seconds(5.4) == "0:05"
    assert format_seconds(5.6) == "0:06"


def test_format_seconds_negative_clamped_to_zero() -> None:
    """Defensive: an estimator bug shouldn't produce e.g. -1:59. Clamp
    at the formatter boundary."""
    assert format_seconds(-10.0) == "0:00"
