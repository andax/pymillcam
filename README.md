# PyMillCAM

A Python-based, open-source 2D/2.5D CAM tool for CNC routers and mills.

PyMillCAM fills the gap between simple but limited tools like Estlcam and powerful but complex tools like Fusion 360's CAM. It's wizard-driven for beginners yet fully editable for experienced users.

## Features (Planned)

- **Wizard-driven workflow** — guided setup for profiles, pockets, drilling, engraving, and more
- **Editable operations tree** — every parameter is accessible and adjustable after wizard creation
- **Smart geometry selection** — directional box select, Select Similar (same diameter/layer/type)
- **Safety checks** — Z stack budget validation, travel limit checks, fixture collision detection
- **Feed/speed calculator** — integrated, machine-aware, with material recommendations
- **Time estimation** — per-operation and total machining time displayed in the operations tree
- **Tool library** — with parametric visual rendering, compatible with FreeCAD .fctb format
- **Machine library** — with custom macros for tool change, probing, homing
- **Multiple post-processors** — UCCNC, Mach3, GRBL, LinuxCNC
- **External tool integration** — launch CAMotics, UGS, bCNC, or any tool with one click
- **DXF layer-to-operation mapping** — convention-based auto-setup for fast workflows
- **Nesting** — automatic part layout for minimal material waste
- **G-code preview** — syntax-highlighted with bidirectional viewport linking

## Installation

```bash
# Clone the repository
git clone https://github.com/pymillcam/pymillcam.git
cd pymillcam

# Install in development mode
pip install -e '.[dev]'

# Run
python -m pymillcam
```

## Requirements

- Python 3.11+
- PySide6 (Qt6)
- Shapely, ezdxf, pyclipper, Pydantic, numpy

## Project Status

**Phase 1: Foundation** — Core data models, DXF import, basic profile toolpath, UCCNC post-processor, minimal GUI.

See `docs/pymillcam_plan.docx` for the full architecture and planning document.

## License

LGPL-3.0-or-later
