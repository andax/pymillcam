# PyMillCAM

A Python-based, open-source 2D/2.5D CAM tool for CNC routers and mills.

PyMillCAM fills the gap between simple but limited tools like Estlcam and powerful but complex tools like Fusion 360's CAM. The goal is a wizard-driven tool for beginners that remains fully editable for experienced users.

> **Status:** active early development. Phase 1 (foundations) is complete
> and most of Phase 2 has landed. The app is usable end-to-end for
> **outside/inside profile cutouts** (with leads, on-contour ramp entry,
> and tabs), **pockets** (offset / zigzag / spiral strategies, islands,
> rest-machining for V-notch corners), and **drilling** (simple / peck /
> chip-break cycles), exported as **UCCNC G-code** with shop-specific
> preamble / footer / tool-change **macros**. A shared **tool library**
> (JSON), **Select Similar**, operation **duplication** and **reorder**,
> per-op **time estimates** in the ops tree, and **per-op G-code export**
> are in, with **UCCNC** and **GRBL** post-processors both shipped
> (the post is picked automatically from the project's machine
> controller). Other posts (Mach3, LinuxCNC), wizards, safety checks,
> and built-in simulation are not built yet — see
> [What's coming](#whats-coming) below.

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
- **UCCNC and GRBL G-code output.** Emits G2/G3 with helical Z for ramps,
  feed modality, tool change and spindle commands. The post is selected
  automatically from the project's machine `controller` (pick
  `uccnc` or `grbl` in the Machine dialog's controller dropdown).
  GRBL defaults to a manual tool-change pause (`M5` + `M0`) since stock
  GRBL has no `M6` handler; UCCNC emits `T<n> M6`. **Machine macros**
  (`program_start`, `program_end`, `tool_change`) override those
  defaults per-project, so shops can swap in their own preamble,
  parking routine, and ATC sequences without forking the post.
  `{tool_number}` is substituted inside `tool_change`.
- **Tool library.** JSON-backed (`~/.config/PyMillCAM/tool_library.json`),
  atomic save (crash-safe). Edit > Tool library opens a dialog to add /
  duplicate / rename / delete entries. The Properties panel has a Tool
  dropdown on each operation; selecting a library tool locks the
  tool-geometry fields so edits happen in one place.
- **Select Similar.** Right-click any entity (tree or viewport) → pick
  *same layer* / *same geometry type* / *same diameter* (circles only,
  0.01 mm tolerance). Critical for selecting 200 identical mounting
  holes in one click.
- **Operation duplication & reordering.** `Ctrl+Shift+D` clones the
  selected op with a unique `(copy)` / `(copy N)` suffix — useful for
  spot drill → peck drill → ream on the same holes, each with its own
  tool and cycle. `Ctrl+Shift+Up` / `Ctrl+Shift+Down` move an op in the
  execution order; ops run top-to-bottom in generated G-code.
- **Per-op G-code.** Select an op in the tree and press `Ctrl+G` to
  generate a standalone program for just that op (preamble + footer
  included); select the *Operations* group (or nothing) to generate the
  combined program. Right-click an op → **Export G-code…** writes that
  single op to a `.nc` file.
- **Machine editor.** `Edit → Machine…` opens a dialog bound to the
  project's machine. Edit the name, controller, and the three macro
  slots that the post substitutes into the program (preamble,
  footer, tool change). `{tool_number}` expands inside *Tool change*.
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
- Machine library (multiple saved machines you can switch between; the
  per-project machine dialog is already in)
- Feed/speed calculator (contextual, in the tool picker)
- FreeCAD `.fctb` / `.fctl` and LinuxCNC tool-table import into the tool library
- Wizards (Sheet Cutout, Pocket, Drill Pattern, …) — scaffold in place
- Pre-flight safety (Z stack budget, travel, fixture collision)
- Built-in simulator
- Mach3 / LinuxCNC post-processors

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

### 30-second path: run the bundled example

1. `uv run pymillcam`
2. `File → Open Project…` (`Ctrl+Shift+O`) → select
   `examples/circle_cutout.pmc`.
3. `Operations → Generate G-code` (`Ctrl+G`, or the play-arrow in the
   toolbar). The bottom pane fills with UCCNC G-code; the viewport
   shows the toolpath overlay in magenta.
4. Click the operation in the tree, then adjust values in the Properties
   panel (cut depth, stepdown, lead-in style, ramp angle). The orange
   preview updates live as you type; press `Ctrl+G` to regenerate the
   G-code.

### Full walkthrough: from a fresh DXF to G-code on the machine

This is the workflow for a basic job. It assumes you have a DXF with a
closed contour — a circle, rectangle, or anything closed — and want to
profile-cut it out of a sheet.

**1. Import the DXF.** `File → Open DXF…` (`Ctrl+O`). PyMillCAM reads
lines, arcs, circles, and bulged LWPOLYLINEs; arcs stay as arcs all the
way through to G2/G3 in the output. The viewport fits the drawing
automatically. If your DXF was drawn with separate unstitched lines
(common for exports from Inkscape and some CAD tools), turn on
`Edit → Preferences → Auto-stitch on DXF import` or run
`Operations → Join paths` (`Ctrl+J`) on the selected entities afterward.

**2. Configure your machine (one-time).** `Edit → Machine…` opens the
Machine dialog. Paste your controller's preamble, footer, and
tool-change sequences into the three macro boxes. The defaults match
UCCNC's standard behaviour (`G21 G90 G94 G17` / `M5 M30` / `T<n> M6`);
most shops add a park move and spindle-off routine. Use
`{tool_number}` inside *Tool change* to insert the target tool
number — handy for both ATC and manual-change setups. The machine is
saved with the project so different jobs can target different machines.

**3. Select the geometry for the first operation.** Drag a box in the
viewport (left-to-right selects fully-contained entities;
right-to-left selects anything crossed by the box) — or click
individual entities. Hold `Ctrl` or `Shift` to add to the selection.
The left-side tree mirrors what's selected.

**4. Add an operation.** Pick the op type that matches the cut:

- **Profile** (`Ctrl+P`) — cut along a contour. Choose *outside*,
  *inside*, or *on-line* in the Properties panel.
- **Pocket** (`Ctrl+K`) — clear the area enclosed by a closed contour.
  Pick *offset*, *zigzag*, or *spiral* as the clearing strategy.
- **Drill** (`Ctrl+D`) — drill at a point, a closed circle centre, or
  the centroid of a closed contour. Pick *simple*, *peck*, or
  *chip-break* as the cycle.

The operation appears in the *Operations* tree group. Its member
entities tint green in the viewport.

**5. Set the operation parameters.** With the op selected, the
Properties panel on the right shows its fields. At minimum you need:

- **Name** — make it descriptive (`"Outer cutout"`, `"6mm dowel holes"`).
  The name shows in the G-code as a comment header.
- **Tool** — pick from the Tool dropdown (library tools) or leave as
  `(Custom)` and edit the fields inline.
- **Cut depth** (negative) — typically `-stock_thickness` for a
  cutout, `-3` to `-5 mm` for a pocket.
- **Stepdown** — how deep each pass cuts. Enable *Multi-pass* if
  `cut_depth / stepdown > 1`.

Profile-specific: offset side, lead-in/out style, ramp strategy, tabs.
Pocket-specific: stepover, strategy, zigzag angle, ramp strategy.
Drill-specific: cycle, peck depth, dwell.

**6. Add more operations if needed.** Repeat steps 3–5 for each
operation in the job. Reorder with `Ctrl+Shift+Up` / `Ctrl+Shift+Down`
(or via the op row's right-click menu) — ops execute top-to-bottom
in the generated G-code. A typical plate with mounting holes runs:
*drill pilots → drill final → pocket recesses → profile cutout*.

**7. Generate G-code.** `Ctrl+G` with nothing selected (or the
*Operations* group selected) writes a full program covering every op;
with one op row selected it writes just that op as a standalone
program. The viewport shows rapids as dashed cyan and feeds as solid
magenta.

**8. Export G-code to your machine.** The bottom pane's text is the
complete program — copy-paste, or right-click any op in the tree →
**Export G-code…** to write just that one op to a `.nc` file. Save
the whole project (`Ctrl+S`) so you can come back to it.

See [`examples/README.md`](examples/README.md) for more sample files
and follow-ups (DXFs with islands, pocket + drill combos, etc.).

## Operations at a glance

| If you want to… | Use | Key strategy options |
| --- | --- | --- |
| Cut a contour free of the sheet | **Profile** | *outside* offset, arc lead-in, tabs |
| Clear area inside a closed shape | **Pocket** | *offset* for arc-preserving rings, *zigzag* for flat floor, *spiral* for no-retract continuous path |
| Drill a set of holes | **Drill** | *simple* for through-holes, *peck* for deep holes, *chip-break* for stringy chips |

Two things that cross all op types:

- **Ramp entry** (Profile / Pocket) — how the cutter enters each depth
  step. *Helical* for pockets with room; *linear* on-contour for open
  contours and tight pockets; *plunge* only for centre-cutting tools or
  pre-drilled starter holes. The engine falls back automatically
  (helical → linear → plunge) if the requested strategy doesn't fit.
- **Multi-depth** — break a deep cut into `stepdown`-sized passes.
  Retracts to the clearance plane between passes, except for on-contour
  ramp which stays at depth.

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
| Move operation up / down | `Ctrl+Shift+Up` / `Ctrl+Shift+Down` |
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
