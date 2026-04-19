"""Machine definition model."""
from uuid import uuid4

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
    """Physical CNC machine definition.

    ``macros`` slots are consumed by the post-processor:

    * ``program_start`` — replaces the preamble (absolute/mm/feed-rate/plane).
      Emitted once at the top of the program, after the header comment.
    * ``program_end`` — replaces the footer (spindle off + M30). Emitted
      once at the bottom; should end with M30 (or an equivalent end-of-
      program signal) on every controller that expects one.
    * ``tool_change`` — replaces the inline ``T<n> M6`` line for each
      ``TOOL_CHANGE`` IR instruction. ``{tool_number}`` is substituted
      with the target tool number (e.g. 1). For manual tool change this
      typically contains an M0 pause; for ATC machines it contains
      ``T{tool_number} M6`` and any pre-position moves.

    Defaults are *neutral* — they reproduce the post-processor's
    hardcoded behaviour so existing projects emit identical G-code on
    load. Users customise per-machine (park moves, probing routines,
    manual-change pauses) by editing the machine definition.
    """
    # Stable identity — lets ``MachineLibrary`` find an entry by id even
    # after the user renames it, and lets a project track "which library
    # machine was this copied from" via ``library_id``.
    id: str = Field(default_factory=lambda: uuid4().hex)
    # When the project's machine was seeded from a library entry this
    # points back at it. None = hand-rolled machine (or pre-library
    # project). Editing the project machine never retro-propagates to
    # the library; users apply the update via "Save to library…".
    library_id: str | None = None
    version: int = 1
    name: str = "Default Machine"
    controller: str = "uccnc"
    travel: Travel = Field(default_factory=Travel)
    spindle: Spindle = Field(default_factory=Spindle)
    defaults: MachineDefaults = Field(default_factory=MachineDefaults)
    macros: dict[str, str] = Field(default_factory=lambda: {
        "program_start": "G21 G90 G94 G17",
        "program_end": "M5\nM30",
        "tool_change": "T{tool_number} M6",
    })
    capabilities: Capabilities = Field(default_factory=Capabilities)
    tool_change_time_seconds: int = 90
    rapid_rate: float = 5000.0
