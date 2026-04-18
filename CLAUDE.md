# PyMillCAM — Project Brief for Claude Code

## What This Project Is
PyMillCAM is a Python-based, open-source 2D/2.5D CAM tool for CNC routers/mills. It fills the gap between simple tools like Estlcam and complex ones like Fusion 360's CAM. The design is wizard-driven for beginners but fully editable via an operations tree for power users.

## Architecture (3 Layers + UI)

### Layer 1: Project Model (`src/pymillcam/core/`)
Pure data layer using Pydantic models. No UI dependencies. Serializes to JSON.
- `project.py` — Project, Stock, ProjectSettings
- `geometry.py` — GeometryLayer, entity wrappers around Shapely objects
- `operations.py` — Operation base class + ProfileOp, PocketOp, DrillOp, EngraveOp, SurfaceOp, ContourOp
- `tools.py` — Tool (geometry + cutting data, optional `library_id` linking back to a library entry), ToolController (binds tool to operation with runtime params)
- `tool_library.py` — `ToolLibrary` Pydantic model + atomic JSON load/save (tmp-write + rename).
- `selection.py` — `SimilarityMode` enum + `find_similar_entities` (same layer / same type / same diameter). Pure data; the viewport + main window drive the UX.
- `containment.py` — Polygon containment tree → (boundary, [islands]) pocket regions.
- `path_stitching.py` — Weld entity endpoints within tolerance (shared by Operations > Join paths and opt-in auto-stitch).
- `machine.py` — MachineDefinition (travel, spindle, macros, capabilities, defaults)
- `fixtures.py` — FixtureSetup, Clamp
- `materials.py` — MaterialDatabase
- `preferences.py` — AppPreferences (cascading: machine → project → operation)

### Layer 2: Toolpath Engine (`src/pymillcam/engine/`)
Pure Python, no UI. Takes project model → produces IR (intermediate representation).
- `ir.py` — IRInstruction dataclass (rapid, feed, arc_cw, arc_ccw, dwell, spindle_on, tool_change, etc.)
- `ir_walker.py` — Translates IR back into XY move segments for the viewport's toolpath overlay.
- `common.py` — `EngineError` base + shared helpers used by every op type: cascade resolvers (`resolve_tool_controller`, `resolve_entity`, `resolve_stepdown`, `resolve_chord_tolerance`, `resolve_safe_height`, `resolve_clearance`), pass planning (`z_levels`), chain walkers (`chain_is_ccw`, `split_chain_at_length`, `walk_closed_chain`), tangent helpers, and IR-emit primitives (`emit_segment`, `emit_ramp_segments`). Every raising helper takes an `error_cls` so `profile.py` keeps raising `ProfileGenerationError` and `pocket.py` raises `PocketGenerationError` — both subclass `EngineError`, so the UI catches once.
- `services.py` — `ToolpathService` facade. Dispatches `(op, project)` to preview / toolpath / program generation by op type via a registry (`register_preview`, `register_toolpath`). The UI talks to this, not to individual engine modules. New op types (drill, surface, engrave, …) register themselves — `MainWindow` never dispatches by op type.
- `profile.py` — Profile toolpath (offsets, lead-in/out, ramp entry, tabs, multi-depth)
- `pocket.py` — Pocket strategies (offset, zigzag — spiral reserved, ramp entry with fallback chain)
- `drill.py` — Drill cycles (SIMPLE / PECK / CHIP_BREAK). Point-driven; resolves POINT entities, full-circle arcs, and closed contours to drill coordinates. Emits expanded G0/G1 IR (not canned G81/G83) for post-processor portability.
- `engrave.py` — Engrave and V-carve — not yet
- `surface.py` — Surface/facing — not yet
- `patterns.py` — Pattern generators (rect grid, hex grid, circular array, text) — not yet
- `tabs.py` — Tab generation (rectangular, triangular, thin-web)
- `validation.py` — Z stack budget, travel limits, fixture collision checks — not yet
- `feeds_speeds.py` — Feed/speed calculator — not yet
- `time_estimate.py` — Pure-function operation time estimator (IR-walker over rapid / feed / arc + dwell + tool-change, feed-rate resolution from op cascade). Consumed by `main_window` to annotate ops in the tree.
- `nesting.py` — Part nesting / layout optimization — not yet
- `optimizer.py` — Toolpath optimization (tool grouping, rapid minimization, drill TSP) — not yet

### Layer 3: Post-Processors (`src/pymillcam/post/`)
Transforms IR → G-code for specific controllers.
- `base.py` — PostProcessor Protocol
- `uccnc.py`, `mach3.py`, `grbl.py`, `linuxcnc.py`

### UI Layer (`src/pymillcam/ui/`) — PySide6
- `main_window.py` — Shell. Owns the `ToolpathService` instance; delegates all engine work through it (no `isinstance` chains on op type).
- `viewport.py` — 2D viewport. Directional box selection (L→R = contained, R→L = crossing), pan/zoom/fit. Arcs render as chord polylines with sub-pixel chord sag (≤ 0.5 px), so adjacent arc junctions share exact widget pixels — no hairline gap.
- `properties_panel.py` — Host + registry. `OperationFormBase` is the abstract per-op-type form; concrete forms register with `@register_form(OpType)` decorator. `PropertiesPanel` looks up the form for the bound op, binds, and listens to one `field_changed` signal. Adding a new op type = one form class + one decorator, no panel changes.
- `wizards/base.py` — `BaseWizard(QWizard)` + `BaseWizardPage` scaffold. `OperationFormPage` reuses the same `OperationFormBase` widget that Properties uses, so forms are defined once and surface in both places.
- `box_selection.py` — Selection-combine semantics + rubber-band rect.
- `tool_library_dialog.py` — Library editor (add / duplicate / delete / rename tool entries, atomic save).
- `preferences_dialog.py` — Stitch tolerance + edit-coalesce window + auto-stitch toggle.
- Operations tree, Select Similar, operation duplication (Ctrl+Shift+D, auto "(copy)" / "(copy N)" suffix), active-op entity highlight, and the unified tree/viewport entity context menu all live in `main_window.py`.

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

## Development Phase (Phase 1 complete; Phase 2 in progress)
Phase 1 goal: Import a DXF, assign a profile operation to a contour, generate G-code.
1. ✅ Core Pydantic data models
2. ✅ DXF import (arcs preserved as ArcSegments, including LWPOLYLINE bulges)
3. ✅ Basic profile toolpath (offset, multi-depth)
4. ✅ IR + UCCNC post-processor (emits G2/G3 arcs; end-to-end DXF → G-code works)
5. ✅ Minimal PySide6 window (shell, viewport, tree, properties, G-code output, save/load)
6. ✅ Undo/redo command infrastructure (snapshot-based stack, Ctrl+Z / Ctrl+Shift+Z, coalesced property edits)
7. ✅ Directional box selection (L→R window, R→L crossing, multi-select)
8. ✅ Project save/load as JSON

Phase 2 progress (ongoing):
- ✅ Profile leads (arc / tangent / direct) + on-contour ramp descent / ascent
- ✅ Pocket toolpath — OFFSET strategy (concentric inward rings, arc-preserving),
  multi-depth with retract-to-clearance between passes, ramp entry with LINEAR
  (default) + HELICAL strategies and HELICAL→LINEAR→PLUNGE fallback chain.
  LINEAR ramp occupies the last `ramp_length` arc of the first ring and ends at
  `first_ring[0].start`, so the full ring runs at pass depth with no witness.
- ✅ Pocket ZIGZAG (parallel raster strokes, arc-preserving finishing
  ring, `angle_deg` rotation, LINEAR ramp with back-and-forth on short
  first strokes).
- ✅ Pocket islands (containment-tree inference at toolpath time:
  `core/containment.build_pocket_regions` groups selected closed
  contours into (boundary, [islands]) regions; even-depth contours are
  boundaries, odd-depth are islands, nested pockets fall out via
  alternation). OFFSET emits ring groups (one per Polygon in the
  buffer-with-holes result) with retract+rapid+plunge between disjoint
  groups. ZIGZAG subtracts dilated islands from the machinable polygon
  and emits one finishing ring per island wall (also retract between).
  Multiple disjoint boundaries selected for one PocketOp become
  multiple regions cut with the same settings.
- ✅ Profile tabs (rectangular, auto-spaced by arc-length, multi-depth
  aware with `effective_z = max(planned_z(s), tab_z(s))`; coexists with
  on-contour ramp).
- ✅ Pocket rest-machining for V-notch corners (OFFSET only). After the
  regular + adaptive passes, the engine tracks each emitted ring's
  centerline, computes `swept = ∪ centerline.buffer(tool_radius)` and
  `cuttable = machinable.buffer(-r).buffer(+r)`, and emits one cleanup
  ring per residual component inside `residual ∩ tool_center_space` —
  i.e., the part of the uncut area the tool center can physically reach.
  This stays inside the uncut region rather than walking through already-
  swept territory (an earlier attempt that used `residual.buffer(+r) ∩
  tool_center_space` produced redundant overlapping paths). Gated by
  `PocketOp.rest_machining` (default True). The main iteration also
  filters pinch-off noise polygons (area < `(10·chord_tolerance)²`) so
  they don't pollute the swept-area model.
- ✅ Drill operation (April 2026) — three cycle types (SIMPLE / PECK
  / CHIP_BREAK). Accepts POINT entities and closed circles / contours
  (engine resolves to centre). Emits expanded G0/G1 for post-processor
  portability; between-hole traversal stays at clearance, inter-op
  travel uses safe_height. First op added via the April 2026 facade
  architecture — zero dispatch changes in MainWindow.
- Pocket SPIRAL — not yet. The preview returns empty (matches the
  generation-time refusal) and the Properties-panel strategy dropdown
  filters it out, so the UI no longer lies about it. Drop both
  safeguards when an actual spiral implementation lands.
- ✅ ZIGZAG multi-region connector safety (April 2026) — connectors
  between consecutive strokes now test the midpoint against the
  machinable polygon; unsafe connectors (those crossing into an
  island) retract+rapid+plunge instead of feed-at-depth. Uses a
  0.1 mm distance slack so chord-approximation artefacts don't
  trigger spurious retracts.
- ✅ Tool library (April 2026) — `core/tool_library.ToolLibrary`
  (atomic JSON IO) + `ui/tool_library_dialog` editor + Properties-
  panel Tool dropdown. Ops keep their own `ToolController` but link
  back to a library entry via `Tool.library_id`; selecting a library
  tool in Properties locks the tool-geometry fields (diameter, flute
  length, …) so edits happen in the library dialog.
- ✅ Operation time estimator (April 2026) — `engine/time_estimate`
  walks the IR for each op (rapid / feed / arc length, dwell,
  tool-change), resolves feed rates through the cascade, and
  `MainWindow` annotates each op row in the tree with
  `[hh:mm:ss]`. Recomputes on project change.
- ✅ Select Similar (April 2026) — `core/selection.SimilarityMode`
  + `find_similar_entities` (same layer / same type / same diameter,
  0.01 mm tolerance for diameter). Available from the unified entity
  context menu (tree and viewport); the diameter option is only
  offered for full-circle seeds.
- ✅ Operation duplication (April 2026) — Ctrl+Shift+D duplicates
  the selected op, preserving geometry refs and cloning the
  `ToolController`. Auto-disambiguated names via `(copy)` /
  `(copy 2)` / `(copy N)` suffix.
- ✅ Active-op highlight + unified context menu (April 2026) —
  selecting an op tints its member entity rows in the tree and
  overlays them in the viewport. Right-click on an entity (tree or
  viewport) opens a single shared menu that is fully dynamic:
  Select Similar entries only show when the mode applies (diameter
  gated to full circles), and per-op Add/Remove toggles by current
  membership. No greyed-out items. Viewport right-click hit-tests
  the cursor first and falls back to the current selection.
- Machine definitions (macros wired through to the post) — not yet.

Infrastructure / architecture refactors (April 2026 — prep for Phase 3):
- ✅ `engine/common.py` extracted. ~280 lines of shared cascade / chain /
  IR-emit helpers that used to be duplicated line-for-line between
  `profile.py` and `pocket.py`. Error classes now inherit from
  `EngineError` — the UI catches once at `main_window`.
- ✅ `engine/services.py::ToolpathService` — op-type dispatch registry
  consumed by `main_window`. Preview and toolpath generation both go
  through it. New op types register (`register_preview`,
  `register_toolpath`); the UI doesn't enumerate types.
- ✅ `ui/properties_panel.OperationFormBase` + `FORM_REGISTRY`. Each form
  owns its widgets, populate/write-back, and signal wiring via
  `self._wire(...)`. `PropertiesPanel` is generic; new op-type forms
  plug in with `@register_form(OpType)`.
- ✅ `ui/wizards/base.py` — `BaseWizard` runs `apply(project)` on each
  page after Finish (not Cancel). `OperationFormPage` embeds an
  `OperationFormBase` widget so wizards and Properties share one form
  surface per op type.
- ✅ `ui/viewport.py` arcs render as chord polylines (sub-pixel sag),
  not `QPainter.drawArc` (1/16° int units) or `QPainterPath.arcTo`
  (Bézier). Eliminates the hairline gap at adjacent-arc junctions on
  dense geometry like gear teeth.

Pocket islands known limitations:
  - OFFSET with islands uses Shapely buffer (chord-discretized), not
    the analytical arc-preserving offsetter. Arc preservation is
    deferred until the analytical offsetter learns about holes.
  - ZIGZAG does not participate in rest-machining; residuals there have
    a different shape (stroke-clipped) and are best tackled the next
    time someone is in that code path.
  - Large residuals get a single cleanup ring, which may not fully
    cover the interior if the residual is wider than ~2·tool_radius.
    Small V-notch corners (the main case) are cleared; revisit if a
    test case surfaces wider residuals.
  - When stepover doesn't divide the wall thickness evenly, the
    OFFSET buffer-iteration would otherwise stop one stepover short of
    the centerline, leaving a sliver. The engine emits an "adaptive
    last pass" at half-stepover past the last successful distance to
    close the residual; skipped if the resulting polygon area is
    < `stepover²` (avoids microscopic Shapely artefacts).

`ProjectSettings.chord_tolerance` defaults to 0.02 mm (was 0.05 in early
Phase 1). Per-op override via the Properties panel.

### Known limitations to revisit
- `core/offsetter.offset_closed_contour` is the analytical, arc-preserving
  offsetter — handles full circles, line-only polygons (with rounded outer
  corners and intersected inner corners), and line+tangent-arc shapes
  (rounded rectangles). `engine/profile.py::_offset_contour` calls it
  first and falls back to `Polygon.buffer` only for cases the MVP doesn't
  cover (non-tangent line↔arc concave joins, multi-loop / holed contours).
  The fallback path still collapses arcs to chords; track the residual
  cases as they come up.
- Machine macros (program_start / program_end / tool_change) are defined
  on `MachineDefinition` but not yet consumed by the post-processor.
- DXF path stitching is now available two ways: an explicit
  `Operations > Join paths` action that welds the current selection,
  and an opt-in `auto_stitch_on_import` preference that runs the same
  pass after every DXF load. Both use `AppPreferences.stitch_tolerance_mm`.
  Conservative: a vertex shared by 3+ entities (Y/T-junction) is left
  unstitched.
  - `stitch_entities` welds chains whose endpoints are within tolerance
    but does NOT snap adjacent endpoints to a shared exact coordinate
    when merging — so a hand-drawn DXF with, say, a 0.05 mm gap stitches
    into "one chain" whose `segments[i].end != segments[i+1].start`.
    Viewport rendering looks continuous now (chord-polyline fix), but
    downstream tangent math (offsetter join classification, profile
    lead anchors) sees a tiny discontinuity. Low priority since the
    generated test fixtures have zero gap; revisit if a hand-drawn DXF
    surfaces a bug.
- Tool library is shipped, but ops still own their own `ToolController`.
  Library membership is a soft link via `Tool.library_id`; edits made
  through the library dialog don't propagate to ops already created
  from that tool. A future "sync from library" action (or live binding)
  is the next step when users start managing real libraries.
- Property-edit coalescing window is a hardcoded 400 ms in
  `MainWindow._edit_timer`. Probably fine, but worth revisiting if real
  users find it laggy or jumpy.
- `engine/pocket.py` is still one ~1800-line file. The cross-file
  duplication with `profile.py` is gone (via `engine/common.py`), but
  internal duplication between OFFSET and ZIGZAG dispatch stacks
  remains. A subpackage split (`engine/pocket/{offset,zigzag,
  rest_machining,_shared}.py`) is the planned next refactor; deferred
  from the April 2026 series because it's mechanically large.

## Test fixtures
`tests/fixtures/dxf/` holds hand-crafted and generated DXFs that each
exercise a specific code path — V-notch rest-machining, ZIGZAG
multi-region connector (known-dangerous), analytical offsetter on mixed
line+arc chains, ramp-fallback chain, containment depth-parity, etc.
See `tests/fixtures/dxf/README.md` for the full catalogue. The
`gear_profile.dxf` file is generated by `scripts/generate_gear_dxf.py`
(48-arc 12-tooth spur gear approximation; stresses tangent convex/
concave joins).

## Code Style
- Type hints everywhere
- Pydantic models for all data structures
- Protocol classes for interfaces (PostProcessor, etc.)
- No UI imports in core/ or engine/ modules
- Tests in tests/ mirroring src/ structure
- src-layout (import as `from pymillcam.core import ...`)

## Reference Document
See `docs/pymillcam_plan.md` for the full architecture and planning document (v0.4).
