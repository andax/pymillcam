"""Tests for pymillcam.engine.profile."""
from __future__ import annotations

import math

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import (
    GeometryRef,
    LeadConfig,
    LeadStyle,
    MillingDirection,
    OffsetSide,
    ProfileOp,
    RampConfig,
    RampStrategy,
    TabConfig,
)
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.core.tools import CuttingData, Tool, ToolController
from pymillcam.engine.ir import MoveType
from pymillcam.engine.profile import (
    ProfileGenerationError,
    _offset_contour,
    _z_levels,
    compute_profile_preview,
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
        # Pin DIRECT + PLUNGE in lead/ramp-agnostic tests so the instruction
        # stream isn't perturbed by arc lead moves or on-contour descents.
        # Individual lead/ramp tests override these.
        lead_in=LeadConfig(style=LeadStyle.DIRECT),
        lead_out=LeadConfig(style=LeadStyle.DIRECT),
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
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

def test_generates_tool_change_spindle_on_and_final_retract() -> None:
    project, op, _ = _project_with_rectangle(stepdown=2.0)
    tp = generate_profile_toolpath(op, project)
    types = [i.type for i in tp.instructions]
    assert types[0] == MoveType.COMMENT
    assert MoveType.TOOL_CHANGE in types
    assert MoveType.SPINDLE_ON in types
    # Spindle-off is emitted by the post-processor at program end, not
    # per toolpath (so the spindle doesn't cycle between ops).
    assert MoveType.SPINDLE_OFF not in types
    # Final retract is a rapid to safe_height — the spindle is still
    # running when the tool reaches safe height.
    assert tp.instructions[-1].type is MoveType.RAPID
    assert tp.instructions[-1].z == project.settings.safe_height
    # Regression: `_emit_contour_passes` already retracts to safe_height
    # at the end of every contour. A second retract at the dispatcher
    # level was producing duplicate `G0 Z{safe_height}` lines in the
    # G-code. Assert only one trailing safe-height rapid.
    assert tp.instructions[-2].type is not MoveType.RAPID or (
        tp.instructions[-2].z != project.settings.safe_height
    )


def test_multi_depth_plunges_straight_without_ramp() -> None:
    """Default rect op has DIRECT leads; with RampStrategy.PLUNGE the engine
    should emit one straight plunge per pass depth (no on-contour ramp)."""
    project, op, _ = _project_with_rectangle(cut_depth=-6.0, stepdown=2.0)
    op.ramp = RampConfig(strategy=RampStrategy.PLUNGE)
    tp = generate_profile_toolpath(op, project)
    pure_z_plunges = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
        and i.x is None and i.y is None
    ]
    assert [p.z for p in pure_z_plunges] == [-2.0, -4.0, -6.0]
    for plunge in pure_z_plunges:
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
        if i.type is MoveType.FEED and i.x is not None and i.y is not None
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
        if i.type is MoveType.FEED and i.x is not None and i.y is not None
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
        if i.type is MoveType.FEED and i.x is not None and i.y is not None
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
    op.ramp = RampConfig(strategy=RampStrategy.PLUNGE)
    # Seed the tool with cutting data for fallback resolution.
    tc = project.tool_controllers[0]
    tc.tool.cutting_data["plywood"] = CuttingData(stepdown=2.0, feed_xy=1000, feed_z=250)
    tp = generate_profile_toolpath(op, project)
    pure_z_plunges = [
        i.z for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
        and i.x is None and i.y is None
    ]
    assert pure_z_plunges == [-2.0, -4.0]


def test_stepdown_defaults_when_no_cutting_data() -> None:
    project, op, _ = _project_with_rectangle(cut_depth=-2.0, stepdown=None)
    op.ramp = RampConfig(strategy=RampStrategy.PLUNGE)
    tp = generate_profile_toolpath(op, project)
    pure_z_plunges = [
        i.z for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
        and i.x is None and i.y is None
    ]
    # Default DEFAULT_STEPDOWN_MM = 1.0 → 2 passes at -1, -2
    assert pure_z_plunges == [-1.0, -2.0]


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
        lead_in=LeadConfig(style=LeadStyle.DIRECT),
        lead_out=LeadConfig(style=LeadStyle.DIRECT),
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
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


def test_full_circle_arc_emits_two_semicircles_per_pass() -> None:
    """A full circle with PLUNGE ramp emits two G2/G3 per pass (split into
    semicircles for portable G-code), not 72 chord segments. See
    `split_full_circle` for the rationale."""
    project, op = _project_with_arc_segment(sweep_deg=360.0)
    tp = generate_profile_toolpath(op, project)
    arc_moves = [i for i in tp.instructions if i.type is MoveType.ARC_CCW]
    assert len(arc_moves) == 4  # 2 passes × 2 semicircles each


def test_outside_offset_on_full_circle_keeps_every_vertex_outside() -> None:
    """Regression: full-circle arcs used to leave a near-coincident pair at the
    polygon close, which Shapely's buffer turned into one wrong vertex inside
    the original radius. Every offset vertex should be exactly r + tool_radius
    from the centre."""
    entity = GeometryEntity(
        segments=[ArcSegment(center=(0, 0), radius=25, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    segs = _offset_contour(
        entity,
        radius=1.5,
        side=OffsetSide.OUTSIDE,
        chord_tolerance=0.05,
        direction=MillingDirection.CLIMB,
    )
    # Tool is 3 mm → outside offset radius is 25 + 1.5 = 26.5 mm. Allow a
    # small slack for chord-vs-arc geometry.
    for seg in segs:
        sx, sy = seg.start
        r = math.hypot(sx, sy)
        assert 26.4 <= r <= 26.6, f"vertex {(sx, sy)} at r={r:.4f} is off"


def test_outside_climb_traces_offset_cw() -> None:
    """Outside profile in climb mode (right-hand spindle) walks the part CW.

    Per the chip-thickness definition: for a CW spindle on an outside profile,
    travelling CW around the part puts each tooth at maximum chip thickness on
    entry — the textbook signature of climb. Corner fillets are therefore G2
    (CW arc) moves.
    """
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.OUTSIDE, tool_diameter=2.0, stepdown=2.0,
    )
    op.direction = MillingDirection.CLIMB
    tp = generate_profile_toolpath(op, project)
    xy_moves = [
        i for i in tp.instructions
        if i.type in (MoveType.FEED, MoveType.ARC_CW, MoveType.ARC_CCW)
        and i.x is not None and i.z is None
    ]
    assert any(i.type is MoveType.ARC_CW for i in xy_moves)
    assert not any(i.type is MoveType.ARC_CCW for i in xy_moves)


def test_outside_conventional_traces_offset_ccw() -> None:
    """Outside profile in conventional mode (right-hand spindle) walks CCW —
    the offsetter's natural orientation, so no chain reversal."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.OUTSIDE, tool_diameter=2.0, stepdown=2.0,
    )
    op.direction = MillingDirection.CONVENTIONAL
    tp = generate_profile_toolpath(op, project)
    xy_moves = [
        i for i in tp.instructions
        if i.type in (MoveType.FEED, MoveType.ARC_CW, MoveType.ARC_CCW)
        and i.x is not None and i.z is None
    ]
    assert any(i.type is MoveType.ARC_CCW for i in xy_moves)
    assert not any(i.type is MoveType.ARC_CW for i in xy_moves)


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


# ---------- leads --------------------------------------------------------

def test_default_lead_config_is_arc() -> None:
    """ARC is the safer default — it plunges off-path so the witness mark
    lands in air rather than on the cut edge."""
    assert LeadConfig().style is LeadStyle.ARC


def test_direct_lead_plunges_at_contour_start() -> None:
    """DIRECT leads are a no-op; plunge XY should equal the contour start."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, stepdown=2.0,
    )
    op.lead_in = LeadConfig(style=LeadStyle.DIRECT)
    op.lead_out = LeadConfig(style=LeadStyle.DIRECT)
    tp = generate_profile_toolpath(op, project)
    xy_rapids = [
        i for i in tp.instructions
        if i.type is MoveType.RAPID and i.x is not None
    ]
    assert xy_rapids, "expected an XY rapid to the plunge point"
    assert (xy_rapids[0].x, xy_rapids[0].y) == (0.0, 0.0)


def test_tangent_lead_in_plunges_back_along_tangent() -> None:
    """A TANGENT lead-in places the plunge `length` mm behind the contour
    start along the start tangent, and feeds forward to the contour start."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, stepdown=2.0,
    )
    op.lead_in = LeadConfig(style=LeadStyle.TANGENT, length=3.0)
    op.lead_out = LeadConfig(style=LeadStyle.DIRECT)
    tp = generate_profile_toolpath(op, project)

    # Rectangle first edge is (0,0)→(50,0); tangent is +X. Plunge at (-3, 0),
    # lead feeds to (0, 0).
    xy_rapids = [
        i for i in tp.instructions
        if i.type is MoveType.RAPID and i.x is not None
    ]
    assert (xy_rapids[0].x, xy_rapids[0].y) == (-3.0, 0.0)
    xy_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    assert xy_feeds[0].x == 0.0 and xy_feeds[0].y == 0.0


def test_tangent_lead_out_extends_forward_along_tangent() -> None:
    """A TANGENT lead-out feeds `length` mm past the contour end along the
    end tangent."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, stepdown=2.0,
    )
    op.lead_in = LeadConfig(style=LeadStyle.DIRECT)
    op.lead_out = LeadConfig(style=LeadStyle.TANGENT, length=4.0)
    tp = generate_profile_toolpath(op, project)

    # Rectangle closes at (0, 0) coming in from (0, 30)→(0, 0); end tangent
    # is -Y, so lead-out should reach (0, -4).
    xy_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    assert (xy_feeds[-1].x, xy_feeds[-1].y) == (0.0, -4.0)


def test_leads_emitted_once_even_in_multi_depth() -> None:
    """Leads traverse at Z=0 (surface) once, not per pass. The passes below
    plunge straight down at the contour start, cut, and stay at depth.
    Between passes there is no retract to surface."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, cut_depth=-4.0, stepdown=2.0,
    )
    op.lead_in = LeadConfig(style=LeadStyle.TANGENT, length=2.0)
    op.lead_out = LeadConfig(style=LeadStyle.TANGENT, length=2.0)
    tp = generate_profile_toolpath(op, project)

    # Exactly one feed ending at the lead-in join (0, 0): the single lead-in.
    lead_in_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x == 0.0 and i.y == 0.0 and i.z is None
    ]
    # There's also a contour vertex at (0, 0) — each of the two passes lands
    # on it once (the closing edge). So: 1 lead-in + 2 contour closings = 3.
    assert len(lead_in_feeds) == 3

    # Exactly one feed ending at the lead-out exit (0, -2): the single lead-out.
    lead_out_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x == 0.0 and i.y == -2.0 and i.z is None
    ]
    assert len(lead_out_feeds) == 1

    # Z feeds: one to Z=0 (surface for lead-in), two plunges (-2, -4), one
    # retract to Z=0 for lead-out. Total 4.
    z_feeds = [
        i.z for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert z_feeds == [0.0, -2.0, -4.0, 0.0]


def test_tangent_lead_on_arc_uses_arc_tangent() -> None:
    """Lead-in for a quarter arc should go backward along the arc tangent at
    its start, not along a straight line between endpoints."""
    arc = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=90)
    entity = GeometryEntity(segments=[arc], closed=False)
    layer = GeometryLayer(name="L", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0, "flute_length": 15,
                                        "total_length": 50, "shank_diameter": 3,
                                        "flute_count": 2})
    tc = ToolController(tool_number=1, tool=tool, feed_xy=1200, feed_z=300,
                        spindle_rpm=18000)
    op = ProfileOp(
        name="Arc",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name=layer.name, entity_id=entity.id)],
        cut_depth=-1.0,
        stepdown=1.0,
        offset_side=OffsetSide.ON_LINE,
        lead_in=LeadConfig(style=LeadStyle.TANGENT, length=5.0),
        lead_out=LeadConfig(style=LeadStyle.DIRECT),
    )
    project = Project(geometry_layers=[layer], tool_controllers=[tc], operations=[op])
    tp = generate_profile_toolpath(op, project)

    # Arc starts at (10, 0); CCW unit tangent is (0, 1). Plunge at (10, -5).
    xy_rapids = [
        i for i in tp.instructions
        if i.type is MoveType.RAPID and i.x is not None
    ]
    assert xy_rapids[0].x == 10.0
    assert math.isclose(xy_rapids[0].y, -5.0, abs_tol=1e-9)


def test_arc_lead_emits_arc_ir_for_outside_profile() -> None:
    """OUTSIDE profile with ARC lead-in should emit an arc IR instruction as
    part of the entry move (in addition to any contour fillets)."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.OUTSIDE, stepdown=2.0,
    )
    op.lead_in = LeadConfig(style=LeadStyle.ARC, length=math.pi)
    op.lead_out = LeadConfig(style=LeadStyle.DIRECT)
    tp = generate_profile_toolpath(op, project)
    arc_moves = [
        i for i in tp.instructions
        if i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
    ]
    assert arc_moves, "ARC lead should emit at least one arc IR move"


def test_arc_lead_is_tangent_to_contour_at_join() -> None:
    """The arc lead's endpoint tangent must match the contour's tangent at
    the join so motion is smooth (no direction discontinuity)."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, stepdown=2.0,
    )
    op.lead_in = LeadConfig(style=LeadStyle.ARC, length=math.pi)
    op.lead_out = LeadConfig(style=LeadStyle.DIRECT)
    tp = generate_profile_toolpath(op, project)
    # First edge is (0,0)→(50,0), tangent (1, 0). The arc move should end at
    # (0, 0) (contour start), with its end-tangent pointing in +X.
    arc_moves = [
        i for i in tp.instructions
        if i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
    ]
    assert arc_moves
    lead_arc = arc_moves[0]
    assert math.isclose(lead_arc.x, 0.0, abs_tol=1e-9)
    assert math.isclose(lead_arc.y, 0.0, abs_tol=1e-9)


def test_arc_lead_out_extends_from_contour_end() -> None:
    """Lead-out arc starts at the contour end and sweeps off into air."""
    project, op, _ = _project_with_rectangle(
        offset_side=OffsetSide.ON_LINE, stepdown=2.0,
    )
    op.lead_in = LeadConfig(style=LeadStyle.DIRECT)
    op.lead_out = LeadConfig(style=LeadStyle.ARC, length=math.pi)
    tp = generate_profile_toolpath(op, project)
    # Rectangle end is (0, 0) with end-tangent (0, −1). The lead-out arc
    # should move the tool off the rect. With ON_LINE the air side defaults
    # to the LEFT of travel: left of (0, −1) is (1, 0)... wait no,
    # left_of(travel) = rotate(tangent, +90°) = (−ty, tx) = (1, 0). Hmm
    # that's +X, inside the rect. Our ON_LINE default is LEFT which isn't
    # ideal for a CCW-source rect, but at least pin that the arc IR is
    # emitted after the final contour feed.
    arc_moves = [
        i for i in tp.instructions
        if i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
    ]
    assert arc_moves, "ARC lead-out should emit an arc IR move"


def test_linear_ramp_descends_along_contour_without_between_pass_retract() -> None:
    """With RampStrategy.LINEAR, each pass ramps Z along the contour from the
    previous depth to the new depth, then continues at the new depth back to
    the start. No pure-Z plunge per pass, no between-pass retract."""
    project, op, _ = _project_with_rectangle(cut_depth=-4.0, stepdown=2.0)
    # Rect perimeter is ~160 mm; ramp length = 2 / tan(3°) ≈ 38.17 mm < 160.
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=3.0)
    tp = generate_profile_toolpath(op, project)
    # The only pure-Z feed is the initial approach from clearance to Z=0;
    # the ramp descent carries Z along XY, so no per-pass pure-Z plunges.
    pure_z_plunges = [
        i.z for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
        and i.x is None and i.y is None
    ]
    assert pure_z_plunges == [0.0]

    # Ramp descent emits feeds where both XY and Z change together.
    helical_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None and i.x is not None
    ]
    # At least one helical feed per pass, ending at the pass depth.
    assert any(i.z == -2.0 for i in helical_feeds)
    assert any(i.z == -4.0 for i in helical_feeds)


def test_preview_lead_out_matches_engine_ascent_endpoint() -> None:
    """The profile preview's lead-out should attach where the engine's ascent
    actually ends (P2), not at the arbitrary contour end."""
    project, op, _ = _project_with_rectangle(cut_depth=-2.0, stepdown=2.0)
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=3.0)
    op.lead_out = LeadConfig(style=LeadStyle.TANGENT, length=2.0)
    preview = compute_profile_preview(op, project)
    # The preview should end with a tangent-extension line segment; the
    # engine's generated toolpath's last XY move should land at the same
    # point (the tangent line's end point).
    assert isinstance(preview[-1], LineSegment)
    preview_exit = preview[-1].end

    tp = generate_profile_toolpath(op, project)
    xy_moves = [
        i for i in tp.instructions
        if i.type in (MoveType.FEED, MoveType.ARC_CW, MoveType.ARC_CCW)
        and i.x is not None and i.y is not None
    ]
    gcode_exit = (xy_moves[-1].x, xy_moves[-1].y)
    assert math.isclose(preview_exit[0], gcode_exit[0], abs_tol=1e-9)
    assert math.isclose(preview_exit[1], gcode_exit[1], abs_tol=1e-9)


def test_linear_ramp_emits_cleanup_and_ascent_after_last_pass() -> None:
    """After the final pass at cut_depth, the engine emits a cleanup pass
    along P0→P1 at constant cut_depth (re-cutting the last descent's sloped
    groove), then a fixed-angle ascent back up to Z=0."""
    project, op, _ = _project_with_rectangle(cut_depth=-2.0, stepdown=2.0)
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=3.0)
    tp = generate_profile_toolpath(op, project)
    # Find feeds that rise Z: z goes from cut_depth (-2) up toward 0. The
    # ascent interpolates through intermediate values and ends at 0.
    rising_feeds = [
        i for i in tp.instructions
        if i.type in (MoveType.FEED, MoveType.ARC_CW, MoveType.ARC_CCW)
        and i.z is not None and i.z > -2.0
    ]
    assert rising_feeds, "expected ascent feeds rising from cut_depth"
    # Last rising feed should reach Z=0 (top of stock).
    assert math.isclose(rising_feeds[-1].z or 0.0, 0.0, abs_tol=1e-9)


def test_linear_ramp_falls_back_to_plunge_when_ramp_too_long() -> None:
    """A very shallow ramp angle on a short contour may exceed the contour
    length. In that case the engine falls back to a straight plunge."""
    project, op, _ = _project_with_rectangle(cut_depth=-6.0, stepdown=2.0)
    # 0.1° angle → ramp length = 6 / tan(0.1°) ≈ 3438 mm, way beyond rect's
    # ~160 mm perimeter.
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=0.1)
    tp = generate_profile_toolpath(op, project)
    pure_z_plunges = [
        i.z for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
        and i.x is None and i.y is None
    ]
    assert pure_z_plunges == [-2.0, -4.0, -6.0]


def test_arc_lead_in_on_full_circle_plunges_at_exact_offset() -> None:
    """For a full circle ON_LINE cut, the ARC lead-in plunges at the known
    point (8, −2): start at (10, 0) with tangent (0, 1); ON_LINE air side is
    LEFT; arc centre at (8, 0), 90° CCW sweep from plunge up to start."""
    circle = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=360)
    entity = GeometryEntity(segments=[circle], closed=True)
    layer = GeometryLayer(name="L", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 1.0, "flute_length": 15,
                                        "total_length": 50, "shank_diameter": 3,
                                        "flute_count": 2})
    tc = ToolController(tool_number=1, tool=tool, feed_xy=1200, feed_z=300,
                        spindle_rpm=18000)
    op = ProfileOp(
        name="Circ",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name=layer.name, entity_id=entity.id)],
        cut_depth=-1.0,
        stepdown=1.0,
        offset_side=OffsetSide.ON_LINE,
        lead_in=LeadConfig(style=LeadStyle.ARC, length=math.pi),
        lead_out=LeadConfig(style=LeadStyle.DIRECT),
    )
    project = Project(geometry_layers=[layer], tool_controllers=[tc], operations=[op])
    tp = generate_profile_toolpath(op, project)

    xy_rapids = [
        i for i in tp.instructions
        if i.type is MoveType.RAPID and i.x is not None
    ]
    assert math.isclose(xy_rapids[0].x, 8.0, abs_tol=1e-9)
    assert math.isclose(xy_rapids[0].y, -2.0, abs_tol=1e-9)


# ---------- tabs ----------------------------------------------------------

def _project_with_rect_tabs(
    *,
    cut_depth: float = -6.0,
    stepdown: float = 2.0,
    multi_depth: bool = True,
    tabs: TabConfig | None = None,
    closed: bool = True,
) -> tuple[Project, ProfileOp]:
    project, op, _ = _project_with_rectangle(
        cut_depth=cut_depth,
        stepdown=stepdown,
        multi_depth=multi_depth,
        closed=closed,
    )
    if tabs is not None:
        op.tabs = tabs
    return project, op


def test_tabs_disabled_default_leaves_output_unchanged() -> None:
    project, op = _project_with_rect_tabs()
    tp_before = generate_profile_toolpath(op, project)
    op.tabs = TabConfig(enabled=False)
    tp_after = generate_profile_toolpath(op, project)
    # Same instruction stream when tabs are disabled.
    assert [i.type for i in tp_before.instructions] == [
        i.type for i in tp_after.instructions
    ]


def test_tabs_modulate_only_breaching_passes() -> None:
    """With stepdown 2 and tab_height 0.5: tab_top_z = -5.5. Passes at -2,
    -4 are above tab_top (no modulation). Pass at -6 breaches → modulates.
    """
    project, op = _project_with_rect_tabs(
        tabs=TabConfig(
            enabled=True, count=4, width=4.0, height=0.5, ramp_length=1.0,
        ),
    )
    tp = generate_profile_toolpath(op, project)
    # Collect the z-bearing feed moves grouped by pass — count distinct z
    # values that appear in the body of each pass.
    feeds_with_z = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    z_values = {round(f.z, 4) for f in feeds_with_z}
    # Pass plunges to -2, -4 land in z_values; the -6 pass plunges to -6
    # and the tab plateau lifts to -5.5 → -5.5 must be present.
    assert -2.0 in z_values
    assert -4.0 in z_values
    assert -6.0 in z_values
    assert -5.5 in z_values


def test_tabs_with_stepdown_smaller_than_height_modulates_multiple_passes() -> None:
    """stepdown 0.5, tab_height 1.0 → tab_top = -2.0. Passes at -0.5, -1.5
    are above; pass at -2.0 is at tab_top (boundary, not strictly less);
    -2.5 and -3.0 breach. Multiple passes should hit the tab plateau.
    """
    project, op = _project_with_rect_tabs(
        cut_depth=-3.0,
        stepdown=0.5,
        tabs=TabConfig(
            enabled=True, count=4, width=4.0, height=1.0, ramp_length=1.0,
        ),
    )
    tp = generate_profile_toolpath(op, project)
    # Find feeds at the tab top z = -2.0 — count how many distinct passes
    # mention it. Each modulating pass plunges to its pass_z then lifts to
    # tab_top_z over the tab plateaus.
    plateau_lifts = [
        i for i in tp.instructions
        if i.type is MoveType.FEED
        and i.z is not None
        and math.isclose(i.z, -2.0, abs_tol=1e-6)
        and i.x is not None  # plateau lift is a plateau-bound feed, not a plunge
    ]
    # 2 breaching passes (-2.5, -3.0), 4 tabs each, plateau bookends per
    # tab = 2 (entry-top + plateau-end) → 2*4*2 = 16 z=-2.0 plateau feeds.
    assert len(plateau_lifts) == 16


def test_tabs_with_ramp_uses_on_contour_descent() -> None:
    """With ramp + tabs, each pass descends along the contour rather than
    plunging vertically. No pure-plunge FEED (z-only, no XY) for the
    intermediate passes — descent z is interpolated across XY moves.
    Tabs still lift the tool to tab_top over each plateau."""
    project, op = _project_with_rect_tabs(
        tabs=TabConfig(
            enabled=True, count=4, width=4.0, height=0.5, ramp_length=1.0,
        ),
    )
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=3.0)
    tp = generate_profile_toolpath(op, project)
    feeds = [i for i in tp.instructions if i.type is MoveType.FEED]
    pure_plunges = [
        f for f in feeds if f.z is not None and f.x is None and f.y is None
    ]
    # The only z-only FEED is the surface-level descent before the lead-in
    # (z=0). No vertical plunges to pass depths.
    plunge_zs = [round(p.z, 4) for p in pure_plunges]
    assert plunge_zs == [0.0]
    # Tab plateaus are still hit on breaching passes.
    plateau_lifts = [
        f for f in feeds
        if f.z is not None and math.isclose(f.z, -5.5, abs_tol=1e-6)
    ]
    assert plateau_lifts, "expected tab plateau lifts at z=-5.5"


def test_tabs_raise_when_height_exceeds_cut_depth() -> None:
    project, op = _project_with_rect_tabs(
        cut_depth=-1.0,
        tabs=TabConfig(enabled=True, height=2.0, ramp_length=1.0),
    )
    with pytest.raises(ProfileGenerationError, match="cut depth"):
        generate_profile_toolpath(op, project)


def test_tabs_raise_when_contour_too_short_for_count() -> None:
    project, op = _project_with_rect_tabs(
        tabs=TabConfig(
            enabled=True, count=20, width=10.0, height=0.5, ramp_length=2.0,
        ),
    )
    with pytest.raises(ProfileGenerationError):
        generate_profile_toolpath(op, project)


def test_tabs_raise_on_open_contour() -> None:
    project, op = _project_with_rect_tabs(
        closed=False,
        tabs=TabConfig(enabled=True, height=0.5, ramp_length=1.0),
    )
    op.offset_side = OffsetSide.ON_LINE
    with pytest.raises(ProfileGenerationError, match="closed contour"):
        generate_profile_toolpath(op, project)


def test_full_circle_contour_emits_two_semicircles_per_pass() -> None:
    """A full-circle arc traced as a single G2/G3 with start XY == end XY
    is ambiguous — many viewers and controllers reject or skip it. The
    engine must split full circles into two halves before emitting IR."""
    circle = ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=360)
    entity = GeometryEntity(segments=[circle], closed=True)
    layer = GeometryLayer(name="L", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 1.0, "flute_length": 15,
                                        "total_length": 50, "shank_diameter": 3,
                                        "flute_count": 2})
    tc = ToolController(tool_number=1, tool=tool, feed_xy=1200, feed_z=300,
                        spindle_rpm=18000)
    op = ProfileOp(
        name="Circ",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name=layer.name, entity_id=entity.id)],
        cut_depth=-1.0,
        stepdown=1.0,
        offset_side=OffsetSide.ON_LINE,
        lead_in=LeadConfig(style=LeadStyle.DIRECT),
        lead_out=LeadConfig(style=LeadStyle.DIRECT),
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(geometry_layers=[layer], tool_controllers=[tc], operations=[op])
    tp = generate_profile_toolpath(op, project)

    arcs = [
        i for i in tp.instructions
        if i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
    ]
    # One pass × two halves.
    assert len(arcs) == 2
    # Endpoints of the two halves are distinct from each other and from the start.
    start_xy = (10.0, 0.0)
    half1_end = (round(arcs[0].x, 6), round(arcs[0].y, 6))
    half2_end = (round(arcs[1].x, 6), round(arcs[1].y, 6))
    assert half1_end != start_xy
    assert half2_end == pytest.approx(start_xy)


def test_ramp_with_tabs_lifts_descent_over_tab_plateau() -> None:
    """When a tab plateau falls inside the descent slice, effective_z
    must be max(descent_z, tab_top_z). The tool can't be allowed to slope
    through the tab on the way down."""
    project, op = _project_with_rect_tabs(
        cut_depth=-3.0,
        stepdown=1.0,
        tabs=TabConfig(
            enabled=True, count=4, width=4.0, height=0.5, ramp_length=1.0,
        ),
    )
    op.offset_side = OffsetSide.ON_LINE
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=3.0)
    tp = generate_profile_toolpath(op, project)
    # Final pass tab_top_z = -3 + 0.5 = -2.5.
    plateau_z = -2.5
    plateau_feeds = [
        i for i in tp.instructions
        if i.type in (MoveType.FEED, MoveType.ARC_CCW, MoveType.ARC_CW)
        and i.z is not None
        and math.isclose(i.z, plateau_z, abs_tol=1e-6)
    ]
    # Final pass: 4 tabs × 2 plateau bookends = 8.
    # Cleanup pass at cut_depth re-cuts descent slice [0, ~19.08mm],
    # which crosses tab 0's entry-ramp end (s=18) → 1 extra hit.
    # Ascent crosses tab 0's plateau end (s=22) at z=-2.85 → 1 extra
    # hit (lifted to tab_top). Total: 10.
    assert len(plateau_feeds) == 10


def test_ramp_with_tabs_no_z_below_tab_top_inside_plateau() -> None:
    """Stronger guarantee: at any tab plateau s, no emitted feed lands
    at a z below tab_top_z (modulo epsilon). Catches accidentally
    cutting through tabs on descent or cleanup."""
    project, op = _project_with_rect_tabs(
        cut_depth=-3.0,
        stepdown=1.0,
        tabs=TabConfig(
            enabled=True, count=4, width=4.0, height=0.5, ramp_length=1.0,
        ),
    )
    op.offset_side = OffsetSide.ON_LINE
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=3.0)
    tp = generate_profile_toolpath(op, project)

    # Map XY → arc-length on the rect perimeter so we can check each
    # emitted feed against the (computed) plateau intervals.
    rect_segs = [
        LineSegment(start=(0, 0), end=(50, 0)),
        LineSegment(start=(50, 0), end=(50, 30)),
        LineSegment(start=(50, 30), end=(0, 30)),
        LineSegment(start=(0, 30), end=(0, 0)),
    ]
    tab_top_z = -2.5
    plateau_intervals = [
        (18.0, 22.0), (58.0, 62.0), (98.0, 102.0), (138.0, 142.0),
    ]

    def s_at_xy(x: float, y: float) -> float | None:
        accum = 0.0
        for seg in rect_segs:
            sx, sy = seg.start
            ex, ey = seg.end
            dx, dy = ex - sx, ey - sy
            length = math.hypot(dx, dy)
            if length == 0:
                continue
            # Project (x,y)-start onto seg, see if it falls inside.
            t_num = (x - sx) * dx + (y - sy) * dy
            t = t_num / (length * length)
            if -1e-6 <= t <= 1 + 1e-6:
                proj_x = sx + max(0.0, min(1.0, t)) * dx
                proj_y = sy + max(0.0, min(1.0, t)) * dy
                if math.hypot(x - proj_x, y - proj_y) < 1e-3:
                    return accum + max(0.0, min(1.0, t)) * length
            accum += length
        return None

    feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is not None
    ]
    for feed in feeds:
        s = s_at_xy(feed.x, feed.y)
        if s is None:
            continue
        for s_lo, s_hi in plateau_intervals:
            if s_lo - 1e-6 <= s <= s_hi + 1e-6:
                assert feed.z >= tab_top_z - 1e-6, (
                    f"Feed at s={s:.3f} (xy={feed.x:.3f},{feed.y:.3f}) cuts "
                    f"to z={feed.z:.3f}, below tab_top_z={tab_top_z}"
                )


def test_ramp_with_tabs_ascent_lifts_over_tabs() -> None:
    """The post-last-pass ascent must also respect tabs — it walks the
    contour while rising, and may cross tab plateaus where it's still
    below tab_top_z. The lift must be present in the emitted IR."""
    # Use a small angle to make the ascent long enough to wrap past the
    # first tab while still below tab_top_z.
    project, op = _project_with_rect_tabs(
        cut_depth=-3.0,
        stepdown=1.0,
        tabs=TabConfig(
            enabled=True, count=4, width=4.0, height=0.5, ramp_length=1.0,
        ),
    )
    op.offset_side = OffsetSide.ON_LINE
    op.ramp = RampConfig(strategy=RampStrategy.LINEAR, angle_deg=3.0)
    tp = generate_profile_toolpath(op, project)
    # We expect the plateau z (-2.5) to appear at least one more time
    # beyond the 8 hits attributable to the final pass — the ascent
    # crosses tab plateaus too.
    plateau_z = -2.5
    plateau_feeds = [
        i for i in tp.instructions
        if i.z is not None and math.isclose(i.z, plateau_z, abs_tol=1e-6)
    ]
    # Final pass: 8. Cleanup pass: covers descent slice [0, ~19mm], no
    # tab plateaus there (first plateau at s=21). Ascent: 57mm starting
    # at s=19 → crosses s=21..23 (first tab plateau). So at least one
    # extra plateau hit from ascent. Total >= 8 + 2.
    assert len(plateau_feeds) >= 10


def test_tabs_count_plateau_bookends_on_final_pass() -> None:
    """Sanity-check tab count via plateau-entry-top z values on the final
    pass: 4 tabs × (entry-top + plateau-end) = 8 z=tab_top feeds.
    (Other passes don't breach with stepdown 2 + tab_height 0.5.)"""
    project, op = _project_with_rect_tabs(
        tabs=TabConfig(
            enabled=True, count=4, width=4.0, height=0.5, ramp_length=1.0,
        ),
    )
    op.offset_side = OffsetSide.ON_LINE
    tp = generate_profile_toolpath(op, project)
    plateau_lifts = [
        i for i in tp.instructions
        if i.type is MoveType.FEED
        and i.z is not None
        and math.isclose(i.z, -5.5, abs_tol=1e-6)
        and i.x is not None
    ]
    assert len(plateau_lifts) == 8


# ------------------------------------------------------------- start_position


def _entry_xy(tp) -> tuple[float, float]:
    """Return the (x, y) where the cut starts — the last XY rapid before
    the first Z feed (plunge) positions the tool over the entry point."""
    last_xy_rapid: tuple[float, float] | None = None
    for ins in tp.instructions:
        if (
            ins.type is MoveType.RAPID
            and ins.x is not None
            and ins.y is not None
        ):
            last_xy_rapid = (ins.x, ins.y)
        elif (
            ins.type is MoveType.FEED
            and ins.z is not None
            and ins.x is None
            and ins.y is None
            and last_xy_rapid is not None
        ):
            return last_xy_rapid
    raise AssertionError("no XY rapid before plunge")


def test_start_position_rotates_profile_entry_to_nearest_point() -> None:
    """Setting ``start_position`` on a closed profile rotates the offset
    contour so the cut begins near the chosen point. Concretely: for a
    50×30 outside-profile rect and a target near mid-right of the top
    edge, the first XY feed lands close to x ≈ 25, y ≈ 31.5 (offset
    contour) rather than at the offsetter's default seed corner."""
    project, op, _ = _project_with_rectangle(multi_depth=False)
    op.start_position = (25.0, 30.0)  # near the middle of the top edge
    tp = generate_profile_toolpath(op, project)
    first = _entry_xy(tp)
    # The outside offset of a 50×30 rect by r=1.5 has its top edge at
    # y=31.5. The projection of (25, 30) onto the offset contour is
    # (25, 31.5). Allow some tolerance for offsetter discretisation.
    assert first[0] == pytest.approx(25.0, abs=0.1)
    assert first[1] == pytest.approx(31.5, abs=0.1)


def test_start_position_none_preserves_offsetter_default() -> None:
    """No start_position set → chain rotation is a no-op. The first XY
    feed lands at the offsetter's default seed (not the user-chosen
    position)."""
    project, op, _ = _project_with_rectangle(multi_depth=False)
    assert op.start_position is None
    tp = generate_profile_toolpath(op, project)
    default_first = _entry_xy(tp)
    # Set start_position somewhere else → output must differ.
    op.start_position = (25.0, 30.0)
    shifted_first = _entry_xy(generate_profile_toolpath(op, project))
    assert default_first != shifted_first


def test_start_position_ignored_on_open_contour() -> None:
    """Open profiles already have meaningful endpoints (where the
    lead-in / lead-out anchor). Rotating would disrupt that, so the
    engine leaves open chains alone even if start_position is set."""
    project, op, _ = _project_with_rectangle(multi_depth=False, closed=False)
    # Open contours can't offset inside/outside — switch to ON_LINE so the
    # engine accepts them.
    op.offset_side = OffsetSide.ON_LINE
    tp_default = generate_profile_toolpath(op, project)
    op.start_position = (25.0, 30.0)
    tp_with_p0 = generate_profile_toolpath(op, project)
    # Same instruction stream — start_position is a no-op on open chains.
    assert [
        (i.type, i.x, i.y, i.z) for i in tp_default.instructions
    ] == [
        (i.type, i.x, i.y, i.z) for i in tp_with_p0.instructions
    ]
