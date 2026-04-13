# PyMillCAM — Project Brief for Claude Code

## What This Project Is
PyMillCAM is a Python-based, open-source 2D/2.5D CAM tool for CNC routers/mills. It fills the gap between simple tools like Estlcam and complex ones like Fusion 360's CAM. The design is wizard-driven for beginners but fully editable via an operations tree for power users.

## Architecture (3 Layers + UI)

### Layer 1: Project Model (`src/pymillcam/core/`)
Pure data layer using Pydantic models. No UI dependencies. Serializes to JSON.
- `project.py` — Project, Stock, ProjectSettings
- `geometry.py` — GeometryLayer, entity wrappers around Shapely objects
- `operations.py` — Operation base class + ProfileOp, PocketOp, DrillOp, EngraveOp, SurfaceOp, ContourOp
- `tools.py` — Tool (geometry + cutting data), ToolController (binds tool to operation with runtime params)
- `machine.py` — MachineDefinition (travel, spindle, macros, capabilities, defaults)
- `fixtures.py` — FixtureSetup, Clamp
- `materials.py` — MaterialDatabase
- `preferences.py` — AppPreferences (cascading: machine → project → operation)

### Layer 2: Toolpath Engine (`src/pymillcam/engine/`)
Pure Python, no UI. Takes project model → produces IR (intermediate representation).
- `ir.py` — IRInstruction dataclass (rapid, feed, arc_cw, arc_ccw, dwell, spindle_on, tool_change, etc.)
- `profile.py` — Profile toolpath (offsets, lead-in/out, ramp entry, tabs, multi-depth)
- `pocket.py` — Pocket strategies (zigzag, spiral, offset-based, ramp entry)
- `drill.py` — Drill cycles (simple, peck, chip-break)
- `engrave.py` — Engrave and V-carve
- `surface.py` — Surface/facing
- `patterns.py` — Pattern generators (rect grid, hex grid, circular array, text)
- `tabs.py` — Tab generation (rectangular, triangular, thin-web)
- `validation.py` — Z stack budget, travel limits, fixture collision checks
- `feeds_speeds.py` — Feed/speed calculator
- `time_estimate.py` — Operation time estimation
- `nesting.py` — Part nesting / layout optimization
- `optimizer.py` — Toolpath optimization (tool grouping, rapid minimization, drill TSP)

### Layer 3: Post-Processors (`src/pymillcam/post/`)
Transforms IR → G-code for specific controllers.
- `base.py` — PostProcessor Protocol
- `uccnc.py`, `mach3.py`, `grbl.py`, `linuxcnc.py`

### UI Layer (`src/pymillcam/ui/`) — PySide6
- Main window with toolbar, dockable panels
- Operations tree (left), 2D viewport (center), properties panel (right)
- Wizards as multi-step dialogs
- Directional box selection (L→R = contained, R→L = crossing)
- Select Similar (same diameter/layer/type)

### Other Modules
- `io/` — DXF import (ezdxf), SVG import, FreeCAD/LinuxCNC tool import, project save/load
- `sim/` — Toolpath view, animated playback, pre-flight dashboard
- `external/` — External tool launcher with template variable substitution

## Key Design Decisions
- **Units**: Internal representation always in mm. Convert on import/export for inch users.
- **Coordinate system**: Z zero can be top or bottom of stock. XY origin configurable (front-left, center, etc.)
- **Settings cascade**: Machine defaults → Project settings → Operation overrides. Inherited values shown differently in UI.
- **Tool library**: JSON format, superset of FreeCAD .fctb. Stores geometry + cutting data per material.
- **Machine library**: JSON. Stores travel, spindle range, macros (tool change, probing, start/end), capabilities.
- **Z stack safety**: Track spoilboard + fixture + stock + tool stickout. Validate reachability before G-code generation.

## Tech Stack
- Python 3.11+
- PySide6 (Qt6) for GUI
- Shapely for 2D geometry
- ezdxf for DXF import
- pyclipper/clipper2 for polygon offsetting
- Pydantic for data models
- numpy for numeric computation

## Development Phase (Current: Phase 1)
Phase 1 goal: Import a DXF, assign a profile operation to a contour, generate G-code.
1. ✅ Core Pydantic data models
2. ✅ DXF import (arcs preserved as ArcSegments, including LWPOLYLINE bulges)
3. ✅ Basic profile toolpath (offset, multi-depth)
4. ✅ IR + UCCNC post-processor (emits G2/G3 arcs; end-to-end DXF → G-code works)
5. ✅ Minimal PySide6 window (shell, viewport, tree, properties, G-code output, save/load)
6. ✅ Undo/redo command infrastructure (snapshot-based stack, Ctrl+Z / Ctrl+Shift+Z, coalesced property edits)
7. ⬜ Directional box selection
8. ✅ Project save/load as JSON

### Known limitations to revisit
- `engine/profile.py::_offset_contour` still routes inside/outside offsets
  through `Polygon.buffer`, collapsing arcs to chords. This is the only
  remaining lossy interface; replacing it with an arc-aware offset
  requires no changes to callers. `OffsetSide.ON_LINE` already preserves
  arcs end-to-end.
- Machine macros (program_start / program_end / tool_change) are defined
  on `MachineDefinition` but not yet consumed by the post-processor.
- DXFs authored as separate `LINE` entities import as one `GeometryEntity`
  per LINE, so a four-sided shape becomes four selectable contours instead
  of one. Polyline-authored DXFs (`LWPOLYLINE`/`POLYLINE`) already arrive
  as single contours. Future fix: a "Join paths" action and/or an opt-in
  on-import endpoint-stitch pass.

## Code Style
- Type hints everywhere
- Pydantic models for all data structures
- Protocol classes for interfaces (PostProcessor, etc.)
- No UI imports in core/ or engine/ modules
- Tests in tests/ mirroring src/ structure
- src-layout (import as `from pymillcam.core import ...`)

## Reference Document
See `docs/pymillcam_plan.md` for the full architecture and planning document (v0.4).
