"""Tests for pymillcam.engine.pocket (offset strategy, single depth)."""
from __future__ import annotations

import math

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import (
    GeometryRef,
    MillingDirection,
    PocketOp,
    PocketStrategy,
    RampConfig,
    RampStrategy,
)
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.core.tools import Tool, ToolController
from pymillcam.engine.ir import MoveType
from pymillcam.engine.pocket import (
    PocketGenerationError,
    _concentric_rings,
    compute_pocket_preview,
    generate_pocket_toolpath,
)


def _rect_segments(w: float = 50.0, h: float = 30.0) -> list[LineSegment]:
    return [
        LineSegment(start=(0, 0), end=(w, 0)),
        LineSegment(start=(w, 0), end=(w, h)),
        LineSegment(start=(w, h), end=(0, h)),
        LineSegment(start=(0, h), end=(0, 0)),
    ]


def _circle_entity(radius: float = 20.0) -> GeometryEntity:
    arc = ArcSegment(
        center=(0.0, 0.0),
        radius=radius,
        start_angle_deg=0.0,
        sweep_deg=360.0,
    )
    return GeometryEntity(segments=[arc], closed=True)


def _project_with_rect_pocket(
    *,
    w: float = 50.0,
    h: float = 30.0,
    cut_depth: float = -3.0,
    stepover: float = 2.0,
    tool_diameter: float = 3.0,
    direction: MillingDirection = MillingDirection.CLIMB,
) -> tuple[Project, PocketOp, GeometryEntity]:
    entity = GeometryEntity(segments=_rect_segments(w, h), closed=True)
    layer = GeometryLayer(name="Pocket_Boundary", entities=[entity])
    tool = Tool(
        name="flat",
        geometry={
            "diameter": tool_diameter,
            "flute_length": 15,
            "total_length": 50,
            "shank_diameter": 3,
            "flute_count": 2,
        },
    )
    tc = ToolController(
        tool_number=1,
        tool=tool,
        feed_xy=1200.0,
        feed_z=300.0,
        spindle_rpm=18000,
    )
    op = PocketOp(
        name="Pocket",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=entity.id)
        ],
        cut_depth=cut_depth,
        stepover=stepover,
        direction=direction,
        strategy=PocketStrategy.OFFSET,
        # Ramp/lead-agnostic tests pin PLUNGE so the instruction stream
        # isn't perturbed by helical or on-contour ramp moves.
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    return project, op, entity


# ---------- _concentric_rings --------------------------------------------


def test_concentric_rings_for_rectangle_shrinks_to_empty() -> None:
    """A 50×30 rect at 3 mm tool / 2 mm stepover should produce several
    rings before the narrow dimension (30 mm) closes up."""
    entity = GeometryEntity(segments=_rect_segments(50, 30), closed=True)
    rings = _concentric_rings(
        entity,
        tool_radius=1.5,
        stepover=2.0,
        direction=MillingDirection.CONVENTIONAL,
        chord_tolerance=0.02,
    )
    assert len(rings) >= 5
    # Each ring is closed (start of first segment == end of last).
    for ring in rings:
        assert math.isclose(ring[0].start[0], ring[-1].end[0], abs_tol=1e-6)
        assert math.isclose(ring[0].start[1], ring[-1].end[1], abs_tol=1e-6)


def test_concentric_rings_for_circle_preserves_arcs() -> None:
    """A circle pocket should yield concentric arc rings — the analytical
    offsetter handles full circles as arcs, not chord polygons."""
    entity = _circle_entity(radius=20.0)
    rings = _concentric_rings(
        entity,
        tool_radius=1.5,
        stepover=2.0,
        direction=MillingDirection.CONVENTIONAL,
        chord_tolerance=0.02,
    )
    assert rings  # at least one ring
    for ring in rings:
        # Each ring should be a single arc (full circle).
        assert len(ring) == 1
        assert isinstance(ring[0], ArcSegment)


def test_concentric_rings_radii_decrease_monotonically() -> None:
    entity = _circle_entity(radius=20.0)
    rings = _concentric_rings(
        entity,
        tool_radius=1.5,
        stepover=2.0,
        direction=MillingDirection.CONVENTIONAL,
        chord_tolerance=0.02,
    )
    radii = [ring[0].radius for ring in rings if isinstance(ring[0], ArcSegment)]
    # Shrinking inward → monotonically decreasing radii.
    for earlier, later in zip(radii[:-1], radii[1:], strict=True):
        assert later < earlier


def test_tool_too_large_returns_no_rings() -> None:
    """A 10 mm tool in a 3 mm-wide slot has no room for a single ring."""
    entity = GeometryEntity(segments=_rect_segments(50, 3), closed=True)
    rings = _concentric_rings(
        entity,
        tool_radius=5.0,
        stepover=2.0,
        direction=MillingDirection.CONVENTIONAL,
        chord_tolerance=0.02,
    )
    assert rings == []


# ---------- generate_pocket_toolpath -------------------------------------


def test_generates_tool_change_spindle_and_final_retract() -> None:
    project, op, _ = _project_with_rect_pocket()
    tp = generate_pocket_toolpath(op, project)
    types = [i.type for i in tp.instructions]
    assert types[0] is MoveType.COMMENT
    assert MoveType.TOOL_CHANGE in types
    assert MoveType.SPINDLE_ON in types
    # Spindle-off is emitted by the post-processor at program end, not
    # per toolpath.
    assert MoveType.SPINDLE_OFF not in types
    assert tp.instructions[-1].type is MoveType.RAPID
    assert tp.instructions[-1].z == project.settings.safe_height


def test_spindle_on_is_followed_by_warmup_dwell() -> None:
    """After M3 we should dwell for spindle_warmup_s so the spindle can
    reach commanded RPM before the first Z move."""
    project, op, _ = _project_with_rect_pocket()
    project.settings.spindle_warmup_s = 2.0
    tp = generate_pocket_toolpath(op, project)
    spindle_idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.SPINDLE_ON
    )
    next_inst = tp.instructions[spindle_idx + 1]
    assert next_inst.type is MoveType.DWELL
    assert next_inst.f == pytest.approx(2.0)


def test_no_warmup_dwell_when_setting_is_zero() -> None:
    project, op, _ = _project_with_rect_pocket()
    project.settings.spindle_warmup_s = 0.0
    tp = generate_pocket_toolpath(op, project)
    types = [i.type for i in tp.instructions]
    assert MoveType.DWELL not in types


def test_single_depth_plunges_once_when_multi_depth_disabled() -> None:
    """With multi_depth=False there's exactly one Z plunge at cut_depth."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-3.0)
    op.multi_depth = False
    tp = generate_pocket_toolpath(op, project)
    z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert len(z_feeds) == 1
    assert z_feeds[0].z == pytest.approx(-3.0)


def test_multi_depth_emits_one_plunge_per_pass() -> None:
    """Multi-depth at -3 mm with 1 mm stepdown → 3 plunges at -1, -2, -3."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-3.0)
    op.multi_depth = True
    op.stepdown = 1.0
    tp = generate_pocket_toolpath(op, project)
    z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert [f.z for f in z_feeds] == [
        pytest.approx(-1.0),
        pytest.approx(-2.0),
        pytest.approx(-3.0),
    ]


def test_multi_depth_retracts_to_clearance_between_passes() -> None:
    """Between passes the tool retracts to the clearance plane, not safe
    height — just enough to rapid back to the first ring start without
    dragging through the cut."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-3.0)
    op.multi_depth = True
    op.stepdown = 1.0
    clearance = project.settings.clearance_plane
    tp = generate_pocket_toolpath(op, project)
    # Collect Z rapids that happen between pass plunges.
    plunge_indices = [
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.FEED and ins.z is not None
    ]
    # Between each pair of consecutive plunges, there should be at least
    # one rapid z=clearance move.
    for before, after in zip(plunge_indices[:-1], plunge_indices[1:], strict=True):
        between = tp.instructions[before + 1 : after]
        assert any(
            ins.type is MoveType.RAPID and ins.z == pytest.approx(clearance)
            for ins in between
        )


def test_multi_depth_rings_are_identical_per_pass() -> None:
    """Ring XY paths are the same at every depth — only Z varies."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-3.0)
    op.multi_depth = True
    op.stepdown = 1.0
    tp = generate_pocket_toolpath(op, project)
    # Group XY feeds (z=None) between each plunge.
    passes: list[list[tuple[float | None, float | None]]] = [[]]
    for ins in tp.instructions:
        if ins.type is MoveType.FEED and ins.z is not None:
            if passes[-1]:
                passes.append([])
        elif ins.type is MoveType.FEED and ins.x is not None and ins.z is None:
            passes[-1].append((ins.x, ins.y))
    passes = [p for p in passes if p]
    assert len(passes) >= 2
    for p in passes[1:]:
        assert p == passes[0]


def test_rings_emit_line_feeds_for_rectangle() -> None:
    project, op, _ = _project_with_rect_pocket()
    tp = generate_pocket_toolpath(op, project)
    # XY feeds should dominate — four per rectangle ring plus transit moves.
    xy_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.z is None
    ]
    assert len(xy_feeds) > 8  # multiple rings


def test_circle_pocket_emits_arc_instructions() -> None:
    """Arcs in the rings should reach IR intact as ARC_CW / ARC_CCW."""
    entity = _circle_entity(radius=20.0)
    layer = GeometryLayer(name="Pocket", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0})
    tc = ToolController(tool_number=1, tool=tool)
    op = PocketOp(
        name="Circle",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=entity.id)
        ],
        cut_depth=-2.0,
        stepover=2.0,
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    tp = generate_pocket_toolpath(op, project)
    arc_count = sum(
        1 for i in tp.instructions
        if i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
    )
    assert arc_count >= 3  # several concentric rings


def test_climb_and_conventional_reverse_each_other() -> None:
    p_climb, op_climb, _ = _project_with_rect_pocket(
        direction=MillingDirection.CLIMB
    )
    p_conv, op_conv, _ = _project_with_rect_pocket(
        direction=MillingDirection.CONVENTIONAL
    )
    climb = generate_pocket_toolpath(op_climb, p_climb)
    conv = generate_pocket_toolpath(op_conv, p_conv)

    def first_ring_feeds(tp) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        seen_plunge = False
        for i in tp.instructions:
            if i.type is MoveType.FEED and i.z is not None and not seen_plunge:
                seen_plunge = True
                continue
            if not seen_plunge:
                continue
            if i.type is MoveType.FEED and i.x is not None and i.z is None:
                out.append((i.x, i.y))
            elif out:
                break
        return out

    climb_pts = first_ring_feeds(climb)
    conv_pts = first_ring_feeds(conv)
    # Same ring, opposite direction: reversing conv should match climb
    # (modulo where the start point lands; at minimum the second vertex
    # of one should be the last distinct vertex of the other).
    assert climb_pts != conv_pts
    assert sorted(climb_pts) == sorted(conv_pts)


def test_unknown_strategy_raises() -> None:
    project, op, _ = _project_with_rect_pocket()
    op.strategy = PocketStrategy.SPIRAL
    with pytest.raises(PocketGenerationError, match="not implemented"):
        generate_pocket_toolpath(op, project)


def test_tool_too_large_raises() -> None:
    project, op, _ = _project_with_rect_pocket(w=10.0, h=3.0, tool_diameter=5.0)
    with pytest.raises(PocketGenerationError, match="tool too large"):
        generate_pocket_toolpath(op, project)


# ---------- compute_pocket_preview ---------------------------------------


def test_preview_concatenates_rings() -> None:
    project, op, _ = _project_with_rect_pocket()
    preview = compute_pocket_preview(op, project)
    rings = _concentric_rings(
        project.geometry_layers[0].entities[0],
        tool_radius=1.5,
        stepover=op.stepover,
        direction=op.direction,
        chord_tolerance=project.settings.chord_tolerance,
    )
    total_segs = sum(len(r) for r in rings)
    assert len(preview) == total_segs


# ---------- ramp entry ---------------------------------------------------


def test_helical_ramp_emits_arc_instructions_before_first_feed() -> None:
    """With HELICAL ramp on a circle pocket (no corners to worry about),
    each pass begins with arcs (the helix descent) before any feeds at
    the pass depth."""
    entity = _circle_entity(radius=20.0)
    layer = GeometryLayer(name="Pocket", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0})
    tc = ToolController(tool_number=1, tool=tool)
    op = PocketOp(
        name="Circle",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=entity.id)
        ],
        cut_depth=-2.0,
        stepover=2.0,
        multi_depth=False,
        ramp=RampConfig(
            strategy=RampStrategy.HELICAL, angle_deg=3.0, radius=1.0
        ),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    tp = generate_pocket_toolpath(op, project)
    # Find the feed to z=0 (descent to prev_z); after it must come the
    # helix arcs with Z interpolating toward cut_depth.
    idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.FEED and ins.z == pytest.approx(0.0)
    )
    after = tp.instructions[idx + 1:]
    helix_arcs = [
        i for i in after
        if i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
        and i.z is not None
    ]
    assert helix_arcs, "expected at least one helical arc with Z set"
    # Z monotonically descends across the helix.
    zs = [a.z for a in helix_arcs]
    for earlier, later in zip(zs[:-1], zs[1:], strict=True):
        assert later <= earlier + 1e-9
    assert zs[-1] == pytest.approx(-2.0)


def test_linear_ramp_interpolates_z_along_first_ring() -> None:
    """LINEAR ramp emits feed moves whose Z descends monotonically from
    prev_z (0 on the first pass) to the pass depth."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-2.0, stepover=2.0)
    op.ramp = RampConfig(
        strategy=RampStrategy.LINEAR, angle_deg=3.0, radius=1.0
    )
    op.multi_depth = False
    tp = generate_pocket_toolpath(op, project)
    # Start from the feed-to-prev_z=0, collect following feeds with z set.
    ramp_feeds = []
    seen_prev_z = False
    for ins in tp.instructions:
        if ins.type is MoveType.FEED and ins.z == pytest.approx(0.0):
            seen_prev_z = True
            continue
        if not seen_prev_z:
            continue
        if ins.type is MoveType.FEED and ins.z is not None and ins.x is not None:
            ramp_feeds.append(ins.z)
        else:
            break
    assert ramp_feeds, "expected ramp feeds after prev_z descent"
    # Monotonically decreasing toward pass_z.
    for earlier, later in zip(ramp_feeds[:-1], ramp_feeds[1:], strict=True):
        assert later <= earlier + 1e-9
    assert ramp_feeds[-1] == pytest.approx(-2.0)


def test_helical_fit_check_accepts_tangent_helix_on_circle_pocket() -> None:
    """Regression: a helix of radius r tangent to the ring at its start
    has its disk's farthest point exactly on the ring boundary. The fit
    check must accept this — not reject it due to polygon chord-sag
    discretization noise."""
    from pymillcam.engine.pocket import _concentric_rings, _helix_fits
    entity = _circle_entity(radius=25.0)
    rings = _concentric_rings(
        entity, 1.5, 2.0, MillingDirection.CLIMB, 0.02
    )
    # helix_radius=1.0 on a ring whose enclosed circle has radius 23.5
    # (25 mm pocket − 1.5 mm tool). The helix disk touches the wall at
    # one point; fit must still pass.
    assert _helix_fits(rings[0], 1.0) is True


def test_helical_ramp_emits_transit_to_ring_one() -> None:
    """Regression: after the helix descent and first-ring cut, the
    engine must emit an explicit transit feed from the end of the first
    ring to the start of the second ring — otherwise the next arc's
    I/J offsets are interpreted relative to the wrong current position
    and the controller sees a malformed arc with mismatched radii."""
    entity = _circle_entity(radius=25.0)
    layer = GeometryLayer(name="Pocket", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0})
    tc = ToolController(tool_number=1, tool=tool)
    op = PocketOp(
        name="Circle",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=entity.id)
        ],
        cut_depth=-1.0,
        stepover=2.0,
        multi_depth=False,
        ramp=RampConfig(
            strategy=RampStrategy.HELICAL, angle_deg=3.0, radius=1.0
        ),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    tp = generate_pocket_toolpath(op, project)
    # Outer ring (radius 23.5) is emitted as two semicircles (full-circle
    # arcs are split for portable G-code). The second half ends at
    # (23.5, 0) with center offset I=+23.5 from the (-23.5, 0) midpoint.
    # Right after that arc we must see a straight FEED to (21.5, 0).
    outer_ring_idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.ARC_CW
        and ins.x == pytest.approx(23.5, abs=1e-6)
        and ins.i == pytest.approx(23.5, abs=1e-6)
        and ins.z is None
    )
    transit = tp.instructions[outer_ring_idx + 1]
    assert transit.type is MoveType.FEED
    assert transit.z is None
    assert transit.x == pytest.approx(21.5, abs=1e-6)


def test_helical_ramp_integer_turns_keeps_center_stable() -> None:
    """Regression: the helix must sweep an integer number of turns so
    it starts and ends at the same XY point. If the sweep doesn't
    close the loop, every emitted arc's I/J is relative to a
    slightly-different start point than the G-code interpreter sees."""
    entity = _circle_entity(radius=25.0)
    layer = GeometryLayer(name="Pocket", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0})
    tc = ToolController(tool_number=1, tool=tool)
    op = PocketOp(
        name="Circle",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=entity.id)
        ],
        cut_depth=-1.0,
        stepover=2.0,
        multi_depth=False,
        ramp=RampConfig(
            strategy=RampStrategy.HELICAL, angle_deg=3.0, radius=1.0
        ),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    tp = generate_pocket_toolpath(op, project)
    helix_arcs = [
        i for i in tp.instructions
        if i.type in (MoveType.ARC_CW, MoveType.ARC_CCW) and i.z is not None
    ]
    assert helix_arcs
    # The helix forms full circles, so the LAST helix arc must end at
    # the ring start (23.5, 0) — same as where the helix began.
    assert helix_arcs[-1].x == pytest.approx(23.5, abs=1e-6)
    assert helix_arcs[-1].y == pytest.approx(0.0, abs=1e-6)


def test_helical_falls_back_to_linear_when_helix_too_big() -> None:
    """If the configured helix_radius doesn't fit inside the first ring,
    the engine falls back to LINEAR on-contour ramp (the instruction
    stream contains straight feeds with Z interpolating, no arcs)."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-1.0, stepover=2.0)
    # Helix radius 50 mm can't fit inside a 47×27 ring → fallback.
    op.ramp = RampConfig(
        strategy=RampStrategy.HELICAL, angle_deg=3.0, radius=50.0
    )
    op.multi_depth = False
    tp = generate_pocket_toolpath(op, project)
    # No arc instructions in the output (the rings are line-only and we
    # fell back to LINEAR, which doesn't emit arcs here either).
    assert not any(
        i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
        for i in tp.instructions
    )


def test_linear_falls_back_to_plunge_when_ramp_too_long() -> None:
    """A large stepdown at a shallow angle produces a ramp_length
    exceeding the first ring's perimeter → PLUNGE fallback."""
    project, op, _ = _project_with_rect_pocket(
        w=10.0, h=10.0, cut_depth=-5.0, stepover=1.0
    )
    op.multi_depth = False
    op.ramp = RampConfig(
        strategy=RampStrategy.LINEAR, angle_deg=0.1, radius=1.0
    )
    tp = generate_pocket_toolpath(op, project)
    # Should be one single-plunge feed to cut_depth, no intermediate
    # descent feeds (ramp machinery was bypassed).
    z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert len(z_feeds) == 1
    assert z_feeds[0].z == pytest.approx(-5.0)


def test_linear_ramp_ends_at_first_ring_start() -> None:
    """The LINEAR ramp now occupies the last `ramp_length` arc of the
    closed first ring — the ramp STARTS `ramp_length` arc-distance
    before first_ring[0].start and ENDS at first_ring[0].start at
    pass_z. No cleanup re-cut is needed because the full first ring
    runs at pass_z right after the ramp, overwriting any witness."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-1.0, stepover=2.0)
    op.multi_depth = False
    op.ramp = RampConfig(
        strategy=RampStrategy.LINEAR, angle_deg=3.0, radius=1.0
    )
    tp = generate_pocket_toolpath(op, project)
    # Find the last ramp feed (z set to the pass depth). Its XY target
    # must equal first_ring[0].start = (1.5, 1.5) for the CLIMB rect.
    ramp_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED
        and i.z is not None
        and i.x is not None
        and i.z != 0.0  # exclude the initial feed-to-prev_z
    ]
    assert ramp_feeds
    last_ramp = ramp_feeds[-1]
    assert last_ramp.z == pytest.approx(-1.0)
    assert last_ramp.x == pytest.approx(1.5, abs=1e-6)
    assert last_ramp.y == pytest.approx(1.5, abs=1e-6)
    # After the ramp ends at (1.5, 1.5) at pass_z, the full first ring
    # runs once. The ring closes back at (1.5, 1.5). The next feed
    # after that close is the transit to ring 2.
    at_depth = [
        (i.x, i.y)
        for i in tp.instructions
        if i.type is MoveType.FEED
        and i.z is None
        and i.x is not None
    ]
    close_idx = next(
        i for i, pt in enumerate(at_depth)
        if pt == pytest.approx((1.5, 1.5), abs=1e-6)
    )
    assert close_idx + 1 < len(at_depth)
    assert at_depth[close_idx + 1] == pytest.approx((3.5, 3.5), abs=1e-6)


def test_linear_ramp_start_is_before_ring_start_along_contour() -> None:
    """The ramp starts at an XY rapid BEFORE the plunge, and that XY
    should not equal first_ring[0].start (the ramp occupies the
    tail of the ring, not the head)."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-1.0, stepover=2.0)
    op.multi_depth = False
    op.ramp = RampConfig(
        strategy=RampStrategy.LINEAR, angle_deg=3.0, radius=1.0
    )
    tp = generate_pocket_toolpath(op, project)
    # Find the rapid XY immediately preceding the first FEED z=prev_z.
    prev_z_feed_idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.FEED and ins.z == pytest.approx(0.0)
    )
    # Walk backwards to find the most recent RAPID with XY set.
    ramp_start_xy = None
    for ins in reversed(tp.instructions[:prev_z_feed_idx]):
        if ins.type is MoveType.RAPID and ins.x is not None:
            ramp_start_xy = (ins.x, ins.y)
            break
    assert ramp_start_xy is not None
    # Rect first_ring CLIMB starts at (1.5, 1.5); the last segment of
    # the ring is the bottom edge, so the ramp start is somewhere along
    # that edge — NOT at (1.5, 1.5) itself.
    assert ramp_start_xy != pytest.approx((1.5, 1.5), abs=1e-6)


# ---------- zigzag strategy ----------------------------------------------


def _project_with_rect_zigzag(
    *,
    w: float = 50.0,
    h: float = 30.0,
    cut_depth: float = -1.0,
    stepover: float = 2.0,
    tool_diameter: float = 3.0,
    angle_deg: float = 0.0,
    direction: MillingDirection = MillingDirection.CLIMB,
) -> tuple[Project, PocketOp, GeometryEntity]:
    entity = GeometryEntity(segments=_rect_segments(w, h), closed=True)
    layer = GeometryLayer(name="Pocket_Boundary", entities=[entity])
    tool = Tool(
        name="flat",
        geometry={
            "diameter": tool_diameter,
            "flute_length": 15,
            "total_length": 50,
            "shank_diameter": 3,
            "flute_count": 2,
        },
    )
    tc = ToolController(
        tool_number=1, tool=tool,
        feed_xy=1200.0, feed_z=300.0, spindle_rpm=18000,
    )
    op = PocketOp(
        name="Zig",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=entity.id)
        ],
        cut_depth=cut_depth,
        stepover=stepover,
        direction=direction,
        strategy=PocketStrategy.ZIGZAG,
        angle_deg=angle_deg,
        multi_depth=False,
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    return project, op, entity


def test_zigzag_strokes_clipped_to_machinable_polygon() -> None:
    """All stroke endpoints must sit within the entity boundary after
    subtracting one tool radius — i.e. the cutter's swept path stays
    inside the pocket."""
    from pymillcam.engine.pocket import _zigzag_strokes_and_finishing_ring
    entity = GeometryEntity(segments=_rect_segments(50, 30), closed=True)
    strokes, finishing, _machinable = _zigzag_strokes_and_finishing_ring(
        entity,
        tool_radius=1.5,
        stepover=2.0,
        direction=MillingDirection.CLIMB,
        angle_deg=0.0,
        chord_tolerance=0.02,
    )
    assert strokes
    assert finishing
    # Every stroke endpoint is inside [1.5, 48.5] × [1.5, 28.5] (bbox
    # minus one tool radius), with a small tolerance for chord sag.
    for stroke in strokes:
        for seg in stroke:
            for x, y in (seg.start, seg.end):
                assert 1.5 - 1e-6 <= x <= 48.5 + 1e-6
                assert 1.5 - 1e-6 <= y <= 28.5 + 1e-6


def test_zigzag_strokes_alternate_direction() -> None:
    """Row 0 goes +X, row 1 goes -X, row 2 goes +X, … — true zigzag."""
    from pymillcam.engine.pocket import _zigzag_strokes_and_finishing_ring
    entity = GeometryEntity(segments=_rect_segments(50, 30), closed=True)
    strokes, _finishing, _machinable = _zigzag_strokes_and_finishing_ring(
        entity, 1.5, 2.0, MillingDirection.CLIMB, 0.0, 0.02,
    )
    assert len(strokes) >= 4
    # Row 0: start.x < end.x (goes +X)
    assert strokes[0][0].start[0] < strokes[0][0].end[0]
    # Row 1: start.x > end.x (goes -X)
    assert strokes[1][0].start[0] > strokes[1][0].end[0]
    # Row 2: back to +X
    assert strokes[2][0].start[0] < strokes[2][0].end[0]


def test_zigzag_stepover_spacing() -> None:
    """Strokes are evenly spaced in the raster direction with spacing ≤
    stepover (slight under-step to land the last row at the far wall)."""
    from pymillcam.engine.pocket import _zigzag_strokes_and_finishing_ring
    entity = GeometryEntity(segments=_rect_segments(50, 30), closed=True)
    strokes, _finishing, _machinable = _zigzag_strokes_and_finishing_ring(
        entity, 1.5, 2.0, MillingDirection.CLIMB, 0.0, 0.02,
    )
    ys = [s[0].start[1] for s in strokes]
    # All y's should be within the machinable bbox [1.5, 28.5].
    assert ys[0] == pytest.approx(1.5, abs=1e-6)
    assert ys[-1] == pytest.approx(28.5, abs=1e-6)
    # Spacing ≤ stepover.
    for a, b in zip(ys[:-1], ys[1:], strict=True):
        assert 0 < (b - a) <= 2.0 + 1e-9


def test_zigzag_finishing_ring_preserves_arcs_on_circle_pocket() -> None:
    """Circle pocket's finishing ring should be a single ArcSegment —
    arc preservation on the wall matters; strokes themselves are always
    lines."""
    from pymillcam.engine.pocket import _zigzag_strokes_and_finishing_ring
    entity = _circle_entity(radius=20.0)
    _strokes, finishing_rings, _machinable = _zigzag_strokes_and_finishing_ring(
        entity, 1.5, 2.0, MillingDirection.CLIMB, 0.0, 0.02,
    )
    assert len(finishing_rings) == 1
    boundary_ring = finishing_rings[0]
    assert len(boundary_ring) == 1
    assert isinstance(boundary_ring[0], ArcSegment)


def test_zigzag_angle_deg_rotates_strokes() -> None:
    """With angle_deg=90, strokes run along ±Y instead of ±X."""
    from pymillcam.engine.pocket import _zigzag_strokes_and_finishing_ring
    entity = GeometryEntity(segments=_rect_segments(50, 30), closed=True)
    strokes, _finishing, _machinable = _zigzag_strokes_and_finishing_ring(
        entity, 1.5, 2.0, MillingDirection.CLIMB, 90.0, 0.02,
    )
    assert strokes
    # All strokes should be vertical: near-constant X, varying Y.
    for stroke in strokes:
        for seg in stroke:
            assert abs(seg.start[0] - seg.end[0]) < 1e-6
            assert abs(seg.start[1] - seg.end[1]) > 1.0


def test_zigzag_toolpath_has_header_and_final_retract() -> None:
    project, op, _ = _project_with_rect_zigzag()
    tp = generate_pocket_toolpath(op, project)
    types = [i.type for i in tp.instructions]
    assert types[0] is MoveType.COMMENT
    assert MoveType.TOOL_CHANGE in types
    assert MoveType.SPINDLE_ON in types
    # Spindle-off is the post-processor's job at program end.
    assert MoveType.SPINDLE_OFF not in types
    assert tp.instructions[-1].type is MoveType.RAPID
    assert tp.instructions[-1].z == project.settings.safe_height


def test_zigzag_plunge_single_depth_plunges_once() -> None:
    """PLUNGE + single-depth → one Z feed at cut_depth."""
    project, op, _ = _project_with_rect_zigzag(cut_depth=-1.0)
    tp = generate_pocket_toolpath(op, project)
    z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert len(z_feeds) == 1
    assert z_feeds[0].z == pytest.approx(-1.0)


def test_zigzag_emits_finishing_ring_after_strokes() -> None:
    """The last feed sequence before the final retract is the finishing
    ring — for a rectangle, four line feeds tracing the machinable-
    polygon wall (bbox minus tool radius)."""
    project, op, _ = _project_with_rect_zigzag(cut_depth=-1.0)
    tp = generate_pocket_toolpath(op, project)
    # Collect XY feed targets at cut depth, in order.
    xy_feeds = [
        (i.x, i.y) for i in tp.instructions
        if i.type is MoveType.FEED and i.z is None and i.x is not None
    ]
    # The last 4 points should form the rectangle wall (in some order,
    # corners at 1.5, 28.5 × 1.5, 48.5).
    corners = {(1.5, 1.5), (48.5, 1.5), (48.5, 28.5), (1.5, 28.5)}
    last4_round = {(round(x, 4), round(y, 4)) for x, y in xy_feeds[-4:]}
    assert last4_round == corners


def test_zigzag_linear_ramp_interpolates_z_along_first_stroke() -> None:
    """LINEAR ramp on zigzag descends along the first stroke's opening
    section from prev_z (0 on first pass) to pass_z."""
    project, op, _ = _project_with_rect_zigzag(cut_depth=-1.0)
    op.ramp = RampConfig(
        strategy=RampStrategy.LINEAR, angle_deg=3.0, radius=1.0
    )
    tp = generate_pocket_toolpath(op, project)
    # After the feed to prev_z=0, the next feeds should have Z
    # interpolating down to -1.0 while X advances along +X (first row).
    seen_prev_z = False
    ramp_feeds = []
    for ins in tp.instructions:
        if ins.type is MoveType.FEED and ins.z == pytest.approx(0.0):
            seen_prev_z = True
            continue
        if not seen_prev_z:
            continue
        if ins.type is MoveType.FEED and ins.z is not None and ins.x is not None:
            ramp_feeds.append(ins.z)
        else:
            break
    assert ramp_feeds
    for a, b in zip(ramp_feeds[:-1], ramp_feeds[1:], strict=True):
        assert b <= a + 1e-9
    assert ramp_feeds[-1] == pytest.approx(-1.0)


def test_zigzag_linear_falls_back_to_plunge_when_stroke_pathologically_short() -> None:
    """Past the back-and-forth leg cap (10), the engine falls back to
    PLUNGE — user asked for an angle the geometry can't reasonably
    accommodate."""
    # Narrow 5x30 rect at stepover=1, angle 0.1°: ramp_length = 1/tan(0.1°)
    # ≈ 573 mm over a 2 mm stroke → ceil(573/2) = 287 legs. Way above the
    # cap → PLUNGE.
    project, op, _ = _project_with_rect_zigzag(
        w=5.0, h=30.0, cut_depth=-1.0, stepover=1.0
    )
    op.ramp = RampConfig(
        strategy=RampStrategy.LINEAR, angle_deg=0.1, radius=1.0
    )
    tp = generate_pocket_toolpath(op, project)
    z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert len(z_feeds) == 1
    assert z_feeds[0].z == pytest.approx(-1.0)


def test_zigzag_linear_back_and_forth_on_short_first_stroke() -> None:
    """Regression: on a circle pocket the boundary-tangent first stroke
    is shorter than the configured ramp_length. The engine oscillates
    back-and-forth (n_legs = 2) to reach pass_z at stroke_start, then
    emits one cleanup leg at pass_z across the full stroke.
    """
    entity = _circle_entity(radius=25.0)
    layer = GeometryLayer(name="P", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0})
    tc = ToolController(tool_number=1, tool=tool)
    op = PocketOp(
        name="Zig",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name="P", entity_id=entity.id)],
        cut_depth=-1.0,
        stepover=2.0,
        multi_depth=False,
        strategy=PocketStrategy.ZIGZAG,
        ramp=RampConfig(
            strategy=RampStrategy.LINEAR, angle_deg=3.0, radius=1.0
        ),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    tp = generate_pocket_toolpath(op, project)
    # Leg 1 descends to −0.5, leg 2 descends to −1 at stroke_start.
    # There is no third XY+Z feed — the cleanup leg at pass_z has
    # z=None in IR (matches the convention for horizontal feed moves).
    xy_z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None and i.x is not None
    ]
    assert len(xy_z_feeds) == 2
    assert xy_z_feeds[0].z == pytest.approx(-0.5)
    assert xy_z_feeds[1].z == pytest.approx(-1.0)
    # The two legs run in opposite X directions (back-and-forth).
    assert (xy_z_feeds[0].x > 0) != (xy_z_feeds[1].x > 0)


def test_zigzag_linear_back_and_forth_emits_cleanup_leg() -> None:
    """After n_legs descending legs end at stroke_start at pass_z, the
    engine emits one cleanup leg A→B at pass_z so stroke 1 is flat at
    pass_z across its full length and the tool lands at stroke_end for
    stroke 2 to continue naturally.
    """
    entity = _circle_entity(radius=25.0)
    layer = GeometryLayer(name="P", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0})
    tc = ToolController(tool_number=1, tool=tool)
    op = PocketOp(
        name="Zig",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name="P", entity_id=entity.id)],
        cut_depth=-1.0,
        stepover=2.0,
        multi_depth=False,
        strategy=PocketStrategy.ZIGZAG,
        ramp=RampConfig(
            strategy=RampStrategy.LINEAR, angle_deg=3.0, radius=1.0
        ),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    from pymillcam.engine.pocket import _zigzag_strokes_and_finishing_ring
    strokes, _finishing, _machinable = _zigzag_strokes_and_finishing_ring(
        entity, 1.5, 2.0, MillingDirection.CLIMB, 0.0, 0.02,
    )
    stroke_start = strokes[0][0].start
    stroke_end = strokes[0][-1].end
    tp = generate_pocket_toolpath(op, project)
    # Last XY+Z feed (end of descent) must be at stroke_start.
    xy_z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None and i.x is not None
    ]
    assert xy_z_feeds[-1].x == pytest.approx(stroke_start[0], abs=1e-6)
    assert xy_z_feeds[-1].y == pytest.approx(stroke_start[1], abs=1e-6)
    assert xy_z_feeds[-1].z == pytest.approx(-1.0)
    # The next XY feed (no Z) is the cleanup leg's endpoint = stroke_end.
    descent_end_idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins is xy_z_feeds[-1]
    )
    cleanup = next(
        i for i in tp.instructions[descent_end_idx + 1:]
        if i.type is MoveType.FEED and i.z is None and i.x is not None
    )
    assert cleanup.x == pytest.approx(stroke_end[0], abs=1e-6)
    assert cleanup.y == pytest.approx(stroke_end[1], abs=1e-6)


def test_zigzag_finishing_ring_starts_near_last_stroke_end() -> None:
    """The finishing contour ring is rotated so it starts near the last
    stroke's end, not at the offsetter's canonical start. For a circle
    pocket this means the `G1 X Y` transit before the ring-arc is
    within chord-tolerance of the last stroke's end (no diagonal feed
    across the cleared pocket)."""
    entity = _circle_entity(radius=25.0)
    layer = GeometryLayer(name="P", entities=[entity])
    tool = Tool(name="flat", geometry={"diameter": 3.0})
    tc = ToolController(tool_number=1, tool=tool)
    op = PocketOp(
        name="Zig",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name="P", entity_id=entity.id)],
        cut_depth=-1.0,
        stepover=2.0,
        multi_depth=False,
        strategy=PocketStrategy.ZIGZAG,
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    tp = generate_pocket_toolpath(op, project)
    # Find the G2/G3 arc that is the finishing ring (the one with z=None
    # after all strokes). Its preceding feed is the transit; the feed
    # after it is (nothing — it's the last toolpath move before retract).
    ring_arc_idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type in (MoveType.ARC_CW, MoveType.ARC_CCW) and ins.z is None
    )
    transit = tp.instructions[ring_arc_idx - 1]
    assert transit.type is MoveType.FEED and transit.z is None
    # Walk back to find the last stroke end (previous feed with XY, no Z).
    last_stroke_feed = next(
        ins for ins in reversed(tp.instructions[:ring_arc_idx - 1])
        if ins.type is MoveType.FEED and ins.x is not None and ins.z is None
    )
    dist = math.hypot(
        transit.x - last_stroke_feed.x, transit.y - last_stroke_feed.y
    )
    # Within chord tolerance (0.02 default) the transit is effectively
    # zero. Give a small margin for accumulated float ops.
    assert dist < 0.05, (
        f"expected transit to finishing ring ≤0.05 mm from last stroke end, "
        f"got {dist} mm"
    )


def test_zigzag_linear_single_leg_when_stroke_is_long_enough() -> None:
    """On a 50×30 rect the first stroke (~47 mm) easily fits the 19 mm
    ramp_length at 3° for 1 mm descent. n_legs = 1, one partial ramp
    then continues at pass_z."""
    project, op, _ = _project_with_rect_zigzag(cut_depth=-1.0, stepover=2.0)
    op.ramp = RampConfig(
        strategy=RampStrategy.LINEAR, angle_deg=3.0, radius=1.0
    )
    tp = generate_pocket_toolpath(op, project)
    xy_z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None and i.x is not None
    ]
    # Exactly one XY+Z ramp feed (single-segment stroke → one IR move).
    assert len(xy_z_feeds) == 1
    assert xy_z_feeds[0].z == pytest.approx(-1.0)


def test_zigzag_helical_falls_back_to_linear() -> None:
    """HELICAL on zigzag isn't supported yet — it resolves to LINEAR
    (observable as ramp feeds descending along X with no arc moves)."""
    project, op, _ = _project_with_rect_zigzag(cut_depth=-1.0)
    op.ramp = RampConfig(
        strategy=RampStrategy.HELICAL, angle_deg=3.0, radius=1.0
    )
    tp = generate_pocket_toolpath(op, project)
    # No arc moves — fallback routed through LINEAR on line strokes.
    assert not any(
        i.type in (MoveType.ARC_CW, MoveType.ARC_CCW)
        for i in tp.instructions
    )
    # And there's more than one Z feed (ramp feeds + pass depth).
    z_feeds = [i for i in tp.instructions
               if i.type is MoveType.FEED and i.z is not None]
    assert len(z_feeds) > 1


def test_zigzag_multi_depth_emits_one_plunge_per_pass() -> None:
    project, op, _ = _project_with_rect_zigzag(cut_depth=-3.0)
    op.multi_depth = True
    op.stepdown = 1.0
    tp = generate_pocket_toolpath(op, project)
    z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert [f.z for f in z_feeds] == [
        pytest.approx(-1.0),
        pytest.approx(-2.0),
        pytest.approx(-3.0),
    ]


def test_zigzag_multi_depth_retracts_between_passes() -> None:
    project, op, _ = _project_with_rect_zigzag(cut_depth=-3.0)
    op.multi_depth = True
    op.stepdown = 1.0
    clearance = project.settings.clearance_plane
    tp = generate_pocket_toolpath(op, project)
    plunge_indices = [
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.FEED and ins.z is not None
    ]
    for before, after in zip(
        plunge_indices[:-1], plunge_indices[1:], strict=True
    ):
        between = tp.instructions[before + 1 : after]
        assert any(
            ins.type is MoveType.RAPID
            and ins.z == pytest.approx(clearance)
            for ins in between
        )


def test_zigzag_climb_and_conventional_reverse_finishing_ring() -> None:
    """Climb/conventional flips the finishing ring direction (the wall
    cut). Strokes themselves are invariant — MVP doesn't try to flip
    interior raster for climb, since "climb" isn't well-defined on a
    stroke cut on both sides."""
    p_climb, op_climb, _ = _project_with_rect_zigzag(
        direction=MillingDirection.CLIMB
    )
    p_conv, op_conv, _ = _project_with_rect_zigzag(
        direction=MillingDirection.CONVENTIONAL
    )
    climb = generate_pocket_toolpath(op_climb, p_climb)
    conv = generate_pocket_toolpath(op_conv, p_conv)

    def last_four_feeds(tp) -> list[tuple[float, float]]:
        xy = [
            (i.x, i.y) for i in tp.instructions
            if i.type is MoveType.FEED and i.z is None and i.x is not None
        ]
        return xy[-4:]

    # Same four corners; order differs.
    assert sorted(last_four_feeds(climb)) == sorted(last_four_feeds(conv))
    assert last_four_feeds(climb) != last_four_feeds(conv)


# ---------- islands -------------------------------------------------------

def _project_with_island_pocket(
    *,
    boundary_w: float = 50.0,
    boundary_h: float = 50.0,
    island_radius: float = 5.0,
    cut_depth: float = -3.0,
    stepover: float = 2.0,
    tool_diameter: float = 3.0,
    strategy: PocketStrategy = PocketStrategy.OFFSET,
) -> tuple[Project, PocketOp, GeometryEntity, GeometryEntity]:
    boundary = GeometryEntity(
        segments=_rect_segments(boundary_w, boundary_h), closed=True,
    )
    cx, cy = boundary_w / 2, boundary_h / 2
    island = GeometryEntity(
        segments=[ArcSegment(
            center=(cx, cy), radius=island_radius,
            start_angle_deg=0.0, sweep_deg=360.0,
        )],
        closed=True,
    )
    layer = GeometryLayer(name="L", entities=[boundary, island])
    tool = Tool(name="flat", geometry={
        "diameter": tool_diameter, "flute_length": 15,
        "total_length": 50, "shank_diameter": 3, "flute_count": 2,
    })
    tc = ToolController(
        tool_number=1, tool=tool, feed_xy=1200.0, feed_z=300.0,
        spindle_rpm=18000,
    )
    op = PocketOp(
        name="Pocket+Island",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=boundary.id),
            GeometryRef(layer_name=layer.name, entity_id=island.id),
        ],
        cut_depth=cut_depth,
        stepover=stepover,
        strategy=strategy,
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op],
    )
    return project, op, boundary, island


def test_offset_with_island_emits_rings_around_both_walls() -> None:
    """A pocket with one circular island should emit rings tracing both
    the outer boundary AND the island wall. With buffer-based offsets,
    the first iteration's polygon-with-holes contributes one exterior
    plus one interior ring."""
    project, op, _, _ = _project_with_island_pocket()
    tp = generate_pocket_toolpath(op, project)
    feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.x is not None and i.y is not None
    ]
    # Sanity: feeds should hit both far-from-center XY (boundary wall)
    # and close-to-center XY (island wall).
    cx, cy = 25.0, 25.0
    distances = [math.hypot(f.x - cx, f.y - cy) for f in feeds]
    assert max(distances) > 20.0  # boundary kerf
    assert min(distances) < 10.0  # island kerf


def test_offset_with_island_retracts_between_disjoint_groups() -> None:
    """Buffer iterations may produce MultiPolygon when the eroded
    boundary splits around an island. Each Polygon's rings are emitted
    as a 'group' with retract → rapid → plunge between groups."""
    # An island close to the boundary forces a split early.
    project, op, _, _ = _project_with_island_pocket(
        boundary_w=40.0, boundary_h=40.0, island_radius=15.0,
    )
    tp = generate_pocket_toolpath(op, project)
    # Look for the retract pattern: RAPID z=clearance, RAPID x/y, FEED z=pass_z.
    rapids = [i for i in tp.instructions if i.type is MoveType.RAPID]
    feed_z_only = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None and i.x is None
    ]
    # At minimum: per-pass init retract + per-pass plunge. With island
    # splitting we expect EXTRA mid-pass retracts/plunges.
    assert len(rapids) > 3
    assert len(feed_z_only) > 1


def test_offset_with_island_too_close_for_tool_raises() -> None:
    """If the island is too close to the boundary for the tool to fit
    in any region, no rings are produced and we error."""
    # 5x5 boundary with a 2.4mm island leaves only ~0.1mm of clearance
    # between island wall and boundary on each side — too tight even
    # for the adaptive last pass to find a meaningful ring.
    project, op, _, _ = _project_with_island_pocket(
        boundary_w=5.0, boundary_h=5.0, island_radius=2.4,
        tool_diameter=3.0,
    )
    with pytest.raises(PocketGenerationError, match="tool too large"):
        generate_pocket_toolpath(op, project)


def test_zigzag_with_island_emits_finishing_ring_per_wall() -> None:
    """ZIGZAG pocket with one island should emit two finishing rings —
    boundary + island wall. The island ring is reached via retract +
    rapid + plunge from the boundary ring (disjoint connector)."""
    project, op, _, _ = _project_with_island_pocket(
        strategy=PocketStrategy.ZIGZAG,
    )
    tp = generate_pocket_toolpath(op, project)
    # Each pass: count the retracts to clearance after the strokes —
    # one retract per island finishing ring (boundary uses feed-at-depth).
    rapid_to_clearance = [
        i for i in tp.instructions
        if i.type is MoveType.RAPID
        and i.z is not None
        and math.isclose(i.z, 3.0, abs_tol=1e-6)
    ]
    # Single pass at -3, with one island: at least one mid-pass retract
    # for the island finishing ring (plus the per-pass retract before
    # the next pass — but here it's a single pass so no extra).
    assert len(rapid_to_clearance) >= 1


def test_multiple_disjoint_pockets_share_op_settings() -> None:
    """Two disjoint boundaries selected for one PocketOp should both be
    cut with the same tool / depth / strategy. Engine emits both regions
    in selection order."""
    b1 = GeometryEntity(segments=_rect_segments(20, 20), closed=True)
    # Translate b2 by 50 in X.
    b2_segs = [
        LineSegment(start=(s.start[0] + 50, s.start[1]),
                    end=(s.end[0] + 50, s.end[1]))
        for s in _rect_segments(20, 20)
    ]
    b2 = GeometryEntity(segments=b2_segs, closed=True)
    layer = GeometryLayer(name="L", entities=[b1, b2])
    tool = Tool(name="flat", geometry={
        "diameter": 3.0, "flute_length": 15,
        "total_length": 50, "shank_diameter": 3, "flute_count": 2,
    })
    tc = ToolController(
        tool_number=1, tool=tool, feed_xy=1200.0, feed_z=300.0,
        spindle_rpm=18000,
    )
    op = PocketOp(
        name="Two pockets",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=b1.id),
            GeometryRef(layer_name=layer.name, entity_id=b2.id),
        ],
        cut_depth=-3.0, stepover=2.0,
        strategy=PocketStrategy.OFFSET,
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op],
    )
    tp = generate_pocket_toolpath(op, project)
    # Expect XY rapids hitting both pockets' regions.
    xy_rapids = [
        i for i in tp.instructions
        if i.type is MoveType.RAPID and i.x is not None
    ]
    xs = [r.x for r in xy_rapids]
    assert any(x < 25 for x in xs)  # b1 region (centered ~10)
    assert any(x > 30 for x in xs)  # b2 region (centered ~60)


def test_offset_with_islands_iterates_to_pocket_centre() -> None:
    """Regression: with multiple islands the buffer can return a
    GeometryCollection (mixed Polygon/LineString) at intermediate
    distances. The previous code bailed out, leaving the centre uncut
    even though stepover was small enough to fill it. Verify that the
    iteration covers the full pocket."""
    from pymillcam.engine.pocket import _concentric_rings_with_islands

    boundary = GeometryEntity(segments=[
        ArcSegment(center=(0, 0), radius=50.0,
                   start_angle_deg=0.0, sweep_deg=360.0)
    ], closed=True)
    islands = []
    for i in range(6):
        angle_rad = math.radians(i * 60.0)
        cx = 40.0 * math.cos(angle_rad)
        cy = 40.0 * math.sin(angle_rad)
        islands.append(GeometryEntity(segments=[
            ArcSegment(center=(cx, cy), radius=2.0,
                       start_angle_deg=0.0, sweep_deg=360.0)
        ], closed=True))

    groups = _concentric_rings_with_islands(
        boundary, islands, tool_radius=1.5, stepover=2.0,
        direction=MillingDirection.CLIMB, chord_tolerance=0.05,
    )
    # Tool radius 1.5 + 2 mm/iter: to reach pocket centre (radius 0)
    # from outer radius 50, we need ~25 iterations.
    assert len(groups) >= 15
    # Smallest exterior radius across all groups should be near zero
    # (within a few stepover-widths of centre).
    smallest_max_radius = min(
        max((s.start[0] ** 2 + s.start[1] ** 2) ** 0.5 for s in group[0])
        for group in groups
        if group and group[0]
    )
    assert smallest_max_radius < 5.0


def test_pocket_with_no_closed_geometry_raises() -> None:
    """An empty geometry_refs (or all-open) should raise — no boundary
    means no pocket region."""
    open_chain = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))], closed=False,
    )
    layer = GeometryLayer(name="L", entities=[open_chain])
    tool = Tool(name="flat", geometry={
        "diameter": 3.0, "flute_length": 15,
        "total_length": 50, "shank_diameter": 3, "flute_count": 2,
    })
    tc = ToolController(
        tool_number=1, tool=tool, feed_xy=1200.0, feed_z=300.0,
        spindle_rpm=18000,
    )
    op = PocketOp(
        name="Bad",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name=layer.name, entity_id=open_chain.id),
        ],
        cut_depth=-3.0, stepover=2.0,
        strategy=PocketStrategy.OFFSET,
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op],
    )
    with pytest.raises(PocketGenerationError, match="no closed boundary"):
        generate_pocket_toolpath(op, project)


# ---------- rest machining ----------------------------------------------

def _v_notch_boundary_and_island() -> tuple[GeometryEntity, GeometryEntity]:
    """20x15 rectangle with a triangular island whose vertex comes close
    to the top wall — reliably produces a V-notch residual with
    tool_radius=1.0 and stepover=2.5."""
    boundary = GeometryEntity(segments=[
        LineSegment(start=(0, 0), end=(20, 0)),
        LineSegment(start=(20, 0), end=(20, 15)),
        LineSegment(start=(20, 15), end=(0, 15)),
        LineSegment(start=(0, 15), end=(0, 0)),
    ], closed=True)
    island = GeometryEntity(segments=[
        LineSegment(start=(8, 8), end=(16, 8)),
        LineSegment(start=(16, 8), end=(12, 13)),
        LineSegment(start=(12, 13), end=(8, 8)),
    ], closed=True)
    return boundary, island


def test_rest_machining_defaults_true_on_pocket_op() -> None:
    """PocketOp should default to rest_machining=True so users get
    V-notch cleanup without opt-in."""
    op = PocketOp(name="P", tool_controller_id=1, cut_depth=-1.0)
    assert op.rest_machining is True


def test_rest_machining_adds_groups_on_v_notch_geometry() -> None:
    """A V-notch corner (island vertex close to boundary) leaves a
    residual after regular concentric rings. rest_machining=True emits
    extra ring-groups to clean it up; rest_machining=False does not."""
    from pymillcam.engine.pocket import _concentric_rings_with_islands

    boundary, island = _v_notch_boundary_and_island()
    groups_off = _concentric_rings_with_islands(
        boundary, [island], tool_radius=1.0, stepover=2.5,
        direction=MillingDirection.CLIMB, chord_tolerance=0.05,
        rest_machining=False,
    )
    groups_on = _concentric_rings_with_islands(
        boundary, [island], tool_radius=1.0, stepover=2.5,
        direction=MillingDirection.CLIMB, chord_tolerance=0.05,
        rest_machining=True,
    )
    assert len(groups_on) > len(groups_off)


def test_rest_machining_no_residual_on_clean_annulus() -> None:
    """Concentric circles form a perfect annulus: after regular passes
    the entire cuttable area is swept. rest_machining should add zero
    groups (no false positives from pinch-off noise)."""
    from pymillcam.engine.pocket import _concentric_rings_with_islands

    outer = GeometryEntity(segments=[
        ArcSegment(center=(0, 0), radius=20.0,
                   start_angle_deg=0.0, sweep_deg=360.0)
    ], closed=True)
    inner = GeometryEntity(segments=[
        ArcSegment(center=(0, 0), radius=10.0,
                   start_angle_deg=0.0, sweep_deg=360.0)
    ], closed=True)
    groups_off = _concentric_rings_with_islands(
        outer, [inner], tool_radius=1.0, stepover=2.0,
        direction=MillingDirection.CLIMB, chord_tolerance=0.02,
        rest_machining=False,
    )
    groups_on = _concentric_rings_with_islands(
        outer, [inner], tool_radius=1.0, stepover=2.0,
        direction=MillingDirection.CLIMB, chord_tolerance=0.02,
        rest_machining=True,
    )
    assert len(groups_on) == len(groups_off)


def test_rest_machining_flag_propagates_through_toolpath() -> None:
    """The PocketOp.rest_machining flag should reach the engine — turning
    it off on a V-notch geometry should produce fewer feed moves than on."""
    boundary, island = _v_notch_boundary_and_island()
    layer = GeometryLayer(name="L", entities=[boundary, island])
    tool = Tool(name="flat", geometry={
        "diameter": 2.0, "flute_length": 15,
        "total_length": 50, "shank_diameter": 3, "flute_count": 2,
    })
    tc = ToolController(
        tool_number=1, tool=tool, feed_xy=1200.0, feed_z=300.0,
        spindle_rpm=18000,
    )

    def _feed_count(rest: bool) -> int:
        op = PocketOp(
            name="VN", tool_controller_id=1,
            geometry_refs=[
                GeometryRef(layer_name=layer.name, entity_id=boundary.id),
                GeometryRef(layer_name=layer.name, entity_id=island.id),
            ],
            cut_depth=-1.0, stepover=2.5,
            strategy=PocketStrategy.OFFSET,
            ramp=RampConfig(strategy=RampStrategy.PLUNGE),
            rest_machining=rest,
        )
        project = Project(
            geometry_layers=[layer], tool_controllers=[tc], operations=[op],
        )
        tp = generate_pocket_toolpath(op, project)
        return sum(
            1 for i in tp.instructions
            if i.type is MoveType.FEED and i.x is not None
        )

    assert _feed_count(rest=True) > _feed_count(rest=False)


def test_rest_machining_groups_within_tool_center_space() -> None:
    """Cleanup ring-groups must have all their points inside the
    tool-center-reachable area: exterior of boundary buffered inward by
    tool_radius, hollowed by each island buffered outward by tool_radius.
    This guarantees the cutter doesn't gouge walls."""
    from pymillcam.engine.pocket import _concentric_rings_with_islands

    boundary, island = _v_notch_boundary_and_island()
    tool_radius = 1.0
    # Baseline groups without rest-machining
    base = _concentric_rings_with_islands(
        boundary, [island], tool_radius=tool_radius, stepover=2.5,
        direction=MillingDirection.CLIMB, chord_tolerance=0.05,
        rest_machining=False,
    )
    full = _concentric_rings_with_islands(
        boundary, [island], tool_radius=tool_radius, stepover=2.5,
        direction=MillingDirection.CLIMB, chord_tolerance=0.05,
        rest_machining=True,
    )
    extra = full[len(base):]
    assert extra, "expected at least one rest-machining group"

    from shapely.geometry import Point, Polygon
    bp = Polygon([(0, 0), (20, 0), (20, 15), (0, 15)])
    ip = Polygon([(8, 8), (16, 8), (12, 13)])
    machinable = Polygon(bp.exterior.coords, holes=[ip.exterior.coords])
    tool_center_space = machinable.buffer(-tool_radius)

    for group in extra:
        for ring in group:
            for seg in ring:
                p = Point(seg.start)
                # Shapely buffer has numerical slop; allow 1e-6.
                assert tool_center_space.buffer(1e-6).contains(p), (
                    f"rest-machining point {seg.start} outside tool-center space"
                )


# =================================================================== SPIRAL UI


def test_spiral_preview_is_empty() -> None:
    """SPIRAL isn't implemented yet; `compute_pocket_preview` should
    return an empty list so the viewport doesn't misleadingly draw
    concentric OFFSET rings for a strategy that will fail at G-code
    generation. Regression — without the short-circuit, the preview
    silently falls through to the concentric-ring branch."""
    entity = GeometryEntity(segments=_rect_segments(40, 30), closed=True)
    layer = GeometryLayer(name="L", entities=[entity])
    tc = ToolController(tool_number=1, tool=Tool(name="t"))
    op = PocketOp(
        name="P",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name="L", entity_id=entity.id)],
        cut_depth=-3.0,
        strategy=PocketStrategy.SPIRAL,
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    assert compute_pocket_preview(op, project) == []


# ========================================== ZIGZAG multi-region connector safety


def _zigzag_island_project(
    *, cut_depth: float = -1.0, stepover: float = 1.5
) -> tuple[Project, PocketOp]:
    """Build a 40×30 rectangular pocket with a circular island at the
    centre — a scan line through the island is split into two disjoint
    pieces, each of which the engine emits as its own stroke. The
    connector between those pieces is the safety case this test
    covers.
    """
    # Outer boundary: 40 × 30 rectangle.
    boundary = GeometryEntity(segments=_rect_segments(40, 30), closed=True)
    # Island: circle r=5 at centre (20, 15). Any horizontal scan line
    # through y ∈ (10, 20) splits into pieces on the left and right of
    # the island.
    island_arc = ArcSegment(
        center=(20.0, 15.0),
        radius=5.0,
        start_angle_deg=0.0,
        sweep_deg=360.0,
    )
    island = GeometryEntity(segments=[island_arc], closed=True)
    layer = GeometryLayer(name="L", entities=[boundary, island])
    tc = ToolController(tool_number=1, tool=Tool(name="t"))
    tc.tool.geometry["diameter"] = 3.0
    op = PocketOp(
        name="Zig",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name="L", entity_id=boundary.id),
            GeometryRef(layer_name="L", entity_id=island.id),
        ],
        cut_depth=cut_depth,
        stepover=stepover,
        multi_depth=False,
        strategy=PocketStrategy.ZIGZAG,
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    project.settings.spindle_warmup_s = 0.0
    return project, op


def test_zigzag_retracts_between_disjoint_scan_pieces_across_island() -> None:
    """Regression for the "feed-at-depth through an island" hazard.

    When a scan line crosses an island, the engine emits its left and
    right pieces as separate strokes. The straight connector between
    them would cut through the island at cut-depth. The fix detects
    the unsafe connector via the machinable polygon and substitutes
    retract → rapid → plunge.

    Behavioural assertion: count the number of Z-only retract moves to
    clearance during the stroke phase. Without the fix, there are
    zero (every connector is a pure XY feed). With the fix, at least
    one retract appears for scan lines that actually cross the island.
    """
    project, op = _zigzag_island_project()
    tp = generate_pocket_toolpath(op, project)

    clearance = project.settings.clearance_plane
    retracts_during_strokes = [
        ins for ins in tp.instructions
        if ins.type is MoveType.RAPID
        and ins.z == pytest.approx(clearance)
        and ins.x is None
        and ins.y is None
    ]
    # At least one retract: scan rows through y ∈ [10, 20] are split by
    # the island, each yielding an unsafe connector.
    assert len(retracts_during_strokes) >= 1


def test_zigzag_no_retract_when_no_islands() -> None:
    """Safety check must not cost anything when there's no island to
    avoid. A plain rectangular zigzag pocket should emit its usual
    feed-connector pattern with zero in-body retracts."""
    entity = GeometryEntity(segments=_rect_segments(40, 30), closed=True)
    layer = GeometryLayer(name="L", entities=[entity])
    tc = ToolController(tool_number=1, tool=Tool(name="t"))
    tc.tool.geometry["diameter"] = 3.0
    op = PocketOp(
        name="Zig",
        tool_controller_id=1,
        geometry_refs=[GeometryRef(layer_name="L", entity_id=entity.id)],
        cut_depth=-1.0,
        stepover=1.5,
        multi_depth=False,
        strategy=PocketStrategy.ZIGZAG,
        ramp=RampConfig(strategy=RampStrategy.PLUNGE),
    )
    project = Project(
        geometry_layers=[layer], tool_controllers=[tc], operations=[op]
    )
    project.settings.spindle_warmup_s = 0.0
    tp = generate_pocket_toolpath(op, project)

    # Find the plunge (first feed to cut-depth) — everything after that
    # and before the final retract is the "body" of the pass.
    plunge_idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.FEED
        and ins.z == pytest.approx(-1.0)
    )
    # Last instruction is the final safe-height retract; stop just before.
    body = tp.instructions[plunge_idx + 1 : -1]
    in_body_retracts = [
        ins for ins in body
        if ins.type is MoveType.RAPID and ins.x is None and ins.y is None
    ]
    assert in_body_retracts == [], (
        f"plain zigzag without islands should not retract mid-pass; got {len(in_body_retracts)}"
    )


def test_zigzag_unsafe_connector_is_replaced_by_rapid_not_feed() -> None:
    """The unsafe-connector substitution emits a Z-only rapid, an XY
    rapid, and a Z feed (plunge) — matching the inter-island-ring
    pattern already used for finishing rings. Prevents a regression
    where someone replaces the retract sequence with a partial
    (e.g. just the Z part) and leaves an XY feed crossing the island.
    """
    project, op = _zigzag_island_project()
    tp = generate_pocket_toolpath(op, project)

    clearance = project.settings.clearance_plane
    # The very first RAPID z=clearance is the pass entry descent (from
    # safe_height), part of the standard preamble — not a retract. Scan
    # only what follows the first plunge to pass depth.
    first_plunge = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.FEED and ins.z is not None and ins.z < 0
    )
    body_retracts = [
        i for i, ins in enumerate(tp.instructions)
        if i > first_plunge
        and ins.type is MoveType.RAPID
        and ins.z == pytest.approx(clearance)
        and ins.x is None
        and ins.y is None
    ]
    assert body_retracts, "fixture should trigger at least one mid-body retract"
    for i in body_retracts:
        # Each such retract is immediately followed by an XY rapid
        # (not an XY feed — that would feed through the island) and
        # then a Z feed down to pass depth. The full "over the
        # obstacle" triple.
        xy_rapid = tp.instructions[i + 1]
        z_feed = tp.instructions[i + 2]
        assert xy_rapid.type is MoveType.RAPID
        assert xy_rapid.x is not None or xy_rapid.y is not None
        assert z_feed.type is MoveType.FEED
        assert z_feed.z is not None and z_feed.z < 0
