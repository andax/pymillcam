"""Project model — top-level container for a CAM job."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from pymillcam.core.geometry import GeometryLayer
from pymillcam.core.operations import ProfileOp
from pymillcam.core.tools import ToolController


class ZReference(StrEnum):
    TOP_OF_STOCK = "top_of_stock"
    BOTTOM_OF_STOCK = "bottom_of_stock"


class WorkOrigin(StrEnum):
    FRONT_LEFT = "front_left"
    FRONT_RIGHT = "front_right"
    CENTER = "center"
    BACK_LEFT = "back_left"
    BACK_RIGHT = "back_right"


class Units(StrEnum):
    MM = "mm"
    INCH = "inch"


class Stock(BaseModel):
    """Stock material definition."""
    width: float = 200.0  # X dimension
    height: float = 200.0  # Y dimension
    thickness: float = 10.0  # Z dimension
    material: str = ""


class ProjectSettings(BaseModel):
    """Project-level settings, overrides machine defaults."""
    units: Units = Units.MM
    z_zero_reference: ZReference = ZReference.TOP_OF_STOCK
    work_origin: WorkOrigin = WorkOrigin.FRONT_LEFT
    safe_height: float = 15.0
    clearance_plane: float = 3.0


class Project(BaseModel):
    """Top-level project container."""
    version: int = 1
    name: str = "Untitled"
    machine_id: str | None = None
    stock: Stock = Field(default_factory=Stock)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    geometry_layers: list[GeometryLayer] = Field(default_factory=list)
    tool_controllers: list[ToolController] = Field(default_factory=list)
    operations: list[ProfileOp] = Field(default_factory=list)
