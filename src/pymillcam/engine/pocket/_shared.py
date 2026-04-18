"""Cross-strategy helpers for the pocket engine.

Shared between OFFSET (`offset.py`), ZIGZAG (`zigzag.py`), and the
rest-machining pass (`rest_machining.py`): the error class, the
offset-or-buffer boundary offset primitive, ring-orientation helpers,
and the low-level emission helpers that both strategies use to walk
ring chains.
"""
from __future__ import annotations

from shapely.geometry import MultiPolygon, Polygon

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.offsetter import OffsetError, offset_closed_contour
from pymillcam.core.operations import MillingDirection, PocketOp
from pymillcam.core.project import Project
from pymillcam.core.segments import (
    LineSegment,
    Segment,
    reverse_segment_chain,
    segments_to_shapely,
)
from pymillcam.core.tools import ToolController
from pymillcam.engine.common import (
    EngineError,
    chain_is_ccw as _chain_is_ccw,
    emit_segment as _common_emit_segment,
    resolve_entity as _common_resolve_entity,
    resolve_tool_controller as _common_resolve_tool_controller,
)
from pymillcam.engine.ir import IRInstruction, MoveType


class PocketGenerationError(EngineError):
    """Raised when a PocketOp cannot be converted into a toolpath."""


def _resolve_tool_controller(op: PocketOp, project: Project) -> ToolController:
    return _common_resolve_tool_controller(
        op, project, error_cls=PocketGenerationError
    )


def _resolve_entity(
    layer_name: str, entity_id: str, project: Project
) -> GeometryEntity:
    return _common_resolve_entity(
        layer_name, entity_id, project, error_cls=PocketGenerationError
    )


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


def _extract_polygons(geom: object) -> list[Polygon]:
    """Recursively extract non-empty Polygons from any Shapely geometry."""
    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty else []
    if isinstance(geom, MultiPolygon):
        return [p for p in geom.geoms if not p.is_empty]
    inner_geoms = getattr(geom, "geoms", None)
    if inner_geoms is None:
        return []
    out: list[Polygon] = []
    for sub in inner_geoms:
        out.extend(_extract_polygons(sub))
    return out


def _coords_to_line_chain(
    coords: list[tuple[float, float]],
) -> list[Segment]:
    """Build a closed LineSegment chain from a Shapely-style ring coord list."""
    if len(coords) < 2:
        return []
    return [
        LineSegment(start=(coords[i][0], coords[i][1]),
                    end=(coords[i + 1][0], coords[i + 1][1]))
        for i in range(len(coords) - 1)
    ]


def _polygon_to_ring_group(
    poly: Polygon, direction: MillingDirection
) -> list[list[Segment]]:
    group: list[list[Segment]] = []
    ext_ring = _coords_to_line_chain(
        [(c[0], c[1]) for c in poly.exterior.coords]
    )
    if ext_ring:
        group.append(_apply_direction(ext_ring, direction))
    for interior in poly.interiors:
        int_ring = _coords_to_line_chain(
            [(c[0], c[1]) for c in interior.coords]
        )
        if not int_ring:
            continue
        # Holes are CW from Shapely; flip to match milling-direction
        # convention (same logic as the boundary).
        group.append(_apply_direction(int_ring, direction))
    return group


def _ramp_stepdown(z_levels: list[float]) -> float:
    """Stepdown used to size the ramp — the max per-pass descent, so the
    ramp geometry is fixed across passes (the last pass may be shallower
    when cut_depth doesn't divide evenly by stepdown, which just makes
    that pass's effective ramp angle gentler)."""
    if not z_levels:
        return 0.0
    descents = [abs(z_levels[0])]
    descents.extend(
        abs(b - a) for a, b in zip(z_levels[:-1], z_levels[1:], strict=True)
    )
    return max(descents)


def _emit_segment(
    instructions: list[IRInstruction], seg: Segment, feed_xy: float
) -> None:
    _common_emit_segment(
        instructions, seg, feed_xy, error_cls=PocketGenerationError
    )


def _emit_ring_chain(
    instructions: list[IRInstruction], rings: list[list[Segment]], feed_xy: float
) -> None:
    """Cut a sequence of rings at whatever Z the tool is already at,
    transitioning between rings via feed moves (no retract)."""
    for ring_index, ring in enumerate(rings):
        if ring_index > 0:
            next_start = ring[0].start
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED,
                    x=next_start[0],
                    y=next_start[1],
                    f=feed_xy,
                )
            )
        for seg in ring:
            _emit_segment(instructions, seg, feed_xy)
