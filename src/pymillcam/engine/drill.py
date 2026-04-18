"""Drill toolpath generator.

Consumes a ``DrillOp`` + ``Project`` and emits IR for drilling each
referenced point in selection order. No TSP ordering yet — layered on
later as a separate pass.

Three cycle types:

* ``SIMPLE`` — one plunge to ``cut_depth``, retract to clearance.
  Analogous to a G81 canned cycle (but we emit expanded G0/G1 so every
  post-processor handles it).
* ``PECK`` — plunge in ``peck_depth`` increments, full retract to
  clearance between pecks so chips clear the flutes. Analogous to G83.
* ``CHIP_BREAK`` — same peck increments, but the between-peck retract
  is only ``chip_break_retract`` (small enough to stay in the hole, big
  enough to snap the chip). Analogous to G73.

A drill target is either a ``POINT`` entity or a closed contour — for
closed contours, the entity's centroid is used (exact for a single
full-circle arc; Shapely centroid for polygons). Mixing points and
circles in one op is fine.
"""
from __future__ import annotations

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.operations import DrillCycle, DrillOp
from pymillcam.core.project import Project
from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    Segment,
    segments_to_shapely,
)
from pymillcam.core.tools import ToolController
from pymillcam.engine.common import (
    EngineError,
    resolve_clearance,
    resolve_entity,
    resolve_safe_height,
    resolve_tool_controller,
)
from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath

# Conservative default peck increment when ``op.peck_depth`` is unset.
# 1 mm is slow but always finishes; users should override for their
# specific bit / material.
_DEFAULT_PECK_DEPTH_MM = 1.0

# How close we get to the previous peck's bottom before switching from
# rapid descent to feed — small enough that we never crash into chips
# left in the hole, large enough that we don't waste time feeding air.
_PECK_RESUME_GAP_MM = 0.2


class DrillGenerationError(EngineError):
    """Raised when a DrillOp cannot be converted into a toolpath."""


# ------------------------------------------------------------------ public


def compute_drill_preview(op: DrillOp, project: Project) -> list[Segment]:
    """Return the plan-view travel path between drill points.

    Drill operations have no XY motion *during* drilling — the preview
    just shows the connecting rapids between successive holes so the
    user can see the drill order and spot an obvious mis-selection.
    For a single-point op the preview is empty.
    """
    points = _resolve_drill_points(op, project)
    if len(points) < 2:
        return []
    return [
        LineSegment(start=a, end=b)
        for a, b in zip(points, points[1:], strict=False)
    ]


def generate_drill_toolpath(op: DrillOp, project: Project) -> Toolpath:
    """Emit IR for one drill operation."""
    tool_controller = resolve_tool_controller(
        op, project, error_cls=DrillGenerationError
    )
    safe_height = resolve_safe_height(op, project)
    clearance = resolve_clearance(op, project)
    points = _resolve_drill_points(op, project)
    if not points:
        raise DrillGenerationError(
            f"Drill {op.name!r}: no drill points in the selected geometry."
        )
    if op.cut_depth >= 0:
        raise DrillGenerationError(
            f"Drill {op.name!r}: cut_depth must be negative (below stock top), "
            f"got {op.cut_depth}"
        )

    toolpath = Toolpath(
        operation_name=op.name, tool_number=tool_controller.tool_number
    )
    instructions = toolpath.instructions
    instructions.append(
        IRInstruction(type=MoveType.COMMENT, comment=f"Drill: {op.name}")
    )
    instructions.append(
        IRInstruction(
            type=MoveType.TOOL_CHANGE, tool_number=tool_controller.tool_number
        )
    )
    instructions.append(
        IRInstruction(type=MoveType.SPINDLE_ON, s=tool_controller.spindle_rpm)
    )
    if project.settings.spindle_warmup_s > 0:
        instructions.append(
            IRInstruction(type=MoveType.DWELL, f=project.settings.spindle_warmup_s)
        )

    # Every drill op starts by lifting to safe_height so inter-op travel
    # never catches a clamp. Between *holes* we stay at clearance, which
    # is a shorter / faster rapid and is safe by definition of the
    # clearance plane.
    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    for idx, pt in enumerate(points):
        _emit_hole(
            instructions,
            pt,
            op=op,
            tool_controller=tool_controller,
            clearance=clearance,
            is_first=(idx == 0),
            safe_height=safe_height,
        )
    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    return toolpath


# ------------------------------------------------------------------ helpers


def _resolve_drill_points(
    op: DrillOp, project: Project
) -> list[tuple[float, float]]:
    """Map each geometry ref to its drill (x, y).

    Point entities contribute their coordinate directly. Closed-contour
    entities contribute their geometric centre — exact centre for a
    single full-circle arc, Shapely centroid for a polygon.
    """
    points: list[tuple[float, float]] = []
    for ref in op.geometry_refs:
        entity = resolve_entity(
            ref.layer_name, ref.entity_id, project, error_cls=DrillGenerationError
        )
        points.append(_entity_drill_point(entity))
    return points


def _entity_drill_point(entity: GeometryEntity) -> tuple[float, float]:
    if entity.point is not None:
        return entity.point
    if entity.closed and entity.segments:
        # Fast-path: a single full-circle arc is by construction a
        # circle; its centre is the exact drill target without needing
        # Shapely's chord-approximated centroid.
        if len(entity.segments) == 1 and isinstance(entity.segments[0], ArcSegment):
            return entity.segments[0].center
        # General closed contour — Shapely centroid. Fine for polygons;
        # for contours with arcs, the chord discretisation slightly
        # biases the centroid but is well within any realistic drill
        # tolerance.
        shape = segments_to_shapely(entity.segments, closed=True, tolerance=0.01)
        centroid = getattr(shape, "centroid", None)
        if centroid is not None:
            return (float(centroid.x), float(centroid.y))
    raise DrillGenerationError(
        "Drill op requires POINT or closed-contour entities; got an open chain"
    )


def _emit_hole(
    instructions: list[IRInstruction],
    point: tuple[float, float],
    *,
    op: DrillOp,
    tool_controller: ToolController,
    clearance: float,
    is_first: bool,
    safe_height: float,
) -> None:
    """Emit the IR for drilling one hole at ``point``."""
    x, y = point
    # Traverse XY at whatever height we're currently safely at. For the
    # first hole we're at safe_height (from the preamble); subsequent
    # holes start from clearance (the previous hole's final retract).
    traverse_z = safe_height if is_first else clearance
    instructions.append(IRInstruction(type=MoveType.RAPID, x=x, y=y, z=traverse_z))
    # Descend to clearance above the stock surface.
    if is_first:
        instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))
    # Drill the hole.
    if op.cycle is DrillCycle.SIMPLE:
        _emit_simple_cycle(instructions, point, op, tool_controller, clearance)
    elif op.cycle is DrillCycle.PECK:
        _emit_peck_cycle(instructions, point, op, tool_controller, clearance)
    elif op.cycle is DrillCycle.CHIP_BREAK:
        _emit_chip_break_cycle(
            instructions, point, op, tool_controller, clearance
        )
    else:  # pragma: no cover - enum is exhaustive
        raise DrillGenerationError(f"Unknown drill cycle: {op.cycle!r}")
    # Every cycle exits with the tool at clearance above the hole,
    # ready for the next traverse.


def _emit_simple_cycle(
    instructions: list[IRInstruction],
    point: tuple[float, float],
    op: DrillOp,
    tool_controller: ToolController,
    clearance: float,
) -> None:
    """Plunge to full depth, optionally dwell, retract to clearance."""
    x, y = point
    instructions.append(
        IRInstruction(
            type=MoveType.FEED, x=x, y=y, z=op.cut_depth, f=tool_controller.feed_z
        )
    )
    if op.dwell_at_bottom_s > 0:
        instructions.append(IRInstruction(type=MoveType.DWELL, f=op.dwell_at_bottom_s))
    instructions.append(IRInstruction(type=MoveType.RAPID, x=x, y=y, z=clearance))


def _emit_peck_cycle(
    instructions: list[IRInstruction],
    point: tuple[float, float],
    op: DrillOp,
    tool_controller: ToolController,
    clearance: float,
) -> None:
    """G83-style: each peck fully retracts to clearance for chip clearance."""
    x, y = point
    peck = _resolve_peck_depth(op)
    current_z = 0.0  # top of stock
    last_bottom_z = 0.0  # where the previous peck ended
    first_peck = True
    while current_z > op.cut_depth + _LENGTH_EPS:
        next_z = max(current_z - peck, op.cut_depth)
        if not first_peck:
            # Rapid back down to just above the previous peck's bottom,
            # then feed through the fresh material. Saves most of the
            # cutting-feed air-time of a full-plunge drill.
            resume_z = last_bottom_z + _PECK_RESUME_GAP_MM
            # Guard against resume_z above current clearance (shouldn't
            # happen with realistic inputs but is cheap insurance).
            resume_z = min(resume_z, clearance)
            instructions.append(
                IRInstruction(type=MoveType.RAPID, x=x, y=y, z=resume_z)
            )
        instructions.append(
            IRInstruction(
                type=MoveType.FEED, x=x, y=y, z=next_z, f=tool_controller.feed_z
            )
        )
        if op.dwell_at_bottom_s > 0:
            instructions.append(
                IRInstruction(type=MoveType.DWELL, f=op.dwell_at_bottom_s)
            )
        # Full retract for chip clearance.
        instructions.append(IRInstruction(type=MoveType.RAPID, x=x, y=y, z=clearance))
        last_bottom_z = next_z
        current_z = next_z
        first_peck = False


def _emit_chip_break_cycle(
    instructions: list[IRInstruction],
    point: tuple[float, float],
    op: DrillOp,
    tool_controller: ToolController,
    clearance: float,
) -> None:
    """G73-style: small in-hole retract between pecks, full retract at end."""
    x, y = point
    peck = _resolve_peck_depth(op)
    current_z = 0.0
    while current_z > op.cut_depth + _LENGTH_EPS:
        next_z = max(current_z - peck, op.cut_depth)
        instructions.append(
            IRInstruction(
                type=MoveType.FEED, x=x, y=y, z=next_z, f=tool_controller.feed_z
            )
        )
        if op.dwell_at_bottom_s > 0:
            instructions.append(
                IRInstruction(type=MoveType.DWELL, f=op.dwell_at_bottom_s)
            )
        # Between pecks: small retract to snap the chip. We stay in the
        # hole — no repositioning needed for the next peck.
        if next_z > op.cut_depth + _LENGTH_EPS:
            break_z = next_z + op.chip_break_retract
            instructions.append(
                IRInstruction(type=MoveType.RAPID, x=x, y=y, z=break_z)
            )
        current_z = next_z
    # Full retract once the hole is at depth.
    instructions.append(IRInstruction(type=MoveType.RAPID, x=x, y=y, z=clearance))


def _resolve_peck_depth(op: DrillOp) -> float:
    if op.peck_depth is not None and op.peck_depth > 0:
        return op.peck_depth
    return _DEFAULT_PECK_DEPTH_MM


_LENGTH_EPS = 1e-9
