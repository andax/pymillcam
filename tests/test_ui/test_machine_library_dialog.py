"""Smoke tests for the Machine Library dialog."""
from __future__ import annotations

from pytestqt.qtbot import QtBot

from pymillcam.core.machine import MachineDefinition
from pymillcam.core.machine_library import MachineLibrary
from pymillcam.ui.machine_library_dialog import MachineLibraryDialog


def _seeded_library() -> MachineLibrary:
    lib = MachineLibrary()
    lib.add(MachineDefinition(name="Alpha", controller="uccnc"))
    lib.add(MachineDefinition(name="Beta", controller="grbl"))
    lib.default_machine_id = lib.machines[0].id
    return lib


def test_dialog_lists_machines_with_default_marker(qtbot: QtBot) -> None:
    d = MachineLibraryDialog(_seeded_library())
    qtbot.addWidget(d)
    labels = [d._list.item(i).text() for i in range(d._list.count())]
    # Default-marked entry has a leading asterisk.
    assert labels[0].startswith("*")
    assert labels[1].startswith("B")  # non-default → no asterisk


def test_selecting_a_row_populates_form(qtbot: QtBot) -> None:
    d = MachineLibraryDialog(_seeded_library())
    qtbot.addWidget(d)
    d._list.setCurrentRow(1)
    assert d._name.text() == "Beta"
    assert d._controller.currentText() == "grbl"


def test_new_appends_and_selects(qtbot: QtBot) -> None:
    d = MachineLibraryDialog(MachineLibrary())
    qtbot.addWidget(d)
    d._on_new()
    assert d._list.count() == 1
    assert d._list.currentRow() == 0
    assert d._name.text() == "New machine"


def test_duplicate_creates_independent_copy_with_fresh_id(
    qtbot: QtBot,
) -> None:
    d = MachineLibraryDialog(_seeded_library())
    qtbot.addWidget(d)
    d._list.setCurrentRow(0)
    d._on_duplicate()
    # Source + copy; the copy's id differs.
    lib = d.result_library()
    assert len(lib.machines) == 3
    assert lib.machines[0].id != lib.machines[2].id
    assert lib.machines[2].name.endswith("(copy)")


def test_delete_removes_and_reselects(qtbot: QtBot) -> None:
    d = MachineLibraryDialog(_seeded_library())
    qtbot.addWidget(d)
    d._list.setCurrentRow(1)
    d._on_delete()
    # Only the default remains; selection moved back to row 0.
    assert d._list.count() == 1
    assert d._list.currentRow() == 0


def test_set_as_default_updates_marker(qtbot: QtBot) -> None:
    lib = _seeded_library()
    second_id = lib.machines[1].id
    d = MachineLibraryDialog(lib)
    qtbot.addWidget(d)
    d._list.setCurrentRow(1)
    d._on_set_default()
    assert d.result_library().default_machine_id == second_id
    # Row 1 now carries the asterisk, row 0 does not.
    assert d._list.item(1).text().startswith("*")
    assert not d._list.item(0).text().startswith("*")


def test_editing_fields_writes_back_to_model(qtbot: QtBot) -> None:
    d = MachineLibraryDialog(_seeded_library())
    qtbot.addWidget(d)
    d._list.setCurrentRow(0)
    d._name.setText("Renamed")
    d._program_start.setPlainText("(CUSTOM)")
    lib = d.result_library()
    assert lib.machines[0].name == "Renamed"
    assert lib.machines[0].macros["program_start"] == "(CUSTOM)"
