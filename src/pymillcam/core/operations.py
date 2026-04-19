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


class PocketStrategy(StrEnum):
    # OFFSET = concentric inward rings, starting at the outermost ring
    # (one tool radius in from the boundary) and stepping inward by the
    # configured stepover until the region closes up.
    # ZIGZAG = parallel raster strokes clipped to the machinable area,
    # alternating direction, followed by a finishing contour pass around
    # the walls so they aren't left scalloped.
    # SPIRAL is reserved.
    OFFSET = "offset"
    ZIGZAG = "zigzag"
    SPIRAL = "spiral"


class DrillCycle(StrEnum):
    # SIMPLE = one plunge to final depth, retract to clearance. Analogous
    # to G81 in standard G-code.
    # PECK = drill in `peck_depth` increments, retract all the way to
    # clearance between each peck so chips fully clear the flutes.
    # Analogous to G83 (deep-hole drilling). Use on deep holes, soft
    # material, or bits that clog easily.
    # CHIP_BREAK = drill in `peck_depth` increments, retract by a small
    # `chip_break_retract` between pecks — enough to break the chip but
    # staying in the hole. Analogous to G73 (high-speed peck). Faster
    # than PECK but leaves chips in the hole.
    SIMPLE = "simple"
    PECK = "peck"
    CHIP_BREAK = "chip_break"


class GeometryRef(BaseModel):
    """Reference to an entity within a specific GeometryLayer."""
    layer_name: str
    entity_id: str


class LeadConfig(BaseModel):
    # ARC is the safer default — it plunges genuinely off-path, so the entry
    # witness mark lands in air rather than on the cut edge. TANGENT and
    # DIRECT are available for cases where the arc doesn't fit or isn't wanted.
    # `length` is the arc length (for ARC) or line length (for TANGENT). For
    # ARC, the derived radius is length × 2/π (quarter-arc geometry).
    style: LeadStyle = LeadStyle.ARC
    length: float = 2.0


class TabConfig(BaseModel):
    """Tabs are bridges of stock left in place so the cut part doesn't
    drift on the final pass.

    `height` is the bridge thickness ABOVE `cut_depth`, not an absolute Z —
    a 0.5 mm tab on a -6 mm cut leaves the tool riding at -5.5 mm over the
    tab. The engine modulates Z per pass: passes shallower than the tab top
    cut as normal; passes that would breach the tab top ramp up to it over
    `ramp_length`, traverse `width`, then ramp down. `count` tabs are
    auto-placed by arc-length along the contour, evenly spaced.
    """
    enabled: bool = False
    style: TabStyle = TabStyle.RECTANGULAR
    count: int = 4
    width: float = 5.0
    height: float = 1.5
    ramp_length: float = 1.5
    auto_place: bool = True


class RampConfig(BaseModel):
    # LINEAR = on-contour ramp (profile default). HELICAL makes sense for
    # pockets, which have clearance area for a spiral; PLUNGE is the fallback
    # for center-cutting bits or pre-drilled starter holes.
    strategy: RampStrategy = RampStrategy.LINEAR
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
    # User-chosen start position (world XY) — the engine rotates the
    # offset contour so its start is the point on the chain nearest this
    # target. Lead-in / on-contour ramp / tabs then land in a predictable
    # place (typically scrap) rather than at the offsetter's arbitrary
    # seed vertex. None = use the offsetter's default start.
    start_position: tuple[float, float] | None = None


class PocketOp(Operation):
    """Area-clearing pocket cut inside a closed contour.

    Pockets always cut interior to the selected boundary — there's no
    outside/inside choice. `strategy` picks how the area is cleared
    (offset = concentric inward rings; zigzag / spiral reserved).
    """
    type: Literal["pocket"] = "pocket"
    strategy: PocketStrategy = PocketStrategy.OFFSET
    direction: MillingDirection = MillingDirection.CLIMB
    # Radial step between successive concentric rings (OFFSET) or
    # perpendicular spacing between parallel strokes (ZIGZAG). Typical
    # sensible range is 30-50% of tool diameter; 2 mm is a safe default
    # for the 3 mm default tool.
    stepover: float = 2.0
    # Rotation of the ZIGZAG stroke direction, CCW degrees from +X. 0 =
    # horizontal raster. Ignored by OFFSET / SPIRAL. Kept on the base
    # op (rather than a ZigzagOp subclass) so the UI can expose it
    # without changing op type when the user switches strategies.
    angle_deg: float = 0.0
    # Pockets default to multi-pass since full-depth plunging with a flat
    # endmill is unusual even for shallow cuts. Stepdown follows the same
    # ToolController cascade as profiles.
    multi_depth: bool = True
    stepdown: float | None = None
    # LINEAR is the default: the ramp occupies the last `ramp_length`
    # arc of the closed first ring, ending at `first_ring[0].start`.
    # Each pass then cuts the full ring at pass depth — no witness on
    # the pocket wall. If the ramp length exceeds the ring, the engine
    # falls back to PLUNGE. HELICAL is available as an opt-in; it's
    # classically safer in small pockets where the helix fits in a
    # cleared area, but LINEAR produces a cleaner wall on large pockets.
    ramp: RampConfig = Field(default_factory=RampConfig)
    # Rest-machining: after concentric rings + adaptive last pass,
    # compute the residual (uncut-but-cuttable area) and emit extra
    # ring-groups inside each residual component to clean up V-notch
    # corners where an island grows close to the boundary. OFFSET only;
    # ZIGZAG has a different residual pattern handled separately.
    rest_machining: bool = True
    # User-chosen start position (world XY) — the engine rotates the
    # outermost ring (OFFSET / SPIRAL first or last ring depending on
    # strategy, ZIGZAG finishing ring) so its start is the point on the
    # ring nearest this target. Plunge / ramp entry then lands in a
    # predictable place. None = use the offsetter's default start.
    start_position: tuple[float, float] | None = None


class DrillOp(Operation):
    """Point-driven drilling operation.

    Selects ``POINT`` geometry entities (or closed circles — the engine
    treats a full-circle / closed-loop entity's centre as the drill
    point). Holes are drilled at each referenced point in selection
    order; TSP re-ordering can be layered on later without changing this
    model.

    ``cut_depth`` is the target Z (negative) at the deepest part of the
    hole — the cutter centre lands there regardless of tool-tip geometry
    (no conical-tip compensation yet; revisit if tapered drills
    surface as a real need).
    """
    type: Literal["drill"] = "drill"
    cycle: DrillCycle = DrillCycle.SIMPLE
    # Peck size for PECK / CHIP_BREAK. ``None`` falls back to a
    # conservative default (1 mm) at generation time. Ignored for SIMPLE.
    peck_depth: float | None = None
    # How far to retract between pecks for CHIP_BREAK — small enough to
    # stay in the hole (so the bit doesn't lose its guide), large enough
    # to snap the chip. 0.5 mm is a common default.
    chip_break_retract: float = 0.5
    # Dwell (seconds) at bottom of each plunge. Helps chip break in
    # through-holes and tougher materials. 0 = no dwell.
    dwell_at_bottom_s: float = 0.0
