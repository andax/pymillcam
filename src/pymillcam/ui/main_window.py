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

from pathlib import Path

from PySide6.QtCore import Qt
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

from pymillcam.core.geometry import GeometryLayer
from pymillcam.core.operations import (
    GeometryRef,
    OffsetSide,
    ProfileOp,
)
from pymillcam.core.project import Project
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
from pymillcam.ui.properties_panel import PropertiesPanel
from pymillcam.ui.viewport import Viewport

# Qt.ItemDataRole.UserRole stores a tuple describing what a tree item maps to:
#   ("layer", layer_name)                   — layer header row
#   ("entity", layer_name, entity_id)       — leaf entity row
#   ("ops_group",)                          — top-level "Operations" parent
#   ("operation", operation_id)             — operation row under that parent
_TreeRef = tuple[str, ...]

DEFAULT_TOOL_DIAMETER_MM = 3.0


class MainWindow(QMainWindow):
    """Top-level window. Menus and docks only; content follows in later commits."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PyMillCAM")
        self.resize(1280, 800)
        self._apply_dock_styling()

        self._project = Project()
        self._project_path: Path | None = None
        self._syncing_selection = False

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
        tree = QTreeWidget()
        tree.setHeaderLabels(["Layers & Operations"])
        tree.setObjectName("tree")
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
        self._action_redo = QAction("&Redo", self)
        self._action_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self._action_redo.setEnabled(False)
        edit_menu.addAction(self._action_undo)
        edit_menu.addAction(self._action_redo)

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
        self._action_add_profile = QAction("Add &Profile", self)
        self._action_add_profile.setEnabled(False)
        self._action_add_profile.triggered.connect(self._on_add_profile)
        self._action_generate_gcode = QAction("&Generate G-code", self)
        self._action_generate_gcode.setEnabled(False)
        self._action_generate_gcode.triggered.connect(self._on_generate_gcode)
        ops_menu.addAction(self._action_add_profile)
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
        self._project = project
        self._viewport.set_layers(project.geometry_layers)
        self._properties.set_operation(None)
        self._viewport.clear_profile_preview()
        self._viewport.clear_toolpath_preview()
        self._rebuild_tree()
        self._viewport.fit_to_view()
        self._refresh_action_state()

    def _refresh_action_state(self) -> None:
        self._action_generate_gcode.setEnabled(bool(self._project.operations))
        # "Add Profile" enables only when a geometry entity is selected.
        layer, entity_id = self._viewport.selected
        self._action_add_profile.setEnabled(bool(layer and entity_id))

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
        try:
            layers: list[GeometryLayer] = import_dxf(path)
        except DxfImportError as exc:
            QMessageBox.critical(self, "DXF import failed", str(exc))
            return
        project = Project(name=path.stem, geometry_layers=layers)
        self.set_project(project)
        self.statusBar().showMessage(f"Imported {path.name}", 5000)

    # ----------------------------------------------------------- selection

    def _on_tree_selection_changed(self) -> None:
        if self._syncing_selection:
            return
        items = self._tree.selectedItems()
        if not items:
            self._viewport.set_selected(None, None)
            self._properties.set_operation(None)
            self._viewport.clear_profile_preview()
            self._refresh_action_state()
            return
        ref: _TreeRef = items[0].data(0, Qt.ItemDataRole.UserRole)
        if ref and ref[0] == "entity":
            _, layer_name, entity_id = ref
            self._viewport.set_selected(layer_name, entity_id)
            self._properties.set_operation(None)
            self._viewport.clear_profile_preview()
        elif ref and ref[0] == "operation":
            op = self._find_operation(ref[1])
            self._viewport.set_selected(None, None)
            self._properties.set_operation(op, self._tool_controller_for(op) if op else None)
            self._update_profile_preview(op)
        else:
            self._viewport.set_selected(None, None)
            self._properties.set_operation(None)
            self._viewport.clear_profile_preview()
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
        self, layer_name: str | None, entity_id: str | None
    ) -> None:
        self._syncing_selection = True
        try:
            self._tree.clearSelection()
            if layer_name and entity_id:
                item = self._find_entity_item(layer_name, entity_id)
                if item is not None:
                    item.setSelected(True)
                    self._tree.scrollToItem(item)
            self._properties.set_operation(None)
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

    def _on_add_profile(self) -> None:
        layer_name, entity_id = self._viewport.selected
        if not layer_name or not entity_id:
            return
        tc = self._create_tool_controller()
        self._project.tool_controllers.append(tc)
        op = ProfileOp(
            name=self._next_op_name(),
            tool_controller_id=tc.tool_number,
            cut_depth=-3.0,
            offset_side=OffsetSide.OUTSIDE,
            geometry_refs=[GeometryRef(layer_name=layer_name, entity_id=entity_id)],
        )
        self._project.operations.append(op)
        self._rebuild_tree()
        self._select_operation_in_tree(op.id)
        self._refresh_action_state()

    def _next_op_name(self) -> str:
        return f"Profile {len(self._project.operations) + 1}"

    def _create_tool_controller(self) -> ToolController:
        next_number = (
            max((tc.tool_number for tc in self._project.tool_controllers), default=0) + 1
        )
        tool = Tool(name=f"{DEFAULT_TOOL_DIAMETER_MM:g}mm endmill", shape=ToolShape.ENDMILL)
        tool.geometry["diameter"] = DEFAULT_TOOL_DIAMETER_MM
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
