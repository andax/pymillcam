"""Application Preferences dialog.

A small modal form bound to an `AppPreferences` instance. The dialog
edits a copy in-place; on accept, the caller persists it.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.preferences import AppPreferences

PREFERENCES_FILENAME = "preferences.json"


def default_preferences_path() -> Path:
    """Per-platform path for the app preferences file."""
    base = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    )
    return base / PREFERENCES_FILENAME


class PreferencesDialog(QDialog):
    """Modal dialog for editing `AppPreferences`."""

    def __init__(
        self, prefs: AppPreferences, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self._prefs = prefs.model_copy(deep=True)

        self._chord = QDoubleSpinBox()
        self._chord.setRange(0.001, 5.0)
        self._chord.setDecimals(3)
        self._chord.setSingleStep(0.01)
        self._chord.setSuffix(" mm")
        self._chord.setValue(prefs.default_chord_tolerance_mm)

        self._tool_diameter = QDoubleSpinBox()
        self._tool_diameter.setRange(0.05, 100.0)
        self._tool_diameter.setDecimals(3)
        self._tool_diameter.setSingleStep(0.5)
        self._tool_diameter.setSuffix(" mm")
        self._tool_diameter.setValue(prefs.default_tool_diameter_mm)

        self._auto_stitch = QCheckBox("Auto-stitch separate LINE entities on DXF import")
        self._auto_stitch.setChecked(prefs.auto_stitch_on_import)
        self._auto_stitch.toggled.connect(self._sync_stitch_enabled)

        self._stitch_tol = QDoubleSpinBox()
        self._stitch_tol.setRange(0.0001, 5.0)
        self._stitch_tol.setDecimals(4)
        self._stitch_tol.setSingleStep(0.001)
        self._stitch_tol.setSuffix(" mm")
        self._stitch_tol.setValue(prefs.stitch_tolerance_mm)

        self._coalesce = QSpinBox()
        self._coalesce.setRange(0, 5000)
        self._coalesce.setSingleStep(50)
        self._coalesce.setSuffix(" ms")
        self._coalesce.setValue(prefs.edit_coalesce_ms)

        form = QFormLayout()
        form.addRow("Default chord tolerance", self._chord)
        form.addRow("Default tool diameter", self._tool_diameter)
        form.addRow(self._auto_stitch)
        form.addRow("Stitch tolerance", self._stitch_tol)
        form.addRow("Edit coalescing window", self._coalesce)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._sync_stitch_enabled(self._auto_stitch.isChecked())

    def _sync_stitch_enabled(self, enabled: bool) -> None:
        self._stitch_tol.setEnabled(enabled)

    def result_preferences(self) -> AppPreferences:
        """Return a fresh AppPreferences reflecting the dialog's current values."""
        return self._prefs.model_copy(
            update={
                "default_chord_tolerance_mm": self._chord.value(),
                "default_tool_diameter_mm": self._tool_diameter.value(),
                "auto_stitch_on_import": self._auto_stitch.isChecked(),
                "stitch_tolerance_mm": self._stitch_tol.value(),
                "edit_coalesce_ms": self._coalesce.value(),
            }
        )
