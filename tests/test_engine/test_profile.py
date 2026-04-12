"""Tests for pymillcam.engine.profile."""
from __future__ import annotations

import math

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import GeometryRef, MillingDirection, OffsetSide, ProfileOp
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.core.tools import CuttingData, Tool, ToolController
from pymillcam.engine.ir import MoveType
from pymillcam.engine.profile import (
    ProfileGenerationError,
    _z_levels,
    generate_profile_toolpath,
)


def _rect_segments(w: float = 50.0, h: float = 30.0, closed: bool = True) -> list[LineSegment]:
    segs = [
        LineSegment(start=(0, 0), end=(w, 0)),
        LineSegment(start=(w, 0), end=(w, h)),
        LineSegment(start=(w, h), end=(0, h)),
    ]
    if closed:
        segs.append(LineSegment(start=(0, h), end=(0, 0)))
    return segs


def _project_with_rectangle(
    *,
    offset_side: OffsetSide = OffsetSide.OUTSIDE,
    cut_depth: float = -6.0,
    stepdown: float | None = None,
    multi_depth: bool = True,
    tool_diameter: float = 3.0,
    closed: bool = True,
) -> tuple[Project, ProfileOp, GeometryEntity]:
    entity = GeometryEntity(segments=_rect_segments(closed=closed), closed=closed)
    layer = GeometryLayer(name="Profile_Outside", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": tool_diameter, "flute_length": 15,
                                        "total_length": 50, "shank_diameter": 3,
                                        "flute_count": 2})
    tc = ToolController(tool_number=1, tool=tool, feed_xy=1200.0, feed_z=300.0,
                        spindle_rpm=18000)
    op = ProfileOp(
        name="Outer",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name=layer.name, entity_id=entity.id)],
        cut_depth=cut_depth,
        stepdown=stepdown,
        multi_depth=multi_depth,
        offset_side=offset_side,
        direction=MillingDirection.CLIMB,
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op],
    )
    return project, op, entity


# ---------- _z_levels ----------------------------------------------------

def test_z_levels_multi_depth_even_division() -> None:
    assert _z_levels(-6.0, 2.0, multi_depth=True) == [-2.0, -4.0, -6.0]


def test_z_levels_multi_depth_uneven_final_pass_clamps_to_cut_depth() -> None:
    assert _z_levels(-5.0, 2.0, multi_depth=True) == [-2.0, -4.0, -5.0]


def test_z_levels_single_pass_when_multi_depth_disabled() -> None:
    assert _z_levels(-6.0, 2.0, multi_depth=False) == [-6.0]


def test_z_levels_non_negative_cut_depth_returns_empty() -> None:
    assert _z_levels(0.0, 1.0, multi_depth=True) == []
    assert _z_levels(1.5, 1.0, multi_depth=True) == []


# ---------- generate_profile_toolpath ------------------------------------

def test_generates_tool_change_spindle_on_and_off() -> None:
    project, op, _ = _project_with_rectangle(stepdown=2.0)
    tp = generate_profile_toolpath(op, project)
    types = [i.type for i in tp.instructions]
    assert types[0] == MoveType.COMMENT
    assert MoveType.TOOL_CHANGE in types
    assert MoveType.SPINDLE_ON in types
    assert MoveType.SPINDLE_OFF in types
    # Final retract is a rapid to safe_height
    assert tp.instructions[-1].type is MoveType.RAPID
    assert tp.instructions[-1].z == project.settings.safe_height


def test_multi_depth_emits_one_plunge_per_level() -> None:
    project, op, _ = _project_with_rectangle(cut_depth=-6.0, stepdown=2.0)
    tp = generate_profile_toolpath(op, project)
    plunges = [i for i in tp.instructions if i.type is MoveType.FEED and i.z is not None]
    assert [p.z for p in plunges] == [-2.0, -4.0, -6.0]
    for plunge in plunges:
        assert plunge.f == 300.0  # feed_z


def test_xy_feed_moves_use_feed_xy_rate() -> None:
    project, op, _ = _project_with_rectangle(stepdown=2.0)
    tp = generate_profile_toolpath(op, project)
    xy_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    assert xy_feeds, "expected at least one XY feed move"
    for m in xy_feeds:
        assert m.f == 1200.0  # feed_xy


def test_outside_offset_expands_contour_by_tool_radius() -> None:
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.OUTSIDE, tool_diameter=4.0, stepdown=2.0,
    )
    tp = generate_profile_toolpath(op, project)
    xy_feeds = [
        (i.x, i.y) for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    xs = [x for x, _ in xy_feeds if x is not None]
    ys = [y for _, y in xy_feeds if y is not None]
    # Rectangle is 50x30; outside offset by r=2 expands to ~54x34 centred on (25,15),
    # so XY extents should reach roughly -2..52 and -2..32.
    assert math.isclose(min(xs), -2.0, abs_tol=1e-6)
    assert math.isclose(max(xs), 52.0, abs_tol=1e-6)
    assert math.isclose(min(ys), -2.0, abs_tol=1e-6)
    assert math.isclose(max(ys), 32.0, abs_tol=1e-6)


def test_inside_offset_shrinks_contour() -> None:
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.INSIDE, tool_diameter=4.0, stepdown=2.0,
    )
    tp = generate_profile_toolpath(op, project)
    xy_feeds = [
        (i.x, i.y) for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    xs = [x for x, _ in xy_feeds if x is not None]
    ys = [y for _, y in xy_feeds if y is not None]
    assert math.isclose(min(xs), 2.0, abs_tol=1e-6)
    assert math.isclose(max(xs), 48.0, abs_tol=1e-6)
    assert math.isclose(min(ys), 2.0, abs_tol=1e-6)
    assert math.isclose(max(ys), 28.0, abs_tol=1e-6)


def test_on_line_offset_traces_contour_exactly() -> None:
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, tool_diameter=4.0, stepdown=2.0,
    )
    tp = generate_profile_toolpath(op, project)
    xy_feeds = [
        (i.x, i.y) for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    xs = [x for x, _ in xy_feeds]
    ys = [y for _, y in xy_feeds]
    assert math.isclose(min(xs), 0.0, abs_tol=1e-6)
    assert math.isclose(max(xs), 50.0, abs_tol=1e-6)
    assert math.isclose(min(ys), 0.0, abs_tol=1e-6)
    assert math.isclose(max(ys), 30.0, abs_tol=1e-6)


def test_missing_tool_controller_raises() -> None:
    project, op, _ = _project_with_rectangle(stepdown=2.0)
    op.tool_controller_id = 99  # nonexistent
    with pytest.raises(ProfileGenerationError, match="tool_controller"):
        generate_profile_toolpath(op, project)


def test_no_tool_controller_id_raises() -> None:
    project, op, _ = _project_with_rectangle(stepdown=2.0)
    op.tool_controller_id = None
    with pytest.raises(ProfileGenerationError, match="no tool_controller_id"):
        generate_profile_toolpath(op, project)


def test_inside_offset_too_large_raises() -> None:
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.INSIDE, tool_diameter=100.0, stepdown=2.0,
    )
    with pytest.raises(ProfileGenerationError, match="tool too large"):
        generate_profile_toolpath(op, project)


def test_inside_offset_on_open_contour_raises() -> None:
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.INSIDE, stepdown=2.0, closed=False,
    )
    with pytest.raises(ProfileGenerationError, match="closed contour"):
        generate_profile_toolpath(op, project)


def test_on_line_works_on_open_contour() -> None:
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, stepdown=2.0, closed=False,
    )
    tp = generate_profile_toolpath(op, project)
    # Should produce XY feeds without error.
    xy_feeds = [i for i in tp.instructions
                if i.type is MoveType.FEED and i.x is not None and i.z is None]
    assert xy_feeds


def test_stepdown_falls_back_to_tool_cutting_data() -> None:
    project, op, _ = _project_with_rectangle(cut_depth=-4.0, stepdown=None)
    # Seed the tool with cutting data for fallback resolution
    tc = project.tool_controllers[0]
    tc.tool.cutting_data["plywood"] = CuttingData(stepdown=2.0, feed_xy=1000, feed_z=250)
    tp = generate_profile_toolpath(op, project)
    plunges = [i.z for i in tp.instructions if i.type is MoveType.FEED and i.z is not None]
    assert plunges == [-2.0, -4.0]


def test_stepdown_defaults_when_no_cutting_data() -> None:
    project, op, _ = _project_with_rectangle(cut_depth=-2.0, stepdown=None)
    tp = generate_profile_toolpath(op, project)
    plunges = [i.z for i in tp.instructions if i.type is MoveType.FEED and i.z is not None]
    # Default DEFAULT_STEPDOWN_MM = 1.0 → 2 passes at -1, -2
    assert plunges == [-1.0, -2.0]


# ---------- Arc preservation through the engine --------------------------

def _project_with_arc_segment(
    *,
    offset_side: OffsetSide = OffsetSide.ON_LINE,
    sweep_deg: float = 90.0,
) -> tuple[Project, ProfileOp]:
    """Build a project whose contour is a single ArcSegment, not chord-approximated."""
    arc = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=sweep_deg)
    entity = GeometryEntity(segments=[arc], closed=(abs(sweep_deg) >= 360))
    layer = GeometryLayer(name="Profile", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0, "flute_length": 15,
                                        "total_length": 50, "shank_diameter": 3,
                                        "flute_count": 2})
    tc = ToolController(tool_number=1, tool=tool, feed_xy=1200.0, feed_z=300.0,
                        spindle_rpm=18000)
    op = ProfileOp(
        name="ArcProfile",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name=layer.name, entity_id=entity.id)],
        cut_depth=-2.0,
        stepdown=1.0,
        multi_depth=True,
        offset_side=offset_side,
        direction=MillingDirection.CLIMB,
    )
    project = Project(geometry_layers=[layer], tool_controllers=[tc], operations=[op])
    return project, op


def test_arc_segment_emits_arc_ccw_ir_on_line() -> None:
    project, op = _project_with_arc_segment(sweep_deg=90.0)
    tp = generate_profile_toolpath(op, project)
    arc_moves = [i for i in tp.instructions if i.type is MoveType.ARC_CCW]
    # Two passes (cut_depth=-2, stepdown=1) → two arc moves.
    assert len(arc_moves) == 2
    move = arc_moves[0]
    # Arc from (10, 0) CCW 90° around (0, 0) ends at (0, 10); I=-10, J=0.
    assert math.isclose(move.x, 0.0, abs_tol=1e-9)
    assert math.isclose(move.y, 10.0, abs_tol=1e-9)
    assert math.isclose(move.i, -10.0)
    assert math.isclose(move.j, 0.0)
    assert move.f == 1200.0


def test_arc_segment_emits_arc_cw_for_negative_sweep() -> None:
    project, op = _project_with_arc_segment(sweep_deg=-90.0)
    tp = generate_profile_toolpath(op, project)
    assert any(i.type is MoveType.ARC_CW for i in tp.instructions)
    assert not any(i.type is MoveType.ARC_CCW for i in tp.instructions)


def test_on_line_offset_never_discretizes_arc_to_many_chords() -> None:
    """ON_LINE offset must not chord-approximate arcs, even with high-sweep arcs."""
    project, op = _project_with_arc_segment(sweep_deg=180.0)
    tp = generate_profile_toolpath(op, project)
    # Each pass emits exactly one ARC move — if we were discretizing, we'd see
    # a stream of FEED moves along the arc instead.
    arc_moves = [i for i in tp.instructions if i.type is MoveType.ARC_CCW]
    assert len(arc_moves) == 2  # 2 passes × 1 arc per pass


def test_full_circle_arc_emits_one_arc_move_per_pass() -> None:
    """A full circle should be one G02/G03 per pass, not 72 chord segments."""
    project, op = _project_with_arc_segment(sweep_deg=360.0)
    tp = generate_profile_toolpath(op, project)
    arc_moves = [i for i in tp.instructions if i.type is MoveType.ARC_CCW]
    assert len(arc_moves) == 2  # 2 passes, each a single full-circle arc


def test_chord_tolerance_cascades_from_operation() -> None:
    """Operation.chord_tolerance overrides ProjectSettings.chord_tolerance."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.OUTSIDE, tool_diameter=4.0, stepdown=2.0,
    )
    # Tighten operation tolerance; buffer result shouldn't change here (rectangle
    # has only line segments) but the resolution path is exercised.
    op.chord_tolerance = 0.001
    tp = generate_profile_toolpath(op, project)
    # Sanity: still produces XY feeds.
    xy_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    assert xy_feeds
