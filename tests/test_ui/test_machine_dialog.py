"""Smoke tests for the Machine editor dialog."""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from pymillcam.core.machine import MachineDefinition
from pymillcam.core.machine_library import MachineLibrary
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


def test_switching_controller_reseeds_default_macros(qtbot: QtBot) -> None:
    """A pristine UCCNC machine (macros = UCCNC defaults) switched to GRBL
    picks up GRBL's defaults — otherwise the project would still emit
    UCCNC preamble / M6 even after choosing GRBL."""
    m = MachineDefinition(
        controller="uccnc",
        macros={
            "program_start": "G21 G90 G94 G17",
            "program_end": "M5\nM30",
            "tool_change": "T{tool_number} M6",
        },
    )
    d = MachineDialog(m)
    qtbot.addWidget(d)
    d._controller.setCurrentText("grbl")
    assert d._program_start.toPlainText() == "G21 G90"
    # tool_change flips to GRBL's manual-pause default.
    assert "M0" in d._tool_change.toPlainText()


def test_switching_controller_preserves_customised_macros(qtbot: QtBot) -> None:
    """When a macro has been hand-edited it shouldn't be replaced on a
    controller switch — the user clearly meant to keep it."""
    m = MachineDefinition(
        controller="uccnc",
        macros={
            "program_start": "(SHOP_SPECIFIC)",
            "program_end": "M5\nM30",
            "tool_change": "T{tool_number} M6",
        },
    )
    d = MachineDialog(m)
    qtbot.addWidget(d)
    d._controller.setCurrentText("grbl")
    # program_start was customised — still there.
    assert d._program_start.toPlainText() == "(SHOP_SPECIFIC)"
    # program_end matched UCCNC default — now GRBL default (same text here).
    assert d._program_end.toPlainText() == "M5\nM30"


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


# -------------------------------------------------- library picker integration


def _library_with_two_machines() -> MachineLibrary:
    lib = MachineLibrary()
    lib.add(MachineDefinition(
        name="Alpha", controller="uccnc",
        macros={
            "program_start": "(ALPHA_START)",
            "program_end": "M5\nM30",
            "tool_change": "T{tool_number} M6",
        },
    ))
    lib.add(MachineDefinition(
        name="Bravo", controller="grbl",
        macros={
            "program_start": "G21 G90",
            "program_end": "M5\nG53 G0 Z0\nM30",
            "tool_change": "M5\nM0",
        },
    ))
    return lib


def test_library_picker_hidden_without_library(qtbot: QtBot) -> None:
    d = MachineDialog(MachineDefinition())
    qtbot.addWidget(d)
    assert d._library_picker is None


def test_library_picker_hidden_with_empty_library(qtbot: QtBot) -> None:
    d = MachineDialog(MachineDefinition(), library=MachineLibrary())
    qtbot.addWidget(d)
    assert d._library_picker is None


def test_library_picker_loads_selected_machine_into_form(qtbot: QtBot) -> None:
    library = _library_with_two_machines()
    d = MachineDialog(MachineDefinition(name="Original"), library=library)
    qtbot.addWidget(d)
    # Picker row 0 is the prompt; row 2 is "Bravo".
    d._library_picker.setCurrentIndex(2)
    assert d._name.text() == "Bravo"
    assert d._controller.currentText() == "grbl"
    assert d._program_start.toPlainText() == "G21 G90"
    # result carries library_id pointing at the source entry.
    assert d.result_machine().library_id == library.machines[1].id


def test_library_picker_preserves_op_machine_id(qtbot: QtBot) -> None:
    """Loading a library machine replaces the dialog's content but keeps
    the project machine's own ``id`` — the project machine is a copy,
    not the library entry itself."""
    library = _library_with_two_machines()
    project_machine = MachineDefinition(name="Original")
    project_id = project_machine.id
    d = MachineDialog(project_machine, library=library)
    qtbot.addWidget(d)
    d._library_picker.setCurrentIndex(1)
    assert d.result_machine().id == project_id


def test_library_picker_resets_to_prompt_after_pick(qtbot: QtBot) -> None:
    """The picker returns to the prompt row so re-picking the same entry
    still triggers a reload — otherwise the combo is inert once chosen."""
    library = _library_with_two_machines()
    d = MachineDialog(MachineDefinition(), library=library)
    qtbot.addWidget(d)
    d._library_picker.setCurrentIndex(1)
    assert d._library_picker.currentIndex() == 0
