"""Tests for AppPreferences load/save."""
from __future__ import annotations

from pathlib import Path

import pytest

from pymillcam.core.preferences import (
    AppPreferences,
    PreferencesLoadError,
    load_preferences,
    save_preferences,
)


def test_defaults_are_sensible() -> None:
    prefs = AppPreferences()
    assert prefs.default_chord_tolerance_mm == pytest.approx(0.02)
    assert prefs.default_tool_diameter_mm == pytest.approx(3.0)
    assert prefs.auto_stitch_on_import is False
    assert prefs.stitch_tolerance_mm == pytest.approx(0.01)
    assert prefs.edit_coalesce_ms == 400


def test_round_trip_save_then_load(tmp_path: Path) -> None:
    prefs = AppPreferences(
        default_chord_tolerance_mm=0.015,
        default_tool_diameter_mm=6.35,
        auto_stitch_on_import=True,
        stitch_tolerance_mm=0.005,
        edit_coalesce_ms=250,
    )
    path = tmp_path / "preferences.json"
    save_preferences(prefs, path)
    assert load_preferences(path) == prefs


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert load_preferences(tmp_path / "nope.json") == AppPreferences()


def test_load_malformed_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(PreferencesLoadError, match="Invalid preferences"):
        load_preferences(path)


def test_load_invalid_schema_raises(tmp_path: Path) -> None:
    path = tmp_path / "wrong.json"
    # Negative tolerance violates the gt=0 constraint.
    path.write_text(
        '{"default_chord_tolerance_mm": -1.0}', encoding="utf-8"
    )
    with pytest.raises(PreferencesLoadError, match="Invalid preferences"):
        load_preferences(path)


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "preferences.json"
    save_preferences(AppPreferences(), nested)
    assert nested.exists()


def test_save_is_atomic(tmp_path: Path) -> None:
    """A failed write must not corrupt an existing prefs file."""
    path = tmp_path / "preferences.json"
    save_preferences(AppPreferences(default_chord_tolerance_mm=0.05), path)
    original = path.read_text(encoding="utf-8")
    # Save again — the .tmp file shouldn't linger after the rename.
    save_preferences(AppPreferences(default_chord_tolerance_mm=0.01), path)
    assert not (path.with_suffix(path.suffix + ".tmp")).exists()
    assert path.read_text(encoding="utf-8") != original
