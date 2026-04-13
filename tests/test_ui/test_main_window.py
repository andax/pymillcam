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
from pymillcam.core.operations import GeometryRef, ProfileOp
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


def _layer_items(main_window: MainWindow) -> list:
    items = []
    for i in range(main_window._tree.topLevelItemCount()):
        item = main_window._tree.topLevelItem(i)
        if item is None:
            continue
        ref = item.data(0, Qt.ItemDataRole.UserRole)
        if ref and ref[0] == "layer":
            items.append(item)
    return items


def test_load_dxf_populates_project_tree_and_viewport(
    main_window: MainWindow, tmp_path: Path
) -> None:
    path = _write_sample_dxf(tmp_path)
    main_window.load_dxf(path)

    assert {layer.name for layer in main_window.project.geometry_layers} == {
        "Outline",
        "Holes",
    }
    layer_items = _layer_items(main_window)
    assert len(layer_items) == 2
    for item in layer_items:
        assert item.childCount() == 1


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

    a_item, b_item = _layer_items(main_window)
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
    assert main_window._viewport.selection == [("L", entity.id)]

    main_window._tree.clearSelection()
    assert main_window._viewport.selection == []


def test_viewport_selection_drives_tree_highlight(main_window: MainWindow) -> None:
    entity = GeometryEntity(
        segments=[ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    layer = GeometryLayer(name="L", entities=[entity])
    main_window.set_project(Project(geometry_layers=[layer]))

    main_window._viewport.selection_changed.emit([("L", entity.id)])
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
    main_window._viewport.selection_changed.emit([("L", entity.id)])

    main_window._viewport.selection_changed.emit([])
    assert main_window._tree.selectedItems() == []


def _project_with_one_circle() -> tuple[Project, GeometryEntity]:
    entity = GeometryEntity(
        segments=[
            ArcSegment(center=(0, 0), radius=25, start_angle_deg=0, sweep_deg=360),
        ],
        closed=True,
    )
    layer = GeometryLayer(name="Holes", entities=[entity])
    return Project(name="circle", geometry_layers=[layer]), entity


def _simulate_viewport_click(
    main_window: MainWindow, layer_name: str, entity_id: str
) -> None:
    """Replay what Viewport.mouseReleaseEvent does on a single-entity hit."""
    items = [(layer_name, entity_id)]
    main_window._viewport.set_selection(items)
    main_window._viewport.selection_changed.emit(items)


def test_add_profile_requires_selected_entity(main_window: MainWindow) -> None:
    project, _ = _project_with_one_circle()
    main_window.set_project(project)
    assert not main_window._action_add_profile.isEnabled()


def test_add_profile_creates_op_and_default_tool_controller(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    assert main_window._action_add_profile.isEnabled()

    main_window._action_add_profile.trigger()

    ops = main_window.project.operations
    assert len(ops) == 1
    op = ops[0]
    assert isinstance(op, ProfileOp)
    assert op.geometry_refs == [GeometryRef(layer_name="Holes", entity_id=entity.id)]
    assert op.tool_controller_id is not None
    assert main_window.project.tool_controllers, "default ToolController not created"
    assert main_window._action_generate_gcode.isEnabled()


def test_generate_gcode_fills_output_pane(main_window: MainWindow) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()

    main_window._action_generate_gcode.trigger()
    text = main_window._output.toPlainText()
    assert "G21 G90 G94 G17" in text  # preamble
    assert "M30" in text  # program end


def test_editing_op_in_properties_panel_updates_regenerated_gcode(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]

    # Select op so the properties panel binds to it.
    main_window._select_operation_in_tree(op.id)
    main_window._properties._form.cut_depth.setValue(-9.0)
    assert op.cut_depth == -9.0

    main_window._action_generate_gcode.trigger()
    first = main_window._output.toPlainText()
    main_window._properties._form.cut_depth.setValue(-1.0)
    main_window._action_generate_gcode.trigger()
    second = main_window._output.toPlainText()
    assert first != second  # cache-free regeneration


def test_each_add_profile_creates_its_own_tool_controller(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    # Selecting the new op clears the viewport selection (real users would
    # re-click the entity before adding the next profile).
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    assert len(main_window.project.tool_controllers) == 2
    nums = sorted(tc.tool_number for tc in main_window.project.tool_controllers)
    assert nums == [1, 2]


def test_selecting_op_pushes_profile_preview_to_viewport(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    # Outside offset on a 50 mm circle with 3 mm tool gives a non-empty preview.
    assert main_window._viewport._profile_preview, "expected preview segments"


def test_editing_op_updates_profile_preview(main_window: MainWindow) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    before = list(main_window._viewport._profile_preview)
    main_window._properties._form.tool_diameter.setValue(10.0)
    after = list(main_window._viewport._profile_preview)
    assert before != after, "tool diameter change should re-compute preview"


def test_generate_gcode_pushes_toolpath_preview(main_window: MainWindow) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    main_window._action_generate_gcode.trigger()
    assert main_window._viewport._toolpath_preview, "expected walked toolpath moves"


def test_editing_op_clears_stale_toolpath_preview(main_window: MainWindow) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    main_window._action_generate_gcode.trigger()
    assert main_window._viewport._toolpath_preview
    main_window._properties._form.cut_depth.setValue(-7.0)
    assert main_window._viewport._toolpath_preview == []


def test_undo_add_profile_removes_op_and_tool_controller(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    assert len(main_window.project.operations) == 1
    assert len(main_window.project.tool_controllers) == 1

    main_window._action_undo.trigger()
    assert main_window.project.operations == []
    assert main_window.project.tool_controllers == []
    assert main_window._action_undo.isEnabled() is False
    assert main_window._action_redo.isEnabled() is True


def test_redo_replays_add_profile(main_window: MainWindow) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    main_window._action_undo.trigger()

    main_window._action_redo.trigger()
    assert len(main_window.project.operations) == 1
    assert len(main_window.project.tool_controllers) == 1


def test_delete_operation_is_undoable(main_window: MainWindow) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    op_id = main_window.project.operations[0].id
    main_window._select_operation_in_tree(op_id)

    main_window._action_delete_operation.trigger()
    assert main_window.project.operations == []

    main_window._action_undo.trigger()
    assert len(main_window.project.operations) == 1
    assert main_window.project.operations[0].id == op_id


def test_property_edits_coalesce_into_one_undo_step(main_window: MainWindow) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Three rapid edits — should coalesce until the timer fires.
    main_window._properties._form.cut_depth.setValue(-5.0)
    main_window._properties._form.cut_depth.setValue(-7.0)
    main_window._properties._form.cut_depth.setValue(-9.0)
    # Force the coalesce timer to fire now rather than waiting 400 ms.
    main_window._commit_pending_edit()

    # One Add Profile + one Edit operation = 2 undo steps to reach empty.
    main_window._action_undo.trigger()  # undo edit → cut_depth back to -3
    assert main_window.project.operations[0].cut_depth == -3.0
    main_window._action_undo.trigger()  # undo add → empty
    assert main_window.project.operations == []


def test_undo_during_in_progress_edit_reverts_without_recording(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    # Mutate but do NOT commit the coalesced edit.
    main_window._properties._form.cut_depth.setValue(-5.0)
    assert main_window.project.operations[0].cut_depth == -5.0

    # Undo while the edit is mid-flight: revert to snapshot, no stack push.
    main_window._action_undo.trigger()
    assert main_window.project.operations[0].cut_depth == -3.0
    # Add Profile is still on the undo stack — one more undo empties it.
    main_window._action_undo.trigger()
    assert main_window.project.operations == []


def test_join_paths_action_is_disabled_when_fewer_than_two_selected(
    main_window: MainWindow,
) -> None:
    layer = GeometryLayer(
        name="L",
        entities=[
            GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))]),
            GeometryEntity(segments=[LineSegment(start=(10, 0), end=(10, 10))]),
        ],
    )
    main_window.set_project(Project(geometry_layers=[layer]))
    assert not main_window._action_join_paths.isEnabled()
    # Single selection: still disabled.
    main_window._viewport.set_selection([("L", layer.entities[0].id)])
    main_window._refresh_action_state()
    assert not main_window._action_join_paths.isEnabled()
    # Two selected: enabled.
    main_window._viewport.set_selection(
        [("L", layer.entities[0].id), ("L", layer.entities[1].id)]
    )
    main_window._refresh_action_state()
    assert main_window._action_join_paths.isEnabled()


def test_join_paths_welds_selected_entities_and_is_undoable(
    main_window: MainWindow,
) -> None:
    e1 = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    e2 = GeometryEntity(segments=[LineSegment(start=(10, 0), end=(10, 10))])
    layer = GeometryLayer(name="L", entities=[e1, e2])
    main_window.set_project(Project(geometry_layers=[layer]))
    main_window._viewport.set_selection([("L", e1.id), ("L", e2.id)])
    main_window._refresh_action_state()
    main_window._action_join_paths.trigger()
    after = main_window.project.geometry_layers[0].entities
    assert len(after) == 1
    assert len(after[0].segments) == 2
    main_window._action_undo.trigger()
    before = main_window.project.geometry_layers[0].entities
    assert len(before) == 2


def test_new_project_inherits_chord_tolerance_from_preferences(
    main_window: MainWindow,
) -> None:
    from pymillcam.core.preferences import AppPreferences

    main_window._preferences = AppPreferences(default_chord_tolerance_mm=0.005)
    fresh = main_window._make_new_project()
    assert fresh.settings.chord_tolerance == pytest.approx(0.005)


def test_add_profile_uses_tool_diameter_from_preferences(
    main_window: MainWindow,
) -> None:
    from pymillcam.core.preferences import AppPreferences

    main_window._preferences = AppPreferences(default_tool_diameter_mm=6.5)
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    tc = main_window.project.tool_controllers[0]
    assert tc.tool.geometry["diameter"] == pytest.approx(6.5)


def test_load_project_clears_undo_history(
    main_window: MainWindow, tmp_path: Path
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    out = tmp_path / "p.pmc"
    main_window._save_to(out)

    from pymillcam.io.project_io import load_project

    main_window.set_project(load_project(out))
    assert not main_window._action_undo.isEnabled()
    assert not main_window._action_redo.isEnabled()


def test_save_and_load_project_round_trip(
    main_window: MainWindow, tmp_path: Path
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    out = tmp_path / "round_trip.pmc"
    main_window._save_to(out)
    assert out.exists()

    fresh = MainWindow()
    try:
        from pymillcam.io.project_io import load_project

        fresh.set_project(load_project(out))
        assert len(fresh.project.operations) == 1
        assert (
            fresh.project.operations[0].geometry_refs[0].entity_id == entity.id
        )
    finally:
        fresh.deleteLater()


def test_exit_action_closes_window(main_window: MainWindow, qtbot: QtBot) -> None:
    main_window.show()
    qtbot.waitExposed(main_window)
    assert main_window.isVisible()
    main_window._action_exit.trigger()
    assert not main_window.isVisible()
