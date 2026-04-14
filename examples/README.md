# Examples

A few sample files to try PyMillCAM with. All DXFs are in millimetres.

| File | What it is | Suggested use |
| --- | --- | --- |
| `50mm_circle.dxf` | A single 50 mm-diameter circle | Smallest working test — outside-profile cutout |
| `motor_section_wall.dxf` | A more complex wall section with holes | Exercises inside/outside offsets and path stitching |
| `circle_cutout.pmc` | Ready-to-run PyMillCAM project for `50mm_circle.dxf` | `File > Open` then `Generate` to see G-code |

## Try it quickly

1. Open PyMillCAM: `uv run pymillcam`
2. `File > Open Project…` (`Ctrl+Shift+O`) → select
   `examples/circle_cutout.pmc`
3. The operations tree shows a pre-configured outside-profile op. Use
   `Operations > Generate G-code` (`Ctrl+G`, or the play-arrow in the
   toolbar) — the bottom pane shows UCCNC G-code with arc leads and
   on-contour ramp descent.
4. Edit values in the Properties panel on the right (cut depth, ramp angle,
   lead length) and regenerate to see the changes.

## Or import a DXF from scratch

1. `File > Open DXF…` (`Ctrl+O`) → select `examples/50mm_circle.dxf`
2. In the viewport, box-select the circle (drag left-to-right for contained,
   right-to-left for crossing).
3. `Operations > Add Profile` (`Ctrl+P`).
4. Set cut depth and tool diameter in the Properties panel, then
   `Operations > Generate G-code` (`Ctrl+G`).
