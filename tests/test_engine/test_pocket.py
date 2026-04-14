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
    assert MoveType.SPINDLE_OFF in types
    assert tp.instructions[-1].type is MoveType.RAPID
    assert tp.instructions[-1].z == project.settings.safe_height


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
    op.strategy = PocketStrategy.ZIGZAG
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
    # Outer ring (radius 23.5) is emitted as a full-circle ARC_CW. Right
    # after that arc we must see a straight FEED to (21.5, 0) (ring 1's
    # start), THEN the ring-1 arc.
    outer_ring_idx = next(
        i for i, ins in enumerate(tp.instructions)
        if ins.type is MoveType.ARC_CW
        and ins.x == pytest.approx(23.5, abs=1e-6)
        and ins.i == pytest.approx(-23.5, abs=1e-6)
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
