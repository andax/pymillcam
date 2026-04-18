"""ZIGZAG pocket strategy — parallel raster strokes + finishing ring.

Strokes are horizontal in a coordinate frame rotated by `angle_deg`
CCW from world +X, clipped against the machinable polygon (entity
boundary buffered inward by tool radius, islands subtracted). A
finishing contour pass around the wall follows so it isn't scalloped.

The finishing ring arc-preserves the wall; raster strokes are
line-only. When a scan line crosses an island it splits into multiple
strokes; the connector between disjoint pieces retracts + rapids +
plunges rather than feeding through the island.
"""
from __future__ import annotations

import math

from shapely.affinity import rotate as shapely_rotate
from shapely.geometry import LineString, MultiLineString, Point, Polygon

from pymillcam.core.containment import build_pocket_regions
from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.operations import (
    MillingDirection,
    PocketOp,
    RampConfig,
    RampStrategy,
)
from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    Segment,
    reverse_segment_chain,
    segments_to_shapely,
)
from pymillcam.core.tools import ToolController
from pymillcam.engine.common import (
    LENGTH_EPSILON as _LENGTH_EPSILON,
    emit_ramp_segments as _emit_ramp_segments,
    split_chain_at_length as _split_chain_at_length,
)
from pymillcam.engine.ir import IRInstruction, MoveType

from ._shared import (
    PocketGenerationError,
    _apply_direction,
    _coords_to_line_chain,
    _emit_ring_chain,
    _emit_segment,
    _offset_boundary_inward,
    _ramp_stepdown,
)


def _zigzag_strokes_and_finishing_ring(
    entity: GeometryEntity,
    tool_radius: float,
    stepover: float,
    direction: MillingDirection,
    angle_deg: float,
    chord_tolerance: float,
    *,
    islands: list[GeometryEntity] | None = None,
) -> tuple[list[list[Segment]], list[list[Segment]], Polygon | None]:
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
    is flush with the island wall).

    Returns ``(strokes, finishing_rings, machinable)`` — ``machinable``
    is the clipping polygon (with island holes already subtracted) so
    the emitter can test whether each inter-stroke connector stays
    inside safe territory; ``None`` when the tool is too large to fit.
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
        return [], [], None
    finishing_rings = [_apply_direction(offset_segments, direction)]

    machinable = segments_to_shapely(
        offset_segments, closed=True, tolerance=chord_tolerance
    )
    if not isinstance(machinable, Polygon) or machinable.is_empty:
        return [], finishing_rings, None

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
        return [], finishing_rings, None
    strokes = _generate_zigzag_strokes(machinable, stepover, angle_deg)
    return strokes, finishing_rings, machinable


def compute_zigzag_preview(
    op: PocketOp,
    *,
    tool_radius: float,
    chord_tolerance: float,
    entities: list[GeometryEntity],
) -> list[Segment]:
    """Preview-path contribution for ZIGZAG across every region in the
    selected geometry."""
    preview: list[Segment] = []
    for boundary, islands in build_pocket_regions(entities):
        strokes, finishing_rings, _machinable = (
            _zigzag_strokes_and_finishing_ring(
                boundary, tool_radius, op.stepover, op.direction,
                op.angle_deg, chord_tolerance, islands=islands,
            )
        )
        for stroke in strokes:
            preview.extend(stroke)
        for ring in finishing_rings:
            preview.extend(ring)
    return preview


def emit_zigzag_region(
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
    """Emit IR for a single pocket region using ZIGZAG strategy.

    Raises PocketGenerationError when the tool is too large to fit.
    """
    strokes, finishing_rings, machinable = (
        _zigzag_strokes_and_finishing_ring(
            boundary, tool_radius, op.stepover, op.direction,
            op.angle_deg, chord_tolerance, islands=islands,
        )
    )
    if not strokes and not finishing_rings:
        raise PocketGenerationError(
            f"Pocket {op.name!r}: tool too large for the selected "
            f"boundary (no zigzag strokes fit at stepover="
            f"{op.stepover} mm, tool radius={tool_radius} mm)."
        )
    resolved_ramp = _resolve_zigzag_ramp_strategy(op.ramp, strokes, stepdown)
    _emit_zigzag(
        instructions,
        strokes=strokes,
        finishing_rings=finishing_rings,
        machinable=machinable,
        tool_controller=tool_controller,
        z_levels=z_levels,
        safe_height=safe_height,
        clearance=clearance,
        ramp_config=op.ramp,
        resolved_strategy=resolved_ramp,
    )


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
    machinable: Polygon | None,
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
                    machinable=machinable,
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
                    machinable=machinable,
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
    machinable: Polygon | None,
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
            machinable=machinable,
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
        machinable=machinable,
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
    machinable: Polygon | None,
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
        machinable=machinable,
        pass_z=pass_z,
        clearance=clearance,
        feed_xy=tool_controller.feed_xy,
        feed_z=tool_controller.feed_z,
    )


def _zigzag_connector_safe(
    a: tuple[float, float],
    b: tuple[float, float],
    machinable: Polygon | None,
) -> bool:
    """True if a straight feed from ``a`` to ``b`` stays inside the
    machinable polygon — i.e. the connector doesn't cross an island.

    Approach: sample the connector's midpoint against ``machinable``.
    For typical zigzag geometry the connector is short (one stepover)
    and both endpoints sit on the machinable boundary, so a midpoint
    test is sufficient:

    * Normal U-turn between adjacent scan rows — midpoint is ~½
      stepover off the boundary, well inside ``machinable``. Safe.
    * Multi-region connector across an island — midpoint lands inside
      the subtracted island region, which is NOT part of
      ``machinable``. Unsafe.

    Returns True when ``machinable`` is None (no safety data available
    — preserves the pre-fix behaviour rather than producing spurious
    retracts; islandless pockets don't compute machinable for rapids).
    Two coincident points are trivially safe.
    """
    if machinable is None:
        return True
    if a == b:
        return True
    midpoint = Point((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    # Distance-based test rather than topological containment. Reasons:
    #   - Normal U-turn midpoint sits on the machinable boundary;
    #     `contains` rejects it (strict interior only).
    #   - Finishing-ring transit jump between the polygonal shadow of
    #     machinable (chord-approximated) and the analytical-arc ring
    #     start can land ~chord_tolerance outside the shadow even
    #     though the connector is safe — `covers` rejects that too.
    #   - Island-crossing midpoint lands deep inside a hole (distance
    #     ~½ island-width >> any realistic chord tolerance), which is
    #     exactly the case we need to flag.
    # The 0.1 mm slack threshold absorbs both boundary-incidence and
    # chord-approximation drift while still rejecting any midpoint that
    # actually enters an island. An island at least one tool-diameter
    # across has midpoint distance ≥ half diameter, an order of
    # magnitude past this threshold.
    return float(machinable.distance(midpoint)) < _CONNECTOR_SAFE_SLACK_MM


# Slack on the midpoint-inside-machinable test. Larger than any
# realistic chord_tolerance (~0.02 mm default) so polygonal shadow
# approximation doesn't cause spurious retracts; smaller by an order
# of magnitude than the smallest plausible island width so real
# island-crossings still trigger.
_CONNECTOR_SAFE_SLACK_MM = 0.1


def _emit_zigzag_remainder(
    instructions: list[IRInstruction],
    *,
    current_xy: tuple[float, float],
    remaining_strokes: list[list[Segment]],
    finishing_rings: list[list[Segment]],
    machinable: Polygon | None,
    pass_z: float,
    clearance: float,
    feed_xy: float,
    feed_z: float,
) -> None:
    """Emit strokes after the first, then trace each finishing ring.

    Between successive strokes:

    * Normal zigzag — the connector is a short U-turn along the stepover,
      entirely inside the machinable polygon. Emit as a feed at cut depth.
    * Multi-region — when a scan line is clipped into multiple disjoint
      pieces by an island, the connector from one piece's end to the next
      piece's start crosses the island. Detect by testing the connector's
      midpoint against ``machinable``: if the midpoint isn't inside, the
      line leaves safe territory. Substitute retract → rapid → plunge so
      the tool goes over the island rather than through it.

    The BOUNDARY finishing ring (index 0) connects via feed-at-depth
    from the last stroke (same safety check applies); ISLAND rings
    (index 1+) are always reached via retract → rapid → plunge.
    """
    tool_xy = current_xy
    for stroke in remaining_strokes:
        start = stroke[0].start
        if _zigzag_connector_safe(tool_xy, start, machinable):
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED, x=start[0], y=start[1], f=feed_xy
                )
            )
        else:
            # Connector would cross an island — lift out, rapid across,
            # plunge back down. Same pattern as an inter-island ring
            # transition below.
            instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))
            instructions.append(
                IRInstruction(type=MoveType.RAPID, x=start[0], y=start[1])
            )
            instructions.append(
                IRInstruction(type=MoveType.FEED, z=pass_z, f=feed_z)
            )
        for seg in stroke:
            _emit_segment(instructions, seg, feed_xy)
        tool_xy = stroke[-1].end
    for ring_index, ring in enumerate(finishing_rings):
        if not ring:
            continue
        rotated = _rotate_ring_start_to_nearest(ring, tool_xy)
        ring_start = rotated[0].start
        if ring_index == 0 and _zigzag_connector_safe(
            tool_xy, ring_start, machinable
        ):
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
