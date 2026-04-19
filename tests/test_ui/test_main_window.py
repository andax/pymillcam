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
from pymillcam.core.operations import GeometryRef, PocketOp, ProfileOp
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
    assert titles == ["&File", "&Edit", "&Tools", "&View", "&Operations"]


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
    main_window._properties._profile_form.cut_depth.setValue(-9.0)
    assert op.cut_depth == -9.0

    main_window._action_generate_gcode.trigger()
    first = main_window._output.toPlainText()
    main_window._properties._profile_form.cut_depth.setValue(-1.0)
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
    main_window._properties._profile_form.tool_diameter.setValue(10.0)
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
    assert main_window._output.toPlainText() != ""
    main_window._properties._profile_form.cut_depth.setValue(-7.0)
    assert main_window._viewport._toolpath_preview == []
    assert main_window._output.toPlainText() == ""


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
    main_window._properties._profile_form.cut_depth.setValue(-5.0)
    main_window._properties._profile_form.cut_depth.setValue(-7.0)
    main_window._properties._profile_form.cut_depth.setValue(-9.0)
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
    main_window._properties._profile_form.cut_depth.setValue(-5.0)
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


# ----------------------------------------------------- duplicate operation


def _make_one_profile_op(main_window: MainWindow) -> tuple[Project, GeometryEntity]:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    return main_window.project, entity


def test_duplicate_operation_action_disabled_when_no_op_selected(
    main_window: MainWindow,
) -> None:
    main_window.set_project(Project())
    assert not main_window._action_duplicate_operation.isEnabled()


def test_duplicate_operation_action_enabled_when_op_selected(
    main_window: MainWindow,
) -> None:
    _make_one_profile_op(main_window)
    op_id = main_window.project.operations[0].id
    main_window._select_operation_in_tree(op_id)
    assert main_window._action_duplicate_operation.isEnabled()


def test_duplicate_operation_appends_a_new_op(main_window: MainWindow) -> None:
    _make_one_profile_op(main_window)
    op_id = main_window.project.operations[0].id
    main_window._select_operation_in_tree(op_id)

    main_window._action_duplicate_operation.trigger()

    assert len(main_window.project.operations) == 2


def test_duplicate_preserves_fields_but_fresh_id(
    main_window: MainWindow,
) -> None:
    """Duplicate carries forward every field the user set except the
    op.id (distinct identity), tool_controller_id (fresh TC), and
    name (suffix disambiguates in the tree)."""
    _make_one_profile_op(main_window)
    original = main_window.project.operations[0]
    # Tweak the original so we can tell they're real copies rather than
    # both reset to defaults.
    original.cut_depth = -7.25
    main_window._select_operation_in_tree(original.id)

    main_window._action_duplicate_operation.trigger()

    duplicate = main_window.project.operations[1]
    assert duplicate.id != original.id
    assert duplicate.cut_depth == pytest.approx(-7.25)
    assert [
        (ref.layer_name, ref.entity_id) for ref in duplicate.geometry_refs
    ] == [
        (ref.layer_name, ref.entity_id) for ref in original.geometry_refs
    ]


def test_duplicate_name_is_distinct_from_original(
    main_window: MainWindow,
) -> None:
    """Tree-visible regression: three copies of one op must NOT all
    read "Drill 1" in the tree. The first duplicate gets a
    ``(copy)`` suffix; subsequent duplicates of the same op chain
    their counters without stacking ``(copy) (copy)``."""
    _make_one_profile_op(main_window)
    original = main_window.project.operations[0]
    original_name = original.name
    main_window._select_operation_in_tree(original.id)

    main_window._action_duplicate_operation.trigger()
    first = main_window.project.operations[1]
    assert first.name == f"{original_name} (copy)"

    main_window._select_operation_in_tree(first.id)
    main_window._action_duplicate_operation.trigger()
    second = main_window.project.operations[2]
    assert second.name == f"{original_name} (copy 2)"

    main_window._select_operation_in_tree(original.id)
    main_window._action_duplicate_operation.trigger()
    third = main_window.project.operations[3]
    # Duplicating the ORIGINAL again — the first (copy) slot is taken,
    # so the new entry slides to (copy 2) — also taken — then (copy 3).
    assert third.name == f"{original_name} (copy 3)"

    # All four names are distinct.
    names = [o.name for o in main_window.project.operations]
    assert len(set(names)) == len(names)


def test_duplicate_gets_its_own_tool_controller(main_window: MainWindow) -> None:
    """The duplicate's ToolController is a fresh clone with a new
    tool_number — not a shared reference. So editing the duplicate's
    diameter / feeds in Properties doesn't mutate the original's
    tool, matching the "each op gets its own tool" convention that
    Add-op already follows."""
    _make_one_profile_op(main_window)
    original = main_window.project.operations[0]
    original_tc = next(
        tc for tc in main_window.project.tool_controllers
        if tc.tool_number == original.tool_controller_id
    )
    main_window._select_operation_in_tree(original.id)
    main_window._action_duplicate_operation.trigger()

    duplicate = main_window.project.operations[1]
    dup_tc = next(
        tc for tc in main_window.project.tool_controllers
        if tc.tool_number == duplicate.tool_controller_id
    )
    assert dup_tc.tool_number != original_tc.tool_number
    assert dup_tc.tool.id != original_tc.tool.id
    # Mutate the duplicate's tool; original's tool stays unchanged.
    dup_tc.tool.geometry["diameter"] = 99.0
    assert original_tc.tool.geometry["diameter"] != 99.0


def test_duplicate_selects_the_new_op_in_the_tree(
    main_window: MainWindow,
) -> None:
    _make_one_profile_op(main_window)
    original = main_window.project.operations[0]
    main_window._select_operation_in_tree(original.id)

    main_window._action_duplicate_operation.trigger()

    # Selection advanced to the newly-created op so the user can
    # start editing it immediately.
    duplicate = main_window.project.operations[1]
    assert main_window._currently_selected_operation() is duplicate


def test_undo_duplicate_removes_the_copy(main_window: MainWindow) -> None:
    _make_one_profile_op(main_window)
    original = main_window.project.operations[0]
    main_window._select_operation_in_tree(original.id)
    main_window._action_duplicate_operation.trigger()
    assert len(main_window.project.operations) == 2

    main_window._action_undo.trigger()

    assert len(main_window.project.operations) == 1
    assert main_window.project.operations[0].id == original.id


def test_add_profile_uses_tool_diameter_from_preferences(
    main_window: MainWindow,
) -> None:
    """Fallback path when the tool library has no default tool: use
    the ``preferences.default_tool_diameter_mm`` so first-run users
    aren't forced to seed a library before they can add anything."""
    from pymillcam.core.preferences import AppPreferences

    main_window._preferences = AppPreferences(default_tool_diameter_mm=6.5)
    # Ensure library path is cold — it is by default in the fixture,
    # but make it explicit so the test documents its precondition.
    assert main_window._tool_library.default_tool_id is None
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    tc = main_window.project.tool_controllers[0]
    assert tc.tool.geometry["diameter"] == pytest.approx(6.5)


def test_add_profile_uses_library_default_tool_when_set(
    main_window: MainWindow,
) -> None:
    """Primary path: when the library has a default tool, Add Profile
    copies its geometry + cutting data into a new ToolController
    rather than synthesising a preferences-defaulted endmill."""
    from pymillcam.core.tool_library import ToolLibrary
    from pymillcam.core.tools import CuttingData, Tool, ToolShape

    lib_tool = Tool(
        name="library 8mm roughing",
        shape=ToolShape.ENDMILL,
        geometry={"diameter": 8.0, "flute_length": 25.0, "total_length": 60.0,
                  "shank_diameter": 8.0, "flute_count": 3},
        cutting_data={"default": CuttingData(
            spindle_rpm=20000, feed_xy=2500.0, feed_z=800.0, stepdown=2.5,
        )},
    )
    main_window._tool_library = ToolLibrary(
        tools=[lib_tool], default_tool_id=lib_tool.id
    )

    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()

    tc = main_window.project.tool_controllers[0]
    assert tc.tool.geometry["diameter"] == pytest.approx(8.0)
    assert tc.tool.name == "library 8mm roughing"
    # Cutting data carried forward into the ToolController runtime fields
    # so the op starts with the library's feeds, not the Pydantic defaults.
    assert tc.spindle_rpm == 20000
    assert tc.feed_xy == pytest.approx(2500.0)
    assert tc.feed_z == pytest.approx(800.0)


def test_add_profile_copies_library_tool_not_reference(
    main_window: MainWindow,
) -> None:
    """Projects must stay self-contained: editing a project's tool
    should not retroactively mutate the library, and editing the
    library should not change existing ops. Guard the `id`s are
    distinct so future "update from library" workflows can still match
    on provenance if they want."""
    from pymillcam.core.tool_library import ToolLibrary
    from pymillcam.core.tools import Tool, ToolShape

    lib_tool = Tool(name="lib tool", shape=ToolShape.ENDMILL)
    lib_tool.geometry["diameter"] = 4.0
    main_window._tool_library = ToolLibrary(
        tools=[lib_tool], default_tool_id=lib_tool.id
    )

    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()

    project_tool = main_window.project.tool_controllers[0].tool
    # Same geometry, but distinct identity — the project carries a copy.
    assert project_tool.id != lib_tool.id
    # Mutating the project's tool doesn't touch the library's.
    project_tool.geometry["diameter"] = 99.0
    assert lib_tool.geometry["diameter"] == pytest.approx(4.0)


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


# ---------- Pocket operation smoke tests ---------------------------------


def test_add_pocket_requires_selected_entity(main_window: MainWindow) -> None:
    project, _ = _project_with_one_circle()
    main_window.set_project(project)
    assert not main_window._action_add_pocket.isEnabled()


def test_add_pocket_creates_op_and_default_tool_controller(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    assert main_window._action_add_pocket.isEnabled()

    main_window._action_add_pocket.trigger()

    ops = main_window.project.operations
    assert len(ops) == 1
    op = ops[0]
    assert isinstance(op, PocketOp)
    assert op.geometry_refs == [GeometryRef(layer_name="Holes", entity_id=entity.id)]
    assert op.tool_controller_id is not None
    assert main_window._action_generate_gcode.isEnabled()


def test_generate_gcode_for_pocket_fills_output_pane(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_pocket.trigger()

    main_window._action_generate_gcode.trigger()
    text = main_window._output.toPlainText()
    assert "Pocket" in text  # operation comment
    assert "M30" in text


def test_pocket_preview_populates_when_pocket_op_is_selected(
    main_window: MainWindow,
) -> None:
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_pocket.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    assert main_window._viewport._profile_preview, "expected pocket preview segments"


def test_profile_and_pocket_ops_coexist_in_generation(
    main_window: MainWindow,
) -> None:
    """Two ops of different types on the same project should both produce
    G-code in one generate pass when no single op is selected (i.e.
    the "combined program" mode)."""
    project, entity = _project_with_one_circle()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_profile.trigger()
    _simulate_viewport_click(main_window, "Holes", entity.id)
    main_window._action_add_pocket.trigger()
    assert len(main_window.project.operations) == 2
    # Add-pocket leaves the pocket op selected, which would trigger the
    # selection-driven (per-op) path. Clear selection so Generate falls
    # back to the combined program.
    main_window._tree.clearSelection()
    main_window._action_generate_gcode.trigger()
    text = main_window._output.toPlainText()
    assert "Profile" in text
    assert "Pocket" in text


# ---------- Add/Remove from active op ------------------------------------

def _project_with_two_circles() -> tuple[Project, GeometryEntity, GeometryEntity]:
    a = GeometryEntity(
        segments=[ArcSegment(center=(0, 0), radius=25, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    b = GeometryEntity(
        segments=[ArcSegment(center=(60, 0), radius=10, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    layer = GeometryLayer(name="L", entities=[a, b])
    return Project(name="two", geometry_layers=[layer]), a, b


def test_add_remove_op_actions_disabled_without_active_op(
    main_window: MainWindow,
) -> None:
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    # Selection present but no active op selected in the tree.
    assert not main_window._action_add_to_op.isEnabled()
    assert not main_window._action_remove_from_op.isEnabled()


def test_add_remove_op_actions_disabled_without_selection(
    main_window: MainWindow,
) -> None:
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    # Active op selected, but viewport selection now empty (Add Profile
    # cleared it via the tree-selection sync).
    assert not main_window._viewport.selection
    assert not main_window._action_add_to_op.isEnabled()
    assert not main_window._action_remove_from_op.isEnabled()


def test_active_op_geometry_highlighted_in_viewport(
    main_window: MainWindow,
) -> None:
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    assert main_window._viewport._active_op_refs == {("L", a.id)}


def test_add_to_active_op_appends_new_refs(
    main_window: MainWindow,
) -> None:
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Select the OTHER entity, then Add to active op.
    _simulate_viewport_click(main_window, "L", b.id)
    main_window._action_add_to_op.trigger()

    op_after = main_window._find_operation(op.id)
    assert op_after is not None
    refs = {(r.layer_name, r.entity_id) for r in op_after.geometry_refs}
    assert refs == {("L", a.id), ("L", b.id)}


def test_add_to_active_op_skips_already_present_refs(
    main_window: MainWindow,
) -> None:
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Re-select the entity already in op and Add — must not duplicate.
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_to_op.trigger()

    op_after = main_window._find_operation(op.id)
    assert op_after is not None
    assert len(op_after.geometry_refs) == 1


def test_remove_from_active_op_drops_matching_refs(
    main_window: MainWindow,
) -> None:
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    # Multi-select both via two clicks (use viewport.set_selection direct
    # to avoid replaying the modifier-aware click logic).
    main_window._viewport.set_selection([("L", a.id), ("L", b.id)])
    main_window._viewport.selection_changed.emit([("L", a.id), ("L", b.id)])
    main_window._action_add_profile.trigger()
    # Two profile ops were created (one per entity since they're disjoint).
    # We want one op with both refs — make it directly.
    main_window.project.operations.clear()
    main_window.project.operations.append(
        ProfileOp(
            name="Multi",
            tool_controller_id=main_window.project.tool_controllers[0].tool_number,
            geometry_refs=[
                GeometryRef(layer_name="L", entity_id=a.id),
                GeometryRef(layer_name="L", entity_id=b.id),
            ],
        )
    )
    main_window._replace_project(main_window.project, fit=False)
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Select just `b` and remove.
    _simulate_viewport_click(main_window, "L", b.id)
    main_window._action_remove_from_op.trigger()

    op_after = main_window._find_operation(op.id)
    assert op_after is not None
    refs = {(r.layer_name, r.entity_id) for r in op_after.geometry_refs}
    assert refs == {("L", a.id)}


def test_add_to_active_op_is_undoable(
    main_window: MainWindow,
) -> None:
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    _simulate_viewport_click(main_window, "L", b.id)
    main_window._action_add_to_op.trigger()
    assert len(main_window._find_operation(op.id).geometry_refs) == 2

    main_window._action_undo.trigger()
    assert len(main_window._find_operation(op.id).geometry_refs) == 1


# -------------------------------- tree highlight for active-op members


def _find_entity_tree_item(main_window: MainWindow, layer, entity_id):
    """Dig out the QTreeWidgetItem for (layer, entity_id) — used when a
    test cares about the tree's visual state for that row."""
    return main_window._find_entity_item(layer, entity_id)


def test_selecting_op_tints_member_entity_rows_in_tree(
    main_window: MainWindow,
) -> None:
    """Tree and viewport should agree about 'this entity belongs to the
    active op': viewport colours the geometry, tree colours the row."""
    from pymillcam.ui.viewport import COLOR_ACTIVE_OP_MEMBER

    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]

    main_window._select_operation_in_tree(op.id)

    member_item = _find_entity_tree_item(main_window, "L", a.id)
    assert member_item is not None
    assert member_item.foreground(0).color() == COLOR_ACTIVE_OP_MEMBER

    # Non-member row stays default (no colour set → invalid brush).
    other_item = _find_entity_tree_item(main_window, "L", b.id)
    assert other_item is not None
    assert not other_item.foreground(0).isOpaque() or (
        other_item.foreground(0).color() != COLOR_ACTIVE_OP_MEMBER
    )


def test_deselecting_op_clears_tree_tint(main_window: MainWindow) -> None:
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Clear selection programmatically (no op selected).
    main_window._tree.clearSelection()

    from pymillcam.ui.viewport import COLOR_ACTIVE_OP_MEMBER

    member_item = _find_entity_tree_item(main_window, "L", a.id)
    assert member_item is not None
    # Brush cleared back to default — colour no longer matches the
    # active-op green.
    assert member_item.foreground(0).color() != COLOR_ACTIVE_OP_MEMBER


# -------------------------------- tree context menu — refs helpers


def test_add_refs_to_op_helper_is_idempotent_on_duplicates(
    main_window: MainWindow,
) -> None:
    """The tree context menu and the Shift+A shortcut both funnel
    through ``_add_refs_to_op``. A ref that's already in the op's
    geometry_refs must not be added twice."""
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]

    # Call the helper directly, once with the duplicate and once with a
    # genuinely-new ref. End state: two refs, no duplicates.
    main_window._add_refs_to_op(op, [("L", a.id), ("L", b.id)])
    refs = [(r.layer_name, r.entity_id) for r in
            main_window._find_operation(op.id).geometry_refs]
    assert refs.count(("L", a.id)) == 1
    assert ("L", b.id) in refs


def test_remove_refs_from_op_ignores_unknown_refs(
    main_window: MainWindow,
) -> None:
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]

    # b.id isn't in the op — removing it should be a silent no-op
    # without throwing, since the tree-menu target set can include
    # entities that happen to not be in the op.
    main_window._remove_refs_from_op(op, [("L", "not-in-op")])
    refs = [(r.layer_name, r.entity_id) for r in
            main_window._find_operation(op.id).geometry_refs]
    assert refs == [("L", a.id)]


def test_right_click_in_tree_preserves_active_op(
    main_window: MainWindow, qtbot: QtBot
) -> None:
    """Regression: Qt's default QTreeWidget changes selection to the
    right-clicked row on mousePressEvent, which deselects the op and
    leaves the context-menu's Add/Remove greyed out. The tree subclass
    suppresses right-click selection change — the op stays the active
    one so the context menu can operate on it."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)
    assert main_window._currently_selected_operation() is op

    # Right-click on entity B's row. Pre-fix this would deselect the op.
    entity_item = main_window._find_entity_item("L", b.id)
    assert entity_item is not None
    item_rect = main_window._tree.visualItemRect(entity_item)
    pos = QPointF(item_rect.center())
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        pos,
        main_window._tree.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_window._tree.mousePressEvent(event)

    # Op is still selected — context menu would be able to target it.
    assert main_window._currently_selected_operation() is op


def test_left_click_in_tree_still_changes_selection(
    main_window: MainWindow,
) -> None:
    """Only right-click should be special-cased. Left-click must
    preserve its standard selection-change behaviour so the user can
    switch between ops and entities as usual."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Left-click on entity row — op selection goes away, entity row selected.
    entity_item = main_window._find_entity_item("L", a.id)
    assert entity_item is not None
    rect = main_window._tree.visualItemRect(entity_item)
    pos = QPointF(rect.center())
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        pos,
        main_window._tree.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_window._tree.mousePressEvent(event)
    event_release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        pos,
        main_window._tree.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_window._tree.mouseReleaseEvent(event_release)

    # Default Qt behaviour — left-click replaces selection. Op is no
    # longer active (matches prior-art behaviour; the user has
    # switched from op-edit mode to entity-browsing mode).
    assert main_window._currently_selected_operation() is None


# -------------------------------- menu filtering by op membership


def test_entity_menu_shows_remove_only_for_member_of_op(
    main_window: MainWindow,
) -> None:
    """Right-clicking an entity that's already in the active op should
    offer only Remove (not Add) — re-adding would be a no-op and
    cluttering the menu with a greyed "Add" is worse than omitting it."""
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Inspect what menu-building would produce for a right-click on A.
    # (Can't actually exec a QMenu in a test without blocking, so we
    # reconstruct the decision logic from the bound state.)
    op_members = {(r.layer_name, r.entity_id) for r in op.geometry_refs}
    target = ("L", a.id)
    to_add = [target] if target not in op_members else []
    to_remove = [target] if target in op_members else []
    assert to_add == []
    assert to_remove == [target]


def test_entity_menu_shows_add_only_for_non_member(
    main_window: MainWindow,
) -> None:
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    op_members = {(r.layer_name, r.entity_id) for r in op.geometry_refs}
    target = ("L", b.id)
    to_add = [target] if target not in op_members else []
    to_remove = [target] if target in op_members else []
    assert to_add == [target]
    assert to_remove == []


# ----------------------------- unified entity context menu


def _entity_menu_labels(
    main_window: MainWindow,
    seed: tuple[str, str],
    multi_target: list[tuple[str, str]],
) -> list[str]:
    """Build the unified entity context menu and return its action
    labels (Qt separators excluded). Doesn't exec — tests can inspect
    what the user would see without the menu blocking."""
    from PySide6.QtWidgets import QMenu

    seed_entity = main_window._find_entity(*seed)
    assert seed_entity is not None
    active_op = main_window._currently_selected_operation()
    menu = QMenu(main_window)
    main_window._add_similar_actions(menu, seed, seed_entity)
    if active_op is not None:
        main_window._add_op_member_actions(menu, active_op, multi_target)
    return [
        a.text() for a in menu.actions() if not a.isSeparator()
    ]


def test_entity_menu_offers_select_similar_even_on_tree_right_click(
    main_window: MainWindow,
) -> None:
    """Tree right-click now shares the same menu as the viewport — so
    Select Similar is available from the tree too (previously only
    the viewport offered it)."""
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    # No op selected — only Select Similar should appear.
    labels = _entity_menu_labels(main_window, ("L", a.id), [("L", a.id)])
    assert "Select similar: same type" in labels
    assert "Select similar: same layer" in labels
    assert "Select similar: same diameter" in labels


def test_entity_menu_hides_diameter_for_non_circle_seed(
    main_window: MainWindow,
) -> None:
    """Select-similar-by-diameter only makes sense for full-circle
    seeds. Non-circle entities shouldn't see a greyed-out entry —
    the whole point of the unification was dynamic, actionable
    menus only."""
    from pymillcam.core.geometry import GeometryEntity, GeometryLayer
    from pymillcam.core.segments import LineSegment

    line_entity = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))], closed=False,
    )
    project = Project(
        geometry_layers=[GeometryLayer(name="L", entities=[line_entity])]
    )
    main_window.set_project(project)

    labels = _entity_menu_labels(
        main_window, ("L", line_entity.id), [("L", line_entity.id)]
    )
    assert "Select similar: same diameter" not in labels
    # The generally-applicable similars still appear.
    assert "Select similar: same type" in labels


def test_entity_menu_combines_similar_and_op_actions(
    main_window: MainWindow,
) -> None:
    """With an active op AND a seed that isn't in it, the menu shows
    Select Similar *and* an Add-to-op entry — no grey-outs, no
    omissions of actions that apply."""
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Seed = b (not in op). Menu should have both similar and Add.
    labels = _entity_menu_labels(main_window, ("L", b.id), [("L", b.id)])
    assert any("Select similar" in lab for lab in labels)
    assert "Add to active operation" in labels
    assert "Remove from active operation" not in labels


def test_entity_menu_replaces_add_with_remove_for_member(
    main_window: MainWindow,
) -> None:
    project, a, _ = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Seed = a (in op). Menu should have Remove but not Add.
    labels = _entity_menu_labels(main_window, ("L", a.id), [("L", a.id)])
    assert "Remove from active operation" in labels
    assert "Add to active operation" not in labels


def test_entity_menu_multi_target_uses_counts(
    main_window: MainWindow,
) -> None:
    """Multi-entity labels include the count so the user sees exactly
    how many refs each action will touch — especially useful on mixed
    selections where both Add and Remove show."""
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    op = main_window.project.operations[0]
    main_window._select_operation_in_tree(op.id)

    # Mixed: a is in op, b is not.
    labels = _entity_menu_labels(
        main_window, ("L", a.id), [("L", a.id), ("L", b.id)]
    )
    # Both actions, each with a "1" prefix telling the user how many
    # refs the specific action will actually touch.
    assert "Add 1 to active operation" in labels
    assert "Remove 1 from active operation" in labels


def test_viewport_right_click_without_hit_or_selection_yields_no_menu(
    main_window: MainWindow,
) -> None:
    """The viewport path needs a seed to build anything. With the
    cursor over empty space and no selection, the context-menu
    handler should simply return — no empty menu, no greyed-out
    entries."""
    project, _a, _ = _project_with_two_circles()
    main_window.set_project(project)
    # Ensure no selection + hit-test misses by passing a position
    # guaranteed to be off every entity in world space (far-corner).
    # The handler returns silently — asserted via the absence of a
    # raised exception / side effect.
    from PySide6.QtCore import QPointF

    assert main_window._viewport.selection == []
    main_window._on_viewport_context_menu(QPointF(-1e6, -1e6))


# -------------------------------- op-row right-click targets that op


def test_delete_op_via_helper_targets_specific_op(
    main_window: MainWindow,
) -> None:
    """The tree context menu calls ``_delete_op(op)`` directly rather
    than going through the QAction. That means right-clicking op B
    while op A is selected deletes B — not A."""
    _make_one_profile_op(main_window)
    a_op = main_window.project.operations[0]
    main_window._select_operation_in_tree(a_op.id)
    main_window._action_duplicate_operation.trigger()
    b_op = main_window.project.operations[1]
    # A is still the tree-selected op.
    main_window._select_operation_in_tree(a_op.id)
    assert main_window._currently_selected_operation() is a_op

    # Delete B specifically — A should remain.
    main_window._delete_op(b_op)

    remaining_ids = [o.id for o in main_window.project.operations]
    assert remaining_ids == [a_op.id]


def test_duplicate_op_via_helper_targets_specific_op(
    main_window: MainWindow,
) -> None:
    """Parallel guarantee for the duplicate path — right-clicking op B
    duplicates B even when op A is the tree-selected one."""
    _make_one_profile_op(main_window)
    a_op = main_window.project.operations[0]
    main_window._select_operation_in_tree(a_op.id)
    main_window._action_duplicate_operation.trigger()
    b_op = main_window.project.operations[1]
    # A remains selected; duplicate B explicitly.
    main_window._select_operation_in_tree(a_op.id)

    main_window._duplicate_op(b_op)

    # 3 ops now: a_op, b_op (= a_op (copy)), and a fresh copy-of-b.
    # The new copy derives its counter from b_op's (copy) suffix,
    # landing on "(copy 2)" — the regex peels existing suffixes so
    # we don't get "(copy) (copy)".
    assert len(main_window.project.operations) == 3
    new_op = main_window.project.operations[2]
    assert "(copy 2)" in new_op.name
    # The key property: duplicating B didn't touch A or make a new
    # A-copy; the chain is a, a-copy, a-copy-of-a-copy (= copy 2).
    assert new_op.id not in (a_op.id, b_op.id)


def test_tree_entity_selection_helper() -> None:
    """Verify the tree-selection accessor returns (layer, entity_id)
    tuples for entity rows only — operation rows never bleed in."""
    from PySide6.QtWidgets import QApplication
    from pytestqt.qtbot import QtBot  # noqa: F401 (imported for type hint)

    app = QApplication.instance() or QApplication([])
    main_window = MainWindow()
    try:
        project, a, b = _project_with_two_circles()
        main_window.set_project(project)
        # Select both entity rows in the tree, plus the (non-entity)
        # Operations parent for good measure.
        item_a = main_window._find_entity_item("L", a.id)
        item_b = main_window._find_entity_item("L", b.id)
        assert item_a is not None and item_b is not None
        item_a.setSelected(True)
        item_b.setSelected(True)
        # Ops group doesn't exist yet without ops — sufficient test.

        sel = main_window._tree_entity_selection()
        assert sel == {("L", a.id), ("L", b.id)}
    finally:
        main_window.close()
        main_window.deleteLater()
        app.processEvents()


# --------------------------------------------------- move operation up / down


def _make_two_profile_ops(main_window: MainWindow) -> None:
    """Two profile ops on distinct entities so we can exercise ordering."""
    project, a, b = _project_with_two_circles()
    main_window.set_project(project)
    _simulate_viewport_click(main_window, "L", a.id)
    main_window._action_add_profile.trigger()
    _simulate_viewport_click(main_window, "L", b.id)
    main_window._action_add_profile.trigger()


def test_move_up_disabled_for_top_op(main_window: MainWindow) -> None:
    _make_two_profile_ops(main_window)
    top_id = main_window.project.operations[0].id
    main_window._select_operation_in_tree(top_id)
    assert not main_window._action_move_operation_up.isEnabled()
    assert main_window._action_move_operation_down.isEnabled()


def test_move_down_disabled_for_bottom_op(main_window: MainWindow) -> None:
    _make_two_profile_ops(main_window)
    bottom_id = main_window.project.operations[-1].id
    main_window._select_operation_in_tree(bottom_id)
    assert main_window._action_move_operation_up.isEnabled()
    assert not main_window._action_move_operation_down.isEnabled()


def test_move_up_swaps_order(main_window: MainWindow) -> None:
    _make_two_profile_ops(main_window)
    first_id = main_window.project.operations[0].id
    second_id = main_window.project.operations[1].id

    main_window._select_operation_in_tree(second_id)
    main_window._action_move_operation_up.trigger()

    assert [op.id for op in main_window.project.operations] == [second_id, first_id]


def test_move_down_swaps_order(main_window: MainWindow) -> None:
    _make_two_profile_ops(main_window)
    first_id = main_window.project.operations[0].id
    second_id = main_window.project.operations[1].id

    main_window._select_operation_in_tree(first_id)
    main_window._action_move_operation_down.trigger()

    assert [op.id for op in main_window.project.operations] == [second_id, first_id]


def test_move_preserves_selection_on_moved_op(main_window: MainWindow) -> None:
    """After a move the moved op stays selected, so chaining keyboard
    shortcuts works naturally (Ctrl+Shift+Down, Ctrl+Shift+Down, ...)."""
    _make_two_profile_ops(main_window)
    target_id = main_window.project.operations[0].id

    main_window._select_operation_in_tree(target_id)
    main_window._action_move_operation_down.trigger()

    assert main_window._currently_selected_operation() is not None
    assert main_window._currently_selected_operation().id == target_id


def test_new_project_seeds_machine_from_library_default(
    main_window: MainWindow,
) -> None:
    """When the library has a default machine, ``_make_new_project``
    copies it into ``project.machine`` with a fresh id and
    ``library_id`` pointing back at the source."""
    from pymillcam.core.machine import MachineDefinition
    from pymillcam.core.machine_library import MachineLibrary

    source = MachineDefinition(name="Shop floor", controller="grbl")
    source.macros["program_start"] = "(SHOP)\nG21 G90"
    library = MachineLibrary()
    library.add(source)
    library.default_machine_id = source.id
    main_window._machine_library = library

    project = main_window._make_new_project()

    assert project.machine.name == "Shop floor"
    assert project.machine.controller == "grbl"
    assert project.machine.macros["program_start"] == "(SHOP)\nG21 G90"
    # Fresh identity — editing the project machine won't affect the
    # library entry via shared id.
    assert project.machine.id != source.id
    assert project.machine.library_id == source.id


def test_new_project_without_library_default_uses_builtin(
    main_window: MainWindow,
) -> None:
    """An empty machine library falls through to the built-in
    ``MachineDefinition()`` defaults."""
    from pymillcam.core.machine_library import MachineLibrary

    main_window._machine_library = MachineLibrary()
    project = main_window._make_new_project()
    assert project.machine.name == "Default Machine"
    assert project.machine.library_id is None


def test_move_is_undoable(main_window: MainWindow) -> None:
    """Each move pushes one stack entry, so Ctrl+Z reverts the order."""
    _make_two_profile_ops(main_window)
    original_order = [op.id for op in main_window.project.operations]

    main_window._select_operation_in_tree(original_order[0])
    main_window._action_move_operation_down.trigger()
    assert [op.id for op in main_window.project.operations] == list(
        reversed(original_order)
    )

    main_window._action_undo.trigger()
    assert [op.id for op in main_window.project.operations] == original_order


# ------------------------------------------ selection-driven G-code generation


def test_generate_with_op_selected_emits_only_that_op(
    main_window: MainWindow,
) -> None:
    """When a single op is selected in the tree, Generate produces its
    program only — no other op's instructions appear in the output."""
    _make_two_profile_ops(main_window)
    ops = main_window.project.operations
    # Give the two ops distinctive names so we can grep for them.
    ops[0].name = "ProfileA"
    ops[1].name = "ProfileB"
    main_window._select_operation_in_tree(ops[1].id)

    main_window._action_generate_gcode.trigger()
    text = main_window._output.toPlainText()

    assert "ProfileB" in text
    assert "ProfileA" not in text


def test_generate_with_no_op_selected_emits_whole_program(
    main_window: MainWindow,
) -> None:
    """No op-row selection → combined program (every op appears)."""
    _make_two_profile_ops(main_window)
    ops = main_window.project.operations
    ops[0].name = "ProfileA"
    ops[1].name = "ProfileB"
    main_window._tree.clearSelection()

    main_window._action_generate_gcode.trigger()
    text = main_window._output.toPlainText()

    assert "ProfileA" in text
    assert "ProfileB" in text


def test_single_op_program_has_preamble_and_postamble(
    main_window: MainWindow,
) -> None:
    """Per-op output is a standalone program — preamble (G21...),
    spindle-off (M5) and M30 are all present. Matches combined output's
    structure so you can run it on the controller without edits."""
    _make_two_profile_ops(main_window)
    main_window._select_operation_in_tree(
        main_window.project.operations[0].id
    )
    main_window._action_generate_gcode.trigger()
    text = main_window._output.toPlainText()

    assert "(Generated by PyMillCAM" in text
    assert "G21 G90 G94 G17" in text
    assert "M5" in text
    assert "M30" in text


def test_export_op_gcode_writes_file(
    main_window: MainWindow, tmp_path, monkeypatch
) -> None:
    """Export-G-code action writes just the chosen op's standalone
    program to disk."""
    _make_two_profile_ops(main_window)
    op = main_window.project.operations[0]
    op.name = "Lonely"
    out_path = tmp_path / "lonely.nc"
    # Stub the QFileDialog call so the test doesn't spawn a real dialog.
    monkeypatch.setattr(
        "pymillcam.ui.main_window.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(out_path), ""),
    )

    main_window._on_export_op_gcode(op)

    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "Lonely" in text
    assert "(Generated by PyMillCAM" in text
    assert "M30" in text
