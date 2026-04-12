"""Operation models — abstract base and concrete ProfileOp.

Operations carry the parameters the toolpath engine needs to emit IR for one
cut. Fields typed `| None` cascade from Project/Machine settings when unset;
callers in the engine are responsible for resolving the cascade.

Additional operation subtypes (pocket, drill, engrave, surface, contour) will
be added when their engine implementations arrive. ProfileOp is enough for the
Phase 1 DXF → profile → G-code path.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class OffsetSide(StrEnum):
    INSIDE = "inside"
    OUTSIDE = "outside"
    ON_LINE = "on_line"


class LeadStyle(StrEnum):
    ARC = "arc"
    TANGENT = "tangent"
    DIRECT = "direct"


class RampStrategy(StrEnum):
    HELICAL = "helical"
    LINEAR = "linear"
    PLUNGE = "plunge"


class MillingDirection(StrEnum):
    CLIMB = "climb"
    CONVENTIONAL = "conventional"


class TabStyle(StrEnum):
    RECTANGULAR = "rectangular"
    TRIANGULAR = "triangular"
    THIN_WEB = "thin_web"


class GeometryRef(BaseModel):
    """Reference to an entity within a specific GeometryLayer."""
    layer_name: str
    entity_id: str


class LeadConfig(BaseModel):
    style: LeadStyle = LeadStyle.ARC
    length: float = 2.0
    radius: float = 2.0


class TabConfig(BaseModel):
    enabled: bool = False
    style: TabStyle = TabStyle.RECTANGULAR
    count: int = 4
    width: float = 5.0
    height: float = 1.5
    auto_place: bool = True


class RampConfig(BaseModel):
    strategy: RampStrategy = RampStrategy.HELICAL
    angle_deg: float = 3.0
    radius: float = 1.0


class Operation(BaseModel):
    """Abstract base for CAM operations. Subclasses set `type` as a Literal."""
    id: str = Field(default_factory=lambda: uuid4().hex)
    type: str
    name: str
    enabled: bool = True
    tool_controller_id: int | None = None
    geometry_refs: list[GeometryRef] = Field(default_factory=list)
    cut_depth: float = 0.0
    safe_height: float | None = None
    clearance_plane: float | None = None
    # Per-operation override of ProjectSettings.chord_tolerance. None = inherit.
    chord_tolerance: float | None = None


class ProfileOp(Operation):
    """Offset-based profile cut along selected contour geometry."""
    type: Literal["profile"] = "profile"
    offset_side: OffsetSide = OffsetSide.OUTSIDE
    direction: MillingDirection = MillingDirection.CLIMB
    multi_depth: bool = True
    stepdown: float | None = None
    lead_in: LeadConfig = Field(default_factory=LeadConfig)
    lead_out: LeadConfig = Field(default_factory=LeadConfig)
    tabs: TabConfig = Field(default_factory=TabConfig)
    ramp: RampConfig = Field(default_factory=RampConfig)
