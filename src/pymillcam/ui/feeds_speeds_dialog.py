"""Feeds & speeds calculator dialog.

Small modal that takes a tool's diameter + flute count, lets the user
pick a material, shows the resulting RPM / feed, and applies them on
OK. Wired in from the Tool Library dialog above its "Cutting data"
section so the user's "I set up a new 3 mm endmill for aluminum"
workflow isn't gated on memorising SFM tables.

The dialog is a pure read-out over :mod:`core.feeds_speeds` —
everything that's material-specific lives in ``DEFAULT_MATERIALS``
there. Result is ``(rpm, feed)`` returned via :meth:`result`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.feeds_speeds import (
    DEFAULT_MATERIALS,
    MaterialPreset,
    compute_feeds_speeds,
)


class FeedsSpeedsDialog(QDialog):
    """Modal calculator: (diameter, flutes, material) → (RPM, feed)."""

    def __init__(
        self,
        *,
        tool_diameter_mm: float,
        flute_count: int,
        parent: QWidget | None = None,
        materials: list[MaterialPreset] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Feeds && Speeds")
        self._materials = list(materials if materials is not None else DEFAULT_MATERIALS)

        self._diameter = QDoubleSpinBox()
        self._diameter.setRange(0.05, 100.0)
        self._diameter.setDecimals(3)
        self._diameter.setSingleStep(0.5)
        self._diameter.setSuffix(" mm")
        self._diameter.setValue(tool_diameter_mm)
        self._diameter.valueChanged.connect(self._recompute)

        self._flutes = QSpinBox()
        self._flutes.setRange(1, 16)
        self._flutes.setValue(max(1, flute_count))
        self._flutes.valueChanged.connect(self._recompute)

        self._material = QComboBox()
        for m in self._materials:
            self._material.addItem(m.name)
        self._material.currentIndexChanged.connect(self._recompute)

        # Read-only display of the computed values. QLabel rather than a
        # disabled spinbox so users understand it's a derived figure.
        self._rpm_label = QLabel("—")
        self._feed_label = QLabel("—")

        form = QFormLayout()
        form.addRow("Tool diameter", self._diameter)
        form.addRow("Flute count", self._flutes)
        form.addRow("Material", self._material)
        form.addRow(QLabel("<b>Suggested</b>"))
        form.addRow("Spindle RPM", self._rpm_label)
        form.addRow("Feed XY", self._feed_label)

        hint = QLabel(
            "Starting values for a hobby-class machine. Watch the cut, "
            "listen for chatter, and adjust by 10-20 % either way as "
            "needed. Tighten feed first, then RPM."
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        self._recompute()

    def _recompute(self) -> None:
        material = self._materials[self._material.currentIndex()]
        try:
            rpm, feed = compute_feeds_speeds(
                tool_diameter_mm=self._diameter.value(),
                flute_count=self._flutes.value(),
                material=material,
            )
        except ValueError:
            self._rpm_label.setText("—")
            self._feed_label.setText("—")
            return
        self._rpm_label.setText(f"{rpm} rpm")
        self._feed_label.setText(f"{feed:.1f} mm/min")

    def result(self) -> tuple[int, float]:
        """Return the computed ``(rpm, feed_mm_per_min)``.

        Recomputes from the live widget state so callers don't have to
        track whether the user wiggled anything after the last paint.
        """
        material = self._materials[self._material.currentIndex()]
        return compute_feeds_speeds(
            tool_diameter_mm=self._diameter.value(),
            flute_count=self._flutes.value(),
            material=material,
        )
