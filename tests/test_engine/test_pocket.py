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


def test_single_depth_plunges_once() -> None:
    """One Z plunge at the first ring start, one retract at the end —
    nothing between rings."""
    project, op, _ = _project_with_rect_pocket(cut_depth=-3.0)
    tp = generate_pocket_toolpath(op, project)
    z_feeds = [
        i for i in tp.instructions
        if i.type is MoveType.FEED and i.z is not None
    ]
    assert len(z_feeds) == 1
    assert z_feeds[0].z == pytest.approx(-3.0)


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
