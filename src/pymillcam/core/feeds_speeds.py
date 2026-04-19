"""Feeds-and-speeds calculator for starting cutting parameters.

Computes an **initial** RPM + feed rate given a tool's diameter, flute
count, and the material being cut. The values come from the classic
cutting-speed formulae:

    RPM   = Vc × 1000 / (π × D)            (D in mm, Vc in m/min)
    feed  = fz × Z × RPM                   (fz in mm/tooth, Z = flute count)

where ``Vc`` (surface speed) and ``fz`` (chipload per tooth) are the
material-specific numbers the ``MaterialPreset`` entries below carry.

These are starting values for hobby-class machines (router/mill,
moderate rigidity). Users should watch, listen, and adjust — no table
replaces the feedback loop of the machine sounding right. The table
leans conservative for softer materials and more conservative still
for metals, since aggressive defaults on a flexy gantry snap tools.
"""
from __future__ import annotations

import math

from pydantic import BaseModel, Field


class MaterialPreset(BaseModel):
    """A cutting-data preset for one material.

    ``surface_speed_m_min`` is the recommended Vc (surface speed, m/min);
    ``chipload_per_tooth_mm`` is fz (mm per flute per revolution),
    calibrated for a small-diameter end mill on a hobby-class machine.
    """

    name: str
    surface_speed_m_min: float = Field(gt=0.0)
    chipload_per_tooth_mm: float = Field(gt=0.0)


# Starting values for hobby-class CNC routers / mills cutting with a
# 1-6 mm carbide end mill. Numbers come from widely-published rules
# of thumb (Onsrud, Whitney, GWizard hobby-mode ranges, FreeCAD's
# defaults) and reflect the small-diameter + flexy-gantry constraint
# — industrial rigid mills can run most of these 2-3× faster. Anyone
# in that bucket overrides via the library editor.
#
# The two aluminium rows encode a real difference: pure / 1100-series
# "soft" aluminium is gummy and prone to built-up edge, so it wants
# a *lower* surface speed and chipload than 6061-T6 even though the
# latter is mechanically stronger. The gap used to be backwards (soft
# was listed 40% slower than 6061); that's fixed.
DEFAULT_MATERIALS: list[MaterialPreset] = [
    MaterialPreset(name="Plywood", surface_speed_m_min=200.0, chipload_per_tooth_mm=0.040),
    MaterialPreset(name="MDF", surface_speed_m_min=250.0, chipload_per_tooth_mm=0.050),
    MaterialPreset(name="Hardwood", surface_speed_m_min=250.0, chipload_per_tooth_mm=0.035),
    MaterialPreset(name="Softwood", surface_speed_m_min=300.0, chipload_per_tooth_mm=0.050),
    MaterialPreset(name="Acrylic", surface_speed_m_min=180.0, chipload_per_tooth_mm=0.050),
    MaterialPreset(name="HDPE / Delrin", surface_speed_m_min=250.0, chipload_per_tooth_mm=0.050),
    MaterialPreset(name="Aluminum (soft)", surface_speed_m_min=100.0, chipload_per_tooth_mm=0.015),
    MaterialPreset(
        name="Aluminum (6061-T6)",
        surface_speed_m_min=140.0,
        chipload_per_tooth_mm=0.025,
    ),
    MaterialPreset(name="Brass", surface_speed_m_min=120.0, chipload_per_tooth_mm=0.020),
    MaterialPreset(name="Mild steel", surface_speed_m_min=30.0, chipload_per_tooth_mm=0.015),
    MaterialPreset(name="Stainless steel", surface_speed_m_min=20.0, chipload_per_tooth_mm=0.010),
    MaterialPreset(name="Foam / wax", surface_speed_m_min=400.0, chipload_per_tooth_mm=0.100),
]


def compute_feeds_speeds(
    *,
    tool_diameter_mm: float,
    flute_count: int,
    material: MaterialPreset,
) -> tuple[int, float]:
    """Return a starting ``(spindle_rpm, feed_mm_per_min)`` for the tool
    + material combination.

    Parameters
    ----------
    tool_diameter_mm:
        Cutter diameter in mm. Must be positive.
    flute_count:
        Number of cutting edges. Must be ≥ 1.
    material:
        Preset carrying the material's surface speed and chipload.

    Raises
    ------
    ValueError:
        For non-positive diameter or non-positive flute count.
    """
    if tool_diameter_mm <= 0.0:
        raise ValueError(
            f"tool_diameter_mm must be positive, got {tool_diameter_mm}"
        )
    if flute_count <= 0:
        raise ValueError(f"flute_count must be ≥ 1, got {flute_count}")
    # RPM = Vc × 1000 / (π × D). Round to the nearest integer — sub-RPM
    # precision is meaningless on VFD-driven hobby spindles.
    rpm = round(
        material.surface_speed_m_min * 1000.0
        / (math.pi * tool_diameter_mm)
    )
    # feed = fz × Z × RPM  (mm/min)
    feed = material.chipload_per_tooth_mm * flute_count * rpm
    return int(rpm), float(feed)
