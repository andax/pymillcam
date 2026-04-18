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

    ``id`` is this tool instance's stable identifier (per-op copy in a
    project, or per-entry in the library).

    ``library_id`` points back at the library tool this one was copied
    from, or is ``None`` for a tool that never came from the library /
    was explicitly switched to "Custom" in the Properties panel. That
    field — not name matching — is what the Tool dropdown uses to
    decide whether the op is pinned to a library entry. It survives
    save/load, so a reopened project still knows which tools are
    library-backed and which aren't.

    Both ``id`` and ``library_id`` have Pydantic default values so
    pre-existing ``.pmc`` files (with neither field) still load.
    """
    version: int = 1
    id: str = Field(default_factory=lambda: uuid4().hex)
    library_id: str | None = None
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
