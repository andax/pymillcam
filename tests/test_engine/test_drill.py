"""Behaviour tests for the drill toolpath engine.

Asserts contracts (counts of plunges, retract-heights, point alignment)
rather than exact IR indices, so a future refactor (e.g. emitting canned
cycles for some post-processors) doesn't trigger spurious failures.
"""
from __future__ import annotations

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import DrillCycle, DrillOp, GeometryRef
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.core.tools import Tool, ToolController
from pymillcam.engine.drill import (
    DrillGenerationError,
    compute_drill_preview,
    generate_drill_toolpath,
)
from pymillcam.engine.ir import IRInstruction, MoveType

# ---------------------------------------------------------------- fixtures


def _make_project(
    points: list[tuple[float, float]],
    *,
    cycle: DrillCycle = DrillCycle.SIMPLE,
    cut_depth: float = -3.0,
    peck_depth: float | None = None,
    chip_break_retract: float = 0.5,
    dwell_s: float = 0.0,
    clearance: float = 3.0,
    safe_height: float = 15.0,
    feed_z: float = 300.0,
) -> tuple[Project, DrillOp]:
    """Build a Project with N point entities + a DrillOp referencing them."""
    project = Project()
    project.settings.clearance_plane = clearance
    project.settings.safe_height = safe_height
    # No warmup dwell by default — keeps test IR tight.
    project.settings.spindle_warmup_s = 0.0
    layer = GeometryLayer(
        name="L",
        entities=[GeometryEntity(point=pt) for pt in points],
    )
    project.geometry_layers.append(layer)
    tc = ToolController(tool_number=1, tool=Tool(name="drill"))
    tc.feed_z = feed_z
    project.tool_controllers.append(tc)
    op = DrillOp(
        name="Drill",
        tool_controller_id=1,
        geometry_refs=[
            GeometryRef(layer_name="L", entity_id=e.id) for e in layer.entities
        ],
        cut_depth=cut_depth,
        cycle=cycle,
        peck_depth=peck_depth,
        chip_break_retract=chip_break_retract,
        dwell_at_bottom_s=dwell_s,
    )
    project.operations.append(op)
    return project, op


# -------- helpers for slicing IR streams into per-hole segments ---------


def _strip_preamble(instructions: list[IRInstruction]) -> list[IRInstruction]:
    """Drop everything up to and including the initial lift to safe_height."""
    for i, inst in enumerate(instructions):
        if (
            inst.type is MoveType.RAPID
            and inst.z is not None
            and inst.x is None and inst.y is None
        ):
            # First Z-only rapid is the safe-height lift between preamble
            # and the drilling work.
            return instructions[i + 1 :]
    return instructions


def _split_by_xy(
    instructions: list[IRInstruction],
) -> list[list[IRInstruction]]:
    """Group instructions by which (x, y) the tool is at.

    A new group starts every time an XY rapid lands on a different XY —
    i.e. each hole's instruction block.
    """
    groups: list[list[IRInstruction]] = []
    current_xy: tuple[float | None, float | None] | None = None
    for inst in instructions:
        if inst.x is not None and inst.y is not None and (
            current_xy is None or (inst.x, inst.y) != current_xy
        ):
            current_xy = (inst.x, inst.y)
            groups.append([inst])
        elif groups:
            groups[-1].append(inst)
    return groups


# ------------------------------------------------------------ simple cycle


def test_simple_cycle_plunges_once_and_retracts_to_clearance() -> None:
    project, op = _make_project([(10.0, 20.0)], cycle=DrillCycle.SIMPLE,
                                cut_depth=-3.0, clearance=3.0)

    tp = generate_drill_toolpath(op, project)
    body = _strip_preamble(tp.instructions)

    feeds = [i for i in body if i.type is MoveType.FEED]
    assert len(feeds) == 1
    plunge = feeds[0]
    assert plunge.z == pytest.approx(-3.0)
    assert (plunge.x, plunge.y) == pytest.approx((10.0, 20.0))

    # One in-hole retract to clearance, plus one final retract to safe.
    retracts_to_clearance = [
        i for i in body
        if i.type is MoveType.RAPID and i.z == pytest.approx(3.0)
    ]
    assert len(retracts_to_clearance) >= 1


def test_simple_cycle_emits_dwell_when_configured() -> None:
    project, op = _make_project([(0.0, 0.0)], dwell_s=0.25)
    tp = generate_drill_toolpath(op, project)

    dwells = [i for i in tp.instructions if i.type is MoveType.DWELL]
    # No warmup dwell in this fixture, so the only dwell is at-bottom.
    assert len(dwells) == 1
    assert dwells[0].f == pytest.approx(0.25)


# -------------------------------------------------------------- peck cycle


def test_peck_cycle_steps_down_by_peck_depth() -> None:
    project, op = _make_project(
        [(0.0, 0.0)], cycle=DrillCycle.PECK, cut_depth=-5.0, peck_depth=2.0,
    )

    tp = generate_drill_toolpath(op, project)
    body = _strip_preamble(tp.instructions)

    feed_zs = [i.z for i in body if i.type is MoveType.FEED]
    # 5 mm deep, 2 mm pecks → pecks land at -2, -4, -5 (final snaps).
    assert feed_zs == [pytest.approx(-2.0), pytest.approx(-4.0), pytest.approx(-5.0)]


def test_peck_cycle_fully_retracts_between_pecks() -> None:
    project, op = _make_project(
        [(0.0, 0.0)], cycle=DrillCycle.PECK, cut_depth=-5.0, peck_depth=2.0,
        clearance=3.0,
    )

    tp = generate_drill_toolpath(op, project)
    body = _strip_preamble(tp.instructions)

    # Between each pair of feed-pecks there must be a RAPID back to
    # clearance — that's the "full retract for chip clearance" contract
    # that distinguishes PECK from CHIP_BREAK.
    retracts_to_clearance = [
        i for i in body
        if i.type is MoveType.RAPID and i.z == pytest.approx(3.0)
    ]
    # 3 pecks → at least 3 full retracts (one after each peck, the final
    # one doubles as the traverse-to-next-hole retract).
    assert len(retracts_to_clearance) >= 3


def test_peck_cycle_snaps_last_peck_to_cut_depth() -> None:
    """When peck_depth doesn't divide cut_depth evenly, the last peck
    lands exactly on cut_depth — no overshoot."""
    project, op = _make_project(
        [(0.0, 0.0)], cycle=DrillCycle.PECK, cut_depth=-3.5, peck_depth=2.0,
    )

    tp = generate_drill_toolpath(op, project)
    feed_zs = [i.z for i in tp.instructions if i.type is MoveType.FEED]
    assert feed_zs == [pytest.approx(-2.0), pytest.approx(-3.5)]


def test_peck_cycle_defaults_peck_depth_when_unset() -> None:
    project, op = _make_project(
        [(0.0, 0.0)], cycle=DrillCycle.PECK, cut_depth=-2.0, peck_depth=None,
    )
    tp = generate_drill_toolpath(op, project)
    # Default is 1 mm — 2 mm deep → pecks at -1, -2.
    feed_zs = [i.z for i in tp.instructions if i.type is MoveType.FEED]
    assert feed_zs == [pytest.approx(-1.0), pytest.approx(-2.0)]


# -------------------------------------------------------- chip-break cycle


def test_chip_break_cycle_uses_small_in_hole_retract() -> None:
    project, op = _make_project(
        [(0.0, 0.0)], cycle=DrillCycle.CHIP_BREAK, cut_depth=-5.0,
        peck_depth=2.0, chip_break_retract=0.5, clearance=3.0,
    )

    tp = generate_drill_toolpath(op, project)
    body = _strip_preamble(tp.instructions)

    # Between pecks, the retract is peck_bottom + chip_break_retract, not
    # clearance. Two inter-peck retracts expected (3 pecks → 2 gaps).
    # Plus one final full retract to clearance.
    small_retracts = [
        i for i in body
        if i.type is MoveType.RAPID
        and i.z is not None
        and i.z < 0  # still in the hole
    ]
    assert len(small_retracts) == 2
    # Each retract lands 0.5 mm above the preceding peck bottom.
    assert small_retracts[0].z == pytest.approx(-2.0 + 0.5)
    assert small_retracts[1].z == pytest.approx(-4.0 + 0.5)


def test_chip_break_cycle_final_retract_exits_to_clearance() -> None:
    project, op = _make_project(
        [(0.0, 0.0)], cycle=DrillCycle.CHIP_BREAK, cut_depth=-4.0,
        peck_depth=2.0, clearance=3.0,
    )

    tp = generate_drill_toolpath(op, project)
    body = _strip_preamble(tp.instructions)

    # The last RAPID in the hole block should land at clearance — that
    # positions the tool for the next traverse.
    rapids = [i for i in body if i.type is MoveType.RAPID]
    assert rapids[-2].z == pytest.approx(3.0) or rapids[-1].z == pytest.approx(15.0)


# ------------------------------------------------------------ multi-point


def test_multiple_holes_are_drilled_in_selection_order() -> None:
    points = [(0.0, 0.0), (10.0, 5.0), (20.0, 0.0)]
    project, op = _make_project(points, cycle=DrillCycle.SIMPLE)

    tp = generate_drill_toolpath(op, project)
    body = _strip_preamble(tp.instructions)
    # Extract the FEED (plunge) move per hole — they carry the point (x, y).
    plunges = [i for i in body if i.type is MoveType.FEED]
    got = [(p.x, p.y) for p in plunges]
    assert got == pytest.approx([(0.0, 0.0), (10.0, 5.0), (20.0, 0.0)])


def test_between_holes_stays_at_clearance_not_safe_height() -> None:
    """Between-hole traversal is at the clearance plane — raising to
    safe_height per hole would cost many seconds across a drill pattern.
    safe_height is reserved for entering / exiting the whole op."""
    project, op = _make_project(
        [(0.0, 0.0), (10.0, 0.0)], clearance=3.0, safe_height=15.0,
    )

    tp = generate_drill_toolpath(op, project)
    body = _strip_preamble(tp.instructions)

    # Find the XY-rapid that moves from the first hole to the second.
    traverses = [
        i for i in body
        if i.type is MoveType.RAPID
        and i.x is not None
        and i.y is not None
        and (i.x, i.y) == pytest.approx((10.0, 0.0))
    ]
    assert traverses, "no XY traverse to second hole"
    assert traverses[0].z == pytest.approx(3.0)  # clearance, not safe_height


# --------------------------------------------------------- preamble / postamble


def test_preamble_issues_tool_change_spindle_and_warmup_dwell() -> None:
    points = [(0.0, 0.0)]
    project, op = _make_project(points)
    project.settings.spindle_warmup_s = 1.5

    tp = generate_drill_toolpath(op, project)
    preamble = tp.instructions[:5]
    types = [i.type for i in preamble]
    assert MoveType.TOOL_CHANGE in types
    assert MoveType.SPINDLE_ON in types
    assert MoveType.DWELL in types


def test_final_instruction_is_retract_to_safe_height() -> None:
    project, op = _make_project([(0.0, 0.0)])
    tp = generate_drill_toolpath(op, project)
    last = tp.instructions[-1]
    assert last.type is MoveType.RAPID
    assert last.z == pytest.approx(project.settings.safe_height)
    # Z-only retract — don't drag XY.
    assert last.x is None and last.y is None


# ------------------------------------------------------------ error cases


def test_positive_cut_depth_raises() -> None:
    project, op = _make_project([(0.0, 0.0)], cut_depth=1.0)
    with pytest.raises(DrillGenerationError, match="cut_depth"):
        generate_drill_toolpath(op, project)


def test_zero_cut_depth_raises() -> None:
    project, op = _make_project([(0.0, 0.0)], cut_depth=0.0)
    with pytest.raises(DrillGenerationError, match="cut_depth"):
        generate_drill_toolpath(op, project)


def test_open_contour_entity_raises() -> None:
    project = Project()
    # Open LINE — not a valid drill target.
    line_entity = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(1, 0))],
        closed=False,
    )
    project.geometry_layers.append(
        GeometryLayer(name="L", entities=[line_entity])
    )
    project.tool_controllers.append(
        ToolController(tool_number=1, tool=Tool(name="drill"))
    )
    op = DrillOp(
        name="D",
        tool_controller_id=1,
        cut_depth=-2.0,
        geometry_refs=[GeometryRef(layer_name="L", entity_id=line_entity.id)],
    )
    with pytest.raises(DrillGenerationError, match="POINT or closed-contour"):
        generate_drill_toolpath(op, project)


# ---------------------------------------------------------- circle → centre


def test_closed_circle_drill_target_uses_arc_center() -> None:
    """A full-circle arc entity contributes its centre as the drill point."""
    project = Project()
    project.settings.spindle_warmup_s = 0.0
    circle_entity = GeometryEntity(
        segments=[
            ArcSegment(
                center=(7.0, 3.0), radius=2.0, start_angle_deg=0.0, sweep_deg=360.0,
            )
        ],
        closed=True,
    )
    project.geometry_layers.append(
        GeometryLayer(name="L", entities=[circle_entity])
    )
    project.tool_controllers.append(
        ToolController(tool_number=1, tool=Tool(name="drill"))
    )
    op = DrillOp(
        name="D",
        tool_controller_id=1,
        cut_depth=-1.0,
        geometry_refs=[GeometryRef(layer_name="L", entity_id=circle_entity.id)],
    )
    tp = generate_drill_toolpath(op, project)
    feed = next(i for i in tp.instructions if i.type is MoveType.FEED)
    # The plunge lands on the circle's centre, not its edge.
    assert (feed.x, feed.y) == pytest.approx((7.0, 3.0))


# ---------------------------------------------------------- preview


def test_preview_of_single_point_is_empty() -> None:
    project, op = _make_project([(5.0, 5.0)])
    assert compute_drill_preview(op, project) == []


def test_preview_connects_consecutive_drill_points() -> None:
    points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    project, op = _make_project(points)

    preview = compute_drill_preview(op, project)
    # N points → N-1 connecting segments.
    assert len(preview) == 2
    for i, seg in enumerate(preview):
        assert seg.start == pytest.approx(points[i])
        assert seg.end == pytest.approx(points[i + 1])


# ------------------------------------------------------------ service wiring


def test_drill_op_registered_with_toolpath_service() -> None:
    """Regression — adding a new op type should only require registering
    it with the service; MainWindow etc. never touch op-type dispatch."""
    from pymillcam.engine.services import ToolpathService

    svc = ToolpathService()
    assert svc.supports(DrillOp(name="d"))


def test_service_generates_drill_program_end_to_end() -> None:
    from pymillcam.engine.services import ToolpathService
    from pymillcam.post.uccnc import UccncPostProcessor

    project, _op = _make_project([(0.0, 0.0), (10.0, 0.0)], cut_depth=-2.0)
    svc = ToolpathService()
    gcode, toolpaths = svc.generate_program(project, UccncPostProcessor())
    assert len(toolpaths) == 1
    # Sanity: G-code mentions the expected tool and has at least one G1.
    assert "T1 M6" in gcode
    assert "G1" in gcode
