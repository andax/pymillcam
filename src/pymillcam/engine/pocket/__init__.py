"""Pocket toolpath generator.

Consumes a PocketOp + Project, walks the selected closed boundary, and
emits IR that clears the interior area. Strategy dispatch lives here;
the strategy-specific engines are split across sibling modules:

- `offset.py` — concentric inward rings (arc-preserving where possible).
- `zigzag.py` — parallel raster strokes + finishing contour ring.
- `rest_machining.py` — OFFSET-only cleanup of V-notch residuals.
- `_shared.py` — shared helpers used by both strategies.

What this does not yet cover:
- SPIRAL strategy — preview returns empty, `generate_pocket_toolpath`
  raises `PocketGenerationError`.
- HELICAL ramp entry on ZIGZAG (falls back to LINEAR, then PLUNGE).
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
    "_zigzag_strokes_and_finishing_ring",
]


def compute_pocket_preview(op: PocketOp, project: Project) -> list[Segment]:
    """Return the 2D plan-view path the cutter centre will follow.

    For OFFSET, concatenates every concentric ring. For ZIGZAG, emits the
    raster strokes followed by the finishing contour ring. Used by the
    UI to show a live preview as the user edits operation parameters.
    """
    # SPIRAL is not implemented. Without this short-circuit, the preview
    # would fall through to the OFFSET ring branch and draw concentric
    # rings — wrong strategy, would mislead the user about what G-code
    # generation will actually produce (it will raise).
    if op.strategy is PocketStrategy.SPIRAL:
        return []

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
    return compute_offset_preview(
        op,
        tool_radius=tool_radius,
        chord_tolerance=chord_tolerance,
        entities=entities,
    )


def generate_pocket_toolpath(op: PocketOp, project: Project) -> Toolpath:
    """Generate an IR Toolpath for a single PocketOp within the given Project."""
    if op.strategy is PocketStrategy.SPIRAL:
        raise PocketGenerationError(
            f"Pocket strategy {op.strategy.value!r} is not implemented yet "
            "— only 'offset' and 'zigzag' are available."
        )

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
    for boundary, islands in regions:
        if op.strategy is PocketStrategy.ZIGZAG:
            emit_zigzag_region(
                instructions, boundary, islands,
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
                instructions, boundary, islands,
                op=op, tool_controller=tool_controller,
                tool_radius=tool_radius,
                chord_tolerance=chord_tolerance,
                stepdown=stepdown,
                z_levels=z_levels,
                safe_height=safe_height,
                clearance=clearance,
            )

    instructions.append(IRInstruction(type=MoveType.RAPID, z=safe_height))
    return toolpath
