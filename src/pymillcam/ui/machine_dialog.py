"""Machine editor dialog.

Bound to a ``MachineDefinition``. For now the dialog surfaces only the
fields the post-processor actually consumes — name, controller, and the
three macro slots (``program_start``, ``program_end``, ``tool_change``).
Other MachineDefinition fields (travel, spindle range, capabilities)
are persisted on the model but don't have UI yet; a future pre-flight
/ feed-speed feature will grow the dialog as those fields gain meaning.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.machine import MachineDefinition


class MachineDialog(QDialog):
    """Modal dialog for editing a ``MachineDefinition``."""

    def __init__(
        self, machine: MachineDefinition, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Machine")
        # Edit a copy — caller applies on accept via `result_machine()`.
        self._machine = machine.model_copy(deep=True)

        self._name = QLineEdit(self._machine.name)
        self._controller = QLineEdit(self._machine.controller)

        # Macros — multi-line so users can paste realistic shop macros.
        # Defaults look neutral; the hint labels below each field make it
        # clear which substitutions are available.
        self._program_start = QPlainTextEdit(
            self._machine.macros.get("program_start", "")
        )
        self._program_start.setPlaceholderText("e.g. G21 G90 G94 G17")
        self._program_start.setFixedHeight(72)

        self._program_end = QPlainTextEdit(
            self._machine.macros.get("program_end", "")
        )
        self._program_end.setPlaceholderText("e.g. M5\nG53 G0 Z0\nM30")
        self._program_end.setFixedHeight(72)

        self._tool_change = QPlainTextEdit(
            self._machine.macros.get("tool_change", "")
        )
        self._tool_change.setPlaceholderText(
            "e.g. T{tool_number} M6  (ATC)\n"
            "or: M5\nG53 G0 Z0\nM0 (Change to T{tool_number})  (manual)"
        )
        self._tool_change.setFixedHeight(84)

        form = QFormLayout()
        form.addRow("Name", self._name)
        form.addRow("Controller", self._controller)
        form.addRow("Program start", self._program_start)
        form.addRow("Program end", self._program_end)
        form.addRow("Tool change", self._tool_change)

        hint = QLabel(
            "The three macros replace the post-processor's preamble, "
            "footer, and tool-change lines. Use <code>{tool_number}</code> "
            "inside <i>Tool change</i> to insert the target tool number."
        )
        hint.setWordWrap(True)
        hint.setTextFormat(hint.textFormat().RichText)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def result_machine(self) -> MachineDefinition:
        """Return a fresh ``MachineDefinition`` reflecting dialog state."""
        macros = dict(self._machine.macros)
        macros["program_start"] = self._program_start.toPlainText()
        macros["program_end"] = self._program_end.toPlainText()
        macros["tool_change"] = self._tool_change.toPlainText()
        return self._machine.model_copy(
            update={
                "name": self._name.text(),
                "controller": self._controller.text(),
                "macros": macros,
            }
        )
