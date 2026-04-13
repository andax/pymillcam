"""Translate IR instructions into XY segments for visualisation.

The viewport is a plan view — no Z — so this collapses each move to its
XY footprint and tags it as `rapid` or `feed`. Z-only moves (plunge,
retract) are silently dropped: they don't trace any XY path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pymillcam.core.segments import ArcSegment, LineSegment, Segment
from pymillcam.engine.ir import IRInstruction, MoveType


class MoveKind(StrEnum):
    RAPID = "rapid"
    FEED = "feed"


@dataclass(frozen=True)
class WalkedMove:
    """A single XY motion segment with its kind."""
    kind: MoveKind
    segment: Segment


def walk_toolpath(instructions: list[IRInstruction]) -> list[WalkedMove]:
    """Return XY moves traced by the IR, in order."""
    cur_x = 0.0
    cur_y = 0.0
    has_position = False
    moves: list[WalkedMove] = []
    for inst in instructions:
        if inst.type is MoveType.RAPID:
            new_x, new_y = _next_xy(inst, cur_x, cur_y)
            if has_position and (new_x != cur_x or new_y != cur_y):
                moves.append(
                    WalkedMove(
                        kind=MoveKind.RAPID,
                        segment=LineSegment(start=(cur_x, cur_y), end=(new_x, new_y)),
                    )
                )
            cur_x, cur_y = new_x, new_y
            has_position = True
        elif inst.type is MoveType.FEED:
            new_x, new_y = _next_xy(inst, cur_x, cur_y)
            if has_position and (new_x != cur_x or new_y != cur_y):
                moves.append(
                    WalkedMove(
                        kind=MoveKind.FEED,
                        segment=LineSegment(start=(cur_x, cur_y), end=(new_x, new_y)),
                    )
                )
            cur_x, cur_y = new_x, new_y
            has_position = True
        elif inst.type in (MoveType.ARC_CW, MoveType.ARC_CCW):
            arc = _arc_from_ir(inst, cur_x, cur_y)
            if arc is not None:
                moves.append(WalkedMove(kind=MoveKind.FEED, segment=arc))
                cur_x, cur_y = arc.end
            has_position = True
        # All other instruction types (spindle, tool change, dwell, comments)
        # don't produce XY motion, so they're dropped.
    return moves


def _next_xy(inst: IRInstruction, cur_x: float, cur_y: float) -> tuple[float, float]:
    return (inst.x if inst.x is not None else cur_x, inst.y if inst.y is not None else cur_y)


def _arc_from_ir(inst: IRInstruction, cur_x: float, cur_y: float) -> ArcSegment | None:
    if inst.i is None or inst.j is None:
        return None
    cx = cur_x + inst.i
    cy = cur_y + inst.j
    radius = math.hypot(cur_x - cx, cur_y - cy)
    if radius == 0:
        return None
    end_x = inst.x if inst.x is not None else cur_x
    end_y = inst.y if inst.y is not None else cur_y
    start_angle = math.degrees(math.atan2(cur_y - cy, cur_x - cx))
    end_angle = math.degrees(math.atan2(end_y - cy, end_x - cx))
    sweep = _sweep_for_direction(start_angle, end_angle, inst.type)
    return ArcSegment(
        center=(cx, cy),
        radius=radius,
        start_angle_deg=start_angle,
        sweep_deg=sweep,
    )


def _sweep_for_direction(
    start_deg: float, end_deg: float, kind: Literal[MoveType.ARC_CW, MoveType.ARC_CCW] | MoveType
) -> float:
    raw = end_deg - start_deg
    full_circle = abs(raw) < 1e-9
    if kind is MoveType.ARC_CCW:
        if full_circle:
            return 360.0
        if raw <= 0:
            raw += 360.0
        return raw
    # CW
    if full_circle:
        return -360.0
    if raw >= 0:
        raw -= 360.0
    return raw
