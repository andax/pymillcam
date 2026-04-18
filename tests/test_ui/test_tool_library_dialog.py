"""Tests for the Tool Library dialog.

Behavioural: assert what the user sees and what gets persisted, not
specific widget types or layout details. Keeps the tests resilient to
future form-layout tweaks.
"""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from pymillcam.core.tool_library import ToolLibrary
from pymillcam.core.tools import CuttingData, Tool, ToolShape
from pymillcam.ui.tool_library_dialog import ToolLibraryDialog


def _library_with(*tools: Tool, default_id: str | None = None) -> ToolLibrary:
    lib = ToolLibrary(tools=list(tools), default_tool_id=default_id)
    return lib


def _tool(name: str, diameter: float = 3.0) -> Tool:
    t = Tool(name=name, shape=ToolShape.ENDMILL)
    t.geometry["diameter"] = diameter
    return t


# ----------------------------------------------------------- initial state


def test_empty_library_shows_empty_list(qtbot: QtBot) -> None:
    dlg = ToolLibraryDialog(ToolLibrary())
    qtbot.addWidget(dlg)
    assert dlg._list.count() == 0
    # Edit form disabled when no tool is selected so the user can't
    # accidentally type into a void.
    assert not dlg._form.isEnabled()
    assert not dlg._btn_delete.isEnabled()
    assert not dlg._btn_default.isEnabled()


def test_populated_library_lists_tools(qtbot: QtBot) -> None:
    lib = _library_with(_tool("3mm endmill"), _tool("6mm endmill", 6.0))
    dlg = ToolLibraryDialog(lib)
    qtbot.addWidget(dlg)
    assert dlg._list.count() == 2
    labels = [dlg._list.item(i).text() for i in range(dlg._list.count())]
    assert any("3mm endmill" in s for s in labels)
    assert any("6mm endmill" in s for s in labels)


def test_default_tool_marked_in_list(qtbot: QtBot) -> None:
    t = _tool("3mm endmill")
    dlg = ToolLibraryDialog(_library_with(t, default_id=t.id))
    qtbot.addWidget(dlg)
    first_label = dlg._list.item(0).text()
    assert first_label.startswith("★"), "default tool should carry a marker"


# ----------------------------------------------------------- form binding


def test_selecting_tool_populates_form(qtbot: QtBot) -> None:
    t = _tool("3mm endmill", diameter=3.0)
    t.supplier = "Sorotec"
    t.cutting_data["default"] = CuttingData(
        spindle_rpm=18000, feed_xy=1200, feed_z=300, stepdown=1.0,
    )
    dlg = ToolLibraryDialog(_library_with(t))
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(0)

    assert dlg._name.text() == "3mm endmill"
    assert dlg._diameter.value() == pytest.approx(3.0)
    assert dlg._supplier.text() == "Sorotec"
    assert dlg._spindle_rpm.value() == 18000
    assert dlg._feed_xy.value() == pytest.approx(1200.0)


def test_editing_name_round_trips_to_model(qtbot: QtBot) -> None:
    t = _tool("original")
    dlg = ToolLibraryDialog(_library_with(t))
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(0)

    dlg._name.setText("renamed")
    dlg._name.textEdited.emit("renamed")

    updated = dlg.result_library().tools[0]
    assert updated.name == "renamed"


def test_editing_diameter_round_trips_to_model(qtbot: QtBot) -> None:
    dlg = ToolLibraryDialog(_library_with(_tool("t")))
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(0)

    dlg._diameter.setValue(6.35)

    updated = dlg.result_library().tools[0]
    assert updated.geometry["diameter"] == pytest.approx(6.35)


def test_editing_cutting_data_round_trips_to_model(qtbot: QtBot) -> None:
    """The cutting-data form writes back to the Tool's ``default`` entry
    under ``cutting_data``. Persisting those values is the whole point
    of the library — users shouldn't re-enter 1200 mm/min per op."""
    dlg = ToolLibraryDialog(_library_with(_tool("t")))
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(0)

    dlg._spindle_rpm.setValue(24000)
    dlg._feed_xy.setValue(2500.0)
    dlg._feed_z.setValue(600.0)

    cd = dlg.result_library().tools[0].cutting_data["default"]
    assert cd.spindle_rpm == 24000
    assert cd.feed_xy == pytest.approx(2500.0)
    assert cd.feed_z == pytest.approx(600.0)


def test_editing_form_while_library_empty_is_noop(qtbot: QtBot) -> None:
    """Defensive: if a signal somehow fires with no selection, we
    don't IndexError on ``self._library.tools[self._selected_index]``."""
    dlg = ToolLibraryDialog(ToolLibrary())
    qtbot.addWidget(dlg)
    # No selection; write-back should early-return silently.
    dlg._name.setText("ghost")
    dlg._name.textEdited.emit("ghost")
    # Library stays empty; no crash.
    assert dlg.result_library().tools == []


# -------------------------------------------------------- list mutations


def test_new_button_adds_tool_and_selects_it(qtbot: QtBot) -> None:
    dlg = ToolLibraryDialog(ToolLibrary())
    qtbot.addWidget(dlg)

    dlg._btn_new.click()

    assert dlg._list.count() == 1
    assert dlg._list.currentRow() == 0
    # First tool in an empty library becomes default automatically so
    # the library has a usable default right away.
    assert dlg.result_library().default_tool_id is not None


def test_delete_button_removes_selected_tool(qtbot: QtBot) -> None:
    lib = _library_with(_tool("to-delete"), _tool("to-keep"))
    dlg = ToolLibraryDialog(lib)
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(0)

    dlg._btn_delete.click()

    remaining = [t.name for t in dlg.result_library().tools]
    assert remaining == ["to-keep"]


def test_delete_selects_neighbour(qtbot: QtBot) -> None:
    """After delete the user still has a tool selected for editing —
    avoids a double-click-to-resume interaction."""
    lib = _library_with(_tool("a"), _tool("b"), _tool("c"))
    dlg = ToolLibraryDialog(lib)
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(1)  # select "b"

    dlg._btn_delete.click()

    # "a" and "c" remain; the dialog should still have a row selected.
    assert dlg._list.currentRow() >= 0
    assert dlg._form.isEnabled()


def test_delete_last_tool_leaves_empty_disabled_form(qtbot: QtBot) -> None:
    dlg = ToolLibraryDialog(_library_with(_tool("only")))
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(0)

    dlg._btn_delete.click()

    assert dlg._list.count() == 0
    assert not dlg._form.isEnabled()
    assert not dlg._btn_delete.isEnabled()


# ------------------------------------------------------------ default


def test_set_as_default_updates_library(qtbot: QtBot) -> None:
    t1 = _tool("t1", 3.0)
    t2 = _tool("t2", 6.0)
    dlg = ToolLibraryDialog(_library_with(t1, t2, default_id=t1.id))
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(1)  # select t2

    dlg._btn_default.click()

    assert dlg.result_library().default_tool_id == t2.id


def test_set_as_default_refreshes_list_marker(qtbot: QtBot) -> None:
    t1 = _tool("t1", 3.0)
    t2 = _tool("t2", 6.0)
    dlg = ToolLibraryDialog(_library_with(t1, t2, default_id=t1.id))
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(1)

    dlg._btn_default.click()

    assert dlg._list.item(1).text().startswith("★")
    assert not dlg._list.item(0).text().startswith("★")


# ---------------------------------------------------------------- result


def test_result_library_returns_edited_copy_not_original(qtbot: QtBot) -> None:
    """The dialog edits a deep copy — the caller's original library
    is untouched unless they accept and use result_library()."""
    original_tool = _tool("keep")
    original = _library_with(original_tool)
    dlg = ToolLibraryDialog(original)
    qtbot.addWidget(dlg)
    dlg._list.setCurrentRow(0)
    dlg._name.setText("mutated")
    dlg._name.textEdited.emit("mutated")

    # Original is untouched.
    assert original.tools[0].name == "keep"
    # Dialog's copy reflects the edit.
    assert dlg.result_library().tools[0].name == "mutated"
