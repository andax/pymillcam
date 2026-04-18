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
    TabConfig,
)
from pymillcam.core.project import Project
from pymillcam.core.segments import (
    ArcSegment,
    LineSegment,
    Segment,
    reverse_segment_chain,
    segments_to_shapely,
    split_full_circle,
)
from pymillcam.core.tools import ToolController
from pymillcam.engine.common import (
    LENGTH_EPSILON as _LENGTH_EPSILON,
    EngineError,
    chain_is_ccw as _chain_is_ccw,
    emit_ramp_segments as _emit_ramp_segments,
    emit_segment as _common_emit_segment,
    resolve_entity as _common_resolve_entity,
    resolve_stepdown as _resolve_stepdown,
    resolve_tool_controller as _common_resolve_tool_controller,
    split_chain_at_length as _split_chain_at_length,
    unit_tangent_at_end as _common_unit_tangent_at_end,
    unit_tangent_at_start as _common_unit_tangent_at_start,
    walk_closed_chain as _walk_closed_chain,
    z_levels as _z_levels,
)
from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath
from pymillcam.engine.tabs import (
    TabPlacementError,
    compute_tab_intervals,
    effective_z_at,
    emit_pass_with_tabs,
    split_chain_at_lengths,
)


class ProfileGenerationError(EngineError):
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
            tabs=op.tabs,
        )

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    return toolpath


def _resolve_tool_controller(op: ProfileOp, project: Project) -> ToolController:
    return _common_resolve_tool_controller(
        op, project, error_cls=ProfileGenerationError
    )


def _resolve_entity(
    layer_name: str, entity_id: str, project: Project
) -> GeometryEntity:
    return _common_resolve_entity(
        layer_name, entity_id, project, error_cls=ProfileGenerationError
    )


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
    tabs: TabConfig,
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

    Tabs (when enabled) modulate Z to ride over each tab's plateau. They
    coexist with the on-contour ramp via `effective_z(s) = max(planned_z(s),
    tab_z(s))` — a tab in the descent slice lifts the tool to tab_top
    where the descent would otherwise cut through it. Same applies to the
    cleanup pass and the ascent.
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

    tab_intervals: list[tuple[float, float]] = []
    tab_top_z = 0.0
    if tabs.enabled:
        if not is_closed_like:
            raise ProfileGenerationError(
                "Tabs require a closed contour; tabs only make sense when the "
                "part would otherwise come free on the final pass."
            )
        if tabs.height >= abs(cut_depth):
            raise ProfileGenerationError(
                f"Tab height ({tabs.height} mm) must be less than cut depth "
                f"({abs(cut_depth)} mm) — tab top would sit above the stock surface."
            )
        try:
            tab_intervals = compute_tab_intervals(
                contour_length, tabs.count, tabs.width, tabs.ramp_length
            )
        except TabPlacementError as exc:
            raise ProfileGenerationError(str(exc)) from exc
        tab_top_z = cut_depth + tabs.height

    use_ramp = _should_use_contour_ramp(
        ramp_config, is_closed_like, contour_length, z_levels[-1]
    )

    # Pre-split the contour at every tab boundary so each piece is
    # entirely inside or outside any ramp/plateau region. Per-pass code
    # then computes z at piece ends without losing tab transitions.
    if tabs.enabled:
        tab_cuts: list[float] = []
        for s_start, s_end in tab_intervals:
            tab_cuts.extend(
                [s_start, s_start + tabs.ramp_length,
                 s_end - tabs.ramp_length, s_end]
            )
        tab_split_segments = split_chain_at_lengths(segments, tab_cuts)
    else:
        tab_split_segments = list(segments)

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
        if use_ramp and tabs.enabled:
            descent_length = abs(z - prev_z) / math.tan(
                math.radians(ramp_config.angle_deg)
            )
            _emit_ramp_pass_with_tabs(
                instructions,
                tab_split_segments,
                prev_z=prev_z,
                pass_z=z,
                descent_length=descent_length,
                tab_top_z=tab_top_z,
                tab_intervals=tab_intervals,
                tab_ramp_length=tabs.ramp_length,
                feed_xy=tool_controller.feed_xy,
            )
        elif use_ramp:
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
        elif tabs.enabled and z < tab_top_z:
            emit_pass_with_tabs(
                instructions,
                segments,
                pass_z=z,
                tab_top_z=tab_top_z,
                intervals=tab_intervals,
                ramp_length=tabs.ramp_length,
                feed_xy=tool_controller.feed_xy,
                feed_z=tool_controller.feed_z,
            )
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
        ascent_length = abs(cut_depth_final) / math.tan(
            math.radians(ramp_config.angle_deg)
        )
        if tabs.enabled:
            _emit_cleanup_with_tabs(
                instructions,
                tab_split_segments,
                descent_length=per_pass_ramp_length,
                cut_depth=cut_depth_final,
                tab_top_z=tab_top_z,
                tab_intervals=tab_intervals,
                tab_ramp_length=tabs.ramp_length,
                feed_xy=tool_controller.feed_xy,
            )
            ascent_segs = _walk_closed_chain(
                tab_split_segments,
                start_offset=per_pass_ramp_length,
                length=ascent_length,
            )
            _emit_ascent_with_tabs(
                instructions,
                ascent_segs,
                ascent_length=ascent_length,
                cut_depth=cut_depth_final,
                ascent_start_offset=per_pass_ramp_length,
                contour_length=contour_length,
                tab_top_z=tab_top_z,
                tab_intervals=tab_intervals,
                tab_ramp_length=tabs.ramp_length,
                feed_xy=tool_controller.feed_xy,
            )
        else:
            # Cleanup pass at Z=cut_depth over the last descent's P0→P1 slice.
            descent_segs, _ = _split_chain_at_length(segments, per_pass_ramp_length)
            for seg in descent_segs:
                _emit_segment(instructions, seg, tool_controller.feed_xy)
            # Ascent: fixed-angle from cut_depth to Z=0, starting at P1.
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


def _emit_ramp_pass_with_tabs(
    instructions: list[IRInstruction],
    pre_split: list[Segment],
    *,
    prev_z: float,
    pass_z: float,
    descent_length: float,
    tab_top_z: float,
    tab_intervals: list[tuple[float, float]],
    tab_ramp_length: float,
    feed_xy: float,
) -> None:
    """Walk a tab-aware pre-split chain emitting one pass.

    Z at arc-length s is `max(descent_z(s), tab_z(s))` — the descent
    ramp linearly interpolates prev_z → pass_z over [0, descent_length],
    then sits at pass_z; tabs lift to tab_top_z over their plateaus and
    smoothly transition over the entry/exit ramps.
    """
    accum = 0.0
    for piece in pre_split:
        accum += piece.length
        descent_z = _descent_z_at(
            accum, prev_z=prev_z, pass_z=pass_z, descent_length=descent_length
        )
        tab_z = effective_z_at(
            accum,
            pass_z=pass_z,
            tab_top_z=tab_top_z,
            intervals=tab_intervals,
            ramp_length=tab_ramp_length,
        )
        _emit_piece_with_z(instructions, piece, max(descent_z, tab_z), feed_xy)


def _emit_cleanup_with_tabs(
    instructions: list[IRInstruction],
    pre_split: list[Segment],
    *,
    descent_length: float,
    cut_depth: float,
    tab_top_z: float,
    tab_intervals: list[tuple[float, float]],
    tab_ramp_length: float,
    feed_xy: float,
) -> None:
    """Re-cut the descent slice at cut_depth, lifted over tabs."""
    accum = 0.0
    for piece in pre_split:
        if accum + piece.length > descent_length + _LENGTH_EPSILON:
            break
        accum += piece.length
        tab_z = effective_z_at(
            accum,
            pass_z=cut_depth,
            tab_top_z=tab_top_z,
            intervals=tab_intervals,
            ramp_length=tab_ramp_length,
        )
        _emit_piece_with_z(instructions, piece, max(cut_depth, tab_z), feed_xy)


def _emit_ascent_with_tabs(
    instructions: list[IRInstruction],
    ascent_segs: list[Segment],
    *,
    ascent_length: float,
    cut_depth: float,
    ascent_start_offset: float,
    contour_length: float,
    tab_top_z: float,
    tab_intervals: list[tuple[float, float]],
    tab_ramp_length: float,
    feed_xy: float,
) -> None:
    """Walk ascent_segs lifting from cut_depth to 0 by local arc-length;
    tab modulation uses original-contour s = (start_offset + local_s) % L.
    """
    if ascent_length <= 0:
        return
    local = 0.0
    for piece in ascent_segs:
        local += piece.length
        ascent_z = cut_depth + (local / ascent_length) * (0.0 - cut_depth)
        original_s = (ascent_start_offset + local) % contour_length
        tab_z = effective_z_at(
            original_s,
            pass_z=cut_depth,
            tab_top_z=tab_top_z,
            intervals=tab_intervals,
            ramp_length=tab_ramp_length,
        )
        _emit_piece_with_z(instructions, piece, max(ascent_z, tab_z), feed_xy)


def _descent_z_at(
    s: float, *, prev_z: float, pass_z: float, descent_length: float
) -> float:
    if descent_length <= _LENGTH_EPSILON or s >= descent_length:
        return pass_z
    return prev_z + (s / descent_length) * (pass_z - prev_z)


def _emit_piece_with_z(
    instructions: list[IRInstruction],
    piece: Segment,
    z: float,
    feed_xy: float,
) -> None:
    """Emit a single segment forcing Z. Splits full circles."""
    if isinstance(piece, ArcSegment) and piece.is_full_circle:
        a, b = split_full_circle(piece)
        _emit_piece_with_z(instructions, a, z, feed_xy)
        _emit_piece_with_z(instructions, b, z, feed_xy)
        return
    if isinstance(piece, LineSegment):
        ex, ey = piece.end
        instructions.append(
            IRInstruction(type=MoveType.FEED, x=ex, y=ey, z=z, f=feed_xy)
        )
        return
    if isinstance(piece, ArcSegment):
        sx, sy = piece.start
        ex, ey = piece.end
        cx, cy = piece.center
        move_type = MoveType.ARC_CCW if piece.ccw else MoveType.ARC_CW
        instructions.append(
            IRInstruction(
                type=move_type,
                x=ex,
                y=ey,
                z=z,
                i=cx - sx,
                j=cy - sy,
                f=feed_xy,
            )
        )


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


def _unit_tangent_at_start(seg: Segment) -> tuple[float, float]:
    return _common_unit_tangent_at_start(seg, error_cls=ProfileGenerationError)


def _unit_tangent_at_end(seg: Segment) -> tuple[float, float]:
    return _common_unit_tangent_at_end(seg, error_cls=ProfileGenerationError)


def _emit_segment(
    instructions: list[IRInstruction], seg: Segment, feed_xy: float
) -> None:
    _common_emit_segment(
        instructions, seg, feed_xy, error_cls=ProfileGenerationError
    )
