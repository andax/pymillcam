"""Pocket toolpath generator.

Consumes a PocketOp + Project, walks the selected closed boundary, and
emits IR that clears the interior area.

Strategies:

- **OFFSET** — concentric inward rings. The outermost ring sits one tool
  radius inside the boundary (cutter edge flush with the wall), and each
  subsequent ring steps inward by `stepover` until the region closes up.
  Arcs are preserved when the analytical offsetter handles the shape;
  otherwise Shapely's buffer is used (chord-based).
- **ZIGZAG** — parallel raster strokes inside the machinable polygon
  (boundary buffered inward by tool radius), alternating direction and
  followed by a single finishing contour pass around the wall so it
  isn't scalloped. Stroke direction is rotated by `angle_deg`. The
  finishing pass arc-preserves the wall; raster strokes are line-only.

What this does not yet cover:
- Islands / holes in the boundary.
- SPIRAL strategy.
- Multi-region zigzag clips (a stroke that enters/exits the polygon
  more than once) — we emit each piece as a separate stroke but the
  connector between disjoint pieces is a feed at cut depth, not a
  retract. Fine for mildly concave pockets; unsafe for shapes with
  island-like narrowings.
- HELICAL ramp entry on ZIGZAG (falls back to LINEAR, then PLUNGE —
  helical needs a different entry layout than a straight stroke, and
  "helix-plunge in interior then raster out" is a future enhancement).
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from shapely.affinity import rotate as shapely_rotate
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
)

from pymillcam.core.containment import build_pocket_regions
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
    split_full_circle,
    split_segment_at_length,
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

    For OFFSET, concatenates every concentric ring. For ZIGZAG, emits the
    raster strokes followed by the finishing contour ring. Used by the
    UI to show a live preview as the user edits operation parameters.
    """
    tool_controller = _resolve_tool_controller(op, project)
    chord_tolerance = (
        op.chord_tolerance
        if op.chord_tolerance is not None
        else project.settings.chord_tolerance
    )
    tool_radius = float(tool_controller.tool.geometry["diameter"]) / 2.0
    entities = [
        _resolve_entity(ref.layer_name, ref.entity_id, project)
        for ref in op.geometry_refs
    ]
    preview: list[Segment] = []
    for boundary, islands in build_pocket_regions(entities):
        if op.strategy is PocketStrategy.ZIGZAG:
            strokes, finishing_rings = _zigzag_strokes_and_finishing_ring(
                boundary, tool_radius, op.stepover, op.direction,
                op.angle_deg, chord_tolerance, islands=islands,
            )
            for stroke in strokes:
                preview.extend(stroke)
            for ring in finishing_rings:
                preview.extend(ring)
        elif islands:
            ring_groups = _concentric_rings_with_islands(
                boundary, islands, tool_radius, op.stepover,
                op.direction, chord_tolerance,
            )
            for group in ring_groups:
                for ring in group:
                    preview.extend(ring)
        else:
            rings = _concentric_rings(
                boundary, tool_radius, op.stepover, op.direction,
                chord_tolerance,
            )
            for ring in rings:
                preview.extend(ring)
    return preview


def generate_pocket_toolpath(op: PocketOp, project: Project) -> Toolpath:
    """Generate an IR Toolpath for a single PocketOp within the given Project."""
    if op.strategy is PocketStrategy.SPIRAL:
        raise PocketGenerationError(
            f"Pocket strategy {op.strategy.value!r} is not implemented yet "
            "— only 'offset' and 'zigzag' are available."
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
    if project.settings.spindle_warmup_s > 0:
        instructions.append(
            IRInstruction(
                type=MoveType.DWELL, f=project.settings.spindle_warmup_s
            )
        )

    entities = [
        _resolve_entity(ref.layer_name, ref.entity_id, project)
        for ref in op.geometry_refs
    ]
    regions = build_pocket_regions(entities)
    if not regions:
        raise PocketGenerationError(
            f"Pocket {op.name!r}: no closed boundary in the selected geometry."
        )
    for boundary, islands in regions:
        if op.strategy is PocketStrategy.ZIGZAG:
            strokes, finishing_rings = _zigzag_strokes_and_finishing_ring(
                boundary, tool_radius, op.stepover, op.direction,
                op.angle_deg, chord_tolerance, islands=islands,
            )
            if not strokes and not finishing_rings:
                raise PocketGenerationError(
                    f"Pocket {op.name!r}: tool too large for the selected "
                    f"boundary (no zigzag strokes fit at stepover="
                    f"{op.stepover} mm, tool radius={tool_radius} mm)."
                )
            resolved_ramp = _resolve_zigzag_ramp_strategy(
                op.ramp, strokes, stepdown
            )
            _emit_zigzag(
                instructions,
                strokes=strokes,
                finishing_rings=finishing_rings,
                tool_controller=tool_controller,
                z_levels=z_levels,
                safe_height=safe_height,
                clearance=clearance,
                ramp_config=op.ramp,
                resolved_strategy=resolved_ramp,
            )
            continue
        if islands:
            ring_groups = _concentric_rings_with_islands(
                boundary, islands, tool_radius, op.stepover,
                op.direction, chord_tolerance,
            )
            rings = [ring for group in ring_groups for ring in group]
            if not rings:
                raise PocketGenerationError(
                    f"Pocket {op.name!r}: tool too large for the selected boundary "
                    f"(no rings fit at stepover={op.stepover} mm, tool radius="
                    f"{tool_radius} mm)."
                )
            resolved_ramp = _resolve_ramp_strategy(op.ramp, rings, stepdown)
            _emit_ring_groups(
                instructions,
                ring_groups,
                tool_controller=tool_controller,
                z_levels=z_levels,
                safe_height=safe_height,
                clearance=clearance,
                ramp_config=op.ramp,
                resolved_strategy=resolved_ramp,
            )
            continue
        rings = _concentric_rings(
            boundary, tool_radius, op.stepover, op.direction, chord_tolerance
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


def _concentric_rings_with_islands(
    boundary: GeometryEntity,
    islands: list[GeometryEntity],
    tool_radius: float,
    stepover: float,
    direction: MillingDirection,
    chord_tolerance: float,
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
            polys = _extract_polygons(offset)
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
            # NOTE: this only helps with annulus-shaped residuals (uniform
            # wall thickness). It does NOT clean up V-notch corners where
            # an island reaches close to the boundary — those need
            # rest-machining (medial axis or residual-area cleanup),
            # which isn't implemented yet. Documented in CLAUDE.md.
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
            break
        for poly in polys:
            g = _polygon_to_ring_group(poly, direction)
            if g:
                groups.append(g)
        distance += stepover
    return groups


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
        seg_a, seg_b = split_segment_at_length(seg, remaining)
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
    if isinstance(seg, ArcSegment) and seg.is_full_circle:
        first, second = split_full_circle(seg)
        _emit_segment(instructions, first, feed_xy)
        _emit_segment(instructions, second, feed_xy)
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
    raise PocketGenerationError(f"Unknown segment type: {type(seg).__name__}")


# --------------------------------------------------------------- zigzag engine


def _zigzag_strokes_and_finishing_ring(
    entity: GeometryEntity,
    tool_radius: float,
    stepover: float,
    direction: MillingDirection,
    angle_deg: float,
    chord_tolerance: float,
    *,
    islands: list[GeometryEntity] | None = None,
) -> tuple[list[list[Segment]], list[list[Segment]]]:
    """Generate zigzag raster strokes plus per-wall finishing rings.

    Strokes are horizontal in a coordinate frame rotated by `angle_deg`
    CCW from world +X, spaced by `stepover` from the machinable
    polygon's rotated-bbox bottom upward. Each scan line is clipped
    against the machinable polygon (entity boundary buffered inward by
    `tool_radius`, with each island buffered outward by tool_radius and
    subtracted). A single LineString clip becomes one stroke; a
    MultiLineString clip (e.g., a scan line crossing an island) becomes
    several pieces ordered by X. Strokes alternate direction row-by-row
    for true zigzag.

    Finishing rings: one for the boundary (arc-preserved when the
    analytical offsetter handles the shape) and one for each island
    (the island contour offset OUTWARD by tool_radius so the cutter edge
    is flush with the island wall). Returns an empty
    `(strokes, finishing_rings)` pair when the tool is too large to fit.

    Known limitation: connectors between disjoint pieces of the same
    scan-line stroke remain feed-at-depth; with islands, those connectors
    can cross the island. Use OFFSET for islanded pockets until this
    safety fix lands.
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
    islands = islands or []

    # Boundary finishing ring: arc-preserved where possible.
    offset_segments = _offset_boundary_inward(
        entity, tool_radius, chord_tolerance
    )
    if offset_segments is None:
        return [], []
    finishing_rings = [_apply_direction(offset_segments, direction)]

    machinable = segments_to_shapely(
        offset_segments, closed=True, tolerance=chord_tolerance
    )
    if not isinstance(machinable, Polygon) or machinable.is_empty:
        return [], finishing_rings

    # Subtract each island (dilated by tool_radius) from the machinable
    # polygon and emit a finishing ring per island.
    for island in islands:
        island_poly = segments_to_shapely(
            island.segments, closed=True, tolerance=chord_tolerance
        )
        if not isinstance(island_poly, Polygon):
            continue
        machinable = machinable.difference(island_poly.buffer(tool_radius))
        # Island wall finishing ring: island contour offset OUTWARD by
        # tool_radius (cutter edge flush with the island). Use Shapely
        # buffer — the analytical outward-offsetter is for boundaries.
        outward = island_poly.buffer(tool_radius, join_style="mitre")
        if isinstance(outward, Polygon) and not outward.is_empty:
            ring_segs = _coords_to_line_chain(
                [(c[0], c[1]) for c in outward.exterior.coords]
            )
            if ring_segs:
                # Islands are obstacles — finishing direction flips so
                # CLIMB still corresponds to the cutter chip-thickness
                # convention against this wall.
                flip_direction = (
                    MillingDirection.CONVENTIONAL
                    if direction is MillingDirection.CLIMB
                    else MillingDirection.CLIMB
                )
                finishing_rings.append(
                    _apply_direction(ring_segs, flip_direction)
                )

    if not isinstance(machinable, Polygon) or machinable.is_empty:
        return [], finishing_rings
    strokes = _generate_zigzag_strokes(machinable, stepover, angle_deg)
    return strokes, finishing_rings


def _generate_zigzag_strokes(
    machinable: Polygon, stepover: float, angle_deg: float
) -> list[list[Segment]]:
    """Rotate `machinable` by -angle_deg so raster runs along X, generate
    clipped scan lines at stepover spacing, alternate direction, then
    rotate stroke endpoints back to world coordinates.
    """
    rotated = (
        shapely_rotate(machinable, -angle_deg, origin=(0.0, 0.0))
        if angle_deg != 0.0
        else machinable
    )
    minx, miny, maxx, maxy = rotated.bounds
    height = maxy - miny
    if height <= _LENGTH_EPSILON or (maxx - minx) <= _LENGTH_EPSILON:
        return []
    # n intervals of `stepover` or less, spanning miny..maxy exactly.
    # Going evenly to maxy (rather than stopping short of it) ensures
    # the row nearest the far wall is placed with the cutter center
    # against the boundary — the wall itself is handled by the finishing
    # pass, but this minimizes the scallop that remains for it to clean.
    n = max(1, math.ceil(height / stepover))
    ys = [miny + i * (height / n) for i in range(n + 1)]

    # Pad the scan line past the polygon bounds so horizontal clipping
    # is robust at the extreme Y rows (where the polygon may touch the
    # bbox at a single point).
    pad = max(1.0, (maxx - minx) * 0.01)
    strokes: list[list[Segment]] = []
    for row_index, y in enumerate(ys):
        scan = LineString([(minx - pad, y), (maxx + pad, y)])
        clip = rotated.intersection(scan)
        pieces = _extract_linestring_pieces(clip)
        if not pieces:
            continue
        pieces.sort(key=lambda ls: ls.coords[0][0])
        reverse_row = row_index % 2 == 1
        if reverse_row:
            pieces = list(reversed(pieces))
        for piece in pieces:
            coords = list(piece.coords)
            if len(coords) < 2:
                continue
            if reverse_row:
                start_r, end_r = coords[-1], coords[0]
            else:
                start_r, end_r = coords[0], coords[-1]
            # Un-rotate back to world frame. Shapely .coords yields
            # variable-arity tuples (with optional Z) — narrow to XY.
            start = _rotate_point((start_r[0], start_r[1]), angle_deg)
            end = _rotate_point((end_r[0], end_r[1]), angle_deg)
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            if math.hypot(dx, dy) <= _LENGTH_EPSILON:
                continue
            strokes.append([LineSegment(start=start, end=end)])
    return strokes


def _extract_linestring_pieces(geom: object) -> list[LineString]:
    """Flatten a polygon-line intersection into non-empty LineStrings."""
    if isinstance(geom, LineString):
        return [geom] if not geom.is_empty else []
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if isinstance(g, LineString) and not g.is_empty]
    if hasattr(geom, "geoms"):  # GeometryCollection
        out: list[LineString] = []
        for g in geom.geoms:
            out.extend(_extract_linestring_pieces(g))
        return out
    return []


def _rotate_point(
    point: tuple[float, float], angle_deg: float
) -> tuple[float, float]:
    if angle_deg == 0.0:
        return (point[0], point[1])
    theta = math.radians(angle_deg)
    c = math.cos(theta)
    s = math.sin(theta)
    x, y = point
    return (x * c - y * s, x * s + y * c)


def _resolve_zigzag_ramp_strategy(
    ramp_config: RampConfig,
    strokes: list[list[Segment]],
    stepdown: float,
) -> RampStrategy:
    """Resolve the requested ramp for a zigzag entry. Chain is HELICAL →
    LINEAR → PLUNGE. HELICAL isn't supported on zigzag yet (requires a
    different entry layout); it falls through to LINEAR.

    LINEAR is accepted whenever the first stroke has positive length —
    the emitter clamps the ramp to the stroke (using a steeper effective
    angle if the configured one needs more length than the stroke
    provides). This matters for circle pockets, where the boundary-
    tangent stroke is always short; rejecting LINEAR on stroke-length
    alone would force PLUNGE in the common case.
    """
    if not strokes or not strokes[0]:
        return RampStrategy.PLUNGE
    requested = ramp_config.strategy
    if requested is RampStrategy.PLUNGE:
        return RampStrategy.PLUNGE
    if requested is RampStrategy.LINEAR or requested is RampStrategy.HELICAL:
        if ramp_config.angle_deg <= 0:
            return RampStrategy.PLUNGE
        first_stroke_length = sum(s.length for s in strokes[0])
        if first_stroke_length <= _LENGTH_EPSILON:
            return RampStrategy.PLUNGE
        # Cap back-and-forth legs: past the cap the geometry is too
        # cramped for the requested angle to be meaningful and we'd
        # emit an absurd number of legs — prefer PLUNGE.
        n_legs = _zigzag_n_legs(strokes[0], ramp_config, stepdown)
        if n_legs <= _ZIGZAG_MAX_RAMP_LEGS:
            return RampStrategy.LINEAR
    return RampStrategy.PLUNGE


def _emit_zigzag(
    instructions: list[IRInstruction],
    *,
    strokes: list[list[Segment]],
    finishing_rings: list[list[Segment]],
    tool_controller: ToolController,
    z_levels: list[float],
    safe_height: float,
    clearance: float,
    ramp_config: RampConfig,
    resolved_strategy: RampStrategy,
) -> None:
    """Emit zigzag strokes + finishing contours for one or more Z passes.

    Mirrors `_emit_rings`' lifecycle: rapid to safe height, rapid to
    entry XY, rapid down to clearance, then per pass ramp down → strokes
    → finishing rings (boundary first, then each island wall, with
    retract+rapid+plunge between disjoint rings) → (if not last) retract
    to clearance + reposition to entry XY.
    """
    if not z_levels or (not strokes and not finishing_rings):
        return

    # For LINEAR zigzag we precompute the number of ramp "legs" needed
    # to reach the per-pass descent at the configured angle. If one leg
    # of stroke 1 is long enough (common on rectangles), n_legs = 1 and
    # we emit a partial ramp over the first `ramp_length`. If the
    # stroke is shorter than `ramp_length` (common on circle pockets at
    # the boundary-tangent rows), we oscillate back-and-forth along the
    # full stroke for n_legs full-stroke passes, each descending
    # `D / n_legs`. Parity of n_legs picks the starting end so the last
    # leg always terminates at stroke_end (B) — stroke 2 then continues
    # normally. This keeps entry XY consistent across all passes.
    n_legs = 1
    if resolved_strategy is RampStrategy.LINEAR and strokes:
        n_legs = _zigzag_n_legs(
            strokes[0], ramp_config, _ramp_stepdown(z_levels)
        )

    if not strokes:
        entry_xy = finishing_rings[0][0].start
    elif resolved_strategy is RampStrategy.LINEAR:
        entry_xy = _zigzag_linear_entry_xy(strokes[0], n_legs)
    else:
        entry_xy = strokes[0][0].start

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    instructions.append(
        IRInstruction(type=MoveType.RAPID, x=entry_xy[0], y=entry_xy[1])
    )
    instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))

    for pass_index, z in enumerate(z_levels):
        is_last = pass_index == len(z_levels) - 1
        prev_z = 0.0 if pass_index == 0 else z_levels[pass_index - 1]

        if strokes:
            if resolved_strategy is RampStrategy.LINEAR:
                _emit_zigzag_linear_pass_body(
                    instructions,
                    strokes=strokes,
                    finishing_rings=finishing_rings,
                    ramp_config=ramp_config,
                    n_legs=n_legs,
                    prev_z=prev_z,
                    pass_z=z,
                    tool_controller=tool_controller,
                    clearance=clearance,
                )
            else:
                _emit_zigzag_plunge_pass_body(
                    instructions,
                    strokes=strokes,
                    finishing_rings=finishing_rings,
                    pass_z=z,
                    tool_controller=tool_controller,
                    clearance=clearance,
                )
        else:
            # No strokes — just finishing rings. Plunge for the first;
            # subsequent rings retract+rapid+plunge between.
            for ring_index, ring in enumerate(finishing_rings):
                if ring_index > 0:
                    ring_start = ring[0].start
                    instructions.append(
                        IRInstruction(type=MoveType.RAPID, z=clearance)
                    )
                    instructions.append(
                        IRInstruction(
                            type=MoveType.RAPID,
                            x=ring_start[0],
                            y=ring_start[1],
                        )
                    )
                instructions.append(
                    IRInstruction(
                        type=MoveType.FEED, z=z, f=tool_controller.feed_z
                    )
                )
                _emit_ring_chain(
                    instructions, [ring], tool_controller.feed_xy
                )

        if not is_last:
            instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))
            instructions.append(
                IRInstruction(
                    type=MoveType.RAPID, x=entry_xy[0], y=entry_xy[1]
                )
            )


# Cap on back-and-forth legs — above this, the configured angle is so
# fine-grained versus the first stroke that back-and-forth becomes
# absurd; we fall back to PLUNGE (via `_resolve_zigzag_ramp_strategy`).
_ZIGZAG_MAX_RAMP_LEGS = 10


def _zigzag_n_legs(
    first_stroke: list[Segment], ramp_config: RampConfig, stepdown: float
) -> int:
    """Number of full-stroke legs needed to descend `stepdown` at no
    steeper than the configured angle. Returns 1 if one partial leg
    fits; ≥2 when the stroke is shorter than `ramp_length`.
    """
    if ramp_config.angle_deg <= 0 or stepdown <= 0:
        return 1
    stroke_length = sum(s.length for s in first_stroke)
    if stroke_length <= _LENGTH_EPSILON:
        return 1
    ramp_length = stepdown / math.tan(math.radians(ramp_config.angle_deg))
    return max(1, math.ceil(ramp_length / stroke_length))


def _zigzag_linear_entry_xy(
    first_stroke: list[Segment], n_legs: int
) -> tuple[float, float]:
    """Entry XY for a LINEAR zigzag ramp.

    For n_legs == 1 (partial ramp on a long stroke): entry at
    stroke_start (A) — the ramp descends along the first `ramp_length`
    of stroke 1 and the rest of the stroke runs at pass_z ending at B.

    For n_legs ≥ 2 (back-and-forth): we want the n descending legs to
    finish at stroke_start (A) so a final cleanup leg can run A→B at
    pass_z and land on B for a natural transition to stroke 2.
    Parity of n_legs picks the entry end:

    - n even → start at A, legs alternate and end at A after n legs.
    - n odd  → start at B, legs alternate and end at A after n legs.
    """
    if n_legs <= 1:
        return first_stroke[0].start
    if n_legs % 2 == 0:
        return first_stroke[0].start
    return first_stroke[-1].end


def _emit_zigzag_linear_pass_body(
    instructions: list[IRInstruction],
    *,
    strokes: list[list[Segment]],
    finishing_rings: list[list[Segment]],
    ramp_config: RampConfig,
    n_legs: int,
    prev_z: float,
    pass_z: float,
    tool_controller: ToolController,
    clearance: float,
) -> None:
    """Pass body for LINEAR zigzag.

    Two shapes depending on `n_legs` (precomputed once at the pocket
    level so entry XY stays consistent across passes):

    - **n_legs == 1** — the stroke is at least as long as the configured
      `ramp_length`. Emit one partial-stroke ramp from stroke_start
      (prev_z) to the ramp-end point (pass_z), then continue the rest
      of stroke 1 at pass_z.
    - **n_legs ≥ 2** — the stroke is shorter than `ramp_length`. Oscillate
      back-and-forth along the full stroke for `n_legs` legs, each
      descending `descent / n_legs`. Parity picks the starting end (see
      `_zigzag_linear_entry_xy`) so the last leg ends at stroke_end (B).

    After the ramp, the remaining strokes (stroke 2…) and the finishing
    ring are emitted at pass_z. On the final pocket floor, a residual
    slope remains along stroke 1:

    - n_legs=1: slope ≈ configured angle over `ramp_length`.
    - n_legs≥2: slope = configured_angle / n_legs over the full stroke,
      with depth exact at stroke_end and residual `descent / n_legs`
      above pass_z at stroke_start.

    Same tradeoff as profile's LINEAR ramp — the sloped entry is in
    scrap most of the time (users can move stroke start by rotating
    `angle_deg` when they care).
    """
    first_stroke = strokes[0]
    stroke_length = sum(s.length for s in first_stroke)
    descent = abs(pass_z - prev_z)
    if (
        ramp_config.angle_deg <= 0
        or descent <= 0
        or stroke_length <= _LENGTH_EPSILON
    ):
        _emit_zigzag_plunge_pass_body(
            instructions,
            strokes=strokes,
            finishing_rings=finishing_rings,
            pass_z=pass_z,
            tool_controller=tool_controller,
            clearance=clearance,
        )
        return

    instructions.append(
        IRInstruction(type=MoveType.FEED, z=prev_z, f=tool_controller.feed_z)
    )

    if n_legs <= 1:
        configured_ramp_length = descent / math.tan(
            math.radians(ramp_config.angle_deg)
        )
        ramp_length = min(configured_ramp_length, stroke_length)
        if ramp_length >= stroke_length - _LENGTH_EPSILON:
            _emit_ramp_segments(
                instructions, first_stroke,
                z_start=prev_z, z_end=pass_z,
                feed_xy=tool_controller.feed_xy,
            )
        else:
            ramp_segs, rest = _split_chain_at_length(
                first_stroke, ramp_length
            )
            _emit_ramp_segments(
                instructions, ramp_segs,
                z_start=prev_z, z_end=pass_z,
                feed_xy=tool_controller.feed_xy,
            )
            for seg in rest:
                _emit_segment(instructions, seg, tool_controller.feed_xy)
    else:
        # Back-and-forth oscillation, followed by one cleanup leg at
        # pass_z. Entry XY (set at `_emit_zigzag` level) is A for even
        # n_legs and B for odd n_legs, so the n descending legs finish
        # at A at pass_z; the final forward leg then runs A→B at
        # pass_z, overwriting every intermediate-Z cut the descent left
        # behind. End result: stroke 1's floor is flat at pass_z across
        # its whole length, and the tool lands at stroke_end for
        # stroke 2 to continue naturally.
        forward = first_stroke
        backward = reverse_segment_chain(first_stroke)
        start_at_stroke_start = n_legs % 2 == 0
        first_leg = forward if start_at_stroke_start else backward
        second_leg = backward if start_at_stroke_start else forward
        leg_start_z = prev_z
        for i in range(n_legs):
            leg_chain = first_leg if i % 2 == 0 else second_leg
            # Clamp the final descending leg to pass_z exactly so
            # cumulative floating-point drift doesn't leave the tool
            # slightly above or below.
            leg_end_z = (
                pass_z if i == n_legs - 1
                else prev_z + (pass_z - prev_z) * (i + 1) / n_legs
            )
            _emit_ramp_segments(
                instructions, leg_chain,
                z_start=leg_start_z, z_end=leg_end_z,
                feed_xy=tool_controller.feed_xy,
            )
            leg_start_z = leg_end_z
        # Cleanup: A → B at pass_z. Tool ends at stroke_end.
        for seg in forward:
            _emit_segment(instructions, seg, tool_controller.feed_xy)

    _emit_zigzag_remainder(
        instructions,
        current_xy=strokes[0][-1].end,
        remaining_strokes=strokes[1:],
        finishing_rings=finishing_rings,
        pass_z=pass_z,
        clearance=clearance,
        feed_xy=tool_controller.feed_xy,
        feed_z=tool_controller.feed_z,
    )


def _emit_zigzag_plunge_pass_body(
    instructions: list[IRInstruction],
    *,
    strokes: list[list[Segment]],
    finishing_rings: list[list[Segment]],
    pass_z: float,
    tool_controller: ToolController,
    clearance: float,
) -> None:
    """Pass body for PLUNGE zigzag — feed straight down to pass_z at
    stroke 1's start, then emit all strokes and the finishing rings."""
    instructions.append(
        IRInstruction(type=MoveType.FEED, z=pass_z, f=tool_controller.feed_z)
    )
    for seg in strokes[0]:
        _emit_segment(instructions, seg, tool_controller.feed_xy)
    _emit_zigzag_remainder(
        instructions,
        current_xy=strokes[0][-1].end,
        remaining_strokes=strokes[1:],
        finishing_rings=finishing_rings,
        pass_z=pass_z,
        clearance=clearance,
        feed_xy=tool_controller.feed_xy,
        feed_z=tool_controller.feed_z,
    )


def _emit_zigzag_remainder(
    instructions: list[IRInstruction],
    *,
    current_xy: tuple[float, float],
    remaining_strokes: list[list[Segment]],
    finishing_rings: list[list[Segment]],
    pass_z: float,
    clearance: float,
    feed_xy: float,
    feed_z: float,
) -> None:
    """Emit strokes after the first (each connected to the previous by a
    feed move — true zigzag), then trace each finishing ring. The
    BOUNDARY ring (index 0) connects via feed-at-depth from the last
    stroke; ISLAND rings (index 1+) are reached via retract → rapid →
    plunge so the tool doesn't drag through uncut island material.
    Each ring is rotated so its traversal starts near the previous
    end to keep the transit short.
    """
    tool_xy = current_xy
    for stroke in remaining_strokes:
        start = stroke[0].start
        instructions.append(
            IRInstruction(
                type=MoveType.FEED, x=start[0], y=start[1], f=feed_xy
            )
        )
        for seg in stroke:
            _emit_segment(instructions, seg, feed_xy)
        tool_xy = stroke[-1].end
    for ring_index, ring in enumerate(finishing_rings):
        if not ring:
            continue
        rotated = _rotate_ring_start_to_nearest(ring, tool_xy)
        ring_start = rotated[0].start
        if ring_index == 0:
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED,
                    x=ring_start[0], y=ring_start[1], f=feed_xy,
                )
            )
        else:
            instructions.append(
                IRInstruction(type=MoveType.RAPID, z=clearance)
            )
            instructions.append(
                IRInstruction(
                    type=MoveType.RAPID, x=ring_start[0], y=ring_start[1]
                )
            )
            instructions.append(
                IRInstruction(type=MoveType.FEED, z=pass_z, f=feed_z)
            )
        for seg in rotated:
            _emit_segment(instructions, seg, feed_xy)
        tool_xy = rotated[-1].end


def _rotate_ring_start_to_nearest(
    ring: list[Segment], target_xy: tuple[float, float]
) -> list[Segment]:
    """Return a ring whose traversal starts at the point closest to
    `target_xy`. Preserves direction (CW stays CW) and segment count.

    - Full-circle single-arc ring: rebuild the arc with `start_angle_deg`
      set to the angle from center to target. The arc still sweeps 360°
      so it returns to the same physical point.
    - Polyline / mixed ring: rotate the segment list so the nearest
      vertex comes first. Snapping to the nearest vertex (not the
      truly closest point on an edge) keeps segments intact and is
      good enough — vertices are dense on buffered/chord-sampled
      rings and scarce rings (rectangles) already align their
      vertices with zigzag stroke endpoints.
    """
    if not ring:
        return ring
    tx, ty = target_xy
    if (
        len(ring) == 1
        and isinstance(ring[0], ArcSegment)
        and ring[0].is_full_circle
    ):
        arc = ring[0]
        cx, cy = arc.center
        dx, dy = tx - cx, ty - cy
        if math.hypot(dx, dy) < _LENGTH_EPSILON:
            return ring
        target_angle = math.degrees(math.atan2(dy, dx))
        return [ArcSegment(
            center=arc.center,
            radius=arc.radius,
            start_angle_deg=target_angle,
            sweep_deg=arc.sweep_deg,
        )]
    best_i = 0
    best_d2 = float("inf")
    for i, seg in enumerate(ring):
        vx, vy = seg.start
        d2 = (vx - tx) ** 2 + (vy - ty) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_i = i
    if best_i == 0:
        return ring
    return list(ring[best_i:]) + list(ring[:best_i])
