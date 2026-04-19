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
    QComboBox,
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
from pymillcam.post import get_post, registered_controller_names


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
        # Controller drives which post-processor runs at generation time.
        # Editable so users can type an unregistered name (e.g. a dialect
        # shipped in a plugin); if the string doesn't match a registered
        # post the engine falls back to UCCNC.
        self._controller = QComboBox()
        self._controller.setEditable(True)
        self._controller.addItems(registered_controller_names())
        # setCurrentText falls back to typed text when the value isn't in
        # the preset list, so hand-rolled controller strings survive.
        self._controller.setCurrentText(self._machine.controller)
        # Track the controller value at the last macro-population so we
        # know which defaults to compare against when it changes. Updated
        # whenever we re-seed the macro fields (initial populate, and
        # every controller-combo change that re-seeds untouched fields).
        self._macro_base_controller = self._machine.controller

        # Macros — multi-line so users can paste realistic shop macros.
        # Initial values come from the project's machine; the placeholder
        # on each field hints at the form the post expects.
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

        # When the user picks a different controller, re-seed any macro
        # field that still matches the *old* controller's defaults so
        # switching UCCNC → GRBL actually changes the generated G-code.
        # User-customised fields are left alone.
        self._controller.currentTextChanged.connect(self._on_controller_changed)

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

    def _on_controller_changed(self, new_controller: str) -> None:
        """Swap the macro fields when the user picks a different controller.

        Each of the three macro widgets is re-seeded only when its current
        text matches the previous controller's default — i.e. the user
        hasn't customised it. That way a pristine machine flips cleanly
        from UCCNC to GRBL and back; a machine with a hand-rolled
        program-start survives the switch.
        """
        old_defaults = get_post(self._macro_base_controller).default_macros
        new_defaults = get_post(new_controller).default_macros
        widgets = {
            "program_start": self._program_start,
            "program_end": self._program_end,
            "tool_change": self._tool_change,
        }
        for key, widget in widgets.items():
            current = widget.toPlainText()
            old_default = old_defaults.get(key, "")
            if current == old_default:
                widget.setPlainText(new_defaults.get(key, ""))
        self._macro_base_controller = new_controller

    def result_machine(self) -> MachineDefinition:
        """Return a fresh ``MachineDefinition`` reflecting dialog state."""
        macros = dict(self._machine.macros)
        macros["program_start"] = self._program_start.toPlainText()
        macros["program_end"] = self._program_end.toPlainText()
        macros["tool_change"] = self._tool_change.toPlainText()
        return self._machine.model_copy(
            update={
                "name": self._name.text(),
                "controller": self._controller.currentText(),
                "macros": macros,
            }
        )
