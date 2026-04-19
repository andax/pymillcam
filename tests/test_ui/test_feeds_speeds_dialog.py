"""Smoke tests for the Feeds & Speeds dialog."""
from __future__ import annotations

from pytestqt.qtbot import QtBot

from pymillcam.ui.feeds_speeds_dialog import FeedsSpeedsDialog


def test_dialog_initial_computed_values_match_inputs(qtbot: QtBot) -> None:
    d = FeedsSpeedsDialog(tool_diameter_mm=3.0, flute_count=2)
    qtbot.addWidget(d)
    rpm, feed = d.result()
    # Default material is the first built-in (Plywood @ 250 m/min),
    # 3 mm cutter, 2 flutes. Exact values are asserted in the
    # feeds_speeds core tests; here we only check the dialog wired
    # them through.
    assert rpm > 0
    assert feed > 0


def test_changing_material_changes_result(qtbot: QtBot) -> None:
    d = FeedsSpeedsDialog(tool_diameter_mm=3.0, flute_count=2)
    qtbot.addWidget(d)
    first_rpm, _ = d.result()
    # Pick a very different material (row N-1 in the combo — last entry,
    # "Foam / wax" at Vc=400) and confirm the suggestion changes.
    d._material.setCurrentIndex(d._material.count() - 1)
    second_rpm, _ = d.result()
    assert first_rpm != second_rpm


def test_changing_diameter_updates_display(qtbot: QtBot) -> None:
    """The read-out labels reflect the live widget state, not the
    stale value at dialog construction time."""
    d = FeedsSpeedsDialog(tool_diameter_mm=3.0, flute_count=2)
    qtbot.addWidget(d)
    initial_label = d._rpm_label.text()
    d._diameter.setValue(10.0)
    updated_label = d._rpm_label.text()
    assert initial_label != updated_label
