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
from pymillcam.core.offsetter import OffsetError, offset_closed_contour
from pymillcam.core.operations import (
    LeadConfig,
    LeadStyle,
    MillingDirection,
    OffsetSide,
    ProfileOp,
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

_LENGTH_EPSILON = 1e-9

DEFAULT_STEPDOWN_MM = 1.0


class ProfileGenerationError(Exception):
    """Raised when a ProfileOp cannot be converted into a toolpath."""


def compute_profile_preview(op: ProfileOp, project: Project) -> list[Segment]:
    """Return the 2D plan-view path the cutter centre will follow.

    Shows the offset contour plus lead-in / lead-out, with the lead-out
    anchored where the Z ascent actually ends (so the preview matches the
    G-code). Used by the UI to show a live preview as the user edits
    operation parameters.
    """
    tool_controller = _resolve_tool_controller(op, project)
    chord_tolerance = (
        op.chord_tolerance
        if op.chord_tolerance is not None
        else project.settings.chord_tolerance
    )
    tool_radius = float(tool_controller.tool.geometry["diameter"]) / 2.0
    stepdown = _resolve_stepdown(op, tool_controller)
    out: list[Segment] = []
    for ref in op.geometry_refs:
        entity = _resolve_entity(ref.layer_name, ref.entity_id, project)
        contour = _offset_contour(
            entity, tool_radius, op.offset_side, chord_tolerance, op.direction
        )
        lead_in = _build_lead_in(contour, op.lead_in, op.offset_side)
        anchor, tangent = _resolve_lead_out_anchor(
            contour, op.ramp, stepdown, op.cut_depth
        )
        lead_out = _build_lead_from_anchor(
            anchor=anchor,
            tangent=tangent,
            config=op.lead_out,
            side=op.offset_side,
            segments=contour,
            entering=False,
        )
        out.extend(lead_in)
        out.extend(contour)
        out.extend(lead_out)
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
    if project.settings.spindle_warmup_s > 0:
        instructions.append(
            IRInstruction(type=MoveType.DWELL, f=project.settings.spindle_warmup_s)
        )

    for ref in op.geometry_refs:
        entity = _resolve_entity(ref.layer_name, ref.entity_id, project)
        segments = _offset_contour(
            entity, tool_radius, op.offset_side, chord_tolerance, op.direction
        )
        _emit_contour_passes(
            instructions,
            segments,
            tool_controller=tool_controller,
            cut_depth=op.cut_depth,
            multi_depth=op.multi_depth,
            stepdown=stepdown,
            safe_height=safe_height,
            clearance=clearance,
            lead_in_config=op.lead_in,
            lead_out_config=op.lead_out,
            offset_side=op.offset_side,
            ramp_config=op.ramp,
        )

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
    direction: MillingDirection,
) -> list[Segment]:
    if not entity.segments:
        raise ProfileGenerationError(
            "Profile operation requires a contour entity; got a point-only entity"
        )

    if side is OffsetSide.ON_LINE or radius == 0:
        segments = list(entity.segments)
    else:
        if not entity.closed:
            raise ProfileGenerationError(
                "Inside/outside offset requires a closed contour; got an open segment chain. "
                "Use offset_side=ON_LINE for open contours."
            )
        # Try the analytical, arc-preserving offsetter first. It handles the
        # common cases (circles, line-only polygons, line+tangent-arc shapes)
        # without collapsing arcs to chords.
        try:
            segments = offset_closed_contour(
                list(entity.segments), radius, outside=side is OffsetSide.OUTSIDE
            )
        except OffsetError:
            # Cases the analytical offsetter punts on (non-tangent line↔arc
            # joins, self-intersection, etc.) fall back to Shapely's buffer.
            segments = _offset_contour_via_buffer(entity, radius, side, chord_tolerance)

    return _apply_milling_direction(segments, side, direction)


def _apply_milling_direction(
    segments: list[Segment], side: OffsetSide, direction: MillingDirection
) -> list[Segment]:
    """Reverse the chain when the chosen milling direction needs the opposite
    travel sense.

    Convention (right-handed CW spindle, looking down the Z axis), derived
    from the chip-thickness definition (climb = chip max at entry):
      - OUTSIDE + CLIMB        → travel CW around the part
      - OUTSIDE + CONVENTIONAL → travel CCW around the part   (offsetter default)
      - INSIDE  + CLIMB        → travel CCW around the hole   (offsetter default)
      - INSIDE  + CONVENTIONAL → travel CW around the hole
    `OffsetSide.ON_LINE` keeps the source contour direction — climb /
    conventional don't have a meaning when the cutter centre rides the line.
    """
    if side is OffsetSide.ON_LINE:
        return segments
    needs_reverse = (
        side is OffsetSide.OUTSIDE and direction is MillingDirection.CLIMB
    ) or (side is OffsetSide.INSIDE and direction is MillingDirection.CONVENTIONAL)
    return reverse_segment_chain(segments) if needs_reverse else segments


def _offset_contour_via_buffer(
    entity: GeometryEntity,
    radius: float,
    side: OffsetSide,
    chord_tolerance: float,
) -> list[Segment]:
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
    lead_in_config: LeadConfig,
    lead_out_config: LeadConfig,
    offset_side: OffsetSide,
    ramp_config: RampConfig,
) -> None:
    """Emit a profile's toolpath IR.

    Scheme: lead-in at Z=0 (surface). Each pass descends along the contour from
    P₀ to P₁ at the configured ramp angle (Z ramps from the previous depth to
    this pass's depth), then continues P₁→P₀ at constant pass depth — so each
    pass ends at P₀ ready for the next descent, no retract between passes.
    Lead-out retracts to Z=0 at P₀ and exits in air.

    When `ramp_config.strategy is PLUNGE`, or the contour isn't closed, or the
    required ramp distance exceeds the contour length, we fall back to a
    straight plunge at the contour start (no on-contour ramp).
    """
    if not segments:
        return
    z_levels = _z_levels(cut_depth, stepdown, multi_depth)
    if not z_levels:
        return

    lead_in_segs = _build_lead_in(segments, lead_in_config, offset_side)
    lead_out_segs = _build_lead_out(segments, lead_out_config, offset_side)

    start_xy_x, start_xy_y = (
        lead_in_segs[0].start if lead_in_segs else segments[0].start
    )
    contour_start_x, contour_start_y = segments[0].start
    contour_end_x, contour_end_y = segments[-1].end
    is_closed_like = math.hypot(
        contour_end_x - contour_start_x, contour_end_y - contour_start_y
    ) < 1e-6
    contour_length = sum(s.length for s in segments)

    use_ramp = _should_use_contour_ramp(
        ramp_config, is_closed_like, contour_length, z_levels[-1]
    )

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    instructions.append(IRInstruction(type=MoveType.RAPID, x=start_xy_x, y=start_xy_y))
    instructions.append(IRInstruction(type=MoveType.RAPID, z=clearance))

    # Surface-level descent needed before lead-in OR before on-contour ramp;
    # straight-plunge passes can go directly from clearance to pass depth.
    needs_surface_feed = bool(lead_in_segs) or use_ramp
    if needs_surface_feed:
        instructions.append(
            IRInstruction(type=MoveType.FEED, z=0.0, f=tool_controller.feed_z)
        )
    for seg in lead_in_segs:
        _emit_segment(instructions, seg, tool_controller.feed_xy)

    prev_z = 0.0
    per_pass_ramp_length = (
        stepdown / math.tan(math.radians(ramp_config.angle_deg)) if use_ramp else 0.0
    )
    for pass_index, z in enumerate(z_levels):
        if use_ramp:
            ramp_length = abs(z - prev_z) / math.tan(math.radians(ramp_config.angle_deg))
            descent_segs, rest_segs = _split_chain_at_length(segments, ramp_length)
            _emit_ramp_segments(
                instructions,
                descent_segs,
                z_start=prev_z,
                z_end=z,
                feed_xy=tool_controller.feed_xy,
            )
            for seg in rest_segs:
                _emit_segment(instructions, seg, tool_controller.feed_xy)
        else:
            instructions.append(
                IRInstruction(type=MoveType.FEED, z=z, f=tool_controller.feed_z)
            )
            for seg in segments:
                _emit_segment(instructions, seg, tool_controller.feed_xy)
        is_last = pass_index == len(z_levels) - 1
        # Open-contour fallback: hop back to contour start so the next pass
        # plunges at the right XY.
        if not is_closed_like and not is_last and not use_ramp:
            instructions.append(
                IRInstruction(
                    type=MoveType.FEED,
                    x=contour_start_x,
                    y=contour_start_y,
                    f=tool_controller.feed_xy,
                )
            )
        prev_z = z

    # After the last pass we're at P0 at cut_depth (closed ramp mode) or at
    # contour end at cut_depth (plunge mode). In ramp mode we still need to:
    #   - Cleanup: re-cut P0→P1 at cut_depth (the final descent left a slope).
    #   - Ascent: fixed-angle rise along the contour up to Z=0, possibly
    #     wrapping the contour multiple times.
    # Lead-out then runs at Z=0 from wherever the ascent ends.
    lead_out_anchor: tuple[float, float] | None = None
    lead_out_tangent: tuple[float, float] | None = None
    if use_ramp:
        cut_depth_final = z_levels[-1]
        # Cleanup pass at Z=cut_depth over the last descent's P0→P1 slice.
        descent_segs, _ = _split_chain_at_length(segments, per_pass_ramp_length)
        for seg in descent_segs:
            _emit_segment(instructions, seg, tool_controller.feed_xy)
        # Ascent: fixed-angle from cut_depth to Z=0, starting at P1.
        ascent_length = abs(cut_depth_final) / math.tan(
            math.radians(ramp_config.angle_deg)
        )
        ascent_segs = _walk_closed_chain(
            segments, start_offset=per_pass_ramp_length, length=ascent_length
        )
        _emit_ramp_segments(
            instructions,
            ascent_segs,
            z_start=cut_depth_final,
            z_end=0.0,
            feed_xy=tool_controller.feed_xy,
        )
        if ascent_segs:
            last_asc = ascent_segs[-1]
            lead_out_anchor = last_asc.end
            lead_out_tangent = _unit_tangent_at_end(last_asc)

    if lead_out_segs:
        # In ramp mode the ascent already brought the tool back to Z=0, and
        # the natural lead-out anchor is the ascent's endpoint (P2), not the
        # original contour end. Rebuild the lead-out from that anchor+tangent.
        if lead_out_anchor is not None and lead_out_tangent is not None:
            lead_out_segs = _build_lead_from_anchor(
                anchor=lead_out_anchor,
                tangent=lead_out_tangent,
                config=lead_out_config,
                side=offset_side,
                segments=segments,
                entering=False,
            )
        else:
            instructions.append(
                IRInstruction(type=MoveType.FEED, z=0.0, f=tool_controller.feed_z)
            )
        for seg in lead_out_segs:
            _emit_segment(instructions, seg, tool_controller.feed_xy)

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))


def _should_use_contour_ramp(
    ramp_config: RampConfig,
    is_closed_like: bool,
    contour_length: float,
    final_depth: float,
) -> bool:
    if ramp_config.strategy is RampStrategy.PLUNGE:
        return False
    if ramp_config.angle_deg <= 0:
        return False
    if not is_closed_like:
        return False
    # Require that the first-pass ramp fits on the contour. Uses the full
    # cut_depth as an upper bound — if the deepest single step fits, all
    # per-pass ramps fit too (they're smaller).
    max_ramp_len = abs(final_depth) / math.tan(math.radians(ramp_config.angle_deg))
    return max_ramp_len <= contour_length


def _resolve_lead_out_anchor(
    segments: list[Segment],
    ramp_config: RampConfig,
    stepdown: float,
    cut_depth: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return the (anchor, tangent) where the lead-out should attach.

    When the engine runs an on-contour ramp, the ascent ends past the contour
    start at distance (per_pass_ramp + ascent_length) along the chain; the
    lead-out attaches there. Otherwise the lead-out attaches at segments[-1].
    """
    contour_length = sum(s.length for s in segments)
    first = segments[0]
    end_x, end_y = segments[-1].end
    is_closed_like = math.hypot(
        end_x - first.start[0], end_y - first.start[1]
    ) < 1e-6
    if not _should_use_contour_ramp(
        ramp_config, is_closed_like, contour_length, cut_depth
    ):
        last = segments[-1]
        return last.end, _unit_tangent_at_end(last)
    angle = math.radians(ramp_config.angle_deg)
    per_pass_ramp_length = stepdown / math.tan(angle)
    ascent_length = abs(cut_depth) / math.tan(angle)
    ascent_segs = _walk_closed_chain(
        segments, start_offset=per_pass_ramp_length, length=ascent_length
    )
    if not ascent_segs:
        last = segments[-1]
        return last.end, _unit_tangent_at_end(last)
    last_ascent = ascent_segs[-1]
    return last_ascent.end, _unit_tangent_at_end(last_ascent)


def _emit_ramp_segments(
    instructions: list[IRInstruction],
    segs: list[Segment],
    *,
    z_start: float,
    z_end: float,
    feed_xy: float,
) -> None:
    """Emit `segs` as feed moves with Z interpolated linearly by arc length —
    z_start at segs[0].start and z_end at segs[-1].end."""
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
        else:  # ArcSegment — emit as helical arc
            sx, sy = seg.start
            ex, ey = seg.end
            cx, cy = seg.center
            move_type = MoveType.ARC_CCW if seg.ccw else MoveType.ARC_CW
            instructions.append(IRInstruction(
                type=move_type, x=ex, y=ey, z=z_here,
                i=cx - sx, j=cy - sy, f=feed_xy,
            ))


def _split_segment_at_length(
    seg: Segment, length: float
) -> tuple[Segment, Segment]:
    """Split seg into two at arc-length `length` from start. Caller must
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
    # ArcSegment: arc length = |sweep| * pi/180 * radius.
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
    """Split a chain at arc-length `length` from start. Returns (first_part,
    second_part)."""
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


def _walk_closed_chain(
    segments: list[Segment], start_offset: float, length: float
) -> list[Segment]:
    """Walk a closed-contour chain from `start_offset` forward by `length`,
    wrapping around as needed. Returns the traversed (sub-)segments in order.
    """
    total = sum(s.length for s in segments)
    if total <= _LENGTH_EPSILON or length <= _LENGTH_EPSILON:
        return []
    start_offset %= total
    before, after = _split_chain_at_length(segments, start_offset)
    loop = after + before  # One full loop of `segments`, rotated to start at `start_offset`.
    full_loops = int(length // total)
    residual = length - full_loops * total
    walked: list[Segment] = []
    for _ in range(full_loops):
        walked.extend(loop)
    if residual > _LENGTH_EPSILON:
        residual_part, _ = _split_chain_at_length(loop, residual)
        walked.extend(residual_part)
    return walked


def _build_lead_in(
    segments: list[Segment], config: LeadConfig, side: OffsetSide
) -> list[Segment]:
    """Return the segments feeding from off-part into the contour start.

    An empty list means "no lead" — plunge directly at the contour start.
    DIRECT style and zero-length configs collapse to that.
    """
    first = segments[0]
    return _build_lead_from_anchor(
        anchor=first.start,
        tangent=_unit_tangent_at_start(first),
        config=config,
        side=side,
        segments=segments,
        entering=True,
    )


def _build_lead_out(
    segments: list[Segment], config: LeadConfig, side: OffsetSide
) -> list[Segment]:
    """Return the segments feeding from the contour end out to an off-part
    point. Empty list means "no lead" — retract directly from contour end."""
    last = segments[-1]
    return _build_lead_from_anchor(
        anchor=last.end,
        tangent=_unit_tangent_at_end(last),
        config=config,
        side=side,
        segments=segments,
        entering=False,
    )


def _build_lead_from_anchor(
    *,
    anchor: tuple[float, float],
    tangent: tuple[float, float],
    config: LeadConfig,
    side: OffsetSide,
    segments: list[Segment],
    entering: bool,
) -> list[Segment]:
    """Build a lead-in or lead-out at an explicit anchor + travel tangent.

    For lead-in (entering=True), the lead ends AT anchor. For lead-out
    (entering=False), the lead starts AT anchor. The contour `segments` is
    passed so ARC leads can resolve the air-side normal from chain
    orientation.
    """
    if config.style is LeadStyle.DIRECT or config.length <= 0:
        return []
    tx, ty = tangent
    ax, ay = anchor
    if config.style is LeadStyle.TANGENT:
        if entering:
            return [
                LineSegment(
                    start=(ax - tx * config.length, ay - ty * config.length),
                    end=(ax, ay),
                )
            ]
        return [
            LineSegment(
                start=(ax, ay),
                end=(ax + tx * config.length, ay + ty * config.length),
            )
        ]
    if config.style is LeadStyle.ARC:
        return [_build_arc_lead(
            anchor=anchor,
            tangent=tangent,
            radius=_arc_radius_from_length(config.length),
            side=side,
            segments=segments,
            entering=entering,
        )]
    return []


def _arc_radius_from_length(length: float) -> float:
    """Quarter-arc length = π·r/2 → r = length·2/π."""
    return length * 2.0 / math.pi


def _build_arc_lead(
    *,
    anchor: tuple[float, float],
    tangent: tuple[float, float],
    radius: float,
    side: OffsetSide,
    segments: list[Segment],
    entering: bool,
) -> ArcSegment:
    """Build a 90° arc tangent to the contour at `anchor`, curving toward air.

    `entering`=True for lead-in (arc ends at anchor); False for lead-out (arc
    starts at anchor). The arc's travel direction matches `tangent` at the
    join so motion is smooth.
    """
    tx, ty = tangent
    nx, ny = _lead_air_normal(tangent, segments, side)
    ax, ay = anchor
    cx, cy = ax + radius * nx, ay + radius * ny
    theta_anchor = math.degrees(math.atan2(ay - cy, ax - cx))
    # CCW tangent direction at θ on circle around (cx, cy) is (-sin θ, cos θ).
    # Compare to the contour tangent at anchor to decide sweep direction.
    theta_rad = math.radians(theta_anchor)
    ccw_tangent = (-math.sin(theta_rad), math.cos(theta_rad))
    # If the CCW parameterisation of the circle matches the contour tangent
    # at anchor, the arc sweeps CCW; otherwise CW.
    matches_ccw = math.isclose(ccw_tangent[0], tx, abs_tol=1e-9) and math.isclose(
        ccw_tangent[1], ty, abs_tol=1e-9
    )
    sweep = 90.0 if matches_ccw else -90.0
    # For lead-in, the arc ends at anchor (θ_start = θ_anchor − sweep). For
    # lead-out, it starts at anchor (θ_start = θ_anchor).
    start_angle = theta_anchor - sweep if entering else theta_anchor
    return ArcSegment(
        center=(cx, cy),
        radius=radius,
        start_angle_deg=start_angle,
        sweep_deg=sweep,
    )


def _lead_air_normal(
    tangent: tuple[float, float],
    segments: list[Segment],
    side: OffsetSide,
) -> tuple[float, float]:
    """Unit normal perpendicular to `tangent`, pointing toward the air side
    of the cut. Returns the LEFT normal for ON_LINE (arbitrary but consistent).
    """
    tx, ty = tangent
    left = (-ty, tx)
    right = (ty, -tx)
    if side is OffsetSide.ON_LINE:
        return left
    ccw = _chain_is_ccw(segments)
    if side is OffsetSide.OUTSIDE:
        # CCW offset loop: part is on the left of travel, air is on the right.
        return right if ccw else left
    # INSIDE: CCW loop encloses the hole (air), so air is on the left.
    return left if ccw else right


def _chain_is_ccw(segments: list[Segment]) -> bool:
    """True if the closed chain's discretised polygon has CCW orientation.

    Falls back to True when discretisation fails — callers should only use
    this for closed chains where orientation is well-defined.
    """
    try:
        shadow = segments_to_shapely(segments, closed=True, tolerance=0.5)
    except ValueError:
        return True
    exterior = getattr(shadow, "exterior", None)
    if exterior is None:
        return True
    return bool(exterior.is_ccw)


def _unit_tangent_at_start(seg: Segment) -> tuple[float, float]:
    """Unit tangent vector at seg.start, pointing in the direction of travel."""
    if isinstance(seg, LineSegment):
        sx, sy = seg.start
        ex, ey = seg.end
        dx, dy = ex - sx, ey - sy
        length = math.hypot(dx, dy)
        if length == 0:
            raise ProfileGenerationError("Zero-length segment has no tangent")
        return (dx / length, dy / length)
    theta = math.radians(seg.start_angle_deg)
    if seg.ccw:
        return (-math.sin(theta), math.cos(theta))
    return (math.sin(theta), -math.cos(theta))


def _unit_tangent_at_end(seg: Segment) -> tuple[float, float]:
    """Unit tangent vector at seg.end, pointing in the direction of travel."""
    if isinstance(seg, LineSegment):
        # Straight-line tangent is constant along the segment.
        return _unit_tangent_at_start(seg)
    theta = math.radians(seg.end_angle_deg)
    if seg.ccw:
        return (-math.sin(theta), math.cos(theta))
    return (math.sin(theta), -math.cos(theta))


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
