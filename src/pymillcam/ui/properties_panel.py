"""Properties panel for editing the currently selected operation.

Currently only `ProfileOp` is supported — the only operation type the
engine can handle. As more operation types land, swap the single
form widget for a stack indexed by op type.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.operations import (
    LeadConfig,
    LeadStyle,
    MillingDirection,
    OffsetSide,
    ProfileOp,
    RampConfig,
    RampStrategy,
)
from pymillcam.core.tools import ToolController


class PropertiesPanel(QWidget):
    """Hosts an editable form for the currently selected operation."""

    operation_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._operation: ProfileOp | None = None
        self._tool_controller: ToolController | None = None
        self._suspend_signals = False

        self._stack = QStackedWidget(self)
        self._empty = QLabel("Select an operation to edit its parameters.")
        self._empty.setMargin(12)
        self._stack.addWidget(self._empty)

        self._form = _ProfileForm()
        self._stack.addWidget(self._form)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self._form.name.textEdited.connect(self._on_field_changed)
        self._form.offset_side.currentTextChanged.connect(self._on_field_changed)
        self._form.direction.currentTextChanged.connect(self._on_field_changed)
        self._form.cut_depth.valueChanged.connect(self._on_field_changed)
        self._form.multi_depth.toggled.connect(self._on_field_changed)
        self._form.stepdown.valueChanged.connect(self._on_field_changed)
        self._form.chord_tolerance.valueChanged.connect(self._on_field_changed)
        self._form.chord_override.toggled.connect(self._on_field_changed)
        self._form.tool_diameter.valueChanged.connect(self._on_field_changed)
        self._form.lead_in_style.currentTextChanged.connect(self._on_field_changed)
        self._form.lead_in_length.valueChanged.connect(self._on_field_changed)
        self._form.lead_out_style.currentTextChanged.connect(self._on_field_changed)
        self._form.lead_out_length.valueChanged.connect(self._on_field_changed)
        self._form.ramp_strategy.currentTextChanged.connect(self._on_field_changed)
        self._form.ramp_angle.valueChanged.connect(self._on_field_changed)

    def set_operation(
        self,
        operation: ProfileOp | None,
        tool_controller: ToolController | None = None,
    ) -> None:
        self._operation = operation
        self._tool_controller = tool_controller
        if operation is None:
            self._stack.setCurrentWidget(self._empty)
            return
        self._suspend_signals = True
        try:
            self._form.name.setText(operation.name)
            self._form.offset_side.setCurrentText(operation.offset_side.value)
            self._form.direction.setCurrentText(operation.direction.value)
            self._form.cut_depth.setValue(operation.cut_depth)
            self._form.multi_depth.setChecked(operation.multi_depth)
            self._form.stepdown.setValue(
                operation.stepdown if operation.stepdown is not None else 1.0
            )
            self._form.stepdown.setEnabled(operation.multi_depth)
            override = operation.chord_tolerance is not None
            self._form.chord_override.setChecked(override)
            self._form.chord_tolerance.setEnabled(override)
            if override:
                self._form.chord_tolerance.setValue(operation.chord_tolerance or 0.05)
            if tool_controller is not None:
                diameter = float(tool_controller.tool.geometry.get("diameter", 3.0))
                self._form.tool_diameter.setValue(diameter)
                self._form.tool_diameter.setEnabled(True)
            else:
                self._form.tool_diameter.setEnabled(False)
            self._populate_lead(
                operation.lead_in,
                self._form.lead_in_style,
                self._form.lead_in_length,
            )
            self._populate_lead(
                operation.lead_out,
                self._form.lead_out_style,
                self._form.lead_out_length,
            )
            self._form.ramp_strategy.setCurrentText(operation.ramp.strategy.value)
            self._form.ramp_angle.setValue(operation.ramp.angle_deg)
        finally:
            self._suspend_signals = False
        self._stack.setCurrentWidget(self._form)

    @staticmethod
    def _populate_lead(
        config: LeadConfig,
        style: QComboBox,
        length: QDoubleSpinBox,
    ) -> None:
        style.setCurrentText(config.style.value)
        length.setValue(config.length)

    def _on_field_changed(self) -> None:
        if self._suspend_signals or self._operation is None:
            return
        op = self._operation
        op.name = self._form.name.text()
        op.offset_side = OffsetSide(self._form.offset_side.currentText())
        op.direction = MillingDirection(self._form.direction.currentText())
        op.cut_depth = self._form.cut_depth.value()
        op.multi_depth = self._form.multi_depth.isChecked()
        self._form.stepdown.setEnabled(op.multi_depth)
        op.stepdown = self._form.stepdown.value() if op.multi_depth else None
        override = self._form.chord_override.isChecked()
        self._form.chord_tolerance.setEnabled(override)
        op.chord_tolerance = (
            self._form.chord_tolerance.value() if override else None
        )
        if self._tool_controller is not None:
            self._tool_controller.tool.geometry["diameter"] = (
                self._form.tool_diameter.value()
            )
        op.lead_in = LeadConfig(
            style=LeadStyle(self._form.lead_in_style.currentText()),
            length=self._form.lead_in_length.value(),
        )
        op.lead_out = LeadConfig(
            style=LeadStyle(self._form.lead_out_style.currentText()),
            length=self._form.lead_out_length.value(),
        )
        op.ramp = RampConfig(
            strategy=RampStrategy(self._form.ramp_strategy.currentText()),
            angle_deg=self._form.ramp_angle.value(),
            # Preserve fields we're not editing in the UI yet.
            radius=op.ramp.radius,
        )
        self.operation_changed.emit()


class _ProfileForm(QWidget):
    """The actual editable form. Held in its own widget so its fields are typed."""

    def __init__(self) -> None:
        super().__init__()
        self.name = QLineEdit()
        self.offset_side = QComboBox()
        self.offset_side.addItems([s.value for s in OffsetSide])
        self.direction = QComboBox()
        self.direction.addItems([d.value for d in MillingDirection])
        self.cut_depth = QDoubleSpinBox()
        self.cut_depth.setRange(-1000.0, 1000.0)
        self.cut_depth.setDecimals(3)
        self.cut_depth.setSingleStep(0.5)
        self.cut_depth.setSuffix(" mm")
        self.multi_depth = QCheckBox("Multi-pass")
        self.stepdown = QDoubleSpinBox()
        self.stepdown.setRange(0.001, 100.0)
        self.stepdown.setDecimals(3)
        self.stepdown.setSingleStep(0.5)
        self.stepdown.setSuffix(" mm")
        self.chord_override = QCheckBox("Override project default")
        self.chord_tolerance = QDoubleSpinBox()
        self.chord_tolerance.setRange(0.001, 5.0)
        self.chord_tolerance.setDecimals(3)
        self.chord_tolerance.setSingleStep(0.01)
        self.chord_tolerance.setSuffix(" mm")
        self.tool_diameter = QDoubleSpinBox()
        self.tool_diameter.setRange(0.05, 100.0)
        self.tool_diameter.setDecimals(3)
        self.tool_diameter.setSingleStep(0.5)
        self.tool_diameter.setSuffix(" mm")
        self.lead_in_style, self.lead_in_length = _make_lead_widgets()
        self.lead_out_style, self.lead_out_length = _make_lead_widgets()
        self.ramp_strategy = QComboBox()
        self.ramp_strategy.addItems([s.value for s in RampStrategy])
        self.ramp_angle = QDoubleSpinBox()
        self.ramp_angle.setRange(0.01, 45.0)
        self.ramp_angle.setDecimals(2)
        self.ramp_angle.setSingleStep(0.5)
        self.ramp_angle.setSuffix(" °")

        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Tool diameter", self.tool_diameter)
        form.addRow("Offset side", self.offset_side)
        form.addRow("Direction", self.direction)
        form.addRow("Cut depth", self.cut_depth)
        form.addRow("", self.multi_depth)
        form.addRow("Stepdown", self.stepdown)
        form.addRow("Chord tolerance", self.chord_override)
        form.addRow("", self.chord_tolerance)
        form.addRow("Lead-in style", self.lead_in_style)
        form.addRow("Lead-in length", self.lead_in_length)
        form.addRow("Lead-out style", self.lead_out_style)
        form.addRow("Lead-out length", self.lead_out_length)
        form.addRow("Ramp strategy", self.ramp_strategy)
        form.addRow("Ramp angle", self.ramp_angle)


def _make_lead_widgets() -> tuple[QComboBox, QDoubleSpinBox]:
    style = QComboBox()
    style.addItems([s.value for s in LeadStyle])
    length = QDoubleSpinBox()
    length.setRange(0.0, 100.0)
    length.setDecimals(3)
    length.setSingleStep(0.5)
    length.setSuffix(" mm")
    return style, length
