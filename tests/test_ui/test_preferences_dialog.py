"""Smoke tests for the Preferences dialog."""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from pymillcam.core.preferences import AppPreferences
from pymillcam.ui.preferences_dialog import PreferencesDialog


@pytest.fixture
def dialog(qtbot: QtBot) -> PreferencesDialog:
    d = PreferencesDialog(AppPreferences())
    qtbot.addWidget(d)
    return d


def test_dialog_populates_fields_from_preferences() -> None:
    prefs = AppPreferences(
        default_chord_tolerance_mm=0.015,
        default_tool_diameter_mm=6.0,
        auto_stitch_on_import=True,
        stitch_tolerance_mm=0.002,
        edit_coalesce_ms=200,
    )
    d = PreferencesDialog(prefs)
    assert d._chord.value() == pytest.approx(0.015)
    assert d._tool_diameter.value() == pytest.approx(6.0)
    assert d._auto_stitch.isChecked()
    assert d._stitch_tol.value() == pytest.approx(0.002)
    assert d._coalesce.value() == 200


def test_result_preferences_round_trips_edits(dialog: PreferencesDialog) -> None:
    dialog._chord.setValue(0.005)
    dialog._auto_stitch.setChecked(True)
    dialog._stitch_tol.setValue(0.05)
    dialog._coalesce.setValue(123)
    result = dialog.result_preferences()
    assert result.default_chord_tolerance_mm == pytest.approx(0.005)
    assert result.auto_stitch_on_import is True
    assert result.stitch_tolerance_mm == pytest.approx(0.05)
    assert result.edit_coalesce_ms == 123


def test_stitch_tolerance_disabled_when_auto_stitch_off(
    dialog: PreferencesDialog,
) -> None:
    dialog._auto_stitch.setChecked(False)
    assert not dialog._stitch_tol.isEnabled()
    dialog._auto_stitch.setChecked(True)
    assert dialog._stitch_tol.isEnabled()


def test_dialog_does_not_mutate_input_prefs() -> None:
    prefs = AppPreferences(default_chord_tolerance_mm=0.04)
    d = PreferencesDialog(prefs)
    d._chord.setValue(0.001)
    # The original instance should be untouched — only result_preferences()
    # exposes the new state.
    assert prefs.default_chord_tolerance_mm == pytest.approx(0.04)
    assert d.result_preferences().default_chord_tolerance_mm == pytest.approx(0.001)
