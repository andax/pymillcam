"""Profile toolpath generator.

Consumes a ProfileOp + Project, walks the referenced contour as a segment
chain, and emits IR. Arc segments translate directly to ARC_CW / ARC_CCW
instructions — they are not chord-approximated in the engine output.

Inside/outside offsetting still routes through Shapely's Polygon.buffer,
which discretizes arcs into chords at the configured chord_tolerance. That
is the only remaining lossy step and is isolated to `_offset_contour`
— when we add an arc-aware offset, the rest of the generator needs no
changes. `OffsetSide.ON_LINE` bypasses the buffer entirely, so any arcs
in the source geometry reach the G-code intact.
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.operations import OffsetSide, ProfileOp
from pymillcam.core.project import Project
from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    Segment,
    segments_to_shapely,
)
from pymillcam.core.tools import ToolController
from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath

DEFAULT_STEPDOWN_MM = 1.0


class ProfileGenerationError(Exception):
    """Raised when a ProfileOp cannot be converted into a toolpath."""


def compute_profile_preview(op: ProfileOp, project: Project) -> list[Segment]:
    """Return the 2D plan-view path the cutter centre will follow.

    No Z passes, no lead-in / lead-out, no rapids — just the offset
    contour. Used by the UI to show a live preview as the user edits
    operation parameters.
    """
    tool_controller = _resolve_tool_controller(op, project)
    chord_tolerance = (
        op.chord_tolerance
        if op.chord_tolerance is not None
        else project.settings.chord_tolerance
    )
    tool_radius = float(tool_controller.tool.geometry["diameter"]) / 2.0
    out: list[Segment] = []
    for ref in op.geometry_refs:
        entity = _resolve_entity(ref.layer_name, ref.entity_id, project)
        out.extend(_offset_contour(entity, tool_radius, op.offset_side, chord_tolerance))
    return out


def generate_profile_toolpath(op: ProfileOp, project: Project) -> Toolpath:
    """Generate an IR Toolpath for a single ProfileOp within the given Project."""
    tool_controller = _resolve_tool_controller(op, project)
    safe_height = op.safe_height if op.safe_height is not None else project.settings.safe_height
    clearance = (
        op.clearance_plane if op.clearance_plane is not None else project.settings.clearance_plane
    )
    chord_tolerance = (
        op.chord_tolerance
        if op.chord_tolerance is not None
        else project.settings.chord_tolerance
    )
    stepdown = _resolve_stepdown(op, tool_controller)
    tool_radius = float(tool_controller.tool.geometry["diameter"]) / 2.0

    toolpath = Toolpath(operation_name=op.name, tool_number=tool_controller.tool_number)
    instructions = toolpath.instructions
    instructions.append(IRInstruction(type=MoveType.COMMENT, comment=f"Profile: {op.name}"))
    instructions.append(
        IRInstruction(type=MoveType.TOOL_CHANGE, tool_number=tool_controller.tool_number)
    )
    instructions.append(IRInstruction(type=MoveType.SPINDLE_ON, s=tool_controller.spindle_rpm))

    for ref in op.geometry_refs:
        entity = _resolve_entity(ref.layer_name, ref.entity_id, project)
        segments = _offset_contour(entity, tool_radius, op.offset_side, chord_tolerance)
        _emit_contour_passes(
            instructions,
            segments,
            tool_controller=tool_controller,
            cut_depth=op.cut_depth,
            multi_depth=op.multi_depth,
            stepdown=stepdown,
            safe_height=safe_height,
            clearance=clearance,
        )

    instructions.append(IRInstruction(type=MoveType.SPINDLE_OFF))
    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    return toolpath


def _resolve_tool_controller(op: ProfileOp, project: Project) -> ToolController:
    if op.tool_controller_id is None:
        raise ProfileGenerationError(f"Operation {op.name!r} has no tool_controller_id set")
    for tc in project.tool_controllers:
        if tc.tool_number == op.tool_controller_id:
            return tc
    raise ProfileGenerationError(
        f"Operation {op.name!r} references tool_controller_id={op.tool_controller_id}, "
        f"which is not present in the project"
    )


def _resolve_entity(layer_name: str, entity_id: str, project: Project) -> GeometryEntity:
    for layer in project.geometry_layers:
        if layer.name != layer_name:
            continue
        entity = layer.find_entity(entity_id)
        if entity is not None:
            return entity
    raise ProfileGenerationError(f"Geometry {layer_name!r}/{entity_id!r} not found in project")


def _resolve_stepdown(op: ProfileOp, tc: ToolController) -> float:
    if op.stepdown is not None:
        return op.stepdown
    if tc.tool.cutting_data:
        return next(iter(tc.tool.cutting_data.values())).stepdown
    return DEFAULT_STEPDOWN_MM


def _offset_contour(
    entity: GeometryEntity,
    radius: float,
    side: OffsetSide,
    chord_tolerance: float,
) -> list[Segment]:
    if not entity.segments:
        raise ProfileGenerationError(
            "Profile operation requires a contour entity; got a point-only entity"
        )

    if side is OffsetSide.ON_LINE or radius == 0:
        return list(entity.segments)

    if not entity.closed:
        raise ProfileGenerationError(
            "Inside/outside offset requires a closed contour; got an open segment chain. "
            "Use offset_side=ON_LINE for open contours."
        )

    shadow = segments_to_shapely(
        entity.segments, closed=True, tolerance=chord_tolerance
    )
    if not isinstance(shadow, Polygon):
        raise ProfileGenerationError(
            f"Expected a Polygon shadow for closed contour; got {shadow.geom_type}"
        )

    distance = radius if side is OffsetSide.OUTSIDE else -radius
    offset = shadow.buffer(distance, join_style="mitre")
    if offset.is_empty:
        raise ProfileGenerationError(
            f"Inside offset by {radius} mm removes all geometry — tool too large"
        )
    if not isinstance(offset, Polygon):
        raise ProfileGenerationError(
            f"Offset produced {offset.geom_type}; MVP only supports a single polygon result"
        )

    # Shapely's buffer has already collapsed arcs to chords, so every output
    # segment is a line. Arc-aware offsetting (future) will return a mix.
    coords = list(offset.exterior.coords)
    return [
        LineSegment(start=(coords[i][0], coords[i][1]), end=(coords[i + 1][0], coords[i + 1][1]))
        for i in range(len(coords) - 1)
    ]


def _emit_contour_passes(
    instructions: list[IRInstruction],
    segments: list[Segment],
    *,
    tool_controller: ToolController,
    cut_depth: float,
    multi_depth: bool,
    stepdown: float,
    safe_height: float,
    clearance: float,
) -> None:
    if not segments:
        return

    start_x, start_y = segments[0].start
    z_levels = _z_levels(cut_depth, stepdown, multi_depth)
    if not z_levels:
        return

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    instructions.append(IRInstruction(type=MoveType.RAPID, x=start_x, y=start_y))
    instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))

    for pass_index, z in enumerate(z_levels):
        instructions.append(
            IRInstruction(type=MoveType.FEED, z=z, f=tool_controller.feed_z)
        )
        for seg in segments:
            _emit_segment(instructions, seg, tool_controller.feed_xy)

        is_last = pass_index == len(z_levels) - 1
        end_x, end_y = segments[-1].end
        # Tolerance compare — full-circle arcs return mathematically to start
        # but floating-point precision can leave a sub-picometre residual.
        if not is_last and math.hypot(end_x - start_x, end_y - start_y) > 1e-6:
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED, x=start_x, y=start_y, f=tool_controller.feed_xy
                )
            )

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))


def _emit_segment(instructions: list[IRInstruction], seg: Segment, feed_xy: float) -> None:
    if isinstance(seg, LineSegment):
        ex, ey = seg.end
        instructions.append(IRInstruction(type=MoveType.FEED, x=ex, y=ey, f=feed_xy))
        return
    if isinstance(seg, ArcSegment):
        sx, sy = seg.start
        ex, ey = seg.end
        cx, cy = seg.center
        move_type = MoveType.ARC_CCW if seg.ccw else MoveType.ARC_CW
        instructions.append(
            IRInstruction(
                type=move_type,
                x=ex,
                y=ey,
                i=cx - sx,
                j=cy - sy,
                f=feed_xy,
            )
        )
        return
    raise ProfileGenerationError(f"Unknown segment type: {type(seg).__name__}")


def _z_levels(cut_depth: float, stepdown: float, multi_depth: bool) -> list[float]:
    if cut_depth >= 0:
        return []
    if not multi_depth or stepdown <= 0:
        return [cut_depth]
    step = abs(stepdown)
    levels: list[float] = []
    z = 0.0
    while z > cut_depth:
        z -= step
        if z < cut_depth:
            z = cut_depth
        levels.append(z)
    return levels
