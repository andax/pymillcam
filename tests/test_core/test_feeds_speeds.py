"""Behaviour tests for core/feeds_speeds."""
from __future__ import annotations

import math

import pytest

from pymillcam.core.feeds_speeds import (
    DEFAULT_MATERIALS,
    MaterialPreset,
    compute_feeds_speeds,
)


def _mdf() -> MaterialPreset:
    return MaterialPreset(
        name="MDF", surface_speed_m_min=300.0, chipload_per_tooth_mm=0.05
    )


def test_default_materials_are_non_empty_and_unique() -> None:
    names = [m.name for m in DEFAULT_MATERIALS]
    assert names, "expected at least one built-in material preset"
    assert len(set(names)) == len(names), "material names should be unique"


def test_compute_feeds_speeds_rpm_follows_surface_speed_formula() -> None:
    """RPM = Vc × 1000 / (π × D). For a 3 mm end mill at Vc=300 m/min
    that's 31831 RPM (rounded)."""
    rpm, _ = compute_feeds_speeds(
        tool_diameter_mm=3.0, flute_count=2, material=_mdf()
    )
    expected = round(300.0 * 1000.0 / (math.pi * 3.0))
    assert rpm == expected


def test_compute_feeds_speeds_feed_scales_with_flutes_and_chipload() -> None:
    """feed = fz × Z × RPM. With chipload 0.05, 2 flutes, and the
    calculated RPM for a 3 mm cutter, feed = 0.05 × 2 × 31831 ≈ 3183."""
    _, feed = compute_feeds_speeds(
        tool_diameter_mm=3.0, flute_count=2, material=_mdf()
    )
    rpm = round(300.0 * 1000.0 / (math.pi * 3.0))
    assert feed == pytest.approx(0.05 * 2 * rpm, abs=1e-6)


def test_compute_feeds_speeds_rejects_non_positive_diameter() -> None:
    with pytest.raises(ValueError, match="tool_diameter_mm must be positive"):
        compute_feeds_speeds(
            tool_diameter_mm=0.0, flute_count=2, material=_mdf()
        )


def test_compute_feeds_speeds_rejects_zero_flute_count() -> None:
    with pytest.raises(ValueError, match="flute_count must be"):
        compute_feeds_speeds(
            tool_diameter_mm=3.0, flute_count=0, material=_mdf()
        )


def test_larger_diameter_slows_rpm() -> None:
    """Same material, bigger cutter → lower RPM (surface speed is fixed)."""
    rpm_small, _ = compute_feeds_speeds(
        tool_diameter_mm=3.0, flute_count=2, material=_mdf()
    )
    rpm_large, _ = compute_feeds_speeds(
        tool_diameter_mm=10.0, flute_count=2, material=_mdf()
    )
    assert rpm_large < rpm_small


def test_metals_suggest_lower_rpm_than_plastics() -> None:
    """Steel has a much lower Vc than MDF — at the same diameter the
    suggested RPM should be much lower."""
    steel = next(m for m in DEFAULT_MATERIALS if m.name == "Mild steel")
    plywood = next(m for m in DEFAULT_MATERIALS if m.name == "Plywood")
    rpm_steel, _ = compute_feeds_speeds(
        tool_diameter_mm=3.0, flute_count=2, material=steel
    )
    rpm_plywood, _ = compute_feeds_speeds(
        tool_diameter_mm=3.0, flute_count=2, material=plywood
    )
    assert rpm_steel < rpm_plywood
