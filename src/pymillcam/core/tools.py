"""Tool and ToolController models."""
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class ToolShape(StrEnum):
    ENDMILL = "endmill"
    BALLNOSE = "ballnose"
    VBIT = "vbit"
    DRILL = "drill"
    CHAMFER = "chamfer"
    BULLNOSE = "bullnose"


class CuttingData(BaseModel):
    """Cutting parameters for a specific material."""
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    stepdown: float = 1.0
    stepover_pct: float = 40.0


class Tool(BaseModel):
    """A physical cutting tool with geometry and cutting data.

    ``id`` is a stable identifier used by the ToolLibrary to reference
    a specific tool across sessions and to dedupe imports. Defaults to
    a UUID hex on construction; projects loaded from disk without an
    ``id`` field get one assigned at validation time, so older ``.pmc``
    files still load cleanly.
    """
    version: int = 1
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str
    shape: ToolShape = ToolShape.ENDMILL
    geometry: dict[str, float | int] = Field(default_factory=lambda: {
        "diameter": 3.0,
        "flute_length": 15.0,
        "total_length": 50.0,
        "shank_diameter": 3.0,
        "flute_count": 2,
    })
    cutting_data: dict[str, CuttingData] = Field(default_factory=dict)
    supplier: str = ""
    part_number: str = ""
    notes: str = ""


class ToolController(BaseModel):
    """Binds a tool to operation-specific runtime parameters."""
    tool_number: int = 1
    tool: Tool
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    stickout: float = 30.0
