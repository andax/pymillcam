"""Profile toolpath generator.

Takes a ProfileOp + the surrounding Project, resolves geometry and tool
settings, and emits IR for a multi-depth offset cut along each referenced
contour. MVP scope: offset by tool radius (inside / outside / on_line),
plunge entry, direct exit. Lead-in/out, tabs, and ramp entry are deferred
to later work — the ProfileOp fields are persisted but not yet consumed.
"""
from __future__ import annotations

from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.operations import OffsetSide, ProfileOp
from pymillcam.core.project import Project
from pymillcam.core.tools import ToolController
from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath

DEFAULT_STEPDOWN_MM = 1.0


class ProfileGenerationError(Exception):
    """Raised when a ProfileOp cannot be converted into a toolpath."""


def generate_profile_toolpath(op: ProfileOp, project: Project) -> Toolpath:
    """Generate an IR Toolpath for a single ProfileOp within the given Project."""
    tool_controller = _resolve_tool_controller(op, project)
    safe_height = op.safe_height if op.safe_height is not None else project.settings.safe_height
    clearance = (
        op.clearance_plane if op.clearance_plane is not None else project.settings.clearance_plane
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
        path = _offset_contour(entity, tool_radius, op.offset_side)
        _emit_contour_passes(
            instructions,
            path,
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
    # No per-material cascade is wired up yet — take the first entry if present,
    # otherwise fall back to a conservative default. Material-aware resolution
    # will land when MaterialDatabase arrives.
    if tc.tool.cutting_data:
        return next(iter(tc.tool.cutting_data.values())).stepdown
    return DEFAULT_STEPDOWN_MM


def _offset_contour(entity: GeometryEntity, radius: float, side: OffsetSide) -> LineString:
    geom: BaseGeometry = entity.geom

    if side is OffsetSide.ON_LINE or radius == 0:
        if isinstance(geom, Polygon):
            return LineString(geom.exterior.coords)
        if isinstance(geom, LineString):
            return geom
        raise ProfileGenerationError(
            f"Unsupported geometry type for profile: {geom.geom_type}"
        )

    if isinstance(geom, Polygon):
        distance = radius if side is OffsetSide.OUTSIDE else -radius
        offset = geom.buffer(distance, join_style="mitre")
        if offset.is_empty:
            raise ProfileGenerationError(
                f"Inside offset by {radius} mm removes all geometry — tool too large"
            )
        if not isinstance(offset, Polygon):
            raise ProfileGenerationError(
                f"Offset produced {offset.geom_type}; MVP only supports a single polygon result"
            )
        return LineString(offset.exterior.coords)

    raise ProfileGenerationError(
        "Inside/outside offset requires a closed contour; got open LineString. "
        "Use offset_side=ON_LINE for open contours."
    )


def _emit_contour_passes(
    instructions: list[IRInstruction],
    path: LineString,
    *,
    tool_controller: ToolController,
    cut_depth: float,
    multi_depth: bool,
    stepdown: float,
    safe_height: float,
    clearance: float,
) -> None:
    coords = [(float(x), float(y)) for x, y, *_ in path.coords]
    if len(coords) < 2:
        return

    start_x, start_y = coords[0]
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
        for x, y in coords[1:]:
            instructions.append(
                IRInstruction(type=MoveType.FEED, x=x, y=y, f=tool_controller.feed_xy)
            )
        is_last = pass_index == len(z_levels) - 1
        if not is_last and (coords[-1] != (start_x, start_y)):
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED, x=start_x, y=start_y, f=tool_controller.feed_xy
                )
            )

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))


def _z_levels(cut_depth: float, stepdown: float, multi_depth: bool) -> list[float]:
    """Z values (most shallow → deepest, inclusive of cut_depth).

    cut_depth is expected to be negative (below stock top). Non-negative
    cut_depth means no cutting and returns an empty list.
    """
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
