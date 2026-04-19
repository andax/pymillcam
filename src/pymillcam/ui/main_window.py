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

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QStyle,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from pymillcam.core.commands import CommandStack
from pymillcam.core.containment import build_pocket_regions
from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import (
    DrillOp,
    GeometryRef,
    OffsetSide,
    Operation,
    PocketOp,
    PocketStrategy,
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
from pymillcam.core.selection import (
    SimilarityMode,
    find_similar_entities,
    full_circle_radius,
)
from pymillcam.core.tool_library import (
    ToolLibrary,
    ToolLibraryLoadError,
    load_library,
    save_library,
)
from pymillcam.core.tools import Tool, ToolController, ToolShape
from pymillcam.engine.common import EngineError
from pymillcam.engine.ir import Toolpath
from pymillcam.engine.ir_walker import walk_toolpath
from pymillcam.engine.services import ToolpathService
from pymillcam.engine.time_estimate import (
    estimate_toolpath_seconds,
    format_seconds,
)
from pymillcam.io.dxf_import import DxfImportError, import_dxf
from pymillcam.io.project_io import ProjectLoadError, load_project, save_project
from pymillcam.post.uccnc import UccncPostProcessor
from pymillcam.ui.preferences_dialog import PreferencesDialog, default_preferences_path
from pymillcam.ui.properties_panel import PropertiesPanel
from pymillcam.ui.tool_library_dialog import (
    ToolLibraryDialog,
    default_library_path,
)
from pymillcam.ui.viewport import COLOR_ACTIVE_OP_MEMBER, Viewport

# Qt.ItemDataRole.UserRole stores a tuple describing what a tree item maps to:
#   ("layer", layer_name)                   — layer header row
#   ("entity", layer_name, entity_id)       — leaf entity row
#   ("ops_group",)                          — top-level "Operations" parent
#   ("operation", operation_id)             — operation row under that parent
_TreeRef = tuple[str, ...]

# Matches an operation name ending in " (copy)" or " (copy N)". Used to
# disambiguate duplicated-op names: "Drill 1" → "Drill 1 (copy)" →
# "Drill 1 (copy 2)" → "Drill 1 (copy 3)", instead of three rows all
# named "Drill 1" in the tree.
class _LayersOpsTree(QTreeWidget):
    """QTreeWidget that does *not* change selection on right-click.

    Qt's default behaviour for ``QAbstractItemView`` is to move the
    selection to the right-clicked row on ``mousePressEvent``, even
    for the right mouse button. In our tree that deselects the active
    operation the moment the user tries to right-click an entity —
    which is precisely the path the user most wants, because the
    context menu's "Add/Remove to active operation" needs an active
    op to target.

    Intercepting right-button presses here preserves whatever was
    selected when the user aimed their cursor. The normal
    ``customContextMenuRequested`` signal still fires from the
    delayed ``contextMenuEvent`` that Qt synthesises after the
    release, so MainWindow's handler runs as usual.

    Left-click behaviour is unchanged — we defer to ``super`` for
    any non-right-click press so shift/ctrl multi-select, drag-
    scroll, and everything else keep working.
    """

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            # Accept without calling super → Qt skips the default
            # "move selection to the clicked row" step. The release-
            # driven context-menu event is unaffected.
            event.accept()
            return
        super().mousePressEvent(event)


_COPY_SUFFIX_RE = re.compile(r"^(.*) \(copy(?: (\d+))?\)$")


def _next_duplicate_name(
    original_name: str, existing_names: set[str]
) -> str:
    """Return a unique copy-of-``original_name`` that isn't in ``existing_names``.

    * ``"Drill 1"`` → ``"Drill 1 (copy)"`` (first duplicate)
    * ``"Drill 1 (copy)"`` → ``"Drill 1 (copy 2)"`` (duplicate of a duplicate)
    * Collision: starts from ``(copy)`` and counts up until a name is
      free. Never grows a suffix chain ``(copy) (copy)`` — the regex
      peels an existing ``(copy N)`` suffix before adding a new one.
    """
    match = _COPY_SUFFIX_RE.match(original_name)
    if match:
        base = match.group(1)
        n = int(match.group(2)) if match.group(2) else 1
    else:
        base = original_name
        n = 0
    while True:
        n += 1
        candidate = f"{base} (copy)" if n == 1 else f"{base} (copy {n})"
        if candidate not in existing_names:
            return candidate


class MainWindow(QMainWindow):
    """Top-level window. Menus and docks only; content follows in later commits."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PyMillCAM")
        self.resize(1280, 800)
        self._apply_dock_styling()

        self._preferences_path = default_preferences_path()
        self._preferences = self._load_preferences_or_default()
        self._tool_library_path = default_library_path()
        self._tool_library = self._load_tool_library_or_default()
        self._project = self._make_new_project()
        self._project_path: Path | None = None
        self._syncing_selection = False
        self._stack = CommandStack()
        # Single engine facade; op types register inside the engine, not
        # here — this just dispatches by type.
        self._toolpath_service = ToolpathService()
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
        self._build_toolbar()
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
        self._viewport.context_menu_requested.connect(
            self._on_viewport_context_menu
        )
        self.setCentralWidget(self._viewport)

    def _build_tree_dock(self) -> None:
        from PySide6.QtWidgets import QAbstractItemView

        tree = _LayersOpsTree()
        tree.setHeaderLabels(["Layers & Operations"])
        tree.setObjectName("tree")
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._on_tree_context_menu)

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
        self._properties.set_tool_library(self._tool_library)

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
        self._action_open_project.setShortcut("Ctrl+Shift+O")
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

        tools_menu = menu_bar.addMenu("&Tools")
        self._action_tool_library = QAction("&Library…", self)
        self._action_tool_library.triggered.connect(self._on_edit_tool_library)
        tools_menu.addAction(self._action_tool_library)

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
        self._action_join_paths.setShortcut("Ctrl+J")
        self._action_join_paths.setEnabled(False)
        self._action_join_paths.triggered.connect(self._on_join_paths)
        self._action_add_profile = QAction("Add &Profile", self)
        self._action_add_profile.setShortcut("Ctrl+P")
        self._action_add_profile.setEnabled(False)
        self._action_add_profile.triggered.connect(self._on_add_profile)
        self._action_add_pocket = QAction("Add Poc&ket", self)
        self._action_add_pocket.setShortcut("Ctrl+K")
        self._action_add_pocket.setEnabled(False)
        self._action_add_pocket.triggered.connect(self._on_add_pocket)
        self._action_add_drill = QAction("Add &Drill", self)
        self._action_add_drill.setShortcut("Ctrl+D")
        self._action_add_drill.setEnabled(False)
        self._action_add_drill.triggered.connect(self._on_add_drill)
        self._action_duplicate_operation = QAction("D&uplicate operation", self)
        self._action_duplicate_operation.setShortcut("Ctrl+Shift+D")
        self._action_duplicate_operation.setEnabled(False)
        self._action_duplicate_operation.triggered.connect(
            self._on_duplicate_operation
        )
        self._action_delete_operation = QAction("&Delete operation", self)
        self._action_delete_operation.setShortcut(QKeySequence.StandardKey.Delete)
        self._action_delete_operation.setEnabled(False)
        self._action_delete_operation.triggered.connect(self._on_delete_operation)
        self._action_move_operation_up = QAction("Move operation &up", self)
        self._action_move_operation_up.setShortcut("Ctrl+Shift+Up")
        self._action_move_operation_up.setEnabled(False)
        self._action_move_operation_up.triggered.connect(
            self._on_move_operation_up
        )
        self._action_move_operation_down = QAction("Move operation d&own", self)
        self._action_move_operation_down.setShortcut("Ctrl+Shift+Down")
        self._action_move_operation_down.setEnabled(False)
        self._action_move_operation_down.triggered.connect(
            self._on_move_operation_down
        )
        self._action_add_to_op = QAction("&Add to active op", self)
        self._action_add_to_op.setShortcut("Shift+A")
        self._action_add_to_op.setEnabled(False)
        self._action_add_to_op.triggered.connect(self._on_add_to_active_op)
        self._action_remove_from_op = QAction("&Remove from active op", self)
        self._action_remove_from_op.setShortcut("Shift+R")
        self._action_remove_from_op.setEnabled(False)
        self._action_remove_from_op.triggered.connect(self._on_remove_from_active_op)
        self._action_generate_gcode = QAction("&Generate G-code", self)
        self._action_generate_gcode.setShortcut("Ctrl+G")
        self._action_generate_gcode.setEnabled(False)
        self._action_generate_gcode.triggered.connect(self._on_generate_gcode)
        ops_menu.addAction(self._action_join_paths)
        ops_menu.addSeparator()
        ops_menu.addAction(self._action_add_profile)
        ops_menu.addAction(self._action_add_pocket)
        ops_menu.addAction(self._action_add_drill)
        ops_menu.addAction(self._action_duplicate_operation)
        ops_menu.addAction(self._action_delete_operation)
        ops_menu.addAction(self._action_move_operation_up)
        ops_menu.addAction(self._action_move_operation_down)
        ops_menu.addSeparator()
        ops_menu.addAction(self._action_add_to_op)
        ops_menu.addAction(self._action_remove_from_op)
        ops_menu.addSeparator()
        ops_menu.addAction(self._action_generate_gcode)

    def _build_toolbar(self) -> None:
        """Main toolbar — reuses the existing QActions from the menus so
        enable/disable state stays in sync automatically."""
        style = self.style()
        # Map a few actions to Qt's built-in icon set. Actions without a
        # mapping fall back to their text label — QToolButtonTextBesideIcon
        # keeps the toolbar visually consistent either way.
        self._action_open_dxf.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self._action_open_project.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        self._action_save.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self._action_undo.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self._action_redo.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
        )
        self._action_delete_operation.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        )
        self._action_generate_gcode.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )

        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("main_toolbar")
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        toolbar.addAction(self._action_open_dxf)
        toolbar.addAction(self._action_open_project)
        toolbar.addAction(self._action_save)
        toolbar.addSeparator()
        toolbar.addAction(self._action_undo)
        toolbar.addAction(self._action_redo)
        toolbar.addSeparator()
        toolbar.addAction(self._action_fit)
        toolbar.addSeparator()
        toolbar.addAction(self._action_join_paths)
        toolbar.addAction(self._action_add_profile)
        toolbar.addAction(self._action_add_pocket)
        toolbar.addAction(self._action_delete_operation)
        toolbar.addSeparator()
        toolbar.addAction(self._action_generate_gcode)

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
        # Preserve op selection across the rebuild so the active-op
        # highlight survives mutations like Add/Remove from active op.
        prev_op = self._currently_selected_operation()
        prev_op_id = prev_op.id if prev_op is not None else None
        self._project = project
        self._viewport.set_layers(project.geometry_layers)
        self._properties.set_operation(None)
        self._viewport.clear_profile_preview()
        self._viewport.clear_toolpath_preview()
        self._viewport.set_active_op_refs([])
        self._rebuild_tree()
        if prev_op_id is not None and self._find_operation(prev_op_id) is not None:
            self._select_operation_in_tree(prev_op_id)
        if fit:
            self._viewport.fit_to_view()
        self._refresh_action_state()

    def _refresh_action_state(self) -> None:
        self._action_generate_gcode.setEnabled(bool(self._project.operations))
        # "Add Profile" / "Add Pocket" enable when a geometry entity is
        # selected. (Pocket needs a closed boundary; the engine will
        # reject non-closed selections at generate time.)
        self._action_add_profile.setEnabled(bool(self._viewport.selection))
        self._action_add_pocket.setEnabled(bool(self._viewport.selection))
        self._action_add_drill.setEnabled(bool(self._viewport.selection))
        # "Join paths" needs ≥ 2 selected entities to be meaningful.
        self._action_join_paths.setEnabled(len(self._viewport.selection) >= 2)
        selected_op = self._currently_selected_operation()
        has_selected_op = selected_op is not None
        self._action_delete_operation.setEnabled(has_selected_op)
        self._action_duplicate_operation.setEnabled(has_selected_op)
        # Move-up disables at the top of the list, move-down at the bottom —
        # there's no neighbour to swap with there.
        op_index = (
            self._project.operations.index(selected_op)
            if selected_op is not None
            else -1
        )
        op_count = len(self._project.operations)
        self._action_move_operation_up.setEnabled(op_index > 0)
        self._action_move_operation_down.setEnabled(
            0 <= op_index < op_count - 1
        )
        # Add/Remove-from-active-op need both an active op AND a non-empty
        # viewport selection.
        active_op = self._currently_selected_operation()
        has_selection = bool(self._viewport.selection)
        self._action_add_to_op.setEnabled(active_op is not None and has_selection)
        self._action_remove_from_op.setEnabled(active_op is not None and has_selection)
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

    def _try_estimate_op_seconds(self, op: Operation) -> float | None:
        """Generate the op's toolpath and estimate its time.

        Returns ``None`` when the op's toolpath can't be produced —
        incomplete geometry refs, unsupported op type, any engine
        error. The tree renders ``None`` as "(—)"; the running total
        ignores it so one broken op doesn't poison the total.
        """
        if not op.enabled:
            return 0.0
        try:
            tp = self._toolpath_service.generate_toolpath(op, self._project)
        except EngineError:
            return None
        if tp is None:
            return None
        return estimate_toolpath_seconds(tp)

    def _refresh_op_tree_labels(self, ops_group_item: QTreeWidgetItem) -> None:
        """In-place refresh of op labels + the ops-group total.

        Used by ``_on_operation_edited`` to pick up time-estimate changes
        without blowing away the tree's current expansion / selection
        state (which a full ``_rebuild_tree`` would reset).
        """
        total_seconds = 0.0
        has_any_estimate = False
        for j in range(ops_group_item.childCount()):
            child = ops_group_item.child(j)
            if child is None:
                continue
            child_ref: _TreeRef = child.data(0, Qt.ItemDataRole.UserRole)
            if not child_ref or child_ref[0] != "operation":
                continue
            op = self._find_operation(child_ref[1])
            if op is None:
                continue
            secs = self._try_estimate_op_seconds(op)
            if secs is not None:
                total_seconds += secs
                has_any_estimate = True
                time_suffix = f"  —  {format_seconds(secs)}"
            else:
                time_suffix = "  —  (—)"
            child.setText(0, f"{op.name} [{op.type}]{time_suffix}")
        total_suffix = (
            f"  —  total {format_seconds(total_seconds)}"
            if has_any_estimate and ops_group_item.childCount()
            else ""
        )
        ops_group_item.setText(
            0,
            f"Operations ({ops_group_item.childCount()}){total_suffix}",
        )

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

            # Compute per-op machining time while we build the tree.
            # Generation failures (incomplete op, engine doesn't support
            # the type yet, …) render as "—" rather than blowing up the
            # whole tree rebuild. The running total only sums ops that
            # estimated cleanly — otherwise an error in one op would
            # make the project total meaningless.
            op_seconds: dict[str, float | None] = {}
            total_seconds = 0.0
            has_any_estimate = False
            for op in self._project.operations:
                op_seconds[op.id] = self._try_estimate_op_seconds(op)
                if op_seconds[op.id] is not None:
                    total_seconds += op_seconds[op.id]  # type: ignore[operator]
                    has_any_estimate = True

            total_suffix = (
                f"  —  total {format_seconds(total_seconds)}"
                if has_any_estimate and self._project.operations
                else ""
            )
            ops_item = QTreeWidgetItem([
                f"Operations ({len(self._project.operations)}){total_suffix}"
            ])
            ops_item.setData(0, Qt.ItemDataRole.UserRole, ("ops_group",))
            for op in self._project.operations:
                secs = op_seconds.get(op.id)
                time_suffix = (
                    f"  —  {format_seconds(secs)}" if secs is not None
                    else "  —  (—)"
                )
                op_item = QTreeWidgetItem([
                    f"{op.name} [{op.type}]{time_suffix}"
                ])
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
            self._update_operation_preview(op)
            self._update_active_op_refs(op)
            self._edit_snapshot = self._project.model_dump(mode="json")
        else:
            self._properties.set_operation(None)
            self._viewport.clear_profile_preview()
            # Route through ``_update_active_op_refs`` (not the bare
            # ``set_active_op_refs``) so the tree-row tint clears in
            # lockstep with the viewport overlay.
            self._update_active_op_refs(None)
            self._edit_snapshot = None
        self._refresh_action_state()

    def _update_active_op_refs(
        self, op: Operation | None
    ) -> None:
        if op is None:
            self._viewport.set_active_op_refs([])
            self._update_tree_active_op_highlight(set())
            return
        refs = {(ref.layer_name, ref.entity_id) for ref in op.geometry_refs}
        self._viewport.set_active_op_refs(list(refs))
        self._update_tree_active_op_highlight(refs)

    def _update_tree_active_op_highlight(
        self, active_refs: set[tuple[str, str]]
    ) -> None:
        """Tint tree entity rows whose entities belong to the active op.

        Uses the same green as the viewport's active-op overlay so
        both surfaces visually agree: "this entity is part of the
        currently-selected operation". Foreground colour only — we
        don't touch Qt's selection state, since that's owned by the
        user's click / Shift+click interaction and would clash with
        the Shift+A / Shift+R flow that needs viewport selection to
        stay independent.
        """
        active_brush = QBrush(COLOR_ACTIVE_OP_MEMBER)
        # Default brush (no colour set) returns to the theme default.
        default_brush = QBrush()
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top is None:
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                if child is None:
                    continue
                ref = child.data(0, Qt.ItemDataRole.UserRole)
                if not ref or ref[0] != "entity":
                    continue
                key = (ref[1], ref[2])
                child.setForeground(
                    0, active_brush if key in active_refs else default_brush
                )

    def _update_operation_preview(
        self, op: Operation | None
    ) -> None:
        if op is None:
            self._viewport.clear_profile_preview()
            return
        try:
            preview = self._toolpath_service.compute_preview(op, self._project)
        except (EngineError, ValueError):
            # Live preview should never block editing — failures (e.g. an inside
            # offset that swallows the geometry) just blank the overlay.
            self._viewport.clear_profile_preview()
            return
        if not preview:
            self._viewport.clear_profile_preview()
            return
        self._viewport.set_profile_preview(preview)
        self._maybe_warn_spiral_with_islands(op)

    def _maybe_warn_spiral_with_islands(self, op: Operation) -> None:
        """Status-bar hint when SPIRAL silently falls back to OFFSET.

        SPIRAL connects consecutive rings with feed-at-depth bridges; those
        bridges could cross uncut island material, so `emit_spiral_region`
        delegates to OFFSET when islands are present. Without a visible cue
        the user sees identical output for both strategies and assumes the
        combo is broken.
        """
        if not isinstance(op, PocketOp) or op.strategy is not PocketStrategy.SPIRAL:
            return
        entities: list[GeometryEntity] = []
        for ref in op.geometry_refs:
            layer = next(
                (
                    layer for layer in self._project.geometry_layers
                    if layer.name == ref.layer_name
                ),
                None,
            )
            if layer is None:
                continue
            entities.extend(e for e in layer.entities if e.id == ref.entity_id)
        if any(islands for _boundary, islands in build_pocket_regions(entities)):
            self.statusBar().showMessage(
                "SPIRAL falls back to OFFSET for pockets with islands.", 5000
            )

    def _find_operation(
        self, operation_id: str
    ) -> Operation | None:
        return next(
            (op for op in self._project.operations if op.id == operation_id), None
        )

    def _on_viewport_selection_changed(
        self, items: list[tuple[str, str]]
    ) -> None:
        self._syncing_selection = True
        try:
            # Clear only entity-tree items; keep op selection so the
            # active-op highlight survives a viewport selection change
            # (lets the user select geometry to feed Add/Remove-from-op).
            for item in list(self._tree.selectedItems()):
                ref = item.data(0, Qt.ItemDataRole.UserRole)
                if ref and ref[0] == "entity":
                    item.setSelected(False)
            for layer_name, entity_id in items:
                tree_item = self._find_entity_item(layer_name, entity_id)
                if tree_item is not None:
                    tree_item.setSelected(True)
            if items:
                last = self._find_entity_item(*items[-1])
                if last is not None:
                    self._tree.scrollToItem(last)
            # If no op is currently selected, the properties pane and
            # preview should stay clear (old behavior). When an op IS
            # selected, leave them alone so the user keeps editing it.
            if self._currently_selected_operation() is None:
                self._properties.set_operation(None)
                self._viewport.clear_profile_preview()
                self._edit_snapshot = None
        finally:
            self._syncing_selection = False
        self._refresh_action_state()

    def _on_tree_context_menu(self, position: QPoint) -> None:
        """Right-click in the tree → context menu by item type.

        Entity rows route through the shared entity-menu builder so
        the offered actions match what the viewport offers for the
        same entity. Operation rows get the op-specific Duplicate /
        Delete menu.
        """
        item = self._tree.itemAt(position)
        if item is None:
            return
        ref: _TreeRef = item.data(0, Qt.ItemDataRole.UserRole)
        if not ref:
            return
        global_pos = self._tree.viewport().mapToGlobal(position)

        if ref[0] == "entity":
            seed = (ref[1], ref[2])
            tree_selection = self._tree_entity_selection()
            multi_target = (
                sorted(tree_selection) if seed in tree_selection else [seed]
            )
            self._exec_entity_context_menu(seed, multi_target, global_pos)
        elif ref[0] == "operation":
            self._show_tree_operation_menu(ref, global_pos)

    def _exec_entity_context_menu(
        self,
        seed: tuple[str, str],
        multi_target: list[tuple[str, str]],
        global_pos: QPoint,
    ) -> None:
        """Build, exec, and dispatch the unified entity context menu.

        Shared by tree and viewport right-clicks so the user sees the
        same choices regardless of where they clicked. Everything is
        dynamic: an action only appears if it would actually do
        something (no greyed-out entries). An empty menu never shows.

        Arguments:
          * ``seed`` — the single (layer, entity_id) identifying the
            entity the user right-clicked *on*. Used by Select Similar
            as the reference for matching.
          * ``multi_target`` — the (layer, entity_id) set that
            Add/Remove-to-op should operate on. Usually ``[seed]``, but
            for multi-selection right-clicks (either the tree's entity
            selection or the viewport's) it's the full selection so
            batch Add/Remove works in one action.
          * ``global_pos`` — where on the screen to exec the menu.
        """
        seed_entity = self._find_entity(*seed)
        if seed_entity is None:
            return
        active_op = self._currently_selected_operation()

        menu = QMenu(self)
        # Each action's ``data()`` holds a zero-arg callback that
        # executes the action. Cleaner than a long if/elif chain after
        # exec() and keeps the dispatch co-located with each action.
        self._add_similar_actions(menu, seed, seed_entity)
        if active_op is not None:
            self._add_op_member_actions(menu, active_op, multi_target)

        if menu.isEmpty():
            return
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        callback = chosen.data()
        if callable(callback):
            callback()

    def _add_similar_actions(
        self,
        menu: QMenu,
        seed: tuple[str, str],
        seed_entity: GeometryEntity,
    ) -> None:
        """Append ``Select similar: …`` entries that apply to the seed.

        ``same type`` and ``same layer`` always make sense; ``same
        diameter`` only for a full-circle seed, so non-circles don't
        carry a greyed-out diameter entry.
        """
        act = menu.addAction("Select similar: same type")
        act.setData(lambda: self._apply_select_similar(seed, SimilarityMode.SAME_TYPE))
        act = menu.addAction("Select similar: same layer")
        act.setData(lambda: self._apply_select_similar(seed, SimilarityMode.SAME_LAYER))
        if full_circle_radius(seed_entity) is not None:
            act = menu.addAction("Select similar: same diameter")
            act.setData(
                lambda: self._apply_select_similar(seed, SimilarityMode.SAME_DIAMETER)
            )

    def _add_op_member_actions(
        self,
        menu: QMenu,
        op: Operation,
        multi_target: list[tuple[str, str]],
    ) -> None:
        """Append Add/Remove-to-op entries, filtered by membership.

        * All targets outside op → only ``Add`` (single-entity label
          or ``Add N …`` when multi-target).
        * All targets inside op → only ``Remove``.
        * Mixed multi-target → both, each carrying the count of what
          it'll actually act on.

        If there's nothing to Add or Remove (shouldn't happen — every
        target is one or the other), the section is simply omitted.
        """
        op_members = {(r.layer_name, r.entity_id) for r in op.geometry_refs}
        to_add = [r for r in multi_target if r not in op_members]
        to_remove = [r for r in multi_target if r in op_members]
        if not to_add and not to_remove:
            return
        if not menu.isEmpty():
            menu.addSeparator()
        is_single = len(multi_target) == 1
        if to_add:
            label = (
                "Add to active operation"
                if is_single
                else f"Add {len(to_add)} to active operation"
            )
            act = menu.addAction(label)
            add_refs = list(to_add)
            act.setData(lambda: self._add_refs_to_op(op, add_refs))
        if to_remove:
            label = (
                "Remove from active operation"
                if is_single
                else f"Remove {len(to_remove)} from active operation"
            )
            act = menu.addAction(label)
            rem_refs = list(to_remove)
            act.setData(lambda: self._remove_refs_from_op(op, rem_refs))

    def _apply_select_similar(
        self, seed: tuple[str, str], mode: SimilarityMode
    ) -> None:
        """Replace the viewport selection with every entity matching
        ``seed`` under ``mode``. Re-emits ``selection_changed`` so the
        tree-sync / action-state pipeline runs through the normal
        path.
        """
        matches = find_similar_entities(seed[0], seed[1], self._project, mode)
        self._viewport.set_selection(matches)
        self._viewport.selection_changed.emit(matches)
        self._viewport.update()

    def _show_tree_operation_menu(
        self, right_clicked_ref: _TreeRef, global_pos: QPoint
    ) -> None:
        """Build and show the Duplicate/Delete menu for an op row.

        The menu targets the **right-clicked** op, not the currently
        tree-selected one. That way right-clicking op B while op A is
        selected deletes / duplicates B — matching every file manager
        the user has already internalised. We don't change the tree's
        selection either (the subclass suppresses selection change on
        right-click), so the user's editing context stays intact.
        """
        _, op_id = right_clicked_ref
        op = self._find_operation(op_id)
        if op is None:
            return

        menu = QMenu(self)
        act_duplicate = menu.addAction(
            self._action_duplicate_operation.text()
        )
        act_delete = menu.addAction(self._action_delete_operation.text())
        # Move entries target the right-clicked op, matching Duplicate /
        # Delete above. Hidden rather than greyed out when the op sits at
        # the list boundary — matches the unified entity context-menu
        # policy of "don't show actions that wouldn't do anything."
        ops = self._project.operations
        op_index = ops.index(op) if op in ops else -1
        act_move_up: QAction | None = None
        act_move_down: QAction | None = None
        if op_index > 0:
            act_move_up = menu.addAction(
                self._action_move_operation_up.text()
            )
        if 0 <= op_index < len(ops) - 1:
            act_move_down = menu.addAction(
                self._action_move_operation_down.text()
            )
        menu.addSeparator()
        act_export = menu.addAction("&Export G-code…")

        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is act_duplicate:
            self._duplicate_op(op)
        elif chosen is act_delete:
            self._delete_op(op)
        elif chosen is act_move_up:
            self._move_op(op, delta=-1)
        elif chosen is act_move_down:
            self._move_op(op, delta=+1)
        elif chosen is act_export:
            self._on_export_op_gcode(op)

    def _tree_entity_selection(self) -> set[tuple[str, str]]:
        """Return the (layer, entity_id) set currently selected in the tree."""
        selected: set[tuple[str, str]] = set()
        for item in self._tree.selectedItems():
            ref = item.data(0, Qt.ItemDataRole.UserRole)
            if ref and ref[0] == "entity":
                selected.add((ref[1], ref[2]))
        return selected

    def _on_viewport_context_menu(self, widget_pos: QPointF) -> None:
        """Right-click in the viewport → unified entity context menu.

        Seed resolution, in order of preference:
          1. The entity directly under the cursor (hit-test). Highest-
             intent signal; matches "right-click what you mean".
          2. If nothing is under the cursor but the user has exactly
             one entity selected, use that (preserves the pre-unified
             behaviour for users who've already clicked their target).
          3. Otherwise no menu — ambiguous target, nothing to act on.

        Multi-target for Add/Remove: if the seed is part of the
        current viewport selection, act on the whole selection;
        otherwise target only the seed.
        """
        hit = self._viewport.hit_test_widget(widget_pos)
        selection = list(self._viewport.selection)
        if hit is not None:
            seed = hit
        elif len(selection) == 1:
            seed = selection[0]
        else:
            return

        multi_target = (
            selection if seed in selection and len(selection) > 1 else [seed]
        )

        global_pos = self._viewport.mapToGlobal(widget_pos.toPoint())
        self._exec_entity_context_menu(seed, multi_target, global_pos)

    def _find_entity(
        self, layer_name: str, entity_id: str
    ) -> GeometryEntity | None:
        for layer in self._project.geometry_layers:
            if layer.name != layer_name:
                continue
            return layer.find_entity(entity_id)
        return None

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

    def _on_add_drill(self) -> None:
        targets = list(self._viewport.selection)
        if not targets:
            return
        new_op_id_box: list[str] = []

        def mutate(project: Project) -> None:
            # One DrillOp for the whole selection — matches the typical
            # "drill these N holes with the same bit" intent. The engine
            # resolves each GeometryRef to a drill point (POINT entity
            # coordinate, closed-arc/circle centre, etc.).
            tc = self._create_tool_controller_for(project)
            project.tool_controllers.append(tc)
            op = DrillOp(
                name=f"Drill {len(project.operations) + 1}",
                tool_controller_id=tc.tool_number,
                cut_depth=-3.0,
                geometry_refs=[
                    GeometryRef(layer_name=layer_name, entity_id=entity_id)
                    for layer_name, entity_id in targets
                ],
            )
            project.operations.append(op)
            new_op_id_box.append(op.id)

        description = "Add Drill" if len(targets) == 1 else f"Add Drill ({len(targets)} holes)"
        self._do_action(description, mutate)
        if new_op_id_box:
            self._select_operation_in_tree(new_op_id_box[0])

    def _on_add_pocket(self) -> None:
        targets = list(self._viewport.selection)
        if not targets:
            return
        new_op_ids: list[str] = []

        def mutate(project: Project) -> None:
            tc = self._create_tool_controller_for(project)
            project.tool_controllers.append(tc)
            # Build the containment tree from the selection so that
            # boundary + islands selected together collapse into a
            # single PocketOp per top-level boundary. Selecting a single
            # contour preserves the old one-op-per-selection behavior.
            layer_by_name = {layer.name: layer for layer in project.geometry_layers}
            entities_by_ref: list[tuple[str, str, GeometryEntity]] = []
            for layer_name, entity_id in targets:
                layer = layer_by_name.get(layer_name)
                if layer is None:
                    continue
                entity = layer.find_entity(entity_id)
                if entity is None:
                    continue
                entities_by_ref.append((layer_name, entity_id, entity))
            entities = [trip[2] for trip in entities_by_ref]
            ref_for_entity = {
                id(trip[2]): GeometryRef(layer_name=trip[0], entity_id=trip[1])
                for trip in entities_by_ref
            }
            regions = build_pocket_regions(entities)
            if not regions:
                # Fall back to one op per selected entity (open contours,
                # invalid geometry, etc.). Lets the user still create
                # ops they can edit.
                for layer_name, entity_id in targets:
                    op = PocketOp(
                        name=f"Pocket {len(project.operations) + 1}",
                        tool_controller_id=tc.tool_number,
                        cut_depth=-3.0,
                        geometry_refs=[
                            GeometryRef(layer_name=layer_name, entity_id=entity_id)
                        ],
                    )
                    project.operations.append(op)
                    new_op_ids.append(op.id)
                return
            for boundary, islands in regions:
                refs = [ref_for_entity[id(boundary)]]
                refs.extend(ref_for_entity[id(i)] for i in islands)
                op = PocketOp(
                    name=f"Pocket {len(project.operations) + 1}",
                    tool_controller_id=tc.tool_number,
                    cut_depth=-3.0,
                    geometry_refs=refs,
                )
                project.operations.append(op)
                new_op_ids.append(op.id)

        self._do_action(
            "Add Pocket" if len(targets) == 1 else "Add Pockets", mutate,
        )
        if len(new_op_ids) == 1:
            self._select_operation_in_tree(new_op_ids[0])

    def _on_add_to_active_op(self) -> None:
        op = self._currently_selected_operation()
        if op is None:
            return
        self._add_refs_to_op(op, list(self._viewport.selection))

    def _on_remove_from_active_op(self) -> None:
        op = self._currently_selected_operation()
        if op is None:
            return
        self._remove_refs_from_op(op, list(self._viewport.selection))

    def _add_refs_to_op(
        self, op: Operation, refs: list[tuple[str, str]]
    ) -> None:
        """Append (layer, entity_id) pairs to ``op.geometry_refs``.

        Used by both Shift+A (viewport selection) and the tree
        context menu (tree entity selection). Existing refs are
        skipped so the action is idempotent on a mixed selection.
        """
        if not refs:
            return
        op_id = op.id
        op_name = op.name

        def mutate(project: Project) -> None:
            target = next((o for o in project.operations if o.id == op_id), None)
            if target is None:
                return
            existing = {
                (r.layer_name, r.entity_id) for r in target.geometry_refs
            }
            for layer_name, entity_id in refs:
                if (layer_name, entity_id) in existing:
                    continue
                target.geometry_refs.append(
                    GeometryRef(layer_name=layer_name, entity_id=entity_id)
                )
                existing.add((layer_name, entity_id))

        self._do_action(f"Add {len(refs)} to {op_name}", mutate)

    def _remove_refs_from_op(
        self, op: Operation, refs: list[tuple[str, str]]
    ) -> None:
        """Drop (layer, entity_id) pairs from ``op.geometry_refs``.

        Mirror of ``_add_refs_to_op``. Refs not present in the op are
        silently ignored — again keeps the action idempotent.
        """
        if not refs:
            return
        op_id = op.id
        op_name = op.name
        to_remove = set(refs)

        def mutate(project: Project) -> None:
            target = next((o for o in project.operations if o.id == op_id), None)
            if target is None:
                return
            target.geometry_refs = [
                r for r in target.geometry_refs
                if (r.layer_name, r.entity_id) not in to_remove
            ]

        self._do_action(f"Remove {len(refs)} from {op_name}", mutate)

    def _on_delete_operation(self) -> None:
        op = self._currently_selected_operation()
        if op is None:
            return
        self._delete_op(op)

    def _delete_op(self, op: Operation) -> None:
        """Delete a specific op by id. Shared by the menu-bar action,
        the keyboard shortcut, and the tree context menu — the latter
        passes the right-clicked op directly so it works on an op the
        user hasn't left-clicked."""
        target_id = op.id

        def mutate(project: Project) -> None:
            project.operations = [
                o for o in project.operations if o.id != target_id
            ]

        self._do_action("Delete operation", mutate)

    def _on_duplicate_operation(self) -> None:
        """Entry point for the Ctrl+Shift+D QAction — duplicates the
        currently-tree-selected op. See :meth:`_duplicate_op` for the
        actual cloning semantics.
        """
        op = self._currently_selected_operation()
        if op is None:
            return
        self._duplicate_op(op)

    def _on_move_operation_up(self) -> None:
        op = self._currently_selected_operation()
        if op is not None:
            self._move_op(op, delta=-1)

    def _on_move_operation_down(self) -> None:
        op = self._currently_selected_operation()
        if op is not None:
            self._move_op(op, delta=+1)

    def _move_op(self, op: Operation, *, delta: int) -> None:
        """Swap `op` with its neighbour in `project.operations`.

        `delta=-1` moves up (toward index 0), `delta=+1` moves down.
        Order matters for G-code: operations are emitted in list order,
        so reordering changes the machining sequence. Pushes one undo
        entry and re-selects the moved op so the user can chain moves.
        """
        ops = self._project.operations
        try:
            index = ops.index(op)
        except ValueError:
            return
        new_index = index + delta
        if not 0 <= new_index < len(ops):
            return
        op_id = op.id

        def mutate(project: Project) -> None:
            project.operations[index], project.operations[new_index] = (
                project.operations[new_index],
                project.operations[index],
            )

        label = "Move operation up" if delta < 0 else "Move operation down"
        self._do_action(label, mutate)
        self._select_operation_in_tree(op_id)

    def _duplicate_op(self, op: Operation) -> None:
        """Clone a specific op.

        Primary use cases:
          * Multi-step drilling — spot / pilot / final cycles on the
            same set of holes without re-picking the geometry.
          * Roughing + finishing of the same profile or pocket with
            different stepdowns / feeds.

        Each duplicate gets:
          * A fresh ``op.id``.
          * A fresh ``ToolController`` (copied from the original so
            the starting point is the same tool + feeds, but with a
            new ``tool_number`` and fresh ``tool.id`` — the user can
            then switch it via the Tool dropdown without affecting
            the original op).
          * The same geometry_refs (references the same entities;
            editing either op's geometry assignment separately is the
            Phase 2 "edit op geometry" path, not this one).
          * A ``" (copy)"`` / ``" (copy N)"`` suffix on the name so
            the tree disambiguates copies from their originals.

        The duplicate is appended to the end of ``project.operations``
        and selected in the tree so the user can start editing it
        immediately. Called from the menu-bar action, keyboard
        shortcut, *and* the tree's right-click context menu — the
        latter passes the right-clicked op directly, which can be a
        different op than the currently-selected one.
        """
        from uuid import uuid4

        source_tc = self._tool_controller_for(op)
        new_op_id_box: list[str] = []

        def mutate(project: Project) -> None:
            # Copy the tool controller first so we have its number to
            # point the duplicated op at. ``tool_controller_id`` on an
            # op is an int (the tool_number) — we don't want the
            # duplicate silently sharing the original's controller.
            next_tc_number = (
                max(
                    (tc.tool_number for tc in project.tool_controllers),
                    default=0,
                ) + 1
            )
            if source_tc is not None:
                new_tc = source_tc.model_copy(
                    deep=True, update={"tool_number": next_tc_number}
                )
                new_tc.tool.id = uuid4().hex
                project.tool_controllers.append(new_tc)
            else:
                # Original had no ToolController resolved — synthesise
                # a library-default one, same as Add-op.
                new_tc = self._create_tool_controller_for(project)
                project.tool_controllers.append(new_tc)

            # Now the op itself. ``model_copy(deep=True)`` handles
            # geometry_refs (list of pydantic models) cleanly.
            new_name = _next_duplicate_name(
                op.name, {o.name for o in project.operations}
            )
            new_op = op.model_copy(
                deep=True,
                update={
                    "id": uuid4().hex,
                    "name": new_name,
                    "tool_controller_id": new_tc.tool_number,
                },
            )
            project.operations.append(new_op)
            new_op_id_box.append(new_op.id)

        self._do_action("Duplicate operation", mutate)
        if new_op_id_box:
            self._select_operation_in_tree(new_op_id_box[0])

    def _create_tool_controller_for(self, project: Project) -> ToolController:
        """Create a new ToolController for a freshly-added op.

        Source of defaults:
          1. The library's default tool (if set and present) — this is
             the typical path once a user has curated their library.
          2. Otherwise, a synthesised 3 mm endmill using
             ``preferences.default_tool_diameter_mm`` — preserves the
             pre-library behaviour for a first-run user with an empty
             library.

        Tools are **copied** from the library (``model_copy(deep=True)``
        with a fresh ``id``) so editing a project's op doesn't
        retroactively mutate the library, and future library edits
        don't alter existing projects. The price is that projects carry
        their own snapshot — which is the right trade-off for ``.pmc``
        portability.
        """
        next_number = (
            max((tc.tool_number for tc in project.tool_controllers), default=0) + 1
        )
        lib_tool = self._tool_library.default_tool()
        if lib_tool is not None:
            from uuid import uuid4
            # library_id points back to the source; id is fresh so the
            # per-op copy doesn't collide with the library entry.
            tool = lib_tool.model_copy(
                deep=True,
                update={"id": uuid4().hex, "library_id": lib_tool.id},
            )
            cd = tool.cutting_data.get("default")
            return ToolController(
                tool_number=next_number,
                tool=tool,
                spindle_rpm=cd.spindle_rpm if cd else 18000,
                feed_xy=cd.feed_xy if cd else 1200.0,
                feed_z=cd.feed_z if cd else 300.0,
            )
        # Library empty / no default — fall back to the preference-driven
        # synthesised tool so first-run users aren't forced to seed the
        # library before they can add a profile.
        diameter = self._preferences.default_tool_diameter_mm
        tool = Tool(name=f"{diameter:g}mm endmill", shape=ToolShape.ENDMILL)
        tool.geometry["diameter"] = diameter
        return ToolController(tool_number=next_number, tool=tool)

    def _load_tool_library_or_default(self) -> ToolLibrary:
        """Load the library from disk; on a malformed file, warn and
        continue with an empty library so the app still starts."""
        try:
            return load_library(self._tool_library_path)
        except ToolLibraryLoadError as exc:
            QMessageBox.warning(
                self,
                "Tool library",
                f"Could not load tool library:\n{exc}\n\nStarting with an empty library.",
            )
            return ToolLibrary()

    def _on_edit_tool_library(self) -> None:
        """Open the Tool Library dialog; persist on accept."""
        dialog = ToolLibraryDialog(self._tool_library, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        new_library = dialog.result_library()
        try:
            save_library(new_library, self._tool_library_path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Tool library",
                f"Saved in-memory but could not write to disk:\n{exc}",
            )
        self._tool_library = new_library
        # Refresh the Properties panel combo so its entries reflect the
        # edited library. If the currently bound op's tool was renamed
        # in the library (unlikely but possible), the sync falls back
        # to "(Custom)" until the user re-picks.
        self._properties.set_tool_library(self._tool_library)

    def _tool_controller_for(
        self, op: Operation
    ) -> ToolController | None:
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
            self._refresh_op_tree_labels(top)
        # Refresh the live operation preview against the new parameters.
        self._update_operation_preview(self._currently_selected_operation())
        # Edits invalidate any previously generated toolpath — clear both the
        # viewport overlay and the G-code text so neither is misleading.
        self._viewport.clear_toolpath_preview()
        self._output.clear()
        # Restart the coalesce timer — the actual stack push happens once the
        # user pauses for `_edit_timer.interval()` ms.
        if self._edit_snapshot is not None:
            self._edit_timer.start()

    def _currently_selected_operation(
        self,
    ) -> Operation | None:
        for item in self._tree.selectedItems():
            ref: _TreeRef = item.data(0, Qt.ItemDataRole.UserRole)
            if ref and ref[0] == "operation":
                return self._find_operation(ref[1])
        return None

    def _on_generate_gcode(self) -> None:
        """Generate G-code for the tree-selected op, or the whole program.

        Selection-driven: when the user has a single operation row
        selected we emit just that op (wrapped in the same preamble /
        postamble the combined program uses, so it's a standalone
        program you can run on the controller). Selecting the
        ``Operations`` group (or nothing) falls back to the combined
        program.
        """
        if not self._project.operations:
            return
        selected_op = self._currently_selected_operation()
        try:
            if selected_op is not None:
                gcode, toolpaths = self._generate_program_for_ops([selected_op])
                status = f"Generated G-code for {selected_op.name!r}"
            else:
                gcode, toolpaths = self._toolpath_service.generate_program(
                    self._project, UccncPostProcessor()
                )
                status = f"Generated G-code for {len(toolpaths)} operation(s)"
        except EngineError as exc:
            QMessageBox.critical(self, "G-code generation failed", str(exc))
            return
        self._output.setPlainText(gcode)
        all_moves = []
        for tp in toolpaths:
            all_moves.extend(walk_toolpath(tp.instructions))
        self._viewport.set_toolpath_preview(all_moves)
        self.statusBar().showMessage(status, 5000)

    def _generate_program_for_ops(
        self, ops: list[Operation]
    ) -> tuple[str, list[Toolpath]]:
        """Post-process a specific subset of ops into a standalone program.

        Reuses the post-processor (so preamble / postamble / spindle-off
        / M30 are all there) but feeds it only the toolpaths for `ops`
        instead of every op in the project. Disabled / unsupported ops
        resolve to ``None`` and are skipped.
        """
        toolpaths: list[Toolpath] = []
        for op in ops:
            tp = self._toolpath_service.generate_toolpath(op, self._project)
            if tp is not None:
                toolpaths.append(tp)
        gcode = UccncPostProcessor().post_program(
            toolpaths, macros=self._project.machine.macros
        )
        return gcode, toolpaths

    def _on_export_op_gcode(self, op: Operation) -> None:
        """Save the selected op's standalone G-code to a file."""
        default_name = f"{op.name or op.id}.nc"
        # Strip characters that tend to upset Windows file systems — users
        # often use "/" in op names as a logical separator.
        default_name = default_name.replace("/", "_").replace("\\", "_")
        path_str, _ = QFileDialog.getSaveFileName(
            self, f"Export G-code — {op.name}", default_name,
            "G-code files (*.nc *.ngc *.tap);;All files (*)",
        )
        if not path_str:
            return
        try:
            gcode, _ = self._generate_program_for_ops([op])
        except EngineError as exc:
            QMessageBox.critical(self, "G-code generation failed", str(exc))
            return
        Path(path_str).write_text(gcode, encoding="utf-8")
        self.statusBar().showMessage(f"Exported G-code to {Path(path_str).name}", 5000)

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
