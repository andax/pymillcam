"""Main application window.

Chrome + viewport + layers/operations tree. File > Open DXF imports a
drawing into the current project; the tree and viewport stay in sync
(clicking an entity in one highlights it in the other). G-code output,
"Add Profile", and Save/Load wiring arrive in sub-commit 4.
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
from pymillcam.core.project import Project
from pymillcam.io.dxf_import import DxfImportError, import_dxf
from pymillcam.ui.viewport import Viewport

# Qt.ItemDataRole.UserRole stores a tuple describing what a tree item maps to:
#   ("layer", layer_name)                   — layer header row
#   ("entity", layer_name, entity_id)       — leaf entity row
_TreeRef = tuple[str, ...]


class MainWindow(QMainWindow):
    """Top-level window. Menus and docks only; content follows in later commits."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PyMillCAM")
        self.resize(1280, 800)
        self._apply_dock_styling()

        self._project = Project()
        self._syncing_selection = False

        self._build_viewport()
        self._build_tree_dock()
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
        self._action_save = QAction("&Save", self)
        self._action_save.setShortcut(QKeySequence.StandardKey.Save)
        self._action_save_as = QAction("Save &As...", self)
        self._action_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
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
        view_menu.addAction(self._tree_dock.toggleViewAction())
        view_menu.addAction(self._output_dock.toggleViewAction())

        ops_menu = menu_bar.addMenu("&Operations")
        self._action_add_profile = QAction("Add &Profile", self)
        self._action_add_profile.setEnabled(False)
        self._action_generate_gcode = QAction("&Generate G-code", self)
        self._action_generate_gcode.setEnabled(False)
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
        self._rebuild_tree()
        self._viewport.fit_to_view()

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
            return
        ref: _TreeRef = items[0].data(0, Qt.ItemDataRole.UserRole)
        if ref and ref[0] == "entity":
            _, layer_name, entity_id = ref
            self._viewport.set_selected(layer_name, entity_id)
        else:
            self._viewport.set_selected(None, None)

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
        finally:
            self._syncing_selection = False

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
