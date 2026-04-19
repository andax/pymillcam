# PyMillCAM Test Plan

Living document. Update as features land and as real machining feedback comes in. Each section lists what's covered automatically, what needs a human to verify, and any known gaps.

## Categories

- **Automated (A)** — `pytest` suite. Runs in CI locally, Claude can verify.
- **Manual visual (M-V)** — Requires a human to eyeball the UI or a plot. Claude cannot verify.
- **Manual G-code (M-G)** — Requires a human to read the G-code or run it through a simulator (e.g. CAMotics, NCViewer, UCCNC's own simulator).
- **Hardware (H)** — Requires running on an actual CNC. Only late-phase.

## Automated test suite

Run via `uv run pytest`. Current count and coverage (April 2026):

| Module | Tests | What they guard |
|---|---|---|
| **Core data model** | | |
| `tests/test_core/test_geometry.py` | 9 | Entity validation, segment-first shape invariants, Shapely shadow derivation, JSON round-trip with arcs |
| `tests/test_core/test_segments.py` | 19 | LineSegment / ArcSegment math, `segments_to_shapely` chord tolerance, full circle handling |
| `tests/test_core/test_operations.py` | 3 | ProfileOp / PocketOp / DrillOp defaults, geometry refs, JSON round-trip |
| `tests/test_core/test_project.py` | 2 | Project with geometry + ops + tool controllers round-trips |
| `tests/test_core/test_commands.py` | 8 | Empty stack, push→undo→redo round-trip, new-push clears redo, multi-step math, clear, no-op push dropped |
| `tests/test_core/test_preferences.py` | 7 | Defaults, save/load round-trip, missing file → defaults, malformed JSON raises, atomic write |
| `tests/test_core/test_path_stitching.py` | 15 | Two/three/four-line chains, Y-junction stays unstitched, tolerance window, arc reversal negates sweep, closure snap, label rewrite |
| `tests/test_core/test_offsetter.py` | 12 | Full-circle grow/shrink, square outside rounds corners, square inside trims corners, CW normalisation, rounded rectangle arc centres, too-large raises |
| `tests/test_core/test_containment.py` | 9 | Depth-parity pocket region grouping, nested pockets alternate, disjoint boundaries each get their own region |
| `tests/test_core/test_selection.py` | 15 | `SimilarityMode` semantics (SAME_LAYER / SAME_TYPE / SAME_DIAMETER), full-circle radius predicate, seed-in-result, missing-seed guards |
| `tests/test_core/test_tool_library.py` | 16 | `ToolLibrary` CRUD, id assignment, atomic load/save (tmp+rename), missing-file defaults, malformed JSON raises |
| **Import / export** | | |
| `tests/test_io/test_dxf_import.py` | 17 | LINE / LWPOLYLINE (incl. bulges) / POLYLINE / CIRCLE / ARC / POINT, inch-to-mm scaling, seam-crossing arcs, layer grouping |
| `tests/test_io/test_project_io.py` | 8 | Save/load round-trip, string vs Path, pretty vs compact, error paths |
| **Engine** | | |
| `tests/test_engine/test_common.py` | 32 | Shared resolvers (tool / entity / stepdown / chord / safe / clearance), `z_levels` pass planning, chain walkers, tangent helpers, IR-emit primitives, `error_cls` pass-through |
| `tests/test_engine/test_profile.py` | 51 | Z-levels, inside/outside/on-line offsets, stepdown cascade, G2/G3 emission, chord_tolerance cascade, leads (arc/tangent/direct), on-contour ramp descent/cleanup/ascent, tabs (rect auto-spaced, multi-depth `max(planned, tab)`), climb vs conventional direction |
| `tests/test_engine/test_pocket.py` | 60 | OFFSET rings + ramp entry + fallback chain, ZIGZAG raster + finishing ring + angle rotation, islands (OFFSET ring-groups with retract; ZIGZAG per-wall rings), multi-region connector safety, rest-machining residual set math + reachability filter, adaptive last-pass |
| `tests/test_engine/test_drill.py` | 20 | SIMPLE / PECK / CHIP_BREAK cycle shape in IR, POINT / full-circle / closed-contour target resolution, between-hole clearance traversal, inter-op safe_height, tool-change emission |
| `tests/test_engine/test_tabs.py` | 19 | Arc-length auto-spacing, ramp plateau geometry, Z modulation across passes, coexistence with on-contour ramp |
| `tests/test_engine/test_services.py` | 12 | `ToolpathService` dispatch registry, preview vs toolpath registration, unknown op type raises, engine errors surface once via `EngineError` |
| `tests/test_engine/test_time_estimate.py` | 24 | Rapid / feed / arc time math, dwell + tool-change addition, feed-rate cascade resolution, zero-feed guard |
| `tests/test_engine/test_ir_walker.py` | 6 | Z-only moves drop, rapid+feed kinds, CCW quarter arc, CW full circle, non-motion skipped |
| **Post-processors** | | |
| `tests/test_post/test_uccnc.py` | 20 | G-code translation, feed-rate modality, coordinate formatting, end-to-end DXF → G-code with arcs preserved |
| **UI** | | |
| `tests/test_ui/test_main_window.py` | 73 | Chrome, DXF load, tree↔viewport sync, Add Profile/Pocket/Drill batch + ToolController, Generate G-code, edit→regen invalidates stale toolpath, save/load, profile + toolpath preview, active-op entity tinting, Ctrl+Shift+D duplication with `(copy N)` suffix, Shift+A / Shift+R active-op geometry edit, unified entity context menu (dynamic Select Similar + per-op Add/Remove), tree right-click preserves op selection |
| `tests/test_ui/test_viewport.py` | 25 | Instantiates, set_layers, world↔widget, Y-up, fit-to-view, wheel-zoom, grid spacings, hit-test, programmatic vs interactive selection, arc-angle-within-sweep, profile preview set/clear, show-toggles, active-op overlay |
| `tests/test_ui/test_viewport_arc_rendering.py` | 7 | Chord-polyline sampling (sub-pixel chord sag ≤ 0.5 px), adjacent-arc junctions share widget pixels exactly, no hairline gaps at any zoom |
| `tests/test_ui/test_properties_panel.py` | 46 | Empty placeholder, `FORM_REGISTRY` lookup by op type, populate-doesn't-re-emit, multi-pass / chord-override toggles, Tool dropdown + locked fields when library tool picked, direction combo, drill cycle fields |
| `tests/test_ui/test_box_selection.py` | 14 | `direction_from_drag`, contained vs crossing, inverted box, arc/point handling, invisible layer skip, empty layer list |
| `tests/test_ui/test_preferences_dialog.py` | 4 | Field population, edits round-trip, stitch field disabled when auto-stitch off, no input mutation |
| `tests/test_ui/test_tool_library_dialog.py` | 15 | Add / duplicate / delete / rename tool entries, save/load, dialog doesn't mutate input until OK, atomic write |
| `tests/test_ui/test_wizards_base.py` | 6 | `BaseWizard.apply(project)` runs on Finish not Cancel, `OperationFormPage` embeds the same `OperationFormBase` widget the Properties panel uses |

**Totals: 600 automated tests. All green. Also covered: `uv run ruff check` and `uv run mypy src` (strict).**

### Critical invariants the suite guards against regression

Any change that breaks these should trip a test. If not, add the test.

1. A DXF CIRCLE with `OffsetSide.ON_LINE` produces a single G3 per Z pass — never a stream of chord G1s.
2. LWPOLYLINE bulge = `tan(θ/4)` round-trips into an ArcSegment with correct center, radius, sweep sign.
3. Inch-unit DXF files scale every coordinate (including bulge-derived arc centers/radii) by 25.4.
4. `ProfileOp.chord_tolerance` overrides `ProjectSettings.chord_tolerance` (cascade direction preserved).
5. Project JSON round-trip preserves arc segments as `ArcSegment`, not chord approximations.
6. Full-circle arcs through `segments_to_shapely` close exactly (no sub-picometre residual edge), so `Polygon.buffer` produces a clean offset ring with every vertex at the expected radius.
7. OUTSIDE/INSIDE offset of a circle, line-only polygon, or rounded rectangle goes through the analytical offsetter and preserves arcs (no chord faceting). G-code shows G3 fillets at convex corners.
8. Pocket OFFSET with islands groups disjoint ring-pieces so the engine emits retract+rapid+plunge between them (no feed-through-an-island).
9. Pocket rest-machining stays inside `residual ∩ tool_center_space` — the engine never emits a cleanup ring that would gouge a wall or cross an already-swept region redundantly.
10. Drill IR is expanded G0/G1 (never canned G81/G83); SIMPLE = plunge+retract, PECK = full retract between pecks, CHIP_BREAK = small in-hole retract between pecks.
11. Viewport renders arcs as chord polylines with sub-pixel chord sag (≤ 0.5 px); adjacent-arc junctions share widget pixels exactly so there's no hairline gap on dense geometry like gear teeth.
12. `find_similar_entities` matches same-diameter circles within 0.01 mm; a non-circle seed returns an empty result for `SAME_DIAMETER`; a rectangle whose bounding size happens to match a circle radius is **not** a diameter match.
13. `ToolpathService` dispatches by op type via `register_preview` / `register_toolpath`; `MainWindow` never branches on op type.
14. `Ctrl+Shift+D` duplication preserves geometry refs, deep-copies the `ToolController`, and assigns a unique `(copy)` / `(copy N)` name.
15. Tool library save is atomic (tmp-write + rename) — a crash mid-save never leaves a truncated library on disk.

## Manual verification log

Append rows when human eyeballs have confirmed something works.

| Date | What | Outcome | Notes |
|---|---|---|---|
| 2026-04-13 | End-to-end circle DXF → G3 arcs in UCCNC output (eyeballed by Claude in terminal) | OK | Output matches expectation; one spurious between-pass G1 was found & fixed |

## Phase 1

### Step 1 — Core Pydantic data models  ✅

- Automated: see `tests/test_core/test_{geometry,segments,operations,project}.py`.
- No manual verification needed.

### Step 2 — DXF import  ✅

- Automated: `tests/test_io/test_dxf_import.py` (uses in-memory DXF fixtures via ezdxf).
- **M-V to do when UI lands**: import a real-world DXF from FreeCAD / LibreCAD / Fusion and check that every entity renders.
- **M-V to do**: test a DXF containing a SPLINE and confirm it's skipped without crashing (SPLINE support is deferred).
- **Gap**: we don't yet cover DXFs with nested blocks / INSERTs; add fixtures when that feature lands.

### Step 3 — Basic profile toolpath  ✅

- Automated: `tests/test_engine/test_profile.py`.
- **M-V to do when UI lands**: visualize the toolpath for a non-trivial contour (with re-entrant corners) and confirm the offset looks right.
- **Known limitation**: inside/outside offsets still go through `Polygon.buffer`. Arc-aware offset replacement will need its own test set — at minimum, confirm that an arc-bounded pocket's offset is itself an arc-bounded contour with the expected radii.

### Step 4 — IR + UCCNC post-processor  ✅

- Automated: `tests/test_post/test_uccnc.py` (incl. end-to-end DXF → G-code).
- **M-G to do before running on hardware**: paste the generated G-code into a simulator (CAMotics or UCCNC's preview) and visually confirm:
  - Rapid moves don't plunge through the stock
  - Safe-height and clearance-plane behavior matches intent
  - Full-circle G3 arcs render as circles (not degenerate)
  - Feed rates switch at the expected moments
- **Machine macros** (wired April 2026): automated coverage in
  `tests/test_post/test_uccnc.py` confirms `program_start` / `program_end`
  / `tool_change` macros emit as expected with `{tool_number}`
  substitution. Manual verification — edit the project machine via
  `Edit → Machine…`, paste shop-specific macros, regenerate, and confirm
  the preamble / footer / per-op tool-change lines match.
- **Gap**: we don't yet emit N-numbered lines. Some UCCNC operators prefer them for fault recovery — add an option if requested.

### Step 5 — Minimal PySide6 window  ✅

Automated coverage via pytest-qt is now extensive (`test_main_window.py` alone has 73 tests).
Manual checklists below are retained so visual correctness gets a human pass whenever that area is touched.

**Sub-commit 1 — Main window shell**  ✅
- A: `tests/test_ui/test_main_window.py` — instantiation, title, menu order, dock placement, placeholder actions disabled, exit action closes window.
- **M-V to do**: run `uv run pymillcam`, confirm
  - [ ] Window opens with title "PyMillCAM"
  - [ ] Menu bar shows File / Edit / View / Operations in that order
  - [ ] Left dock "Layers & Operations" tree panel visible and resizable
  - [ ] Bottom dock "G-code Output" panel visible and resizable
  - [ ] Central area shows the viewport placeholder text
  - [ ] View menu can toggle tree and output docks

**Sub-commit 2 — Viewport (the hardest to verify)**  ✅
- A: `tests/test_ui/test_viewport.py` — instantiation, mixed line/arc `set_layers`, world↔widget transform, Y-up invariant, `fit_to_view` centring, wheel-zoom preserves cursor world point, grid-spacing heuristic, `mouse_position_changed` signal fires.
- Implementation note: arcs render as chord polylines sampled to sub-pixel chord sag (≤ 0.5 px). An earlier version used `QPainter.drawArc` (1/16° integer quantisation) and `QPainterPath.arcTo` (Bézier approximation) but both produced hairline gaps at adjacent-arc junctions on dense geometry (e.g. gear teeth). `test_viewport_arc_rendering.py` guards the replacement.
- **M-V to do** (human verification — run `uv run pymillcam`, load a DXF):
  - [ ] A 50 mm circle renders as a smooth circle (not a visible polygon), even when zoomed way in.
  - [ ] Y-axis points up (CAD convention): world (0, 10) appears above world (0, 0).
  - [ ] Red X-axis and green Y-axis are visible and cross at the origin marker.
  - [ ] Grid lines at sensible spacing — spacing adapts as you zoom.
  - [ ] Mouse-wheel zoom centres on the cursor, not the widget centre.
  - [ ] Middle-mouse drag pans without jitter.
  - [ ] Pressing `F` frames all loaded geometry with a small margin.
  - [ ] Status bar coordinate readout (`X: …  Y: …`) updates as the mouse moves.
  - [ ] No visible lag panning/zooming a DXF with 1k+ entities.

**Sub-commit 3 — Tree + DXF import action**  ✅
- A: `tests/test_ui/test_main_window.py` — DXF load populates project, tree, viewport; tree node counts match layer entity counts; tree↔viewport selection round-trips both ways without recursion.
- A: `tests/test_ui/test_viewport.py` — `_hit_test` picks nearest entity within tolerance, `set_selected` doesn't re-emit, stale selection is dropped on `set_layers`.
- **M-V to do**:
  - [ ] File > Open DXF... opens a dialog, accepts a .dxf file, and populates the viewport + tree.
  - [ ] Tree shows layers as top-level nodes (with entity counts), entities nested under them.
  - [ ] Selecting a tree entity highlights the corresponding geometry in the viewport (bright cyan, drawn on top).
  - [ ] Left-clicking near an entity in the viewport highlights it AND selects its tree node.
  - [ ] Left-clicking on empty space clears both selections.
  - [ ] Importing a second DXF replaces the first (single-project semantics).
- **Known gap (out of sub-commit 3 scope, future feature):** DXFs whose
  contours are authored as separate `LINE` entities import as one
  `GeometryEntity` per line. We need a "Join paths" action (and possibly
  an opt-in stitch-on-import) before such DXFs can be profiled with one
  selection. Users with `LWPOLYLINE`/`POLYLINE` DXFs are unaffected.

**Sub-commit 4 — Selection + profile op + G-code output + properties + save/load**  ✅
- A: `tests/test_ui/test_main_window.py` — Add Profile creates op + default ToolController, Generate G-code fills output, edit-then-regen produces different output (no stale cache), Save→Load round-trip.
- A: `tests/test_ui/test_properties_panel.py` — empty placeholder, fields populate from op, edits update model + emit signal, multi-pass / chord-override toggles, populate doesn't re-emit.
- **M-V to do**:
  - [ ] Click-select an entity → Operations > Add Profile → an "Operations" group appears in the tree with a "Profile 1 [profile]" leaf.
  - [ ] Selecting the new op shows its fields in the right-side Properties dock.
  - [ ] Operations > Generate G-code fills the bottom output pane with G-code starting `G21 G90 G94 G17`.
  - [ ] Editing the cut depth (or any field) and re-generating shows a different G-code body.
  - [ ] File > Save As writes a .pmc file; closing and File > Open Project on it restores the same tree, ops, and viewport content.
  - [ ] An entity's first ProfileOp action also creates a default 3 mm endmill ToolController in the project; each subsequent Add Profile gets its own ToolController with the next tool number.
  - [ ] Selecting an op shows its tool diameter in the Properties panel; editing it changes the live profile preview.
  - [ ] Live **profile preview** (orange polyline) appears the moment an op is selected and updates with every property edit. ON_LINE just traces the source contour; OUTSIDE / INSIDE show the offset by tool radius. Failing offsets (e.g. inside larger than feature) blank the preview without a popup.
  - [ ] Generate G-code populates the **toolpath preview** (magenta feeds + dashed cyan rapids); editing any op afterwards clears it (it's stale).
  - [ ] View > Show profile preview / Show toolpath preview toggle the overlays without losing the underlying data.

### Step 6 — Undo/redo command infrastructure  ✅

Implemented as a snapshot-based stack: each entry holds `(description, before_dict, after_dict)` of `Project.model_dump`. Concrete `Command` subclasses can replace this when project size makes whole-state snapshots expensive — public API of `CommandStack` won't change.

- A: `tests/test_core/test_commands.py` — stack invariants, push/undo/redo, redo cleared on new push, multi-step done/undone arithmetic.
- A: `tests/test_ui/test_main_window.py` — undo Add Profile removes both the op and its ToolController; redo restores; delete operation is undoable; coalesced property edits collapse to one undo step; in-progress edit reverts cleanly without recording; loading a project clears history.
- **M-V to do**:
  - [ ] Ctrl+Z / Ctrl+Shift+Z visibly revert in tree + viewport (Add Profile, Delete operation, property edits).
  - [ ] Edit menu shows the action that will be undone/redone in the label (e.g. "Undo Add Profile").
  - [ ] Rapid spinbox jiggles count as one undo step, not many (400 ms idle commits the coalesced edit).
  - [ ] Pressing Ctrl+Z while typing in a field reverts to the bind-time snapshot rather than partial keystrokes.
  - [ ] Loading a project (File > Open Project) clears Edit menu's undo/redo state.

### Step 7 — Directional box selection  ✅

- A: `tests/test_ui/test_box_selection.py` — pure `select_in_box` matches CONTAINED vs CROSSING semantics for lines, arcs, and points; handles inverted rect, invisible layers, empty input.
- A: `tests/test_ui/test_viewport.py` — drag L→R picks only contained entities; drag R→L picks crossing; sub-threshold movement still treated as a click; click on empty space clears selection.
- The viewport's selection model is now a list of `(layer_name, entity_id)` pairs; the layers/operations tree uses ExtendedSelection so Ctrl-clicking and box-selection both produce multi-select.
- Add Profile creates one ProfileOp per selected entity in a single batch, all sharing one fresh ToolController — undoable in one step.
- **M-V to do**:
  - [ ] Drag left → right paints a green solid-outline rectangle; release selects only entities fully inside it.
  - [ ] Drag right → left paints a blue dashed-outline rectangle; release selects everything the box touches.
  - [ ] Multi-selected entities all draw highlighted in the viewport and all show selected in the tree.
  - [ ] Ctrl-click in the tree adds/removes entries from the selection.
  - [ ] Add Profile with multiple entities selected creates one op per entity (all sharing one ToolController); undo removes them all in one step.
  - [ ] A single quick click anywhere still works as before (single-select or clear).

### Step 8 — Project save/load as JSON  ✅

- Automated: `tests/test_io/test_project_io.py`.
- **M-V to do when UI lands**: File > Save / Open round-trip produces a project identical to what was on screen.
- **M-G**: open a saved .pmc in a text editor, confirm it's readable and diff-friendly.

## Phase 2

Phase 2 items are covered by automated tests — see the module table above. Manual verification checklists here for the UI-visible pieces; tick off in the log when a human has confirmed them.

### Leads + on-contour ramp  ✅
- A: `test_engine/test_profile.py` (leads, ramp descent/cleanup/ascent).
- **M-V**: preview draws leads on top of the contour; ramp descent slice is visibly sloped in the toolpath preview.

### Pocket OFFSET + ZIGZAG + islands + rest-machining  ✅
- A: `test_engine/test_pocket.py` (60 tests).
- **M-G**: generate a multi-region pocket with islands → open the G-code in CAMotics / UCCNC preview → confirm retract between disjoint groups and no feed-through-an-island.
- **M-G**: V-notch fixture (`tests/fixtures/dxf/vnotch_pocket.dxf`) → generate with `rest_machining=True` → confirm cleanup passes stay inside the notch.

### Profile tabs  ✅
- A: `test_engine/test_tabs.py`.
- **M-V**: in multi-pass mode, confirm tabs render visibly raised in the toolpath preview on the final pass.

### Drill operation  ✅
- A: `test_engine/test_drill.py`, `test_ui/test_main_window.py`.
- **M-G**: generate PECK and CHIP_BREAK cycles → confirm IR expands correctly (no G81/G83).
- **M-V**: drill targets accept POINT entities AND closed-circle selection in the UI.

### Tool library  ✅
- A: `test_core/test_tool_library.py`, `test_ui/test_tool_library_dialog.py`, Tool dropdown in `test_properties_panel.py`.
- **M-V**: Edit > Tool library → add / duplicate / rename tool → restart app → tool persists.
- **M-V**: selecting a library tool on an op locks the tool-geometry fields; editing library tool in dialog does NOT retroactively change existing ops (known soft-link behaviour).

### Select Similar  ✅
- A: `test_core/test_selection.py`, `test_ui/test_main_window.py` (unified menu).
- **M-V**: right-click a full-circle → "Select similar diameter" picks all same-diameter circles across layers.
- **M-V**: right-click a line → the "Select similar diameter" entry does NOT appear (dynamic menu).

### Operation duplication  ✅
- A: `test_ui/test_main_window.py`.
- **M-V**: Ctrl+Shift+D → new op gets `(copy)` suffix; a second duplicate gets `(copy 2)`.
- **M-V**: duplicated op's ToolController edits don't leak back to the original.

### Active-op entity highlight + unified context menu  ✅
- A: `test_ui/test_main_window.py`, `test_ui/test_viewport.py`.
- **M-V**: selecting an op in the tree tints its member rows + overlays them in the viewport (green).
- **M-V**: right-click an entity anywhere → same menu from either surface; items that don't apply don't appear.
- **M-V**: Shift+A / Shift+R add/remove viewport-selected entities to/from the active op.

### Operation time estimate  ✅
- A: `test_engine/test_time_estimate.py`.
- **M-V**: each op row in the tree shows `[hh:mm:ss]`; edits recompute.

## Integration / end-to-end scenarios

Expand as scenarios accumulate. Each should be runnable as either a pytest test or a manual sequence in the UI.

| Scenario | Automated? | Notes |
|---|---|---|
| DXF circle → profile on-line → G-code with two full-circle G3s | ✅ (`test_end_to_end_dxf_to_gcode_has_arcs_for_circular_contour`) | Guards arc preservation across the whole pipeline |
| DXF rectangle → profile outside → G-code with no arcs | ✅ (`test_end_to_end_rectangle_profile_emits_linear_moves`) | Confirms offset path integrates cleanly |
| LWPOLYLINE with bulges → profile on-line → correct arcs in G-code | ⬜ (add when bulges hit a real test) | Should prove the bulge-to-arc math survives end-to-end |
| Project saved to disk → reopened → same G-code generated | ⬜ | Round-trip durability |
| DXF in inch units → G-code with scaled mm coordinates | ⬜ | Unit conversion across the pipeline |
| Inside profile with tool larger than feature → friendly error, no G-code emitted | ✅ (`test_inside_offset_too_large_raises`) | Currently at engine level; when UI lands, confirm the error surfaces to the user |

## Sample fixtures

When we accumulate real-world DXFs worth regression-testing against, drop them under `tests/fixtures/` and document here. Currently: all DXFs in tests are built in-memory with ezdxf, so no binary artifacts live in the repo.

Candidates to collect once the UI makes it easy:
- Part with mixed lines, arcs, and circles in multiple layers.
- Part exported from FreeCAD TechDraw with the Profile_Outside / Pocket_Xmm layer naming convention.
- Part exported from LibreCAD.
- Part with SPLINEs (to confirm graceful skip).
- Part with nested INSERTs / blocks (for later).

## Running the suite

```bash
uv run pytest                      # all tests
uv run pytest -x                   # stop on first failure
uv run pytest -k profile           # just profile-related tests
uv run ruff check src tests        # lint
uv run mypy src                    # strict type check
```

## Maintenance rules

- A new feature without a test is incomplete. Exception: UI-visible behavior that genuinely can't be automated — log it under Manual verification.
- When a bug is found in code that has a test, add a regression test *first*, reproduce the failure, then fix.
- Remove checklist items from the "M-V to do" lists as they're verified — move them to the Manual verification log with a date.
- Treat this document as code: review it when PRs land, prune stale items.
