# PyMillCAM

A Python-based, open-source 2D/2.5D CAM tool for CNC routers and mills.

PyMillCAM fills the gap between simple but limited tools like Estlcam and powerful but complex tools like Fusion 360's CAM. The goal is a wizard-driven tool for beginners that remains fully editable for experienced users.

> **Status:** active early development. Phase 1 (foundations) is complete and
> a good chunk of Phase 2 has landed. The app is usable end-to-end for
> **outside/inside profile cutouts** (with leads, on-contour ramp entry,
> and tabs), **pockets** (concentric-offset and zigzag strategies, islands,
> rest-machining for V-notch corners), and **drilling** (simple / peck /
> chip-break cycles), exported as **UCCNC G-code**. A shared **tool library**
> (JSON), **Select Similar**, operation **duplication** and per-op **time
> estimates** in the ops tree are in. Other posts (Mach3, GRBL, LinuxCNC),
> wizards, machine macros, safety checks and built-in simulation are not
> built yet — see [Roadmap](#roadmap) below.

## What works today

- **DXF import.** Lines, arcs, circles, LWPOLYLINE with bulges. Arcs are
  preserved as arcs end-to-end (they survive into the G-code as G2/G3,
  not chord-approximated).
- **Path stitching.** Open segments that meet at their endpoints can be
  welded with `Operations > Join paths`, or automatically on import via
  `Edit > Preferences > Auto-stitch on DXF import`.
- **Profile toolpath.** Outside / inside / on-line offsets, climb or
  conventional direction, multi-depth stepping. Analytical arc-preserving
  offsetter for circles and line+tangent-arc contours (rounded rectangles);
  falls back to Shapely's buffer otherwise.
- **Pocket toolpath.** Three strategies — **offset** (concentric inward
  rings, arc-preserving for boundaries the analytical offsetter handles),
  **zigzag** (parallel raster strokes with a finishing contour ring,
  configurable angle), and **spiral** (same rings as offset but walked
  inner → outer with feed-at-depth bridges, so the path is a single
  continuous spiral from the pocket interior outward — lower cycle time,
  fewer floor witness marks). Multi-depth stepping with retract-to-
  clearance between passes. Ramp entries: **linear** (default — last
  slice of the first ring, so the full ring cuts flat at pass depth),
  **helical** (spiral tangent to ring-start); fallback chain helical →
  linear → plunge. **Islands** (holes inside the pocket) are detected
  from selection by a containment tree, with retract+rapid between
  disjoint ring groups. **Rest-machining** cleans up V-notch corners
  where an island grows close to the boundary.
- **Drill operation.** Three cycle types: **simple** (one plunge per
  hole), **peck** (full retract between pecks for chip clearance),
  **chip-break** (small in-hole retract to snap the chip). Drill targets
  can be POINT entities, closed circles, or closed contours — the engine
  resolves each target to a centre (exact for full-circle arcs; Shapely
  centroid for closed contours). Multi-hole per operation; between-hole
  traversal at the clearance plane. Expanded G0/G1 IR (not canned
  G81/G83) for post-processor portability.
- **Profile tabs.** Rectangular tabs, auto-spaced by arc-length, tuned
  with count / width / height / ramp length. Multi-depth aware — on
  passes that would cut through the tab, Z modulates to
  ``max(planned_z, tab_z)`` so the tab plateau survives while
  lead-in/out and on-contour ramps still work normally.
- **Lead-in / lead-out.** Arc, tangent, or direct styles, traversed at the
  stock surface (Z=0) so the plunge witness mark lands in air.
- **On-contour ramp entry.** Each pass descends along the contour at a
  fixed angle from the previous depth to the new depth — no plunging into
  material, no between-pass retract. After the final pass, a cleanup slice
  re-cuts the sloped groove at full depth and a fixed-angle ascent rises
  back to the surface before the lead-out.
- **UCCNC G-code output.** Emits G2/G3 with helical Z for ramps, feed
  modality, tool change and spindle commands. **Machine macros**
  (`program_start`, `program_end`, `tool_change`) from the project's
  `MachineDefinition` are threaded through the post so shops can swap
  in their own preamble, parking routine, and ATC-vs-manual tool change
  without forking the post-processor. `{tool_number}` is substituted
  inside `tool_change`.
- **Tool library.** JSON-backed (`~/.config/PyMillCAM/tool_library.json`),
  atomic save (crash-safe). Edit > Tool library opens a dialog to add /
  duplicate / rename / delete entries. The Properties panel has a Tool
  dropdown on each operation; selecting a library tool locks the
  tool-geometry fields so edits happen in one place.
- **Select Similar.** Right-click any entity (tree or viewport) → pick
  *same layer* / *same geometry type* / *same diameter* (circles only,
  0.01 mm tolerance). Critical for selecting 200 identical mounting
  holes in one click.
- **Operation duplication.** `Ctrl+Shift+D` clones the selected op with
  a unique `(copy)` / `(copy N)` suffix. Use case: spot drill → peck
  drill → ream on the same holes, each with its own tool and cycle.
- **Active-op geometry editing.** Selecting an op tints its member
  entities (green) in the viewport and the tree. `Shift+A` / `Shift+R`
  add/remove the current viewport selection to the active op — or use
  the same actions from the unified right-click menu on any entity.
- **Per-op time estimate.** Each op row in the tree shows an `[hh:mm:ss]`
  estimate (rapids + feeds + arcs + dwell + tool-change), recomputed
  on project change.
- **PySide6 GUI.** 2D viewport with pan / zoom / fit, directional box
  selection (L→R contained, R→L crossing) with `Ctrl`/`Shift` modifiers
  for multi-select, operations tree, Properties panel, G-code output pane,
  undo / redo with command coalescing on property edits, project save/load
  as JSON (`.pmc`), and a toolbar + keyboard shortcuts for the common
  actions (see below).

## What's coming

See [`docs/pymillcam_plan.md`](docs/pymillcam_plan.md) for the full roadmap.
Short version:

- User-selectable contour start position (so lead / ramp marks land in scrap)
- Machine library + editor dialog (macros already flow through the post;
  users just can't edit them from the UI yet)
- Feed/speed calculator (contextual, in the tool picker)
- FreeCAD `.fctb` / `.fctl` and LinuxCNC tool-table import into the tool library
- Wizards (Sheet Cutout, Pocket, Drill Pattern, …) — scaffold in place
- Pre-flight safety (Z stack budget, travel, fixture collision)
- Built-in simulator
- Mach3 / GRBL / LinuxCNC post-processors

## Installation

PyMillCAM uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
# Install uv (once, if you don't already have it)
curl -LsSf https://astral.sh/uv/install.sh | sh    # Linux/macOS
# Or: winget install --id=astral-sh.uv -e          # Windows

# Clone and run
git clone https://github.com/pymillcam/pymillcam.git
cd pymillcam
uv sync                    # creates .venv and installs all deps
uv run pymillcam           # launch the GUI
```

No separate install step — `uv sync` handles the virtualenv and deps in
one go.

### Requirements

- Python 3.11+ (installed automatically by `uv sync`)
- A desktop environment with Qt6 support (Linux, macOS, Windows)

## Getting started

The quickest path from "just cloned" to "G-code in hand":

1. `uv run pymillcam`
2. `File > Open Project…` (`Ctrl+Shift+O`) → select
   `examples/circle_cutout.pmc`.
3. `Operations > Generate G-code` (`Ctrl+G`, or the play-arrow button in
   the toolbar). The bottom pane fills with UCCNC G-code. The viewport
   shows the toolpath overlay in magenta.
4. Click the operation in the tree, then adjust values in the Properties
   panel (cut depth, stepdown, lead-in style, ramp angle). The preview
   updates live; regenerate to refresh the G-code.

See [`examples/README.md`](examples/README.md) for more samples and the
"start from a DXF" walkthrough.

### Keyboard shortcuts

| Action | Shortcut |
| --- | --- |
| Open DXF | `Ctrl+O` |
| Open Project | `Ctrl+Shift+O` |
| Save / Save As | `Ctrl+S` / `Ctrl+Shift+S` |
| Undo / Redo | `Ctrl+Z` / `Ctrl+Shift+Z` |
| Preferences | `Ctrl+,` |
| Fit to View | `F` |
| Join paths | `Ctrl+J` |
| Add Profile | `Ctrl+P` |
| Add Pocket | `Ctrl+K` |
| Add Drill | `Ctrl+D` |
| Duplicate operation | `Ctrl+Shift+D` |
| Add to active op | `Shift+A` |
| Remove from active op | `Shift+R` |
| Delete operation | `Del` |
| Generate G-code | `Ctrl+G` |

## Testing and feedback

If you're trying PyMillCAM as a tester, things that are especially useful
to hear about:

- DXFs that fail to import cleanly (attach the file).
- G-code that your controller doesn't accept or that cuts wrong —
  attach the `.pmc`, the DXF, and the generated G-code.
- UI flows that are confusing or assume too much.
- What's the smallest real job you'd want to do with this, and what's
  blocking it today?

Please file issues on GitHub. For bug reports, a minimal `.pmc` that
reproduces the problem is worth 1,000 words.

## Developer notes

```bash
uv run pytest              # full test suite (fast — a couple of seconds)
uv run ruff check .        # lint
uv run mypy src            # strict type-check
```

Architecture is documented in [`CLAUDE.md`](CLAUDE.md) and in much more
detail in [`docs/pymillcam_plan.md`](docs/pymillcam_plan.md).

## License

LGPL-3.0-or-later
