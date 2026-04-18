# DXF test fixtures

Hand-crafted DXFs that each exercise a specific code path in the
toolpath engine, DXF importer, or analytical offsetter. Kept small
(≤ 50 entities each) so the toolpath is eyeball-verifiable in the
viewport, and grouped here so pytest fixtures can reach them via
`tests/fixtures/dxf/<name>.dxf`.

Not meant as a "try PyMillCAM" showcase — those demos live in
`examples/`. The files here are deliberately boring visually; each
exists to reproduce a specific engineering concern.

| File | What it stresses | Known-limitation reference |
| --- | --- | --- |
| `v_notch_pocket.dxf` | Pocket rest-machining — triangle island tip near outer wall leaves a V-notch the regular passes can't clear. | `CLAUDE.md`: rest-machining, V-notch cleanup. |
| `island_near_concave_wall.dxf` | **Known-dangerous.** ZIGZAG strokes clipped into disjoint pieces by a circular island near a peanut-shaped boundary — current engine emits a feed-at-depth connector across the island. | `CLAUDE.md`: "ZIGZAG strokes that get split by an island into multiple disjoint pieces still use feed-at-depth between pieces." |
| `rounded_rect_with_slot.dxf` | Analytical offsetter on mixed line + tangent-arc chains. Outer rounded rectangle + obround island. | `core/offsetter.py`: line + tangent-arc branch. |
| `gear_profile.dxf` | Dense alternating convex/concave tangent arc joins (48 of them in one closed chain). Regenerate via `scripts/generate_gear_dxf.py`. | `core/offsetter.py`: convex fill vs. concave line-line intersection branches. |
| `full_circle_hole.dxf` | G2/G3 full-circle split. One standalone `CIRCLE` entity. | `core/segments.py::split_full_circle`. |
| `nested_pockets.dxf` | Containment depth-parity to three levels (pocket → island → pocket). | `core/containment.py::build_pocket_regions`. |
| `narrow_slot.dxf` | Ramp-strategy fallback chain — HELICAL doesn't fit, LINEAR short-flanks, PLUNGE last resort. | `engine/pocket.py::_resolve_ramp_strategy`. |
| `bulged_polyline.dxf` | DXF `LWPOLYLINE` with bulge values → `ArcSegment` imports. | `io/dxf_import.py`: bulge decoding. |
| `dogbone_pocket.dxf` | Inside-corner relief arcs — mixed convex/concave join transitions at each dogbone, tangent on both sides. | `core/offsetter.py`: non-trivial convex ↔ concave transitions. |
| `enclosure_top.dxf` | Integration test — profile outer + display pocket + mounting holes, multiple layers. Exercises layer-to-operation auto-mapping once Phase 3 lands. | Phase 3 wizards, DXF layer convention. |

## Regenerating

Only `gear_profile.dxf` has a generator script
(`scripts/generate_gear_dxf.py`). The rest are hand-drawn in
LibreCAD / QCAD. See the drafting spec in the conversation that
produced them.

## Using from a test

```python
from pathlib import Path
from pymillcam.io.dxf_import import import_dxf

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures/dxf"

def test_gear_stitches_to_closed_chain():
    layers = import_dxf(FIXTURES / "gear_profile.dxf", stitch_tolerance=0.001)
    assert len(layers) == 1
    assert layers[0].entities[0].closed
    assert len(layers[0].entities[0].segments) == 96
```

When Phase 3's integration test suite lands, tests for rest-machining,
island handling, multi-region ZIGZAG, ramp fallback, and containment
will read from here rather than generating inline geometry.
