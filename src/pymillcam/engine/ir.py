"""Intermediate Representation for toolpath instructions."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MoveType(StrEnum):
    RAPID = "rapid"
    FEED = "feed"
    ARC_CW = "arc_cw"
    ARC_CCW = "arc_ccw"
    DWELL = "dwell"
    SPINDLE_ON = "spindle_on"
    SPINDLE_OFF = "spindle_off"
    TOOL_CHANGE = "tool_change"
    COOLANT_ON = "coolant_on"
    COOLANT_OFF = "coolant_off"
    COMMENT = "comment"
    MACRO = "macro"


@dataclass
class IRInstruction:
    """A single abstract machine instruction."""
    type: MoveType
    x: float | None = None
    y: float | None = None
    z: float | None = None
    f: float | None = None  # feed rate
    s: int | None = None  # spindle RPM
    i: float | None = None  # arc center X offset
    j: float | None = None  # arc center Y offset
    tool_number: int | None = None
    comment: str | None = None
    macro_name: str | None = None
    macro_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Toolpath:
    """A complete toolpath for one operation."""
    operation_name: str
    tool_number: int
    instructions: list[IRInstruction] = field(default_factory=list)
