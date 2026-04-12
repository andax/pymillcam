"""Machine definition model."""
from pydantic import BaseModel, Field


class Travel(BaseModel):
    x: float = 600.0
    y: float = 400.0
    z: float = 80.0


class Spindle(BaseModel):
    min_rpm: int = 3000
    max_rpm: int = 24000
    type: str = "er11"


class MachineDefaults(BaseModel):
    safe_height: float = 15.0
    clearance_plane: float = 3.0
    z_zero_reference: str = "top_of_stock"
    work_origin: str = "front_left"
    units: str = "mm"


class Capabilities(BaseModel):
    coolant: bool = False
    mist: bool = False
    atc: bool = False
    probe: bool = True
    fourth_axis: bool = False


class MachineDefinition(BaseModel):
    """Physical CNC machine definition."""
    version: int = 1
    name: str = "Default Machine"
    controller: str = "uccnc"
    travel: Travel = Field(default_factory=Travel)
    spindle: Spindle = Field(default_factory=Spindle)
    defaults: MachineDefaults = Field(default_factory=MachineDefaults)
    macros: dict[str, str] = Field(default_factory=lambda: {
        "program_start": "G90 G94 G21\nG17\n",
        "program_end": "M5\nG53 G0 Z0\nM30\n",
        "tool_change": "M5\nG53 G0 Z0\nM0 (Change to T{tool_number})\n",
    })
    capabilities: Capabilities = Field(default_factory=Capabilities)
    tool_change_time_seconds: int = 90
    rapid_rate: float = 5000.0
