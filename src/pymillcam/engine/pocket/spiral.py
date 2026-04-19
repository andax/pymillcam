"""SPIRAL pocket strategy — connected OFFSET rings walked inner → outer.

Generates the same concentric rings as OFFSET, then reverses the order
so travel starts at the innermost (interior) ring and spirals outward.
Consecutive rings are connected via feed-at-depth bridges (the same
short connectors `_emit_ring_chain` produces between any two rings),
yielding a single continuous path with no retract-between-rings.

Compared to OFFSET:
- Entry lands at the pocket interior (innermost ring's start), which
  typically has more clearance for HELICAL ramps.
- No tool-lift between rings means lower cycle time and fewer witness
  marks on the floor of the pocket.

Limitations (accepted):
- Not a morphing Archimedean spiral. The path is connected OFFSET rings;
  each ring is still a closed contour, bridged to the next by a short
  feed move.
- Seed position = the innermost ring's start point (wherever the offsetter
  places it), not the pole of inaccessibility.
- With islands, the bridges between rings could cross uncut island
  material — so island regions fall back to OFFSET emission.
"""
from __future__ import annotations

from pymillcam.core.containment import build_pocket_regions
from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.operations import MillingDirection, PocketOp
from pymillcam.core.segments import Segment
from pymillcam.core.tools import ToolController
from pymillcam.engine.ir import IRInstruction

from ._shared import PocketGenerationError, _rotate_rings_to_start_position
from .offset import (
    _concentric_rings,
    _emit_rings,
    _resolve_ramp_strategy,
    emit_offset_region,
)


def _spiral_rings(
    boundary: GeometryEntity,
    *,
    tool_radius: float,
    stepover: float,
    direction: MillingDirection,
    chord_tolerance: float,
    start_position: tuple[float, float] | None = None,
) -> list[list[Segment]]:
    """Build inner-first concentric rings for a SPIRAL traversal.

    Same rings as OFFSET, reversed so `rings[0]` is the innermost ring
    (where the tool enters the pocket) and `rings[-1]` is the outermost
    (flush with the wall). When ``start_position`` is set, each ring is
    rotated to begin at its nearest point to that target so the plunge
    (at ``rings[0][0].start``) lands where the user asked.
    """
    rings = _concentric_rings(
        boundary, tool_radius, stepover, direction, chord_tolerance
    )
    rings = _rotate_rings_to_start_position(rings, start_position)
    return list(reversed(rings))


def compute_spiral_preview(
    op: PocketOp,
    *,
    tool_radius: float,
    chord_tolerance: float,
    entities: list[GeometryEntity],
) -> list[Segment]:
    """Preview-path contribution for SPIRAL across every region.

    Island regions mirror the emit-time fallback (OFFSET order) so the
    preview reflects what G-code generation will actually produce.
    """
    preview: list[Segment] = []
    for boundary, islands in build_pocket_regions(entities):
        if islands:
            # Fall back to OFFSET preview for island regions — matches
            # emit_spiral_region's emit-time behaviour.
            from .offset import compute_offset_preview
            preview.extend(
                compute_offset_preview(
                    op,
                    tool_radius=tool_radius,
                    chord_tolerance=chord_tolerance,
                    entities=[boundary, *islands],
                )
            )
            continue
        rings = _spiral_rings(
            boundary,
            tool_radius=tool_radius,
            stepover=op.stepover,
            direction=op.direction,
            chord_tolerance=chord_tolerance,
            start_position=op.start_position,
        )
        for ring in rings:
            preview.extend(ring)
    return preview


def emit_spiral_region(
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
    """Emit IR for one pocket region using SPIRAL strategy.

    Island regions fall back to OFFSET emission — the bridges SPIRAL uses
    between consecutive rings are straight feed-at-depth moves, which
    could cross uncut island material in a boundary-with-islands case.
    """
    if islands:
        emit_offset_region(
            instructions, boundary, islands,
            op=op, tool_controller=tool_controller,
            tool_radius=tool_radius,
            chord_tolerance=chord_tolerance,
            stepdown=stepdown,
            z_levels=z_levels,
            safe_height=safe_height,
            clearance=clearance,
        )
        return

    rings = _spiral_rings(
        boundary,
        tool_radius=tool_radius,
        stepover=op.stepover,
        direction=op.direction,
        chord_tolerance=chord_tolerance,
        start_position=op.start_position,
    )
    if not rings:
        raise PocketGenerationError(
            f"Pocket {op.name!r}: tool too large for the selected boundary "
            f"(no rings fit at stepover={op.stepover} mm, tool radius="
            f"{tool_radius} mm)."
        )
    resolved_ramp = _resolve_ramp_strategy(op.ramp, rings, stepdown)
    _emit_rings(
        instructions, rings,
        tool_controller=tool_controller,
        z_levels=z_levels,
        safe_height=safe_height,
        clearance=clearance,
        ramp_config=op.ramp,
        resolved_strategy=resolved_ramp,
    )
