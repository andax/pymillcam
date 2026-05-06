"""Pocket toolpath generator.

Consumes a PocketOp + Project, walks the selected closed boundary, and
emits IR that clears the interior area. Strategy dispatch lives here;
the strategy-specific engines are split across sibling modules:

- `offset.py` — concentric inward rings (arc-preserving where possible).
- `zigzag.py` — parallel raster strokes + finishing contour ring.
- `spiral.py` — connected OFFSET rings walked inner → outer.
- `rest_machining.py` — OFFSET-only cleanup of V-notch residuals.
- `_shared.py` — shared helpers used across strategies.

What this does not yet cover:
- HELICAL ramp entry on ZIGZAG (falls back to LINEAR, then PLUNGE).
- SPIRAL with islands falls back to OFFSET emission (bridges between
  spiral rings would otherwise cross uncut island material).
"""
from __future__ import annotations

from pymillcam.core.containment import build_pocket_regions
from pymillcam.core.operations import PocketOp, PocketStrategy
from pymillcam.core.project import Project
from pymillcam.core.segments import Segment
from pymillcam.engine.common import (
    resolve_chord_tolerance as _resolve_chord_tolerance,
    resolve_clearance as _resolve_clearance,
    resolve_safe_height as _resolve_safe_height,
    resolve_stepdown as _resolve_stepdown,
    z_levels as _z_levels,
)
from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath
from pymillcam.engine.optimizer import VisitItem, optimize_visit_order

from ._shared import (
    PocketGenerationError,
    _resolve_entity,
    _resolve_tool_controller,
)
from .offset import (
    _concentric_rings,
    _concentric_rings_with_islands,
    _helix_fits,
    compute_offset_preview,
    emit_offset_region,
)
from .spiral import (
    _spiral_rings,
    compute_spiral_preview,
    emit_spiral_region,
)
from .zigzag import (
    _zigzag_strokes_and_finishing_ring,
    compute_zigzag_preview,
    emit_zigzag_region,
)

__all__ = [
    "PocketGenerationError",
    "compute_pocket_preview",
    "generate_pocket_toolpath",
    # Internal helpers re-exported for test access.
    "_concentric_rings",
    "_concentric_rings_with_islands",
    "_helix_fits",
    "_spiral_rings",
    "_zigzag_strokes_and_finishing_ring",
]


def compute_pocket_preview(op: PocketOp, project: Project) -> list[Segment]:
    """Return the 2D plan-view path the cutter centre will follow.

    For OFFSET, concatenates every concentric ring. For ZIGZAG, emits the
    raster strokes followed by the finishing contour ring. For SPIRAL,
    same rings as OFFSET but walked inner → outer. Used by the UI to
    show a live preview as the user edits operation parameters.
    """
    tool_controller = _resolve_tool_controller(op, project)
    chord_tolerance = _resolve_chord_tolerance(op, project)
    tool_radius = float(tool_controller.tool.geometry["diameter"]) / 2.0
    entities = [
        _resolve_entity(ref.layer_name, ref.entity_id, project)
        for ref in op.geometry_refs
    ]
    if op.strategy is PocketStrategy.ZIGZAG:
        return compute_zigzag_preview(
            op,
            tool_radius=tool_radius,
            chord_tolerance=chord_tolerance,
            entities=entities,
        )
    if op.strategy is PocketStrategy.SPIRAL:
        return compute_spiral_preview(
            op,
            tool_radius=tool_radius,
            chord_tolerance=chord_tolerance,
            entities=entities,
        )
    return compute_offset_preview(
        op,
        tool_radius=tool_radius,
        chord_tolerance=chord_tolerance,
        entities=entities,
    )


def generate_pocket_toolpath(op: PocketOp, project: Project) -> Toolpath:
    """Generate an IR Toolpath for a single PocketOp within the given Project."""
    tool_controller = _resolve_tool_controller(op, project)
    safe_height = _resolve_safe_height(op, project)
    clearance = _resolve_clearance(op, project)
    chord_tolerance = _resolve_chord_tolerance(op, project)
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

    # Generate each region's IR into its own block so we can reorder
    # before concatenation. Every strategy emits a block that begins
    # with `RAPID z=safe_height` (which retracts whatever Z we're at)
    # before positioning XY, so the blocks are independently
    # concatenable in any order.
    region_blocks: list[list[IRInstruction]] = []
    for boundary, islands in regions:
        block: list[IRInstruction] = []
        if op.strategy is PocketStrategy.ZIGZAG:
            emit_zigzag_region(
                block, boundary, islands,
                op=op, tool_controller=tool_controller,
                tool_radius=tool_radius,
                chord_tolerance=chord_tolerance,
                stepdown=stepdown,
                z_levels=z_levels,
                safe_height=safe_height,
                clearance=clearance,
            )
        elif op.strategy is PocketStrategy.SPIRAL:
            emit_spiral_region(
                block, boundary, islands,
                op=op, tool_controller=tool_controller,
                tool_radius=tool_radius,
                chord_tolerance=chord_tolerance,
                stepdown=stepdown,
                z_levels=z_levels,
                safe_height=safe_height,
                clearance=clearance,
            )
        else:
            emit_offset_region(
                block, boundary, islands,
                op=op, tool_controller=tool_controller,
                tool_radius=tool_radius,
                chord_tolerance=chord_tolerance,
                stepdown=stepdown,
                z_levels=z_levels,
                safe_height=safe_height,
                clearance=clearance,
            )
        region_blocks.append(block)

    if op.optimize_region_order and len(region_blocks) > 2:
        region_blocks = _reorder_region_blocks(region_blocks)

    for block in region_blocks:
        instructions.extend(block)

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    return toolpath


def _reorder_region_blocks(
    blocks: list[list[IRInstruction]],
) -> list[list[IRInstruction]]:
    """Reorder per-region IR blocks via NN + asymmetric 2-opt.

    Each region's entry XY is the first instruction with both x and y
    set (the strategy's RAPID-to-entry); its exit XY is the last such
    instruction (where cutting ends). Blocks without any XY-setting
    instruction (degenerate / empty) keep their input position.
    """
    items: list[VisitItem] = []
    indexed_blocks: list[tuple[int, list[IRInstruction]]] = []
    for idx, block in enumerate(blocks):
        entry = _first_xy(block)
        exit_xy = _last_xy(block)
        if entry is None or exit_xy is None:
            continue
        items.append(VisitItem(entry=entry, exit=exit_xy))
        indexed_blocks.append((idx, block))
    if len(items) < 2:
        return blocks
    order = optimize_visit_order(
        items, start=(0.0, 0.0), assume_symmetric=False
    )
    # Splice the optimized blocks back into their original positions
    # (so any blocks we skipped — currently none in practice — keep
    # their original index).
    reordered = list(blocks)
    optimized_blocks = [indexed_blocks[i][1] for i in order]
    for slot, (orig_idx, _) in enumerate(indexed_blocks):
        reordered[orig_idx] = optimized_blocks[slot]
    return reordered


def _first_xy(
    block: list[IRInstruction],
) -> tuple[float, float] | None:
    for inst in block:
        if inst.x is not None and inst.y is not None:
            return (inst.x, inst.y)
    return None


def _last_xy(
    block: list[IRInstruction],
) -> tuple[float, float] | None:
    for inst in reversed(block):
        if inst.x is not None and inst.y is not None:
            return (inst.x, inst.y)
    return None
