"""Main application window.

Chrome + viewport + layers/operations tree + properties panel + G-code
output. File > Open DXF imports a drawing into the current project; the
tree and viewport stay in sync (clicking an entity in one highlights it
in the other). Operations > Add Profile builds a default ProfileOp on
the current selection; Operations > Generate G-code runs the engine and
post-processor and shows the result in the bottom dock. File > Save /
Open Project round-trips the project as JSON.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from pymillcam.core.commands import CommandStack
from pymillcam.core.geometry import GeometryLayer
from pymillcam.core.operations import (
    GeometryRef,
    OffsetSide,
    ProfileOp,
)
from pymillcam.core.path_stitching import stitch_entities
from pymillcam.core.preferences import (
    AppPreferences,
    PreferencesLoadError,
    load_preferences,
    save_preferences,
)
from pymillcam.core.project import Project, ProjectSettings
from pymillcam.core.tools import Tool, ToolController, ToolShape
from pymillcam.engine.ir_walker import walk_toolpath
from pymillcam.engine.profile import (
    ProfileGenerationError,
    compute_profile_preview,
    generate_profile_toolpath,
)
from pymillcam.io.dxf_import import DxfImportError, import_dxf
from pymillcam.io.project_io import ProjectLoadError, load_project, save_project
from pymillcam.post.uccnc import UccncPostProcessor
from pymillcam.ui.preferences_dialog import PreferencesDialog, default_preferences_path
from pymillcam.ui.properties_panel import PropertiesPanel
from pymillcam.ui.viewport import Viewport

# Qt.ItemDataRole.UserRole stores a tuple describing what a tree item maps to:
#   ("layer", layer_name)                   — layer header row
#   ("entity", layer_name, entity_id)       — leaf entity row
#   ("ops_group",)                          — top-level "Operations" parent
#   ("operation", operation_id)             — operation row under that parent
_TreeRef = tuple[str, ...]


class MainWindow(QMainWindow):
    """Top-level window. Menus and docks only; content follows in later commits."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PyMillCAM")
        self.resize(1280, 800)
        self._apply_dock_styling()

        self._preferences_path = default_preferences_path()
        self._preferences = self._load_preferences_or_default()
        self._project = self._make_new_project()
        self._project_path: Path | None = None
        self._syncing_selection = False
        self._stack = CommandStack()
        # Snapshot taken when an op gets bound to the Properties panel; used as
        # the 'before' state for any edits that follow until they're committed
        # by the idle timer below.
        self._edit_snapshot: dict[str, Any] | None = None
        self._edit_timer = QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.setInterval(self._preferences.edit_coalesce_ms)
        self._edit_timer.timeout.connect(self._commit_pending_edit)

        self._build_viewport()
        self._build_tree_dock()
        self._build_properties_dock()
        self._build_output_dock()
        self._build_menus()
        self._build_status_bar()

    @property
    def project(self) -> Project:
        return self._project

    def _apply_dock_styling(self) -> None:
        # Qt's default dock chrome blends into QPalette.Window on several Linux
        # themes, so dock boundaries and title bars are hard to see. Bump
        # contrast on the title bar and widen the drag separators without
        # touching anything else so the host theme still drives menus, buttons,
        # etc.
        self.setStyleSheet(
            """
            QDockWidget::title {
                background: rgba(0, 0, 0, 0.10);
                padding: 4px 8px;
                border-bottom: 1px solid rgba(0, 0, 0, 0.25);
                font-weight: bold;
            }
            QMainWindow::separator {
                background: rgba(0, 0, 0, 0.25);
                width: 4px;
                height: 4px;
            }
            QMainWindow::separator:hover {
                background: rgba(0, 0, 0, 0.45);
            }
            """
        )

    def _build_viewport(self) -> None:
        self._viewport = Viewport(self)
        self._viewport.setObjectName("viewport")
        self._viewport.selection_changed.connect(self._on_viewport_selection_changed)
        self.setCentralWidget(self._viewport)

    def _build_tree_dock(self) -> None:
        from PySide6.QtWidgets import QAbstractItemView

        tree = QTreeWidget()
        tree.setHeaderLabels(["Layers & Operations"])
        tree.setObjectName("tree")
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.itemSelectionChanged.connect(self._on_tree_selection_changed)

        dock = QDockWidget("Layers & Operations", self)
        dock.setObjectName("tree_dock")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        dock.setWidget(tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        self._tree = tree
        self._tree_dock = dock

    def _build_properties_dock(self) -> None:
        self._properties = PropertiesPanel(self)
        self._properties.setObjectName("properties")
        self._properties.operation_changed.connect(self._on_operation_edited)

        dock = QDockWidget("Properties", self)
        dock.setObjectName("properties_dock")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        dock.setWidget(self._properties)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._properties_dock = dock

    def _build_output_dock(self) -> None:
        output = QPlainTextEdit()
        output.setReadOnly(True)
        output.setPlaceholderText("Generated G-code will appear here.")
        output.setObjectName("output")

        dock = QDockWidget("G-code Output", self)
        dock.setObjectName("output_dock")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        dock.setWidget(output)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

        self._output = output
        self._output_dock = dock

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        self._action_open_dxf = QAction("&Open DXF...", self)
        self._action_open_dxf.setShortcut(QKeySequence.StandardKey.Open)
        self._action_open_dxf.triggered.connect(self._on_open_dxf)
        self._action_open_project = QAction("Open &Project...", self)
        self._action_open_project.triggered.connect(self._on_open_project)
        self._action_save = QAction("&Save", self)
        self._action_save.setShortcut(QKeySequence.StandardKey.Save)
        self._action_save.triggered.connect(self._on_save)
        self._action_save_as = QAction("Save &As...", self)
        self._action_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self._action_save_as.triggered.connect(self._on_save_as)
        self._action_exit = QAction("E&xit", self)
        self._action_exit.setShortcut(QKeySequence.StandardKey.Quit)
        self._action_exit.triggered.connect(self.close)

        file_menu.addAction(self._action_open_dxf)
        file_menu.addSeparator()
        file_menu.addAction(self._action_open_project)
        file_menu.addAction(self._action_save)
        file_menu.addAction(self._action_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self._action_exit)

        edit_menu = menu_bar.addMenu("&Edit")
        self._action_undo = QAction("&Undo", self)
        self._action_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self._action_undo.setEnabled(False)
        self._action_undo.triggered.connect(self._on_undo)
        self._action_redo = QAction("&Redo", self)
        self._action_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self._action_redo.setEnabled(False)
        self._action_redo.triggered.connect(self._on_redo)
        self._action_preferences = QAction("&Preferences...", self)
        self._action_preferences.setShortcut(QKeySequence.StandardKey.Preferences)
        self._action_preferences.triggered.connect(self._on_preferences)
        edit_menu.addAction(self._action_undo)
        edit_menu.addAction(self._action_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self._action_preferences)

        view_menu = menu_bar.addMenu("&View")
        self._action_fit = QAction("&Fit to View", self)
        self._action_fit.setShortcut("F")
        self._action_fit.triggered.connect(self._viewport.fit_to_view)
        view_menu.addAction(self._action_fit)
        view_menu.addSeparator()
        self._action_show_profile_preview = QAction("Show &profile preview", self)
        self._action_show_profile_preview.setCheckable(True)
        self._action_show_profile_preview.setChecked(True)
        self._action_show_profile_preview.toggled.connect(
            self._viewport.set_show_profile_preview
        )
        self._action_show_toolpath_preview = QAction("Show &toolpath preview", self)
        self._action_show_toolpath_preview.setCheckable(True)
        self._action_show_toolpath_preview.setChecked(True)
        self._action_show_toolpath_preview.toggled.connect(
            self._viewport.set_show_toolpath_preview
        )
        view_menu.addAction(self._action_show_profile_preview)
        view_menu.addAction(self._action_show_toolpath_preview)
        view_menu.addSeparator()
        view_menu.addAction(self._tree_dock.toggleViewAction())
        view_menu.addAction(self._properties_dock.toggleViewAction())
        view_menu.addAction(self._output_dock.toggleViewAction())

        ops_menu = menu_bar.addMenu("&Operations")
        self._action_join_paths = QAction("&Join paths", self)
        self._action_join_paths.setEnabled(False)
        self._action_join_paths.triggered.connect(self._on_join_paths)
        self._action_add_profile = QAction("Add &Profile", self)
        self._action_add_profile.setEnabled(False)
        self._action_add_profile.triggered.connect(self._on_add_profile)
        self._action_delete_operation = QAction("&Delete operation", self)
        self._action_delete_operation.setShortcut(QKeySequence.StandardKey.Delete)
        self._action_delete_operation.setEnabled(False)
        self._action_delete_operation.triggered.connect(self._on_delete_operation)
        self._action_generate_gcode = QAction("&Generate G-code", self)
        self._action_generate_gcode.setEnabled(False)
        self._action_generate_gcode.triggered.connect(self._on_generate_gcode)
        ops_menu.addAction(self._action_join_paths)
        ops_menu.addSeparator()
        ops_menu.addAction(self._action_add_profile)
        ops_menu.addAction(self._action_delete_operation)
        ops_menu.addSeparator()
        ops_menu.addAction(self._action_generate_gcode)

    def _build_status_bar(self) -> None:
        self._coord_label = QLabel("X: —   Y: —")
        self._coord_label.setObjectName("coord_label")
        self._coord_label.setMinimumWidth(160)
        self.statusBar().addPermanentWidget(self._coord_label)
        self._viewport.mouse_position_changed.connect(self._on_mouse_position_changed)

    def _on_mouse_position_changed(self, x: float, y: float) -> None:
        self._coord_label.setText(f"X: {x:8.3f}   Y: {y:8.3f}")

    # -------------------------------------------------------------- project

    def set_project(self, project: Project) -> None:
        """User-facing project load — clears undo history and refits view."""
        self._stack.clear()
        self._edit_snapshot = None
        self._edit_timer.stop()
        self._replace_project(project, fit=True)

    def _replace_project(self, project: Project, *, fit: bool) -> None:
        """Drop the current project for a new one; does NOT touch the undo stack."""
        self._project = project
        self._viewport.set_layers(project.geometry_layers)
        self._properties.set_operation(None)
        self._viewport.clear_profile_preview()
        self._viewport.clear_toolpath_preview()
        self._rebuild_tree()
        if fit:
            self._viewport.fit_to_view()
        self._refresh_action_state()

    def _refresh_action_state(self) -> None:
        self._action_generate_gcode.setEnabled(bool(self._project.operations))
        # "Add Profile" enables when at least one geometry entity is selected.
        self._action_add_profile.setEnabled(bool(self._viewport.selection))
        # "Join paths" needs ≥ 2 selected entities to be meaningful.
        self._action_join_paths.setEnabled(len(self._viewport.selection) >= 2)
        self._action_delete_operation.setEnabled(
            self._currently_selected_operation() is not None
        )
        self._action_undo.setEnabled(self._stack.can_undo)
        self._action_redo.setEnabled(self._stack.can_redo)
        self._action_undo.setText(
            f"&Undo {self._stack.undo_description}"
            if self._stack.can_undo
            else "&Undo"
        )
        self._action_redo.setText(
            f"&Redo {self._stack.redo_description}"
            if self._stack.can_redo
            else "&Redo"
        )

    def _do_action(self, description: str, mutator: Callable[[Project], None]) -> None:
        """Run `mutator(project)` and record the before/after as one stack entry."""
        # Any pending coalesced edit must commit first, or it would land on
        # top of this new action and confuse the history.
        self._commit_pending_edit()
        before = self._project.model_dump(mode="json")
        mutator(self._project)
        after = self._project.model_dump(mode="json")
        self._stack.push(description, before, after)
        self._replace_project(self._project, fit=False)

    def _on_undo(self) -> None:
        # If the user is mid-edit, undo first reverts the in-flight edits to
        # the snapshot (without recording them) — the natural editor feel.
        if self._edit_snapshot is not None and (
            self._project.model_dump(mode="json") != self._edit_snapshot
        ):
            self._edit_timer.stop()
            snapshot = self._edit_snapshot
            self._edit_snapshot = None
            self._replace_project(Project.model_validate(snapshot), fit=False)
            self.statusBar().showMessage("Reverted in-progress edit", 3000)
            return
        entry = self._stack.undo()
        if entry is None:
            return
        self._edit_snapshot = None
        self._replace_project(Project.model_validate(entry.before), fit=False)
        self.statusBar().showMessage(f"Undo: {entry.description}", 3000)

    def _on_redo(self) -> None:
        self._commit_pending_edit()
        entry = self._stack.redo()
        if entry is None:
            return
        self._edit_snapshot = None
        self._replace_project(Project.model_validate(entry.after), fit=False)
        self.statusBar().showMessage(f"Redo: {entry.description}", 3000)

    def _commit_pending_edit(self) -> None:
        self._edit_timer.stop()
        if self._edit_snapshot is None:
            return
        after = self._project.model_dump(mode="json")
        if after != self._edit_snapshot:
            self._stack.push("Edit operation", self._edit_snapshot, after)
            self._edit_snapshot = after
            self._refresh_action_state()

    def _rebuild_tree(self) -> None:
        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            for layer in self._project.geometry_layers:
                layer_item = QTreeWidgetItem(
                    [f"{layer.name} ({len(layer.entities)})"]
                )
                layer_item.setData(0, Qt.ItemDataRole.UserRole, ("layer", layer.name))
                for entity in layer.entities:
                    label = entity.dxf_entity_type or (
                        "POINT" if entity.point is not None else "CONTOUR"
                    )
                    entity_item = QTreeWidgetItem([label])
                    entity_item.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        ("entity", layer.name, entity.id),
                    )
                    layer_item.addChild(entity_item)
                self._tree.addTopLevelItem(layer_item)
                layer_item.setExpanded(True)

            ops_item = QTreeWidgetItem([f"Operations ({len(self._project.operations)})"])
            ops_item.setData(0, Qt.ItemDataRole.UserRole, ("ops_group",))
            for op in self._project.operations:
                op_item = QTreeWidgetItem([f"{op.name} [{op.type}]"])
                op_item.setData(0, Qt.ItemDataRole.UserRole, ("operation", op.id))
                ops_item.addChild(op_item)
            self._tree.addTopLevelItem(ops_item)
            ops_item.setExpanded(True)
        finally:
            self._tree.blockSignals(False)

    # -------------------------------------------------------------- DXF I/O

    def _on_open_dxf(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open DXF", "", "DXF files (*.dxf);;All files (*)"
        )
        if not path_str:
            return
        self.load_dxf(Path(path_str))

    def load_dxf(self, path: Path) -> None:
        """Import a DXF from disk and install its layers into a fresh project."""
        stitch_tol = (
            self._preferences.stitch_tolerance_mm
            if self._preferences.auto_stitch_on_import
            else None
        )
        try:
            layers: list[GeometryLayer] = import_dxf(path, stitch_tolerance=stitch_tol)
        except DxfImportError as exc:
            QMessageBox.critical(self, "DXF import failed", str(exc))
            return
        project = self._make_new_project()
        project.name = path.stem
        project.geometry_layers = layers
        self.set_project(project)
        self.statusBar().showMessage(f"Imported {path.name}", 5000)

    # ----------------------------------------------------------- selection

    def _on_tree_selection_changed(self) -> None:
        if self._syncing_selection:
            return
        # Switching the bound op flushes any half-finished edits on the
        # previous one — they belong to that op's history, not the next one's.
        self._commit_pending_edit()
        items = self._tree.selectedItems()
        entity_pairs: list[tuple[str, str]] = []
        op_ids: list[str] = []
        for item in items:
            ref: _TreeRef = item.data(0, Qt.ItemDataRole.UserRole)
            if not ref:
                continue
            if ref[0] == "entity":
                entity_pairs.append((ref[1], ref[2]))
            elif ref[0] == "operation":
                op_ids.append(ref[1])

        self._viewport.set_selection(entity_pairs)

        # Properties + preview only when exactly one operation is selected.
        if len(op_ids) == 1 and not entity_pairs:
            op = self._find_operation(op_ids[0])
            self._properties.set_operation(
                op, self._tool_controller_for(op) if op else None
            )
            self._update_profile_preview(op)
            self._edit_snapshot = self._project.model_dump(mode="json")
        else:
            self._properties.set_operation(None)
            self._viewport.clear_profile_preview()
            self._edit_snapshot = None
        self._refresh_action_state()

    def _update_profile_preview(self, op: ProfileOp | None) -> None:
        if op is None:
            self._viewport.clear_profile_preview()
            return
        try:
            preview = compute_profile_preview(op, self._project)
        except (ProfileGenerationError, ValueError):
            # Live preview should never block editing — failures (e.g. an inside
            # offset that swallows the geometry) just blank the overlay.
            self._viewport.clear_profile_preview()
            return
        self._viewport.set_profile_preview(preview)

    def _find_operation(self, operation_id: str) -> ProfileOp | None:
        return next(
            (op for op in self._project.operations if op.id == operation_id), None
        )

    def _on_viewport_selection_changed(
        self, items: list[tuple[str, str]]
    ) -> None:
        self._syncing_selection = True
        try:
            self._tree.clearSelection()
            for layer_name, entity_id in items:
                tree_item = self._find_entity_item(layer_name, entity_id)
                if tree_item is not None:
                    tree_item.setSelected(True)
            if items:
                last = self._find_entity_item(*items[-1])
                if last is not None:
                    self._tree.scrollToItem(last)
            self._properties.set_operation(None)
            self._viewport.clear_profile_preview()
            self._edit_snapshot = None
        finally:
            self._syncing_selection = False
        self._refresh_action_state()

    def _find_entity_item(
        self, layer_name: str, entity_id: str
    ) -> QTreeWidgetItem | None:
        for i in range(self._tree.topLevelItemCount()):
            layer_item = self._tree.topLevelItem(i)
            if layer_item is None:
                continue
            for j in range(layer_item.childCount()):
                child = layer_item.child(j)
                if child is None:
                    continue
                ref: _TreeRef = child.data(0, Qt.ItemDataRole.UserRole)
                if (
                    ref
                    and ref[0] == "entity"
                    and ref[1] == layer_name
                    and ref[2] == entity_id
                ):
                    return child
        return None

    # ---------------------------------------------------------- operations

    def _on_join_paths(self) -> None:
        """Stitch the currently-selected entities into one or more contours."""
        targets = list(self._viewport.selection)
        if len(targets) < 2:
            return
        # Group targets by layer so we stitch within each layer in isolation.
        per_layer: dict[str, list[str]] = {}
        for layer_name, entity_id in targets:
            per_layer.setdefault(layer_name, []).append(entity_id)
        tolerance = self._preferences.stitch_tolerance_mm

        def mutate(project: Project) -> None:
            for layer in project.geometry_layers:
                ids = per_layer.get(layer.name)
                if not ids:
                    continue
                selected = [e for e in layer.entities if e.id in ids]
                kept = [e for e in layer.entities if e.id not in ids]
                stitched = stitch_entities(selected, tolerance)
                layer.entities = kept + stitched

        self._do_action("Join paths", mutate)

    def _on_add_profile(self) -> None:
        targets = list(self._viewport.selection)
        if not targets:
            return
        new_op_ids: list[str] = []
        description = "Add Profile" if len(targets) == 1 else f"Add {len(targets)} Profiles"

        def mutate(project: Project) -> None:
            # All entities in one batch share one ToolController — typical CAM
            # intent is "cut these contours with the same tool".
            tc = self._create_tool_controller_for(project)
            project.tool_controllers.append(tc)
            for layer_name, entity_id in targets:
                op = ProfileOp(
                    name=f"Profile {len(project.operations) + 1}",
                    tool_controller_id=tc.tool_number,
                    cut_depth=-3.0,
                    offset_side=OffsetSide.OUTSIDE,
                    geometry_refs=[GeometryRef(layer_name=layer_name, entity_id=entity_id)],
                )
                project.operations.append(op)
                new_op_ids.append(op.id)

        self._do_action(description, mutate)
        if len(new_op_ids) == 1:
            self._select_operation_in_tree(new_op_ids[0])

    def _on_delete_operation(self) -> None:
        op = self._currently_selected_operation()
        if op is None:
            return
        target_id = op.id

        def mutate(project: Project) -> None:
            project.operations = [o for o in project.operations if o.id != target_id]

        self._do_action("Delete operation", mutate)

    def _create_tool_controller_for(self, project: Project) -> ToolController:
        diameter = self._preferences.default_tool_diameter_mm
        next_number = (
            max((tc.tool_number for tc in project.tool_controllers), default=0) + 1
        )
        tool = Tool(name=f"{diameter:g}mm endmill", shape=ToolShape.ENDMILL)
        tool.geometry["diameter"] = diameter
        return ToolController(tool_number=next_number, tool=tool)

    def _tool_controller_for(self, op: ProfileOp) -> ToolController | None:
        if op.tool_controller_id is None:
            return None
        return next(
            (
                tc
                for tc in self._project.tool_controllers
                if tc.tool_number == op.tool_controller_id
            ),
            None,
        )

    def _select_operation_in_tree(self, operation_id: str) -> None:
        item = self._find_operation_item(operation_id)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        self._tree.scrollToItem(item)

    def _find_operation_item(self, operation_id: str) -> QTreeWidgetItem | None:
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top is None:
                continue
            ref: _TreeRef = top.data(0, Qt.ItemDataRole.UserRole)
            if not ref or ref[0] != "ops_group":
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                if child is None:
                    continue
                child_ref: _TreeRef = child.data(0, Qt.ItemDataRole.UserRole)
                if child_ref and child_ref[0] == "operation" and child_ref[1] == operation_id:
                    return child
        return None

    def _on_operation_edited(self) -> None:
        # Refresh the op's tree label in case the name changed.
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top is None:
                continue
            ref: _TreeRef = top.data(0, Qt.ItemDataRole.UserRole)
            if not ref or ref[0] != "ops_group":
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                if child is None:
                    continue
                child_ref: _TreeRef = child.data(0, Qt.ItemDataRole.UserRole)
                if not child_ref or child_ref[0] != "operation":
                    continue
                op = self._find_operation(child_ref[1])
                if op is not None:
                    child.setText(0, f"{op.name} [{op.type}]")
        # Refresh the live profile preview against the new parameters.
        self._update_profile_preview(self._currently_selected_operation())
        # Edits invalidate any previously generated toolpath.
        self._viewport.clear_toolpath_preview()
        # Restart the coalesce timer — the actual stack push happens once the
        # user pauses for `_edit_timer.interval()` ms.
        if self._edit_snapshot is not None:
            self._edit_timer.start()

    def _currently_selected_operation(self) -> ProfileOp | None:
        for item in self._tree.selectedItems():
            ref: _TreeRef = item.data(0, Qt.ItemDataRole.UserRole)
            if ref and ref[0] == "operation":
                return self._find_operation(ref[1])
        return None

    def _on_generate_gcode(self) -> None:
        if not self._project.operations:
            return
        try:
            toolpaths = [
                generate_profile_toolpath(op, self._project)
                for op in self._project.operations
                if op.enabled
            ]
        except ProfileGenerationError as exc:
            QMessageBox.critical(self, "G-code generation failed", str(exc))
            return
        gcode = UccncPostProcessor().post_program(toolpaths)
        self._output.setPlainText(gcode)
        # Walk every toolpath into XY moves and show them as the
        # "what the machine will actually do" overlay.
        all_moves = []
        for tp in toolpaths:
            all_moves.extend(walk_toolpath(tp.instructions))
        self._viewport.set_toolpath_preview(all_moves)
        self.statusBar().showMessage(
            f"Generated G-code for {len(toolpaths)} operation(s)", 5000
        )

    # -------------------------------------------------------------- save/load

    def _on_open_project(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", "PyMillCAM projects (*.pmc);;All files (*)"
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            project = load_project(path)
        except ProjectLoadError as exc:
            QMessageBox.critical(self, "Open project failed", str(exc))
            return
        self.set_project(project)
        self._project_path = path
        self.statusBar().showMessage(f"Loaded {path.name}", 5000)

    def _on_save(self) -> None:
        if self._project_path is None:
            self._on_save_as()
            return
        self._save_to(self._project_path)

    def _on_save_as(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save Project As", "", "PyMillCAM projects (*.pmc);;All files (*)"
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix == "":
            path = path.with_suffix(".pmc")
        self._save_to(path)
        self._project_path = path

    def _save_to(self, path: Path) -> None:
        try:
            save_project(self._project, path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.statusBar().showMessage(f"Saved {path.name}", 5000)

    # -------------------------------------------------------------- preferences

    def _load_preferences_or_default(self) -> AppPreferences:
        try:
            return load_preferences(self._preferences_path)
        except PreferencesLoadError as exc:
            # A corrupt prefs file shouldn't keep the app from starting; tell
            # the user once and proceed with defaults.
            QMessageBox.warning(
                self,
                "Could not load preferences",
                f"{exc}\n\nFalling back to defaults.",
            )
            return AppPreferences()

    def _make_new_project(self) -> Project:
        return Project(
            settings=ProjectSettings(
                chord_tolerance=self._preferences.default_chord_tolerance_mm
            )
        )

    def _on_preferences(self) -> None:
        dialog = PreferencesDialog(self._preferences, self)
        if dialog.exec() != PreferencesDialog.DialogCode.Accepted:
            return
        new_prefs = dialog.result_preferences()
        try:
            save_preferences(new_prefs, self._preferences_path)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not save preferences",
                f"{exc}\n\nChanges will apply for this session only.",
            )
        self._preferences = new_prefs
        self._edit_timer.setInterval(new_prefs.edit_coalesce_ms)
