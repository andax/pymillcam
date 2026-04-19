"""Shared helpers for all toolpath generators.

Lifted here out of ``profile.py`` / ``pocket.py`` so new op types (drill,
surface, engrave, pocket sub-strategies) pick up the same cascade
resolution, pass planning, arc-preserving chain walk, and IR emit
conventions without another copy.

Every resolver / raising helper accepts an ``error_cls`` so the caller
keeps raising its own op-specific type (``ProfileGenerationError``,
``PocketGenerationError``, ...). The default ``EngineError`` is the
parent of those, which makes ``except EngineError`` a convenient
catch-all (e.g. in the UI) while ``except PocketGenerationError``
still targets a single op-type.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.operations import Operation
from pymillcam.core.project import Project
from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    Segment,
    segments_to_shapely,
    split_full_circle,
    split_segment_at_length,
)
from pymillcam.core.tools import ToolController
from pymillcam.engine.ir import IRInstruction, MoveType

# Arc-length / vector-magnitude epsilon. Anything below this is treated
# as zero when splitting chains or summing lengths.
LENGTH_EPSILON = 1e-9

# Fallback stepdown used when neither the op nor the ToolController's
# cutting-data mentions one. Conservative but always completes.
DEFAULT_STEPDOWN_MM = 1.0

# Discretisation tolerance for orientation checks on a closed chain.
# Orientation is invariant under small chord perturbations, so the
# cheapest discretisation is fine.
_ORIENTATION_TOLERANCE_MM = 0.5


class EngineError(Exception):
    """Base class for toolpath-engine failures."""


class _HasStepdown(Protocol):
    """Structural type for ops that expose a per-op stepdown override."""

    stepdown: float | None


# --------------------------------------------------------- cascade resolvers


def resolve_tool_controller(
    op: Operation,
    project: Project,
    *,
    error_cls: type[Exception] = EngineError,
) -> ToolController:
    """Find the op's ToolController in the project.

    Raises ``error_cls`` if ``op.tool_controller_id`` is unset or no
    matching controller lives in the project.
    """
    if op.tool_controller_id is None:
        raise error_cls(f"Operation {op.name!r} has no tool_controller_id set")
    for tc in project.tool_controllers:
        if tc.tool_number == op.tool_controller_id:
            return tc
    raise error_cls(
        f"Operation {op.name!r} references tool_controller_id="
        f"{op.tool_controller_id}, which is not present in the project"
    )


def resolve_entity(
    layer_name: str,
    entity_id: str,
    project: Project,
    *,
    error_cls: type[Exception] = EngineError,
) -> GeometryEntity:
    """Look up a geometry entity by (layer_name, entity_id)."""
    for layer in project.geometry_layers:
        if layer.name != layer_name:
            continue
        entity = layer.find_entity(entity_id)
        if entity is not None:
            return entity
    raise error_cls(f"Geometry {layer_name!r}/{entity_id!r} not found in project")


def resolve_stepdown(op: _HasStepdown, tc: ToolController) -> float:
    """Cascade: explicit op override > first cutting-data entry > default."""
    if op.stepdown is not None:
        return op.stepdown
    if tc.tool.cutting_data:
        return next(iter(tc.tool.cutting_data.values())).stepdown
    return DEFAULT_STEPDOWN_MM


def resolve_chord_tolerance(op: Operation, project: Project) -> float:
    """Cascade: explicit op override > project default."""
    if op.chord_tolerance is not None:
        return op.chord_tolerance
    return project.settings.chord_tolerance


def resolve_safe_height(op: Operation, project: Project) -> float:
    if op.safe_height is not None:
        return op.safe_height
    return project.settings.safe_height


def resolve_clearance(op: Operation, project: Project) -> float:
    if op.clearance_plane is not None:
        return op.clearance_plane
    return project.settings.clearance_plane


# ------------------------------------------------------------ pass planning


def z_levels(cut_depth: float, stepdown: float, multi_depth: bool) -> list[float]:
    """Descending pass Z levels from Z=0 down to ``cut_depth``.

    ``cut_depth`` is negative; returns ``[]`` for a zero / positive
    cut_depth (nothing to cut) and ``[cut_depth]`` when multi-depth is
    off. The last level snaps to ``cut_depth`` exactly so a sliver pass
    doesn't appear because of floating-point drift.
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


# ---------------------------------------------------------- chain geometry


def chain_is_ccw(segments: list[Segment]) -> bool:
    """True if the closed chain's shadow polygon winds CCW.

    Falls back to ``True`` if the discretisation fails — callers should
    only use this for closed chains where orientation is well-defined.
    """
    try:
        shadow = segments_to_shapely(
            segments, closed=True, tolerance=_ORIENTATION_TOLERANCE_MM
        )
    except ValueError:
        return True
    exterior = getattr(shadow, "exterior", None)
    if exterior is None:
        return True
    return bool(exterior.is_ccw)


def split_chain_at_length(
    segments: list[Segment], length: float
) -> tuple[list[Segment], list[Segment]]:
    """Split ``segments`` at arc-length ``length`` from start.

    ``length <= 0`` returns ``([], segments)``. A length past the chain
    end returns ``(segments, [])``. The seam segment is split via
    ``core.segments.split_segment_at_length``.
    """
    first: list[Segment] = []
    remaining = length
    for i, seg in enumerate(segments):
        if remaining <= LENGTH_EPSILON:
            return (first, list(segments[i:]))
        if remaining >= seg.length - LENGTH_EPSILON:
            first.append(seg)
            remaining -= seg.length
            continue
        seg_a, seg_b = split_segment_at_length(seg, remaining)
        first.append(seg_a)
        return (first, [seg_b, *segments[i + 1:]])
    return (first, [])


def walk_closed_chain(
    segments: list[Segment], start_offset: float, length: float
) -> list[Segment]:
    """Walk a closed chain from ``start_offset`` forward by ``length``.

    Treats the chain as a closed loop: wraps around as needed. Returns
    the traversed (sub-)segments in order. ``length`` can exceed the
    chain total — the loop repeats.
    """
    total = sum(s.length for s in segments)
    if total <= LENGTH_EPSILON or length <= LENGTH_EPSILON:
        return []
    start_offset %= total
    before, after = split_chain_at_length(segments, start_offset)
    loop = after + before
    full_loops = int(length // total)
    residual = length - full_loops * total
    walked: list[Segment] = []
    for _ in range(full_loops):
        walked.extend(loop)
    if residual > LENGTH_EPSILON:
        residual_part, _ = split_chain_at_length(loop, residual)
        walked.extend(residual_part)
    return walked


def rotate_closed_chain_to_nearest_point(
    segments: list[Segment], target: tuple[float, float]
) -> list[Segment]:
    """Rotate a closed chain so its start is the chain point nearest ``target``.

    Consumers (Profile / Pocket emit paths) use this to honour the user-
    chosen contour start position (P₀): instead of emitting the offsetter's
    arbitrary seed vertex, the chain is rotated so that lead-in, on-contour
    ramp, or SPIRAL entry lands at a deliberate location — typically in
    scrap material where witness marks are harmless.

    The chain is assumed closed (first segment's start == last segment's
    end, within discretisation tolerance). If the chain is open, the
    rotation still runs but produces a path that joins start-to-end with
    a seam not in the original data.
    """
    if not segments:
        return segments
    cumulative = 0.0
    best_dist_sq = math.inf
    best_offset = 0.0
    for seg in segments:
        if isinstance(seg, LineSegment):
            d_sq, local = _nearest_on_line(seg, target)
        else:
            d_sq, local = _nearest_on_arc(seg, target)
        if d_sq < best_dist_sq:
            best_dist_sq = d_sq
            best_offset = cumulative + local
        cumulative += seg.length
    if best_offset <= LENGTH_EPSILON:
        return segments
    before, after = split_chain_at_length(segments, best_offset)
    return after + before


def _nearest_on_line(
    seg: LineSegment, target: tuple[float, float]
) -> tuple[float, float]:
    """Closest point on a line segment to ``target``.

    Returns ``(distance_squared, length_along_segment)``. Uses squared
    distance to avoid a sqrt per comparison.
    """
    sx, sy = seg.start
    ex, ey = seg.end
    dx, dy = ex - sx, ey - sy
    length_sq = dx * dx + dy * dy
    tx, ty = target
    if length_sq < 1e-18:
        return (tx - sx) ** 2 + (ty - sy) ** 2, 0.0
    t = ((tx - sx) * dx + (ty - sy) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    nx, ny = sx + t * dx, sy + t * dy
    return (tx - nx) ** 2 + (ty - ny) ** 2, t * math.sqrt(length_sq)


def _nearest_on_arc(
    seg: ArcSegment, target: tuple[float, float]
) -> tuple[float, float]:
    """Closest point on an arc segment to ``target``.

    Returns ``(distance_squared, length_along_segment)``. Projects the
    target onto the circle and clamps to the arc's angular range; if the
    projection falls outside the sweep the nearest endpoint wins.
    """
    cx, cy = seg.center
    tx, ty = target
    dx, dy = tx - cx, ty - cy
    if dx * dx + dy * dy < 1e-18:
        # Target at the arc centre — every arc point equidistant, pick start.
        sx, sy = seg.start
        return (tx - sx) ** 2 + (ty - sy) ** 2, 0.0
    target_angle_deg = math.degrees(math.atan2(dy, dx))
    delta = target_angle_deg - seg.start_angle_deg
    # Wrap into (-180, 180] so the sign reflects "CCW from start".
    delta = (delta + 180.0) % 360.0 - 180.0
    # Bring into the arc's sweep half-plane before clamping.
    if seg.sweep_deg > 0 and delta < 0:
        delta += 360.0
    elif seg.sweep_deg < 0 and delta > 0:
        delta -= 360.0
    lo, hi = sorted((0.0, seg.sweep_deg))
    delta_clamped = max(lo, min(hi, delta))
    nearest_angle_deg = seg.start_angle_deg + delta_clamped
    rad = math.radians(nearest_angle_deg)
    nx, ny = cx + seg.radius * math.cos(rad), cy + seg.radius * math.sin(rad)
    dist_sq = (tx - nx) ** 2 + (ty - ny) ** 2
    length_along = abs(math.radians(delta_clamped)) * seg.radius
    return dist_sq, length_along


# ----------------------------------------------------------------- tangents


def unit_tangent_at_start(
    seg: Segment, *, error_cls: type[Exception] = EngineError
) -> tuple[float, float]:
    """Unit tangent at ``seg.start`` in the direction of travel."""
    if isinstance(seg, LineSegment):
        sx, sy = seg.start
        ex, ey = seg.end
        dx, dy = ex - sx, ey - sy
        length = math.hypot(dx, dy)
        if length == 0:
            raise error_cls("Zero-length segment has no tangent")
        return (dx / length, dy / length)
    theta = math.radians(seg.start_angle_deg)
    if seg.ccw:
        return (-math.sin(theta), math.cos(theta))
    return (math.sin(theta), -math.cos(theta))


def unit_tangent_at_end(
    seg: Segment, *, error_cls: type[Exception] = EngineError
) -> tuple[float, float]:
    """Unit tangent at ``seg.end`` in the direction of travel."""
    if isinstance(seg, LineSegment):
        return unit_tangent_at_start(seg, error_cls=error_cls)
    theta = math.radians(seg.end_angle_deg)
    if seg.ccw:
        return (-math.sin(theta), math.cos(theta))
    return (math.sin(theta), -math.cos(theta))


# -------------------------------------------------------------- IR emission


def emit_segment(
    instructions: list[IRInstruction],
    seg: Segment,
    feed_xy: float,
    *,
    error_cls: type[Exception] = EngineError,
) -> None:
    """Append one segment as a feed / arc-feed IR instruction.

    Full circles are split into two half-arcs first — G2/G3 with
    start XY == end XY is ambiguous on most controllers.
    """
    if isinstance(seg, ArcSegment) and seg.is_full_circle:
        first, second = split_full_circle(seg)
        emit_segment(instructions, first, feed_xy, error_cls=error_cls)
        emit_segment(instructions, second, feed_xy, error_cls=error_cls)
        return
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
    raise error_cls(f"Unknown segment type: {type(seg).__name__}")


def emit_ramp_segments(
    instructions: list[IRInstruction],
    segs: Sequence[Segment],
    *,
    z_start: float,
    z_end: float,
    feed_xy: float,
) -> None:
    """Append ``segs`` as feed / helical-arc moves with Z interpolated
    linearly by arc length.

    Z is ``z_start`` at ``segs[0].start`` and ``z_end`` at
    ``segs[-1].end``. Arcs become helical G2/G3 (X/Y/I/J plus Z on the
    same line). Empty or zero-length inputs are silently ignored.
    """
    total = sum(s.length for s in segs)
    if total <= LENGTH_EPSILON:
        return
    accum = 0.0
    for seg in segs:
        accum += seg.length
        z_here = z_start + (accum / total) * (z_end - z_start)
        if isinstance(seg, LineSegment):
            ex, ey = seg.end
            instructions.append(IRInstruction(
                type=MoveType.FEED, x=ex, y=ey, z=z_here, f=feed_xy,
            ))
        else:
            sx, sy = seg.start
            ex, ey = seg.end
            cx, cy = seg.center
            move_type = MoveType.ARC_CCW if seg.ccw else MoveType.ARC_CW
            instructions.append(IRInstruction(
                type=move_type, x=ex, y=ey, z=z_here,
                i=cx - sx, j=cy - sy, f=feed_xy,
            ))
