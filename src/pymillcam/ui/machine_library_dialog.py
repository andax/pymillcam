"""Machine Library dialog.

Browse / edit the application-wide ``MachineLibrary``. Edits happen on a
deep copy of the library so Cancel truly discards changes; the caller
reads ``result_library()`` on Accepted and persists via
``core.machine_library.save_library``.

Layout mirrors the Tool Library dialog — a list on the left, a form on
the right, and a button row at the bottom. The "default machine" is the
one new projects inherit; the list shows it with a leading asterisk so
the user can see at a glance which one that is.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.machine import MachineDefinition
from pymillcam.core.machine_library import MachineLibrary
from pymillcam.post import registered_controller_names

LIBRARY_FILENAME = "machine_library.json"


def default_library_path() -> Path:
    """Per-platform path for the machine library file.

    Sits alongside ``tool_library.json`` and ``preferences.json`` so a
    user wiping their PyMillCAM config gets everything in one place.
    """
    base = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
    )
    return base / LIBRARY_FILENAME


class MachineLibraryDialog(QDialog):
    """Modal dialog for editing a ``MachineLibrary``."""

    def __init__(
        self, library: MachineLibrary, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Machine Library")
        self.resize(720, 480)
        self._library = library.model_copy(deep=True)
        self._selected_index: int | None = None
        # Guard against field-change signals firing while we programmatically
        # populate the form for a newly-selected machine — otherwise
        # populate would turn into write_back and corrupt the model.
        self._suspend_writeback = False

        self._build_ui()
        self._rebuild_list(
            select=0 if self._library.machines else None
        )

    # ------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_selection_changed)

        self._btn_new = QPushButton("New")
        self._btn_new.clicked.connect(self._on_new)
        self._btn_duplicate = QPushButton("Duplicate")
        self._btn_duplicate.clicked.connect(self._on_duplicate)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_default = QPushButton("Set as default")
        self._btn_default.clicked.connect(self._on_set_default)

        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self._btn_new)
        list_buttons.addWidget(self._btn_duplicate)
        list_buttons.addWidget(self._btn_delete)

        left = QVBoxLayout()
        left.addWidget(self._list, stretch=1)
        left.addLayout(list_buttons)
        left.addWidget(self._btn_default)

        left_container = QWidget()
        left_container.setLayout(left)
        left_container.setFixedWidth(240)

        self._form = self._build_form()

        middle = QHBoxLayout()
        middle.addWidget(left_container)
        middle.addWidget(self._form, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(middle, stretch=1)
        root.addWidget(buttons)

    def _build_form(self) -> QWidget:
        self._name = QLineEdit()
        self._name.textEdited.connect(self._on_field_changed)
        self._controller = QComboBox()
        self._controller.setEditable(True)
        self._controller.addItems(registered_controller_names())
        self._controller.currentTextChanged.connect(self._on_field_changed)

        self._safe_height = QDoubleSpinBox()
        self._safe_height.setRange(0.1, 500.0)
        self._safe_height.setDecimals(2)
        self._safe_height.setSingleStep(1.0)
        self._safe_height.setSuffix(" mm")
        self._safe_height.valueChanged.connect(self._on_field_changed)
        self._clearance_plane = QDoubleSpinBox()
        self._clearance_plane.setRange(0.01, 100.0)
        self._clearance_plane.setDecimals(2)
        self._clearance_plane.setSingleStep(0.5)
        self._clearance_plane.setSuffix(" mm")
        self._clearance_plane.valueChanged.connect(self._on_field_changed)

        self._program_start = QPlainTextEdit()
        self._program_start.setFixedHeight(64)
        self._program_start.textChanged.connect(self._on_field_changed)
        self._program_end = QPlainTextEdit()
        self._program_end.setFixedHeight(64)
        self._program_end.textChanged.connect(self._on_field_changed)
        self._tool_change = QPlainTextEdit()
        self._tool_change.setFixedHeight(80)
        self._tool_change.textChanged.connect(self._on_field_changed)

        form_layout = QFormLayout()
        form_layout.addRow("Name", self._name)
        form_layout.addRow("Controller", self._controller)
        form_layout.addRow("Safe height", self._safe_height)
        form_layout.addRow("Clearance plane", self._clearance_plane)
        form_layout.addRow("Program start", self._program_start)
        form_layout.addRow("Program end", self._program_end)
        form_layout.addRow("Tool change", self._tool_change)

        hint = QLabel(
            "Use <code>{tool_number}</code> inside <i>Tool change</i> to "
            "insert the target tool number. Use the default machine when "
            "starting a new project via the button on the left."
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)

        wrapper = QVBoxLayout()
        wrapper.addLayout(form_layout)
        wrapper.addWidget(hint)
        wrapper.addStretch(1)

        widget = QWidget()
        widget.setLayout(wrapper)
        return widget

    # ----------------------------------------------------------- list ops

    def _rebuild_list(self, *, select: int | None) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for machine in self._library.machines:
            marker = "* " if machine.id == self._library.default_machine_id else ""
            self._list.addItem(f"{marker}{machine.name}")
        if select is not None and 0 <= select < self._list.count():
            self._list.setCurrentRow(select)
        else:
            self._selected_index = None
            self._populate_form(None)
        self._list.blockSignals(False)
        # The signal was blocked during setCurrentRow, so drive the
        # selection handler manually to refresh the form.
        if select is not None and 0 <= select < self._list.count():
            self._on_selection_changed(select)

    def _on_selection_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._library.machines):
            self._selected_index = None
            self._populate_form(None)
            return
        self._selected_index = index
        self._populate_form(self._library.machines[index])

    def _on_new(self) -> None:
        machine = MachineDefinition(name="New machine")
        self._library.add(machine)
        self._rebuild_list(select=len(self._library.machines) - 1)

    def _on_duplicate(self) -> None:
        if self._selected_index is None:
            return
        source = self._library.machines[self._selected_index]
        clone = deepcopy(source)
        clone.id = MachineDefinition().id
        clone.library_id = None
        clone.name = f"{source.name} (copy)"
        self._library.add(clone)
        self._rebuild_list(select=len(self._library.machines) - 1)

    def _on_delete(self) -> None:
        if self._selected_index is None:
            return
        target = self._library.machines[self._selected_index]
        self._library.remove(target.id)
        # Move the selection one entry up so the user keeps a row
        # selected (or clears if the library is now empty).
        new_index: int | None = (
            max(0, self._selected_index - 1)
            if self._library.machines
            else None
        )
        self._rebuild_list(select=new_index)

    def _on_set_default(self) -> None:
        if self._selected_index is None:
            return
        self._library.default_machine_id = self._library.machines[
            self._selected_index
        ].id
        self._rebuild_list(select=self._selected_index)

    # ----------------------------------------------------------- form ops

    def _populate_form(self, machine: MachineDefinition | None) -> None:
        self._suspend_writeback = True
        try:
            enabled = machine is not None
            self._name.setEnabled(enabled)
            self._controller.setEnabled(enabled)
            self._safe_height.setEnabled(enabled)
            self._clearance_plane.setEnabled(enabled)
            self._program_start.setEnabled(enabled)
            self._program_end.setEnabled(enabled)
            self._tool_change.setEnabled(enabled)
            self._btn_delete.setEnabled(enabled)
            self._btn_duplicate.setEnabled(enabled)
            self._btn_default.setEnabled(enabled)
            if machine is None:
                self._name.clear()
                self._controller.setCurrentText("")
                self._safe_height.setValue(self._safe_height.minimum())
                self._clearance_plane.setValue(self._clearance_plane.minimum())
                self._program_start.setPlainText("")
                self._program_end.setPlainText("")
                self._tool_change.setPlainText("")
                return
            self._name.setText(machine.name)
            self._controller.setCurrentText(machine.controller)
            self._safe_height.setValue(machine.defaults.safe_height)
            self._clearance_plane.setValue(machine.defaults.clearance_plane)
            self._program_start.setPlainText(
                machine.macros.get("program_start", "")
            )
            self._program_end.setPlainText(
                machine.macros.get("program_end", "")
            )
            self._tool_change.setPlainText(
                machine.macros.get("tool_change", "")
            )
        finally:
            self._suspend_writeback = False

    def _on_field_changed(self) -> None:
        """Write each form field back to the selected machine as the
        user types, and refresh the list row when the name changes."""
        if self._suspend_writeback or self._selected_index is None:
            return
        machine = self._library.machines[self._selected_index]
        machine.name = self._name.text()
        machine.controller = self._controller.currentText()
        machine.defaults.safe_height = self._safe_height.value()
        machine.defaults.clearance_plane = self._clearance_plane.value()
        machine.macros = {
            "program_start": self._program_start.toPlainText(),
            "program_end": self._program_end.toPlainText(),
            "tool_change": self._tool_change.toPlainText(),
        }
        # Update the list label so the user sees rename feedback live.
        item = self._list.item(self._selected_index)
        if item is not None:
            marker = "* " if machine.id == self._library.default_machine_id else ""
            item.setText(f"{marker}{machine.name}")

    # -------------------------------------------------------------- result

    def result_library(self) -> MachineLibrary:
        """Return the edited library after the dialog was accepted."""
        return self._library
