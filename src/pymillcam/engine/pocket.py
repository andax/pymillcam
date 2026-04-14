"""Pocket toolpath generator.

Consumes a PocketOp + Project, walks the selected closed boundary, and
emits IR that clears the interior area.

Strategy: OFFSET (concentric inward rings). The outermost ring sits one
tool radius inside the boundary (cutter edge flush with the wall), and
each subsequent ring steps inward by `stepover` until the region closes
up. Arcs are preserved end-to-end when the analytical offsetter handles
the shape; otherwise we fall back to Shapely's buffer (chord-based).

What this MVP does not yet cover:
- Multi-depth stepping (single-depth only for this first slice).
- Ramp entry — rings plunge straight at the start point. Users should
  pre-drill a starter hole for real material until helical/linear ramp
  lands for pockets.
- Islands / holes in the boundary.
- ZIGZAG / SPIRAL strategies.
"""
from __future__ import annotations

from shapely.geometry import Polygon

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.offsetter import OffsetError, offset_closed_contour
from pymillcam.core.operations import (
    MillingDirection,
    PocketOp,
    PocketStrategy,
)
from pymillcam.core.project import Project
from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    Segment,
    reverse_segment_chain,
    segments_to_shapely,
)
from pymillcam.core.tools import ToolController
from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath


class PocketGenerationError(Exception):
    """Raised when a PocketOp cannot be converted into a toolpath."""


def compute_pocket_preview(op: PocketOp, project: Project) -> list[Segment]:
    """Return the 2D plan-view path the cutter centre will follow.

    Concatenates every concentric ring in cut order. Used by the UI to
    show a live preview as the user edits operation parameters.
    """
    tool_controller = _resolve_tool_controller(op, project)
    chord_tolerance = (
        op.chord_tolerance
        if op.chord_tolerance is not None
        else project.settings.chord_tolerance
    )
    tool_radius = float(tool_controller.tool.geometry["diameter"]) / 2.0
    preview: list[Segment] = []
    for ref in op.geometry_refs:
        entity = _resolve_entity(ref.layer_name, ref.entity_id, project)
        rings = _concentric_rings(
            entity, tool_radius, op.stepover, op.direction, chord_tolerance
        )
        for ring in rings:
            preview.extend(ring)
    return preview


def generate_pocket_toolpath(op: PocketOp, project: Project) -> Toolpath:
    """Generate an IR Toolpath for a single PocketOp within the given Project."""
    if op.strategy is not PocketStrategy.OFFSET:
        raise PocketGenerationError(
            f"Pocket strategy {op.strategy.value!r} is not implemented yet "
            "— only 'offset' is available in this slice."
        )

    tool_controller = _resolve_tool_controller(op, project)
    safe_height = (
        op.safe_height if op.safe_height is not None else project.settings.safe_height
    )
    clearance = (
        op.clearance_plane
        if op.clearance_plane is not None
        else project.settings.clearance_plane
    )
    chord_tolerance = (
        op.chord_tolerance
        if op.chord_tolerance is not None
        else project.settings.chord_tolerance
    )
    tool_radius = float(tool_controller.tool.geometry["diameter"]) / 2.0

    toolpath = Toolpath(
        operation_name=op.name, tool_number=tool_controller.tool_number
    )
    instructions = toolpath.instructions
    instructions.append(
        IRInstruction(type=MoveType.COMMENT, comment=f"Pocket: {op.name}")
    )
    instructions.append(
        IRInstruction(type=MoveType.TOOL_CHANGE, tool_number=tool_controller.tool_number)
    )
    instructions.append(
        IRInstruction(type=MoveType.SPINDLE_ON, s=tool_controller.spindle_rpm)
    )

    for ref in op.geometry_refs:
        entity = _resolve_entity(ref.layer_name, ref.entity_id, project)
        rings = _concentric_rings(
            entity, tool_radius, op.stepover, op.direction, chord_tolerance
        )
        if not rings:
            raise PocketGenerationError(
                f"Pocket {op.name!r}: tool too large for the selected boundary "
                f"(no rings fit at stepover={op.stepover} mm, tool radius="
                f"{tool_radius} mm)."
            )
        _emit_rings(
            instructions,
            rings,
            tool_controller=tool_controller,
            cut_depth=op.cut_depth,
            safe_height=safe_height,
            clearance=clearance,
        )

    instructions.append(IRInstruction(type=MoveType.SPINDLE_OFF))
    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    return toolpath


def _resolve_tool_controller(op: PocketOp, project: Project) -> ToolController:
    if op.tool_controller_id is None:
        raise PocketGenerationError(
            f"Operation {op.name!r} has no tool_controller_id set"
        )
    for tc in project.tool_controllers:
        if tc.tool_number == op.tool_controller_id:
            return tc
    raise PocketGenerationError(
        f"Operation {op.name!r} references tool_controller_id={op.tool_controller_id}, "
        f"which is not present in the project"
    )


def _resolve_entity(
    layer_name: str, entity_id: str, project: Project
) -> GeometryEntity:
    for layer in project.geometry_layers:
        if layer.name != layer_name:
            continue
        entity = layer.find_entity(entity_id)
        if entity is not None:
            return entity
    raise PocketGenerationError(
        f"Geometry {layer_name!r}/{entity_id!r} not found in project"
    )


def _concentric_rings(
    entity: GeometryEntity,
    tool_radius: float,
    stepover: float,
    direction: MillingDirection,
    chord_tolerance: float,
) -> list[list[Segment]]:
    """Build inward concentric rings from a closed boundary.

    The outermost ring sits `tool_radius` inside the boundary (so the
    cutter edge is flush with the wall). Subsequent rings step inward
    by `stepover` until the analytical offsetter or Shapely buffer
    returns empty — that's the "no more material to remove" state.
    """
    if not entity.segments:
        raise PocketGenerationError(
            "Pocket operation requires a contour entity; got a point-only entity"
        )
    if not entity.closed:
        raise PocketGenerationError(
            "Pocket operation requires a closed boundary contour"
        )
    if tool_radius <= 0:
        raise PocketGenerationError(
            f"Tool radius must be positive, got {tool_radius}"
        )
    if stepover <= 0:
        raise PocketGenerationError(f"Stepover must be positive, got {stepover}")

    rings: list[list[Segment]] = []
    offset = tool_radius
    # Belt-and-braces cap: stops a pathological case where the offsetter
    # keeps returning non-empty results but the result area never shrinks.
    # 10,000 rings on a 1 m pocket at 0.1 mm stepover is still 1 m of work.
    safety_cap = 10_000
    for _ in range(safety_cap):
        ring = _offset_boundary_inward(entity, offset, chord_tolerance)
        if ring is None:
            break
        rings.append(_apply_direction(ring, direction))
        offset += stepover
    return rings


def _offset_boundary_inward(
    entity: GeometryEntity, distance: float, chord_tolerance: float
) -> list[Segment] | None:
    """Offset the entity inward by `distance`. Returns None if the offset
    collapses the area to nothing (pocket is now full)."""
    try:
        return offset_closed_contour(
            list(entity.segments), distance, outside=False
        )
    except OffsetError:
        return _offset_via_buffer(entity, distance, chord_tolerance)


def _offset_via_buffer(
    entity: GeometryEntity, distance: float, chord_tolerance: float
) -> list[Segment] | None:
    shadow = segments_to_shapely(
        entity.segments, closed=True, tolerance=chord_tolerance
    )
    if not isinstance(shadow, Polygon):
        raise PocketGenerationError(
            f"Expected a Polygon shadow for closed contour; got {shadow.geom_type}"
        )
    offset = shadow.buffer(-distance, join_style="mitre")
    if offset.is_empty:
        return None
    if not isinstance(offset, Polygon):
        # MultiPolygon: the pocket split into disjoint regions as it shrank.
        # MVP punts — treating that case needs per-region sequencing.
        return None
    coords = list(offset.exterior.coords)
    if len(coords) < 2:
        return None
    return [
        LineSegment(
            start=(coords[i][0], coords[i][1]),
            end=(coords[i + 1][0], coords[i + 1][1]),
        )
        for i in range(len(coords) - 1)
    ]


def _apply_direction(
    segments: list[Segment], direction: MillingDirection
) -> list[Segment]:
    """Orient a ring so travel matches the requested milling direction.

    The analytical offsetter returns CCW chains; the buffer fallback also
    returns CCW exteriors. Inside a pocket, CCW travel = conventional
    (chip thickness increases from zero) and CW = climb. So climb needs
    a reversal.
    """
    ccw = _chain_is_ccw(segments)
    needs_reverse = (direction is MillingDirection.CLIMB) == ccw
    return reverse_segment_chain(segments) if needs_reverse else segments


def _chain_is_ccw(segments: list[Segment]) -> bool:
    try:
        shadow = segments_to_shapely(segments, closed=True, tolerance=0.5)
    except ValueError:
        return True
    exterior = getattr(shadow, "exterior", None)
    if exterior is None:
        return True
    return bool(exterior.is_ccw)


def _emit_rings(
    instructions: list[IRInstruction],
    rings: list[list[Segment]],
    *,
    tool_controller: ToolController,
    cut_depth: float,
    safe_height: float,
    clearance: float,
) -> None:
    """Emit rings as a single Z-pass: rapid to the first ring start,
    plunge to depth, cut each ring, feed between rings, retract at the
    end.
    """
    if not rings:
        return
    first_start = rings[0][0].start
    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    instructions.append(
        IRInstruction(type=MoveType.RAPID, x=first_start[0], y=first_start[1])
    )
    instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))
    instructions.append(
        IRInstruction(type=MoveType.FEED, z=cut_depth, f=tool_controller.feed_z)
    )
    for ring_index, ring in enumerate(rings):
        if ring_index > 0:
            # Transit feed to the next ring's start — cutter stays at depth.
            next_start = ring[0].start
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED,
                    x=next_start[0],
                    y=next_start[1],
                    f=tool_controller.feed_xy,
                )
            )
        for seg in ring:
            _emit_segment(instructions, seg, tool_controller.feed_xy)
    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))


def _emit_segment(
    instructions: list[IRInstruction], seg: Segment, feed_xy: float
) -> None:
    if isinstance(seg, LineSegment):
        ex, ey = seg.end
        instructions.append(
            IRInstruction(type=MoveType.FEED, x=ex, y=ey, f=feed_xy)
        )
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
    raise PocketGenerationError(f"Unknown segment type: {type(seg).__name__}")
