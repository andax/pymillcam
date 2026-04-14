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

import math
from collections.abc import Sequence

from shapely.geometry import Point, Polygon

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.offsetter import OffsetError, offset_closed_contour
from pymillcam.core.operations import (
    MillingDirection,
    PocketOp,
    PocketStrategy,
    RampConfig,
    RampStrategy,
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

DEFAULT_STEPDOWN_MM = 1.0
_LENGTH_EPSILON = 1e-9
# Split a helix into ≤180° arc chunks so single G2/G3 commands stay well
# inside the "<360°" envelope most controllers expect, and Z interpolation
# lands on meaningful waypoints.
_MAX_HELIX_CHUNK_DEG = 180.0


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
    stepdown = _resolve_stepdown(op, tool_controller)
    z_levels = _z_levels(op.cut_depth, stepdown, op.multi_depth)

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
        resolved_ramp = _resolve_ramp_strategy(op.ramp, rings, stepdown)
        _emit_rings(
            instructions,
            rings,
            tool_controller=tool_controller,
            z_levels=z_levels,
            safe_height=safe_height,
            clearance=clearance,
            ramp_config=op.ramp,
            resolved_strategy=resolved_ramp,
        )

    instructions.append(IRInstruction(type=MoveType.SPINDLE_OFF))
    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    return toolpath


def _resolve_stepdown(op: PocketOp, tc: ToolController) -> float:
    """Resolve the pass stepdown with the same cascade profile uses:
    explicit op override > ToolController cutting_data > a sane default."""
    if op.stepdown is not None:
        return op.stepdown
    if tc.tool.cutting_data:
        return next(iter(tc.tool.cutting_data.values())).stepdown
    return DEFAULT_STEPDOWN_MM


def _z_levels(cut_depth: float, stepdown: float, multi_depth: bool) -> list[float]:
    """Step from Z=0 down to cut_depth. Mirrors `profile._z_levels`."""
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
    z_levels: list[float],
    safe_height: float,
    clearance: float,
    ramp_config: RampConfig,
    resolved_strategy: RampStrategy,
) -> None:
    """Emit rings for one or more Z passes, dispatching on ramp strategy.

    Each pass:
      - Positions above the first ring's start at safe height (first pass
        only — subsequent passes are already at clearance from the prior
        pass's retract).
      - Descends from Z=0 (first pass) or prev pass depth to this pass
        depth using the resolved ramp strategy (HELICAL / LINEAR / PLUNGE).
      - Cuts any remaining portion of the first ring, then all inner
        rings at this pass depth.
      - Retracts to clearance unless this is the last pass (caller adds
        the final safe-height retract).
    """
    if not rings or not z_levels:
        return
    first_ring = rings[0]

    # Entry XY varies by strategy: HELICAL / PLUNGE descend at first
    # ring start; LINEAR descends at the ramp start (one ramp_length
    # before first ring start along the contour).
    ramp_length = (
        _linear_ramp_length(ramp_config, stepdown=_ramp_stepdown(z_levels))
        if resolved_strategy is RampStrategy.LINEAR
        else 0.0
    )
    entry_xy = _strategy_entry_xy(
        resolved_strategy, first_ring, ramp_length
    )

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    instructions.append(
        IRInstruction(type=MoveType.RAPID, x=entry_xy[0], y=entry_xy[1])
    )
    instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))

    helix_plan: _HelixPlan | None = None
    if resolved_strategy is RampStrategy.HELICAL:
        helix_plan = _build_helix_plan(first_ring, ramp_config)

    for pass_index, z in enumerate(z_levels):
        is_last = pass_index == len(z_levels) - 1
        prev_z = 0.0 if pass_index == 0 else z_levels[pass_index - 1]

        if resolved_strategy is RampStrategy.HELICAL and helix_plan is not None:
            _emit_helical_pass_body(
                instructions,
                rings,
                plan=helix_plan,
                prev_z=prev_z,
                pass_z=z,
                tool_controller=tool_controller,
            )
        elif resolved_strategy is RampStrategy.LINEAR:
            _emit_linear_pass_body(
                instructions,
                rings,
                ramp_length=ramp_length,
                prev_z=prev_z,
                pass_z=z,
                tool_controller=tool_controller,
            )
        else:
            _emit_plunge_pass_body(
                instructions,
                rings,
                pass_z=z,
                tool_controller=tool_controller,
            )
        if not is_last:
            # Retract and reposition above entry_xy so the next pass
            # starts from the same (entry_xy, clearance) state.
            instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))
            instructions.append(
                IRInstruction(
                    type=MoveType.RAPID, x=entry_xy[0], y=entry_xy[1]
                )
            )


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


def _linear_ramp_length(ramp_config: RampConfig, stepdown: float) -> float:
    if ramp_config.angle_deg <= 0 or stepdown <= 0:
        return 0.0
    return stepdown / math.tan(math.radians(ramp_config.angle_deg))


def _strategy_entry_xy(
    strategy: RampStrategy,
    first_ring: list[Segment],
    ramp_length: float,
) -> tuple[float, float]:
    """Where the pre-pass rapids should position the tool for this
    strategy. LINEAR enters at the ramp start (ring_length - ramp_length
    arc before ring_start); others enter at ring_start itself.
    """
    first_start = first_ring[0].start
    if strategy is not RampStrategy.LINEAR or ramp_length <= 0:
        return first_start
    ring_length = sum(s.length for s in first_ring)
    if ramp_length >= ring_length:
        return first_start
    _, ramp_segs = _split_chain_at_length(first_ring, ring_length - ramp_length)
    return ramp_segs[0].start if ramp_segs else first_start


def _emit_plunge_pass_body(
    instructions: list[IRInstruction],
    rings: list[list[Segment]],
    *,
    pass_z: float,
    tool_controller: ToolController,
) -> None:
    """Pass body for PLUNGE — straight-down feed from clearance to
    `pass_z` at first_start, then cut all rings. Assumes the caller
    already positioned the tool at (first_start, clearance)."""
    instructions.append(
        IRInstruction(type=MoveType.FEED, z=pass_z, f=tool_controller.feed_z)
    )
    _emit_ring_chain(instructions, rings, tool_controller.feed_xy)


def _emit_linear_pass_body(
    instructions: list[IRInstruction],
    rings: list[list[Segment]],
    *,
    ramp_length: float,
    prev_z: float,
    pass_z: float,
    tool_controller: ToolController,
) -> None:
    """Pass body for LINEAR — descend tangent to the first ring such
    that the ramp ENDS at `first_ring[0].start`, then cut the full
    first ring at `pass_z` and all inner rings.

    The ramp occupies the last `ramp_length` arc of the closed first
    ring (the slice immediately "before" ring start in the traversal
    direction). After pass 1's full-ring cut, the ramp-start XY is
    already cleared to `pass_z`, so subsequent passes plunge in air
    there. No cleanup is needed — the ramp's sloped cut is overwritten
    by the same pass's full-ring cut (and deeper by later passes).
    """
    first_ring = rings[0]
    ring_length = sum(s.length for s in first_ring)
    if ramp_length >= ring_length:
        # Defensive: _resolve_ramp_strategy should have downgraded us to
        # PLUNGE, but if ramp still exceeds ring, emit a straight plunge
        # at first_start.
        _emit_plunge_pass_body(
            instructions, rings, pass_z=pass_z, tool_controller=tool_controller
        )
        return
    _, ramp_segs = _split_chain_at_length(first_ring, ring_length - ramp_length)
    # Caller positioned us at ramp_segs[0].start already; just feed Z.
    instructions.append(
        IRInstruction(type=MoveType.FEED, z=prev_z, f=tool_controller.feed_z)
    )
    _emit_ramp_segments(
        instructions,
        ramp_segs,
        z_start=prev_z,
        z_end=pass_z,
        feed_xy=tool_controller.feed_xy,
    )
    # Tool is now at first_ring[0].start at pass_z. Cut all rings.
    _emit_ring_chain(instructions, rings, tool_controller.feed_xy)


def _emit_helical_pass_body(
    instructions: list[IRInstruction],
    rings: list[list[Segment]],
    *,
    plan: _HelixPlan,
    prev_z: float,
    pass_z: float,
    tool_controller: ToolController,
) -> None:
    """Pass body for HELICAL — spiral down from `prev_z` to `pass_z`
    tangent to the first ring's start, then cut all rings at `pass_z`.
    The helix starts and ends at `first_ring[0].start`.
    """
    instructions.append(
        IRInstruction(type=MoveType.FEED, z=prev_z, f=tool_controller.feed_z)
    )
    descent = abs(pass_z - prev_z)
    helix = _build_helix_arcs(
        plan, total_sweep_deg=_helix_sweep_deg(plan, descent)
    )
    _emit_ramp_segments(
        instructions,
        helix,
        z_start=prev_z,
        z_end=pass_z,
        feed_xy=tool_controller.feed_xy,
    )
    _emit_ring_chain(instructions, rings, tool_controller.feed_xy)


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


# ---------------------------------------------------------------- ramp helpers


class _HelixPlan:
    """XY geometry for a helical-entry descent tangent to a ring's start.

    The per-pass sweep is recomputed at emit time from the actual descent
    (prev_z → pass_z), since the last pass is often clamped shorter than
    one full stepdown.
    """

    __slots__ = ("center", "radius", "theta_end_deg", "ccw",
                 "start_point", "angle_deg")

    def __init__(
        self,
        center: tuple[float, float],
        radius: float,
        theta_end_deg: float,
        ccw: bool,
        start_point: tuple[float, float],
        angle_deg: float,
    ) -> None:
        self.center = center
        self.radius = radius
        self.theta_end_deg = theta_end_deg
        self.ccw = ccw
        self.start_point = start_point
        self.angle_deg = angle_deg


def _resolve_ramp_strategy(
    ramp_config: RampConfig, rings: list[list[Segment]], stepdown: float
) -> RampStrategy:
    """Resolve the requested strategy to one that actually fits.

    Fallback chain: HELICAL → LINEAR → PLUNGE. The caller still passes
    `ramp_config` to the emitters so they can use its `radius` /
    `angle_deg` for the resolved strategy.
    """
    if not rings:
        return RampStrategy.PLUNGE
    first_ring = rings[0]
    requested = ramp_config.strategy
    if requested is RampStrategy.PLUNGE:
        return RampStrategy.PLUNGE
    if requested is RampStrategy.HELICAL:
        if _helix_fits(first_ring, ramp_config.radius):
            return RampStrategy.HELICAL
        # Fall through to LINEAR.
        requested = RampStrategy.LINEAR
    if requested is RampStrategy.LINEAR:
        if ramp_config.angle_deg <= 0:
            return RampStrategy.PLUNGE
        # Worst-case per-pass descent is `stepdown`; check the required
        # ramp length fits on the first ring.
        ramp_length = stepdown / math.tan(math.radians(ramp_config.angle_deg))
        first_ring_length = sum(s.length for s in first_ring)
        if ramp_length < first_ring_length:
            return RampStrategy.LINEAR
    return RampStrategy.PLUNGE


def _helix_fits(first_ring: list[Segment], helix_radius: float) -> bool:
    """True if a circle of `helix_radius` tangent to the ring at its
    start sits entirely within the ring's enclosed area.

    Uses a tight chord tolerance (0.01 mm) when discretising the ring
    plus a matching outward buffer to absorb chord-sag error — without
    this, a helix that touches the true ring boundary at a single point
    (the common case for circular pockets, where the helix is tangent
    to the wall at the ring's start) registers as "outside" because the
    polygonalised ring's edges sag slightly inward.
    """
    if helix_radius <= 0:
        return False
    tolerance = 0.01
    try:
        ring_poly = segments_to_shapely(
            first_ring, closed=True, tolerance=tolerance
        )
    except ValueError:
        return False
    if not isinstance(ring_poly, Polygon) or ring_poly.is_empty:
        return False
    start = first_ring[0].start
    tangent = _unit_tangent_at_start(first_ring[0])
    ccw = _chain_is_ccw(first_ring)
    normal = _inward_normal(tangent, ccw)
    center = (start[0] + helix_radius * normal[0], start[1] + helix_radius * normal[1])
    helix_disk = Point(center).buffer(helix_radius, quad_segs=64)
    return bool(ring_poly.buffer(tolerance).covers(helix_disk))


def _build_helix_plan(
    first_ring: list[Segment], ramp_config: RampConfig
) -> _HelixPlan:
    """Build the XY helix geometry tangent to the ring at its start.

    Assumes `_helix_fits` returned True — caller is responsible for the
    fit check via `_resolve_ramp_strategy`.
    """
    start = first_ring[0].start
    tangent = _unit_tangent_at_start(first_ring[0])
    ccw = _chain_is_ccw(first_ring)
    normal = _inward_normal(tangent, ccw)
    radius = ramp_config.radius
    center = (start[0] + radius * normal[0], start[1] + radius * normal[1])
    # Parameterise θ on the circle around `center`. `start` sits on the
    # circle at angle θ_end = atan2(-n_y, -n_x). The helix sweeps from
    # θ_start to θ_end, direction matching the ring.
    theta_end_deg = math.degrees(math.atan2(-normal[1], -normal[0]))
    return _HelixPlan(
        center=center,
        radius=radius,
        theta_end_deg=theta_end_deg,
        ccw=ccw,
        start_point=start,
        angle_deg=ramp_config.angle_deg,
    )


def _helix_sweep_deg(plan: _HelixPlan, descent: float) -> float:
    """Sweep in degrees needed to descend `descent` mm at `plan.angle_deg`
    or gentler. Rounds up to an integer number of turns so the helix
    starts and ends at the same physical point (the ring start) — a
    partial-turn helix would leave the tool displaced by a fraction of
    the helix radius when it's time to cut the ring.
    """
    if descent <= 0 or plan.radius <= 0 or plan.angle_deg <= 0:
        return 0.0
    descent_per_turn = (
        2.0 * math.pi * plan.radius * math.tan(math.radians(plan.angle_deg))
    )
    if descent_per_turn <= 0:
        return 0.0
    turns = max(1, math.ceil(descent / descent_per_turn))
    return turns * 360.0


def _build_helix_arcs(
    plan: _HelixPlan, total_sweep_deg: float
) -> list[ArcSegment]:
    """Build a list of ≤180° arc segments forming a helix that ends at
    `plan.start_point`. The arcs have no Z information — Z is applied
    at emit time by `_emit_ramp_segments`.
    """
    if total_sweep_deg <= 0:
        return []
    sign = 1.0 if plan.ccw else -1.0
    # θ_start = θ_end − (signed total sweep)
    total_signed = sign * total_sweep_deg
    theta_start = plan.theta_end_deg - total_signed
    arcs: list[ArcSegment] = []
    remaining = total_sweep_deg
    theta_cursor = theta_start
    while remaining > 0:
        chunk = min(remaining, _MAX_HELIX_CHUNK_DEG)
        chunk_signed = sign * chunk
        arcs.append(
            ArcSegment(
                center=plan.center,
                radius=plan.radius,
                start_angle_deg=theta_cursor,
                sweep_deg=chunk_signed,
            )
        )
        theta_cursor += chunk_signed
        remaining -= chunk
    return arcs


def _inward_normal(
    tangent: tuple[float, float], ccw: bool
) -> tuple[float, float]:
    """Unit normal to `tangent` pointing toward the interior of a ring
    with the given orientation.

    CCW ring (enclosed area on the left of travel): inward = left perp.
    CW ring  (enclosed area on the right of travel): inward = right perp.
    """
    tx, ty = tangent
    if ccw:
        return (-ty, tx)
    return (ty, -tx)


def _unit_tangent_at_start(seg: Segment) -> tuple[float, float]:
    """Unit tangent at seg.start pointing in the direction of travel."""
    if isinstance(seg, LineSegment):
        sx, sy = seg.start
        ex, ey = seg.end
        dx, dy = ex - sx, ey - sy
        length = math.hypot(dx, dy)
        if length == 0:
            raise PocketGenerationError("Zero-length segment has no tangent")
        return (dx / length, dy / length)
    theta = math.radians(seg.start_angle_deg)
    if seg.ccw:
        return (-math.sin(theta), math.cos(theta))
    return (math.sin(theta), -math.cos(theta))


def _split_segment_at_length(
    seg: Segment, length: float
) -> tuple[Segment, Segment]:
    """Split `seg` in two at arc-length `length` from start. Caller must
    ensure 0 < length < seg.length."""
    if isinstance(seg, LineSegment):
        sx, sy = seg.start
        ex, ey = seg.end
        t = length / seg.length
        mx = sx + t * (ex - sx)
        my = sy + t * (ey - sy)
        return (
            LineSegment(start=(sx, sy), end=(mx, my)),
            LineSegment(start=(mx, my), end=(ex, ey)),
        )
    sweep_used_deg = math.degrees(length / seg.radius) * math.copysign(
        1, seg.sweep_deg
    )
    remaining_deg = seg.sweep_deg - sweep_used_deg
    first = ArcSegment(
        center=seg.center,
        radius=seg.radius,
        start_angle_deg=seg.start_angle_deg,
        sweep_deg=sweep_used_deg,
    )
    second = ArcSegment(
        center=seg.center,
        radius=seg.radius,
        start_angle_deg=seg.start_angle_deg + sweep_used_deg,
        sweep_deg=remaining_deg,
    )
    return first, second


def _split_chain_at_length(
    segments: list[Segment], length: float
) -> tuple[list[Segment], list[Segment]]:
    """Split a chain at arc-length `length` from start."""
    first: list[Segment] = []
    remaining = length
    for i, seg in enumerate(segments):
        if remaining <= _LENGTH_EPSILON:
            return (first, list(segments[i:]))
        if remaining >= seg.length - _LENGTH_EPSILON:
            first.append(seg)
            remaining -= seg.length
            continue
        seg_a, seg_b = _split_segment_at_length(seg, remaining)
        first.append(seg_a)
        return (first, [seg_b, *segments[i + 1:]])
    return (first, [])


def _emit_ramp_segments(
    instructions: list[IRInstruction],
    segs: Sequence[Segment],
    *,
    z_start: float,
    z_end: float,
    feed_xy: float,
) -> None:
    """Emit `segs` as feed moves with Z interpolated linearly by arc
    length — z_start at segs[0].start, z_end at segs[-1].end."""
    total = sum(s.length for s in segs)
    if total <= _LENGTH_EPSILON:
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
