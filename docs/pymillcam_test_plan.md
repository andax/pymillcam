# PyMillCAM Test Plan

Living document. Update as features land and as real machining feedback comes in. Each section lists what's covered automatically, what needs a human to verify, and any known gaps.

## Categories

- **Automated (A)** — `pytest` suite. Runs in CI locally, Claude can verify.
- **Manual visual (M-V)** — Requires a human to eyeball the UI or a plot. Claude cannot verify.
- **Manual G-code (M-G)** — Requires a human to read the G-code or run it through a simulator (e.g. CAMotics, NCViewer, UCCNC's own simulator).
- **Hardware (H)** — Requires running on an actual CNC. Only late-phase.

## Automated test suite

Run via `uv run pytest`. Current count and coverage:

| Module | Tests | What they guard |
|---|---|---|
| `tests/test_core/test_geometry.py` | ~9 | Entity validation, segment-first shape invariants, Shapely shadow derivation, JSON round-trip with arcs |
| `tests/test_core/test_segments.py` | ~15 | LineSegment / ArcSegment math, `segments_to_shapely` chord tolerance, full circle handling |
| `tests/test_core/test_operations.py` | ~3 | ProfileOp defaults, geometry refs, JSON round-trip |
| `tests/test_core/test_project.py` | ~2 | Project with geometry + ops + tool controllers round-trips |
| `tests/test_io/test_dxf_import.py` | ~16 | LINE / LWPOLYLINE (incl. bulges) / POLYLINE / CIRCLE / ARC / POINT, inch-to-mm scaling, seam-crossing arcs, layer grouping |
| `tests/test_io/test_project_io.py` | ~8 | Save/load round-trip, string vs Path, pretty vs compact, error paths |
| `tests/test_engine/test_profile.py` | ~20 | Z-level computation, inside/outside/on-line offsets, stepdown cascade, arc IR emission (CW/CCW), chord_tolerance cascade |
| `tests/test_post/test_uccnc.py` | ~18 | Individual G-code translations, feed-rate modality, coordinate formatting, end-to-end DXF → G-code with arcs preserved |
| `tests/test_ui/test_main_window.py` | ~7 | Main window instantiates, menu structure, dock placement, placeholder actions start disabled |

**Totals: ~101 automated tests. All green. Also covered: `uv run ruff check` and `uv run mypy --strict`.**

### Critical invariants the suite guards against regression

Any change that breaks these should trip a test. If not, add the test.

1. A DXF CIRCLE with `OffsetSide.ON_LINE` produces a single G3 per Z pass — never a stream of chord G1s.
2. LWPOLYLINE bulge = `tan(θ/4)` round-trips into an ArcSegment with correct center, radius, sweep sign.
3. Inch-unit DXF files scale every coordinate (including bulge-derived arc centers/radii) by 25.4.
4. `ProfileOp.chord_tolerance` overrides `ProjectSettings.chord_tolerance` (cascade direction preserved).
5. Project JSON round-trip preserves arc segments as `ArcSegment`, not chord approximations.

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
- **M-G to do when machine macros are wired**: confirm `program_start` / `program_end` / `tool_change` macros from `MachineDefinition` emit correctly.
- **Gap**: we don't yet emit N-numbered lines. Some UCCNC operators prefer them for fault recovery — add an option if requested.

### Step 5 — Minimal PySide6 window  ⬜

Mostly manual; automated coverage is limited to signal/slot smoke tests via pytest-qt.

**Sub-commit 1 — Main window shell**  ✅
- A: `tests/test_ui/test_main_window.py` — instantiation, title, menu order, dock placement, placeholder actions disabled, exit action closes window.
- **M-V to do**: run `uv run pymillcam`, confirm
  - [ ] Window opens with title "PyMillCAM"
  - [ ] Menu bar shows File / Edit / View / Operations in that order
  - [ ] Left dock "Layers & Operations" tree panel visible and resizable
  - [ ] Bottom dock "G-code Output" panel visible and resizable
  - [ ] Central area shows the viewport placeholder text
  - [ ] View menu can toggle tree and output docks

**Sub-commit 2 — Viewport (the hardest to verify)**
- A: Viewport widget instantiates; `set_layers(list[GeometryLayer])` doesn't crash with mixed line/arc content.
- M-V checklist:
  - [ ] A 50 mm circle in a DXF renders as a smooth circle (not a visible polygon).
  - [ ] Y-axis points up (CAD convention), not down (screen convention).
  - [ ] Grid lines at sensible spacing (e.g. 10 mm major, 1 mm minor at normal zoom).
  - [ ] Origin marker visible.
  - [ ] Mouse-wheel zoom centers on the cursor, not the widget center.
  - [ ] Middle-mouse drag pans without inertia / jitter.
  - [ ] Fit-to-view action (keyboard shortcut, e.g. `F`) frames all loaded geometry.
  - [ ] Coordinate readout (mouse X/Y in mm) updates in the status bar.
  - [ ] No visible lag panning/zooming a DXF with 1k+ entities.
  - [ ] Arcs don't facet visibly even when zoomed way in (discretize-on-draw with adaptive tolerance).

**Sub-commit 3 — Tree + DXF import action**
- A: Tree populated from a Project shows expected node counts.
- M-V:
  - [ ] File > Open DXF... opens a dialog, accepts a .dxf file, and populates the viewport + tree.
  - [ ] Tree shows layers as top-level nodes, entities nested under them.
  - [ ] Selecting a tree node highlights the corresponding geometry in the viewport.
  - [ ] Selecting in the viewport highlights the corresponding tree node.

**Sub-commit 4 — Selection + profile op + G-code output**
- A: "Add Profile" action constructs a ProfileOp with expected defaults.
- M-V:
  - [ ] Click-select an entity in the viewport, then Operations > Add Profile, then trigger Generate G-code → output pane shows G-code.
  - [ ] Saving the project via File > Save writes a .pmc file that `load_project` can read back.
  - [ ] Re-generating G-code after editing the op parameters produces updated output (no stale cache).

### Step 6 — Undo/redo command infrastructure  ⬜

- A:
  - Each command type exposes `do()` / `undo()` round-trip invariant: applying do then undo leaves the Project equal to its starting state (use Pydantic equality).
  - Command stack: `undo()` moves a command from done→undone, `redo()` reverses.
  - Multi-step scenarios: 5 commands done, 3 undone, new command clears redo stack.
- M-V: Ctrl+Z / Ctrl+Shift+Z keyboard shortcuts work in the UI and visibly revert changes in the tree + viewport.

### Step 7 — Directional box selection  ⬜

- A: Selection logic given a bounding box and a list of entities returns the right set for L→R (contained) vs R→L (crossing) modes — independent of the UI.
- M-V:
  - [ ] Drag left-to-right → green rectangle, selects only entities fully inside.
  - [ ] Drag right-to-left → blue (or different color) rectangle, selects entities touched by the box.
  - [ ] Visual feedback during drag matches final selection.

### Step 8 — Project save/load as JSON  ✅

- Automated: `tests/test_io/test_project_io.py`.
- **M-V to do when UI lands**: File > Save / Open round-trip produces a project identical to what was on screen.
- **M-G**: open a saved .pmc in a text editor, confirm it's readable and diff-friendly.

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
