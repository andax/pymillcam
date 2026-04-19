"""Behaviour tests for engine/common.py.

These exercise each helper in isolation through its behavioural
contract rather than through profile / pocket integration — the goal
is that when the helpers are reused by drill / surface / engrave,
failures surface here instead of only in end-to-end tests.
"""
from __future__ import annotations

import math

import pytest

from pymillcam.core.operations import ProfileOp
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.core.tools import CuttingData, Tool, ToolController
from pymillcam.engine import common
from pymillcam.engine.common import EngineError
from pymillcam.engine.ir import IRInstruction, MoveType


class _FlavourError(EngineError):
    """Caller-specific error used to verify ``error_cls`` wiring."""


# ------------------------------------------------------------ resolve_tool_controller


def test_resolve_tool_controller_finds_by_tool_number() -> None:
    project = Project()
    tc = ToolController(tool_number=7, tool=Tool(name="t"))
    project.tool_controllers.append(tc)
    op = ProfileOp(name="op", tool_controller_id=7)

    assert common.resolve_tool_controller(op, project) is tc


def test_resolve_tool_controller_raises_error_cls_when_unset() -> None:
    op = ProfileOp(name="op", tool_controller_id=None)

    with pytest.raises(_FlavourError, match="tool_controller_id"):
        common.resolve_tool_controller(
            op, Project(), error_cls=_FlavourError
        )


def test_resolve_tool_controller_raises_error_cls_when_unknown_id() -> None:
    project = Project()
    project.tool_controllers.append(
        ToolController(tool_number=1, tool=Tool(name="t"))
    )
    op = ProfileOp(name="op", tool_controller_id=99)

    with pytest.raises(_FlavourError, match="99"):
        common.resolve_tool_controller(
            op, project, error_cls=_FlavourError
        )


# ------------------------------------------------------------ resolve_stepdown


def test_resolve_stepdown_prefers_explicit_override() -> None:
    op = ProfileOp(name="op", stepdown=1.5)
    tc = ToolController(tool_number=1, tool=Tool(name="t"))
    tc.tool.cutting_data["wood"] = CuttingData(stepdown=3.0)

    assert common.resolve_stepdown(op, tc) == 1.5


def test_resolve_stepdown_uses_first_cutting_data_entry_when_op_unset() -> None:
    op = ProfileOp(name="op", stepdown=None)
    tc = ToolController(tool_number=1, tool=Tool(name="t"))
    tc.tool.cutting_data["wood"] = CuttingData(stepdown=2.5)

    assert common.resolve_stepdown(op, tc) == 2.5


def test_resolve_stepdown_falls_back_to_default_when_no_data() -> None:
    op = ProfileOp(name="op", stepdown=None)
    tc = ToolController(tool_number=1, tool=Tool(name="t"))  # empty cutting_data

    assert common.resolve_stepdown(op, tc) == common.DEFAULT_STEPDOWN_MM


# ------------------------------------------------------------ resolve_chord_tolerance


def test_resolve_chord_tolerance_prefers_op_override() -> None:
    project = Project()
    project.settings.chord_tolerance = 0.05
    op = ProfileOp(name="op", chord_tolerance=0.01)

    assert common.resolve_chord_tolerance(op, project) == 0.01


def test_resolve_chord_tolerance_falls_back_to_project() -> None:
    project = Project()
    project.settings.chord_tolerance = 0.03
    op = ProfileOp(name="op", chord_tolerance=None)

    assert common.resolve_chord_tolerance(op, project) == 0.03


# ------------------------------------------------------------ z_levels


def test_z_levels_zero_depth_returns_empty() -> None:
    assert common.z_levels(0.0, 1.0, multi_depth=True) == []


def test_z_levels_positive_depth_returns_empty() -> None:
    # Positive cut_depth is a no-op — nothing to machine above Z=0.
    assert common.z_levels(5.0, 1.0, multi_depth=True) == []


def test_z_levels_single_level_when_multi_depth_off() -> None:
    assert common.z_levels(-3.0, 1.0, multi_depth=False) == [-3.0]


def test_z_levels_stepdown_non_positive_collapses_to_single_level() -> None:
    # A zero / negative stepdown is invalid but shouldn't hang — fall
    # back to a single full-depth pass.
    assert common.z_levels(-3.0, 0.0, multi_depth=True) == [-3.0]
    assert common.z_levels(-3.0, -0.5, multi_depth=True) == [-3.0]


def test_z_levels_emits_descending_levels_snapping_last_to_depth() -> None:
    levels = common.z_levels(-3.5, 1.0, multi_depth=True)

    # Strictly descending, endpoints sensible, and the last level hits
    # cut_depth exactly — no floating-point residue.
    assert all(a > b for a, b in zip(levels, levels[1:], strict=False))
    assert levels[-1] == -3.5
    assert levels[0] < 0  # cuts into the material on the first pass
    # Intermediate steps step by exactly `stepdown`; only the last one
    # may be shorter.
    for a, b in zip(levels[:-2], levels[1:-1], strict=False):
        assert a - b == pytest.approx(1.0)


# ------------------------------------------------------------ chain_is_ccw


def _square_ccw() -> list[LineSegment]:
    # (0,0) → (1,0) → (1,1) → (0,1) → (0,0) is CCW.
    return [
        LineSegment(start=(0, 0), end=(1, 0)),
        LineSegment(start=(1, 0), end=(1, 1)),
        LineSegment(start=(1, 1), end=(0, 1)),
        LineSegment(start=(0, 1), end=(0, 0)),
    ]


def _square_cw() -> list[LineSegment]:
    # (0,0) → (0,1) → (1,1) → (1,0) → (0,0) is CW.
    pts = [(0, 0), (0, 1), (1, 1), (1, 0)]
    return [
        LineSegment(start=pts[i], end=pts[(i + 1) % 4]) for i in range(4)
    ]


def test_chain_is_ccw_detects_ccw() -> None:
    assert common.chain_is_ccw(_square_ccw()) is True


def test_chain_is_ccw_detects_cw() -> None:
    assert common.chain_is_ccw(_square_cw()) is False


# -------------------------------------------------------- split_chain_at_length


def test_split_chain_at_length_zero_returns_empty_prefix() -> None:
    chain = _square_ccw()
    first, rest = common.split_chain_at_length(chain, 0.0)

    assert first == []
    assert len(rest) == len(chain)


def test_split_chain_at_length_past_end_returns_full_prefix() -> None:
    chain = _square_ccw()
    first, rest = common.split_chain_at_length(chain, 1e6)

    assert first == chain
    assert rest == []


def test_split_chain_at_length_mid_segment_produces_seam() -> None:
    chain = _square_ccw()  # total length = 4
    first, rest = common.split_chain_at_length(chain, 0.25)

    # Prefix arc-length equals the requested split length.
    assert sum(s.length for s in first) == pytest.approx(0.25)
    # Prefix + suffix cover the full chain (no material lost at the seam).
    assert sum(s.length for s in first) + sum(s.length for s in rest) == pytest.approx(4.0)


# ------------------------------------------------------- walk_closed_chain


def test_walk_closed_chain_wraps_around() -> None:
    chain = _square_ccw()
    walked = common.walk_closed_chain(chain, start_offset=3.5, length=1.5)

    # Walking 1.5 from offset 3.5 crosses the wrap (3.5 → 4.0 → 0.0 → 1.0).
    assert sum(s.length for s in walked) == pytest.approx(1.5)


def test_walk_closed_chain_zero_length_returns_empty() -> None:
    assert common.walk_closed_chain(_square_ccw(), 0.0, 0.0) == []
    assert common.walk_closed_chain(_square_ccw(), 1.0, 0.0) == []


def test_walk_closed_chain_longer_than_total_wraps_multiple_times() -> None:
    chain = _square_ccw()
    walked = common.walk_closed_chain(chain, start_offset=0.0, length=10.0)

    assert sum(s.length for s in walked) == pytest.approx(10.0)


# ---------------------------------------------------------------- emit_segment


def _line(start: tuple[float, float], end: tuple[float, float]) -> LineSegment:
    return LineSegment(start=start, end=end)


def test_emit_segment_line_emits_feed_to_endpoint() -> None:
    instructions: list[IRInstruction] = []
    common.emit_segment(instructions, _line((0, 0), (5, 0)), feed_xy=1200)

    assert len(instructions) == 1
    inst = instructions[0]
    assert inst.type is MoveType.FEED
    assert (inst.x, inst.y) == (5, 0)
    assert inst.f == 1200


def test_emit_segment_full_circle_is_split_into_two_arcs() -> None:
    instructions: list[IRInstruction] = []
    full_circle = ArcSegment(
        center=(0, 0), radius=1.0, start_angle_deg=0.0, sweep_deg=360.0
    )
    common.emit_segment(instructions, full_circle, feed_xy=500)

    # Full-circle G2/G3 is ambiguous on most controllers — emit two
    # distinct arc IR instructions with different endpoints.
    assert len(instructions) == 2
    assert all(i.type in (MoveType.ARC_CW, MoveType.ARC_CCW) for i in instructions)
    assert (instructions[0].x, instructions[0].y) != (instructions[1].x, instructions[1].y)


def test_emit_segment_ccw_arc_uses_arc_ccw_move() -> None:
    instructions: list[IRInstruction] = []
    arc = ArcSegment(
        center=(0, 0), radius=1.0, start_angle_deg=0.0, sweep_deg=90.0
    )
    common.emit_segment(instructions, arc, feed_xy=1000)

    assert len(instructions) == 1
    assert instructions[0].type is MoveType.ARC_CCW


def test_emit_segment_cw_arc_uses_arc_cw_move() -> None:
    instructions: list[IRInstruction] = []
    arc = ArcSegment(
        center=(0, 0), radius=1.0, start_angle_deg=90.0, sweep_deg=-90.0
    )
    common.emit_segment(instructions, arc, feed_xy=1000)

    assert instructions[0].type is MoveType.ARC_CW


# ----------------------------------------------------------- emit_ramp_segments


def test_emit_ramp_segments_interpolates_z_by_arc_length() -> None:
    # A two-segment chain of total length 2.0: the midpoint (after the
    # first segment) should land at the halfway Z value.
    chain = [_line((0, 0), (1, 0)), _line((1, 0), (2, 0))]
    instructions: list[IRInstruction] = []
    common.emit_ramp_segments(
        instructions, chain, z_start=0.0, z_end=-2.0, feed_xy=1000
    )

    assert instructions[0].z == pytest.approx(-1.0)
    assert instructions[-1].z == pytest.approx(-2.0)


def test_emit_ramp_segments_empty_chain_is_silent() -> None:
    instructions: list[IRInstruction] = []
    common.emit_ramp_segments(
        instructions, [], z_start=0.0, z_end=-1.0, feed_xy=1000
    )

    assert instructions == []


def test_emit_ramp_segments_zero_length_chain_is_silent() -> None:
    instructions: list[IRInstruction] = []
    # A zero-length line would be unusual but shouldn't produce IR.
    zero_chain = [_line((0, 0), (0, 0))]
    # ``sum(length)`` is 0 so the helper short-circuits without raising.
    common.emit_ramp_segments(
        instructions, zero_chain, z_start=0, z_end=-1, feed_xy=1000
    )

    assert instructions == []


# --------------------------------------------------------- unit tangent


def test_unit_tangent_at_start_line_points_along_line() -> None:
    seg = _line((0, 0), (3, 4))  # length 5
    tx, ty = common.unit_tangent_at_start(seg)

    assert (tx, ty) == pytest.approx((0.6, 0.8))


def test_unit_tangent_at_start_zero_length_line_raises_error_cls() -> None:
    seg = _line((1, 1), (1, 1))

    with pytest.raises(_FlavourError, match="Zero-length"):
        common.unit_tangent_at_start(seg, error_cls=_FlavourError)


def test_unit_tangent_at_ccw_arc_is_perpendicular_to_radius() -> None:
    # CCW arc starting at angle 0 → tangent points along +Y.
    arc = ArcSegment(
        center=(0, 0), radius=5.0, start_angle_deg=0.0, sweep_deg=90.0
    )
    tx, ty = common.unit_tangent_at_start(arc)

    assert (tx, ty) == pytest.approx((0.0, 1.0), abs=1e-9)
    assert math.hypot(tx, ty) == pytest.approx(1.0)


def test_unit_tangent_at_end_respects_sweep_direction() -> None:
    # CW arc ending at angle 0 → tangent opposite the CCW case.
    arc = ArcSegment(
        center=(0, 0), radius=5.0, start_angle_deg=90.0, sweep_deg=-90.0
    )
    tx, ty = common.unit_tangent_at_end(arc)

    # Ending at angle 0, CW travel → tangent points along -Y.
    assert (tx, ty) == pytest.approx((0.0, -1.0), abs=1e-9)


# -------------------------------------- rotate_closed_chain_to_nearest_point


def _rect_chain() -> list[LineSegment]:
    """10 × 10 square, CCW, starting at origin."""
    return [
        LineSegment(start=(0.0, 0.0), end=(10.0, 0.0)),
        LineSegment(start=(10.0, 0.0), end=(10.0, 10.0)),
        LineSegment(start=(10.0, 10.0), end=(0.0, 10.0)),
        LineSegment(start=(0.0, 10.0), end=(0.0, 0.0)),
    ]


def test_rotate_places_nearest_point_first_when_target_on_edge() -> None:
    """Target inside the top edge → the rotated chain starts exactly
    at the projection onto that edge (segment split inside the run)."""
    rotated = common.rotate_closed_chain_to_nearest_point(
        _rect_chain(), (3.0, 10.0)
    )
    assert rotated[0].start == pytest.approx((3.0, 10.0), abs=1e-9)
    # Chain is still closed (last end meets first start).
    assert rotated[-1].end == pytest.approx(rotated[0].start, abs=1e-9)


def test_rotate_preserves_total_chain_length() -> None:
    original = _rect_chain()
    rotated = common.rotate_closed_chain_to_nearest_point(original, (7.0, -5.0))
    assert sum(s.length for s in rotated) == pytest.approx(
        sum(s.length for s in original), abs=1e-9
    )


def test_rotate_preserves_original_corners_in_same_circular_order() -> None:
    """The rotation is a seam-swap: the original corners still appear in
    the same cyclic order, just with a new seam vertex injected where
    the chain was split."""
    original = _rect_chain()
    rotated = common.rotate_closed_chain_to_nearest_point(original, (10.0, 7.0))
    # All four original corners still appear as segment starts (or ends)
    # somewhere in the rotated chain.
    def chain_points(chain: list[LineSegment]) -> list[tuple[float, float]]:
        pts = [chain[0].start]
        for seg in chain:
            pts.append(seg.end)
        return [(round(x, 6), round(y, 6)) for x, y in pts]

    orig_corners = set(chain_points(original))
    rot_pts = chain_points(rotated)
    assert orig_corners.issubset(set(rot_pts))


def test_rotate_on_arc_finds_angular_projection() -> None:
    """Full circle radius 10 centred at origin. Target at (15, 0) projects
    onto the circle at (10, 0) — which already matches the arc's start,
    so the chain is unchanged."""
    arc = ArcSegment(
        center=(0.0, 0.0), radius=10.0, start_angle_deg=0.0, sweep_deg=360.0
    )
    rotated = common.rotate_closed_chain_to_nearest_point([arc], (15.0, 0.0))
    assert rotated[0].start == pytest.approx((10.0, 0.0), abs=1e-9)


def test_rotate_on_empty_chain_returns_empty() -> None:
    assert common.rotate_closed_chain_to_nearest_point([], (0.0, 0.0)) == []


# -------------------------- resolve_safe_height / resolve_clearance cascade


def _project_for_cascade() -> Project:
    """Minimal project for cascade tests — we only touch settings +
    machine.defaults, so no geometry / operations needed."""
    return Project()


def _op(
    *, safe_height: float | None = None, clearance_plane: float | None = None,
) -> ProfileOp:
    return ProfileOp(
        name="P", safe_height=safe_height, clearance_plane=clearance_plane,
    )


def test_safe_height_cascade_prefers_op_override() -> None:
    project = _project_for_cascade()
    project.settings.safe_height = 20.0
    project.machine.defaults.safe_height = 40.0
    op = _op(safe_height=7.5)
    assert common.resolve_safe_height(op, project) == pytest.approx(7.5)


def test_safe_height_cascade_falls_through_to_project_setting() -> None:
    project = _project_for_cascade()
    project.settings.safe_height = 20.0
    project.machine.defaults.safe_height = 40.0
    op = _op()  # no op override
    assert common.resolve_safe_height(op, project) == pytest.approx(20.0)


def test_safe_height_cascade_falls_through_to_machine_default() -> None:
    """Empty op override + unset project setting → machine default."""
    project = _project_for_cascade()
    project.settings.safe_height = None  # explicit inherit
    project.machine.defaults.safe_height = 40.0
    op = _op()
    assert common.resolve_safe_height(op, project) == pytest.approx(40.0)


def test_clearance_cascade_matches_safe_height_shape() -> None:
    project = _project_for_cascade()
    project.settings.clearance_plane = None
    project.machine.defaults.clearance_plane = 5.0
    op = _op()
    assert common.resolve_clearance(op, project) == pytest.approx(5.0)
    # Project setting wins over machine.
    project.settings.clearance_plane = 2.0
    assert common.resolve_clearance(op, project) == pytest.approx(2.0)
    # Op override wins over both.
    op.clearance_plane = 0.5
    assert common.resolve_clearance(op, project) == pytest.approx(0.5)
