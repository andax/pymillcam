"""OFFSET pocket strategy — concentric inward rings.

The outermost ring sits one tool radius inside the boundary (cutter
edge flush with the wall). Each subsequent ring steps inward by
`stepover` until the offsetter returns empty. An adaptive last pass
closes a sliver when stepover doesn't divide wall thickness evenly,
and rest-machining (see `rest_machining.py`) cleans up V-notch
residuals.

Arcs are preserved when the analytical offsetter handles the shape;
otherwise Shapely's buffer is used (chord-based). Buffer fallback
applies unconditionally for boundary-with-islands because the
analytical offsetter doesn't take holes.
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon

from pymillcam.core.containment import build_pocket_regions
from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.operations import (
    MillingDirection,
    PocketOp,
    RampConfig,
    RampStrategy,
)
from pymillcam.core.segments import ArcSegment, Segment, segments_to_shapely
from pymillcam.core.tools import ToolController
from pymillcam.engine.common import (
    chain_is_ccw as _chain_is_ccw,
    emit_ramp_segments as _emit_ramp_segments,
    split_chain_at_length as _split_chain_at_length,
    unit_tangent_at_start as _common_unit_tangent_at_start,
)
from pymillcam.engine.ir import IRInstruction, MoveType

from ._shared import (
    PocketGenerationError,
    _apply_direction,
    _emit_ring_chain,
    _extract_polygons,
    _offset_boundary_inward,
    _polygon_to_ring_group,
    _ramp_stepdown,
    _rotate_rings_to_start_position,
)
from .rest_machining import _polygon_centerlines, _rest_machining_groups

# Split a helix into ≤180° arc chunks so single G2/G3 commands stay well
# inside the "<360°" envelope most controllers expect, and Z interpolation
# lands on meaningful waypoints.
_MAX_HELIX_CHUNK_DEG = 180.0


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


def _concentric_rings_with_islands(
    boundary: GeometryEntity,
    islands: list[GeometryEntity],
    tool_radius: float,
    stepover: float,
    direction: MillingDirection,
    chord_tolerance: float,
    rest_machining: bool = True,
) -> list[list[list[Segment]]]:
    """Build inward concentric rings from a boundary-with-holes.

    Returns a list of "ring groups". Each group is the rings produced by
    one buffer iteration of one connected machinable region — an exterior
    plus zero or more interiors. Within a group, the engine can transit
    between rings via feed-at-depth; between groups the engine retracts
    and rapids, because adjacent groups can be separated by uncut island
    material.

    Arc preservation isn't supported here (the analytical offsetter
    doesn't take holes); the buffer fallback discretises arcs at the
    op's chord_tolerance.

    When `rest_machining` is True, after the regular + adaptive passes
    we compute the uncut-but-cuttable residual area and emit one extra
    ring-group per reachable residual component. This cleans up V-notch
    corners where an island grows close to the boundary and the
    inward-offset iteration notches away from the corner tip.
    """
    if not boundary.segments or not boundary.closed:
        raise PocketGenerationError(
            "Pocket operation requires a closed boundary contour"
        )
    if tool_radius <= 0:
        raise PocketGenerationError(
            f"Tool radius must be positive, got {tool_radius}"
        )
    if stepover <= 0:
        raise PocketGenerationError(f"Stepover must be positive, got {stepover}")

    boundary_poly = segments_to_shapely(
        boundary.segments, closed=True, tolerance=chord_tolerance
    )
    if not isinstance(boundary_poly, Polygon):
        raise PocketGenerationError(
            f"Boundary must discretize to a Polygon; got {boundary_poly.geom_type}"
        )
    hole_rings: list[list[tuple[float, float]]] = []
    for island in islands:
        island_poly = segments_to_shapely(
            island.segments, closed=True, tolerance=chord_tolerance
        )
        if not isinstance(island_poly, Polygon):
            raise PocketGenerationError(
                f"Island must discretize to a Polygon; got {island_poly.geom_type}"
            )
        hole_rings.append([(c[0], c[1]) for c in island_poly.exterior.coords])
    machinable = Polygon(
        [(c[0], c[1]) for c in boundary_poly.exterior.coords],
        holes=hole_rings,
    )

    groups: list[list[list[Segment]]] = []
    # Track each emitted polygon's ring centerlines as Shapely LineStrings
    # so rest-machining can compute the swept area from the actual cutter
    # paths (exterior + interior rings around islands).
    centerlines: list[LineString] = []
    # When the eroded polygon pinches around an island, Shapely's mitre
    # buffer can return dozens of microscopic polygons (numerical noise)
    # alongside the legitimate result. Filter them out so they don't
    # become spurious ring-groups — and don't pollute rest-machining's
    # swept-area estimate, which would otherwise produce false residuals.
    # Threshold scales with chord_tolerance so it tracks discretization
    # error; the 100× factor is empirical, catching observed noise up to
    # ~0.005 mm² at default chord_tolerance=0.02.
    min_ring_area = max((chord_tolerance * 10) ** 2, 1e-4)
    distance = tool_radius
    safety_cap = 10_000
    for _ in range(safety_cap):
        offset = machinable.buffer(-distance, join_style="mitre")
        if offset.is_empty:
            polys: list[Polygon] = []
        else:
            # Buffer-of-polygon-with-holes can return a GeometryCollection
            # mixing Polygons with degenerate LineStrings/Points as the
            # polygon pinches off around an island. Walk recursively so we
            # don't stop iterating just because an intermediate result has
            # mixed types — there's still material to clear.
            polys = [
                p for p in _extract_polygons(offset)
                if p.area >= min_ring_area
            ]
        if not polys:
            # Adaptive last pass: when the next regular iteration is
            # empty, the previous ring may still be > tool_diameter from
            # the opposing wall's last ring (when stepover doesn't
            # divide the wall thickness evenly). Try one ring at
            # half-stepover past the last successful distance to close
            # the residual annulus. Skip if the resulting polygon is too
            # small to be a meaningful cut (avoids emitting microscopic
            # multi-polygon artefacts from Shapely's near-empty results).
            #
            # This handles annulus-shaped residuals (uniform wall
            # thickness). V-notch corners — where an island grows close
            # to the boundary — are handled by the rest-machining pass
            # below.
            half_d = distance - stepover / 2.0
            half_polys = _extract_polygons(
                machinable.buffer(-half_d, join_style="mitre")
            )
            min_area = stepover * stepover
            for poly in half_polys:
                if poly.area < min_area:
                    continue
                g = _polygon_to_ring_group(poly, direction)
                if g:
                    groups.append(g)
                    centerlines.extend(_polygon_centerlines(poly))
            break
        for poly in polys:
            g = _polygon_to_ring_group(poly, direction)
            if g:
                groups.append(g)
                centerlines.extend(_polygon_centerlines(poly))
        distance += stepover

    if rest_machining and centerlines:
        groups.extend(
            _rest_machining_groups(
                machinable, centerlines, tool_radius, direction
            )
        )
    return groups


def _compute_offset_rings(
    boundary: GeometryEntity,
    islands: list[GeometryEntity],
    *,
    tool_radius: float,
    stepover: float,
    direction: MillingDirection,
    chord_tolerance: float,
    rest_machining: bool,
    start_position: tuple[float, float] | None = None,
) -> list[list[list[Segment]]]:
    """Compute ring groups for one region. Island-free regions get a
    single flat group; regions with islands go through the buffer +
    rest-machining pipeline.

    ``start_position`` rotates every ring to begin at its nearest point
    to that target — only applied to island-free regions (with-islands
    emission relies on the offsetter's default seam for safe group-to-
    group bridges).
    """
    if islands:
        return _concentric_rings_with_islands(
            boundary, islands, tool_radius, stepover,
            direction, chord_tolerance,
            rest_machining=rest_machining,
        )
    rings = _concentric_rings(
        boundary, tool_radius, stepover, direction, chord_tolerance
    )
    rings = _rotate_rings_to_start_position(rings, start_position)
    return [rings] if rings else []


def compute_offset_preview(
    op: PocketOp,
    *,
    tool_radius: float,
    chord_tolerance: float,
    entities: list[GeometryEntity],
) -> list[Segment]:
    """Preview-path contribution for OFFSET across every region in the
    selected geometry."""
    preview: list[Segment] = []
    for boundary, islands in build_pocket_regions(entities):
        ring_groups = _compute_offset_rings(
            boundary, islands,
            tool_radius=tool_radius,
            stepover=op.stepover,
            direction=op.direction,
            chord_tolerance=chord_tolerance,
            rest_machining=op.rest_machining,
            start_position=op.start_position,
        )
        for group in ring_groups:
            for ring in group:
                preview.extend(ring)
    return preview


def emit_offset_region(
    instructions: list[IRInstruction],
    boundary: GeometryEntity,
    islands: list[GeometryEntity],
    *,
    op: PocketOp,
    tool_controller: ToolController,
    tool_radius: float,
    chord_tolerance: float,
    stepdown: float,
    z_levels: list[float],
    safe_height: float,
    clearance: float,
) -> None:
    """Emit IR for a single pocket region using OFFSET strategy.

    Raises PocketGenerationError when the tool is too large to produce
    any rings.
    """
    ring_groups = _compute_offset_rings(
        boundary, islands,
        tool_radius=tool_radius,
        stepover=op.stepover,
        direction=op.direction,
        chord_tolerance=chord_tolerance,
        rest_machining=op.rest_machining,
        start_position=op.start_position,
    )
    rings = [ring for group in ring_groups for ring in group]
    if not rings:
        raise PocketGenerationError(
            f"Pocket {op.name!r}: tool too large for the selected boundary "
            f"(no rings fit at stepover={op.stepover} mm, tool radius="
            f"{tool_radius} mm)."
        )
    resolved_ramp = _resolve_ramp_strategy(op.ramp, rings, stepdown)
    if islands:
        _emit_ring_groups(
            instructions, ring_groups,
            tool_controller=tool_controller,
            z_levels=z_levels,
            safe_height=safe_height,
            clearance=clearance,
            ramp_config=op.ramp,
            resolved_strategy=resolved_ramp,
        )
    else:
        _emit_rings(
            instructions, rings,
            tool_controller=tool_controller,
            z_levels=z_levels,
            safe_height=safe_height,
            clearance=clearance,
            ramp_config=op.ramp,
            resolved_strategy=resolved_ramp,
        )


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


def _emit_ring_groups(
    instructions: list[IRInstruction],
    ring_groups: list[list[list[Segment]]],
    *,
    tool_controller: ToolController,
    z_levels: list[float],
    safe_height: float,
    clearance: float,
    ramp_config: RampConfig,
    resolved_strategy: RampStrategy,
) -> None:
    """Emit ring groups for one or more Z passes.

    Within a group: feed-at-depth between rings (the no-island
    `_emit_ring_chain` behavior). Between groups: retract → rapid →
    plunge so the tool doesn't drag through uncut island material.

    Ramp entry uses the FIRST group's first ring (typically the
    outermost exterior of the first iteration).
    """
    if not ring_groups or not z_levels:
        return
    first_group = ring_groups[0]
    first_ring = first_group[0]

    ramp_length = (
        _linear_ramp_length(ramp_config, stepdown=_ramp_stepdown(z_levels))
        if resolved_strategy is RampStrategy.LINEAR
        else 0.0
    )
    entry_xy = _strategy_entry_xy(resolved_strategy, first_ring, ramp_length)

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

        # First group: descend with the resolved ramp strategy and cut.
        if resolved_strategy is RampStrategy.HELICAL and helix_plan is not None:
            _emit_helical_pass_body(
                instructions, first_group, plan=helix_plan,
                prev_z=prev_z, pass_z=z, tool_controller=tool_controller,
            )
        elif resolved_strategy is RampStrategy.LINEAR:
            _emit_linear_pass_body(
                instructions, first_group, ramp_length=ramp_length,
                prev_z=prev_z, pass_z=z, tool_controller=tool_controller,
            )
        else:
            _emit_plunge_pass_body(
                instructions, first_group, pass_z=z,
                tool_controller=tool_controller,
            )

        # Subsequent groups: retract → rapid to next group's first ring
        # start → plunge → cut. Safe across uncut island material.
        for group in ring_groups[1:]:
            group_start = group[0][0].start
            instructions.append(
                IRInstruction(type=MoveType.RAPID, z=clearance)
            )
            instructions.append(
                IRInstruction(
                    type=MoveType.RAPID, x=group_start[0], y=group_start[1]
                )
            )
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED, z=z, f=tool_controller.feed_z
                )
            )
            _emit_ring_chain(instructions, group, tool_controller.feed_xy)

        if not is_last:
            # Retract and reposition above first group's entry for next pass.
            instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))
            instructions.append(
                IRInstruction(
                    type=MoveType.RAPID, x=entry_xy[0], y=entry_xy[1]
                )
            )


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
    return _common_unit_tangent_at_start(seg, error_cls=PocketGenerationError)
