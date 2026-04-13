"""Main application window shell.

This sub-commit lands only the chrome: menus, dock layout, and placeholder
panels. The viewport, tree, and G-code output pane are filled in by later
sub-commits of Phase 1 step 5.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QTreeWidget,
    QWidget,
)


class MainWindow(QMainWindow):
    """Top-level window. Menus and docks only; content follows in later commits."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PyMillCAM")
        self.resize(1280, 800)
        self._apply_dock_styling()

        self._build_central_placeholder()
        self._build_tree_dock()
        self._build_output_dock()
        self._build_menus()

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

    def _build_central_placeholder(self) -> None:
        placeholder = QLabel("Viewport (coming in sub-commit 2)")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setObjectName("viewport_placeholder")
        self.setCentralWidget(placeholder)

    def _build_tree_dock(self) -> None:
        tree = QTreeWidget()
        tree.setHeaderLabels(["Layers & Operations"])
        tree.setObjectName("tree")

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
        self._action_fit.setEnabled(False)
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
