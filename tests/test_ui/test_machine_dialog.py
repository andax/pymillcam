"""Smoke tests for the Machine editor dialog."""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from pymillcam.core.machine import MachineDefinition
from pymillcam.ui.machine_dialog import MachineDialog


@pytest.fixture
def dialog(qtbot: QtBot) -> MachineDialog:
    d = MachineDialog(MachineDefinition())
    qtbot.addWidget(d)
    return d


def test_dialog_populates_fields_from_machine(qtbot: QtBot) -> None:
    m = MachineDefinition(
        name="Mini Mill",
        controller="grbl",
        macros={
            "program_start": "(SHOP_HEADER)",
            "program_end": "M5\nM30",
            "tool_change": "M0",
        },
    )
    d = MachineDialog(m)
    qtbot.addWidget(d)
    assert d._name.text() == "Mini Mill"
    assert d._controller.currentText() == "grbl"
    assert d._program_start.toPlainText() == "(SHOP_HEADER)"
    assert d._program_end.toPlainText() == "M5\nM30"
    assert d._tool_change.toPlainText() == "M0"


def test_result_machine_round_trips_edits(dialog: MachineDialog) -> None:
    dialog._name.setText("New name")
    dialog._controller.setCurrentText("linuxcnc")
    dialog._program_start.setPlainText("G21 G90 G17")
    dialog._program_end.setPlainText("M5\nG53 G0 Z0\nM30")
    dialog._tool_change.setPlainText(
        "M5\nG53 G0 Z0\nM0 (Change to T{tool_number})"
    )
    result = dialog.result_machine()
    assert result.name == "New name"
    assert result.controller == "linuxcnc"
    assert result.macros["program_start"] == "G21 G90 G17"
    assert result.macros["program_end"] == "M5\nG53 G0 Z0\nM30"
    assert "{tool_number}" in result.macros["tool_change"]


def test_dialog_does_not_mutate_input_machine(qtbot: QtBot) -> None:
    """The dialog edits a deep copy — the caller's ``MachineDefinition``
    stays intact until they apply ``result_machine()``. Matches the
    Preferences dialog convention so undo/redo snapshots are clean."""
    m = MachineDefinition(name="Original")
    d = MachineDialog(m)
    qtbot.addWidget(d)
    d._name.setText("Edited")
    assert m.name == "Original"
    assert d.result_machine().name == "Edited"


def test_non_macro_machine_fields_survive_edit(dialog: MachineDialog) -> None:
    """Travel, spindle range, capabilities etc. aren't exposed in the
    dialog yet; make sure they're not stripped when the user saves."""
    original = MachineDefinition()
    original.travel.x = 999.0
    original.capabilities.atc = True
    d = MachineDialog(original)
    d._name.setText("Edited")
    result = d.result_machine()
    assert result.travel.x == pytest.approx(999.0)
    assert result.capabilities.atc is True
