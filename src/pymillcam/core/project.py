"""Project model — top-level container for a CAM job."""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

from pymillcam.core.geometry import GeometryLayer
from pymillcam.core.machine import MachineDefinition
from pymillcam.core.operations import DrillOp, PocketOp, ProfileOp
from pymillcam.core.tools import ToolController

# Pydantic v2 discriminated union — the `type` literal on each concrete
# op tells the validator which class to reconstruct when loading JSON.
OperationUnion = Annotated[
    ProfileOp | PocketOp | DrillOp, Field(discriminator="type")
]


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
    """Project-level settings, overrides machine defaults.

    ``safe_height`` and ``clearance_plane`` are ``None`` by default —
    that signals "inherit from machine defaults" at generation time.
    The resolvers in ``engine/common.py`` cascade op-override → project
    setting → ``Project.machine.defaults`` → hardcoded ultimate fallback.
    Set a concrete value here when a specific job needs to override the
    machine's configured safe travel (e.g. a fixture raised the stock).
    """
    units: Units = Units.MM
    z_zero_reference: ZReference = ZReference.TOP_OF_STOCK
    work_origin: WorkOrigin = WorkOrigin.FRONT_LEFT
    safe_height: float | None = None
    clearance_plane: float | None = None
    # Max chord sag (mm) used when arcs must be collapsed to straight-line
    # segments for G-code output. 0.02 mm balances visual smoothness against
    # G-code length; tighten to 0.01 mm or below for metal finishing, loosen
    # to 0.05 mm for very rough cuts where every byte counts.
    chord_tolerance: float = 0.02
    # Dwell in seconds after M3 to let the spindle reach commanded RPM
    # before cutting. Emitted as a G4 P<s> immediately after each
    # SPINDLE_ON. 2 s is a safe default for most router/mill spindles;
    # VFD-driven spindles ramp up in 1-3 s. Will move to MachineDefinition
    # when machine macros are wired to post-processors.
    spindle_warmup_s: float = 2.0


class Project(BaseModel):
    """Top-level project container."""
    version: int = 1
    name: str = "Untitled"
    # ``machine_id`` is reserved for a future machine library (currently
    # unused). ``machine`` holds the actual definition embedded in the
    # project file so projects are portable between machines that don't
    # share the same library.
    machine_id: str | None = None
    machine: MachineDefinition = Field(default_factory=MachineDefinition)
    stock: Stock = Field(default_factory=Stock)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    geometry_layers: list[GeometryLayer] = Field(default_factory=list)
    tool_controllers: list[ToolController] = Field(default_factory=list)
    operations: list[OperationUnion] = Field(default_factory=list)
