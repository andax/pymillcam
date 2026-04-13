"""Smoke tests for the main window shell.

Visual correctness (dock placement looking right, menus feeling native, etc.)
cannot be verified automatically. These tests only guarantee that the window
instantiates, wires its menus, and exposes the docks expected by later
sub-commits.
"""
from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.ui.main_window import MainWindow


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def _write_sample_dxf(tmp_path: Path) -> Path:
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_line((0, 0), (50, 0), dxfattribs={"layer": "Outline"})
    msp.add_circle((25, 25), radius=10, dxfattribs={"layer": "Holes"})
    path = tmp_path / "sample.dxf"
    doc.saveas(path)
    return path


def test_window_title(main_window: MainWindow) -> None:
    assert main_window.windowTitle() == "PyMillCAM"


def test_has_all_top_level_menus(main_window: MainWindow) -> None:
    titles = [
        action.text()
        for action in main_window.menuBar().actions()
        if action.menu() is not None
    ]
    assert titles == ["&File", "&Edit", "&View", "&Operations"]


def test_tree_dock_is_on_left(main_window: MainWindow) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDockWidget

    dock = main_window.findChild(QDockWidget, "tree_dock")
    assert dock is not None
    assert main_window.dockWidgetArea(dock) == Qt.DockWidgetArea.LeftDockWidgetArea


def test_output_dock_is_on_bottom(main_window: MainWindow) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDockWidget

    dock = main_window.findChild(QDockWidget, "output_dock")
    assert dock is not None
    assert main_window.dockWidgetArea(dock) == Qt.DockWidgetArea.BottomDockWidgetArea


def test_placeholder_actions_start_disabled(main_window: MainWindow) -> None:
    # Actions that still depend on later sub-commits (tree + ops wiring) are disabled.
    assert not main_window._action_undo.isEnabled()
    assert not main_window._action_redo.isEnabled()
    assert not main_window._action_add_profile.isEnabled()
    assert not main_window._action_generate_gcode.isEnabled()


def test_fit_action_is_enabled(main_window: MainWindow) -> None:
    # Viewport is wired up in sub-commit 2, so the Fit action goes live.
    assert main_window._action_fit.isEnabled()


def test_central_widget_is_viewport(main_window: MainWindow) -> None:
    from pymillcam.ui.viewport import Viewport

    assert isinstance(main_window.centralWidget(), Viewport)


def test_mouse_move_updates_status_bar(main_window: MainWindow, qtbot: QtBot) -> None:
    main_window.show()
    qtbot.waitExposed(main_window)
    # Emit directly — we test the connection, not Qt's mouse delivery.
    main_window._viewport.mouse_position_changed.emit(12.5, -3.25)
    assert "12.500" in main_window._coord_label.text()
    assert "-3.250" in main_window._coord_label.text()


def test_load_dxf_populates_project_tree_and_viewport(
    main_window: MainWindow, tmp_path: Path
) -> None:
    path = _write_sample_dxf(tmp_path)
    main_window.load_dxf(path)

    assert {layer.name for layer in main_window.project.geometry_layers} == {
        "Outline",
        "Holes",
    }
    # Tree: one top-level item per layer, each with one entity child.
    assert main_window._tree.topLevelItemCount() == 2
    for i in range(2):
        layer_item = main_window._tree.topLevelItem(i)
        assert layer_item is not None
        assert layer_item.childCount() == 1


def test_set_project_with_known_layout_populates_tree_counts(
    main_window: MainWindow,
) -> None:
    layer_a = GeometryLayer(
        name="A",
        entities=[
            GeometryEntity(segments=[LineSegment(start=(0, 0), end=(1, 0))]),
            GeometryEntity(segments=[LineSegment(start=(0, 0), end=(0, 1))]),
        ],
    )
    layer_b = GeometryLayer(name="B", entities=[GeometryEntity(point=(5, 5))])
    main_window.set_project(Project(geometry_layers=[layer_a, layer_b]))

    assert main_window._tree.topLevelItemCount() == 2
    a_item = main_window._tree.topLevelItem(0)
    b_item = main_window._tree.topLevelItem(1)
    assert a_item is not None and b_item is not None
    assert a_item.childCount() == 2
    assert b_item.childCount() == 1
    assert "(2)" in a_item.text(0)
    assert "(1)" in b_item.text(0)


def test_tree_selection_drives_viewport_highlight(main_window: MainWindow) -> None:
    entity = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))],
    )
    layer = GeometryLayer(name="L", entities=[entity])
    main_window.set_project(Project(geometry_layers=[layer]))

    layer_item = main_window._tree.topLevelItem(0)
    assert layer_item is not None
    entity_item = layer_item.child(0)
    assert entity_item is not None

    entity_item.setSelected(True)
    assert main_window._viewport.selected == ("L", entity.id)

    main_window._tree.clearSelection()
    assert main_window._viewport.selected == (None, None)


def test_viewport_selection_drives_tree_highlight(main_window: MainWindow) -> None:
    entity = GeometryEntity(
        segments=[ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    layer = GeometryLayer(name="L", entities=[entity])
    main_window.set_project(Project(geometry_layers=[layer]))

    main_window._viewport.selection_changed.emit("L", entity.id)
    selected_items = main_window._tree.selectedItems()
    assert len(selected_items) == 1
    ref = selected_items[0].data(0, Qt.ItemDataRole.UserRole)
    assert ref == ("entity", "L", entity.id)


def test_viewport_clearing_selection_clears_tree(main_window: MainWindow) -> None:
    entity = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))],
    )
    layer = GeometryLayer(name="L", entities=[entity])
    main_window.set_project(Project(geometry_layers=[layer]))
    main_window._viewport.selection_changed.emit("L", entity.id)

    main_window._viewport.selection_changed.emit(None, None)
    assert main_window._tree.selectedItems() == []


def test_exit_action_closes_window(main_window: MainWindow, qtbot: QtBot) -> None:
    main_window.show()
    qtbot.waitExposed(main_window)
    assert main_window.isVisible()
    main_window._action_exit.trigger()
    assert not main_window.isVisible()
