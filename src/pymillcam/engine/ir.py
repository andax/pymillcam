"""Intermediate Representation for toolpath instructions."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MoveType(str, Enum):
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
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    f: Optional[float] = None  # feed rate
    s: Optional[int] = None  # spindle RPM
    i: Optional[float] = None  # arc center X offset
    j: Optional[float] = None  # arc center Y offset
    tool_number: Optional[int] = None
    comment: Optional[str] = None
    macro_name: Optional[str] = None
    macro_params: dict = field(default_factory=dict)


@dataclass
class Toolpath:
    """A complete toolpath for one operation."""
    operation_name: str
    tool_number: int
    instructions: list[IRInstruction] = field(default_factory=list)
