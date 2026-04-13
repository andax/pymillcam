"""Smoke tests for the main window shell.

Visual correctness (dock placement looking right, menus feeling native, etc.)
cannot be verified automatically. These tests only guarantee that the window
instantiates, wires its menus, and exposes the docks expected by later
sub-commits.
"""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from pymillcam.ui.main_window import MainWindow


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    window = MainWindow()
    qtbot.addWidget(window)
    return window


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


def test_central_widget_exists(main_window: MainWindow) -> None:
    assert main_window.centralWidget() is not None


def test_placeholder_actions_start_disabled(main_window: MainWindow) -> None:
    # Actions that depend on later sub-commits should be disabled until wired up.
    assert not main_window._action_undo.isEnabled()
    assert not main_window._action_redo.isEnabled()
    assert not main_window._action_fit.isEnabled()
    assert not main_window._action_add_profile.isEnabled()
    assert not main_window._action_generate_gcode.isEnabled()


def test_exit_action_closes_window(main_window: MainWindow, qtbot: QtBot) -> None:
    main_window.show()
    qtbot.waitExposed(main_window)
    assert main_window.isVisible()
    main_window._action_exit.trigger()
    assert not main_window.isVisible()
