"""Properties panel for editing the currently selected operation.

Hosts a QStackedWidget with one form per op type (ProfileOp, PocketOp)
plus an empty-state page. `set_operation` picks the right page by
inspecting `operation.type` (via isinstance) and routes field-changed
signals to the type-specific writeback handler.
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
    PocketOp,
    PocketStrategy,
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
        self._operation: ProfileOp | PocketOp | None = None
        self._tool_controller: ToolController | None = None
        self._suspend_signals = False

        self._stack = QStackedWidget(self)
        self._empty = QLabel("Select an operation to edit its parameters.")
        self._empty.setMargin(12)
        self._stack.addWidget(self._empty)

        self._profile_form = _ProfileForm()
        self._stack.addWidget(self._profile_form)

        self._pocket_form = _PocketForm()
        self._stack.addWidget(self._pocket_form)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self._wire_profile_signals()
        self._wire_pocket_signals()

    def _wire_profile_signals(self) -> None:
        f = self._profile_form
        f.name.textEdited.connect(self._on_profile_changed)
        f.offset_side.currentTextChanged.connect(self._on_profile_changed)
        f.direction.currentTextChanged.connect(self._on_profile_changed)
        f.cut_depth.valueChanged.connect(self._on_profile_changed)
        f.multi_depth.toggled.connect(self._on_profile_changed)
        f.stepdown.valueChanged.connect(self._on_profile_changed)
        f.chord_tolerance.valueChanged.connect(self._on_profile_changed)
        f.chord_override.toggled.connect(self._on_profile_changed)
        f.tool_diameter.valueChanged.connect(self._on_profile_changed)
        f.lead_in_style.currentTextChanged.connect(self._on_profile_changed)
        f.lead_in_length.valueChanged.connect(self._on_profile_changed)
        f.lead_out_style.currentTextChanged.connect(self._on_profile_changed)
        f.lead_out_length.valueChanged.connect(self._on_profile_changed)
        f.ramp_strategy.currentTextChanged.connect(self._on_profile_changed)
        f.ramp_angle.valueChanged.connect(self._on_profile_changed)

    def _wire_pocket_signals(self) -> None:
        f = self._pocket_form
        f.name.textEdited.connect(self._on_pocket_changed)
        f.strategy.currentTextChanged.connect(self._on_pocket_changed)
        f.direction.currentTextChanged.connect(self._on_pocket_changed)
        f.tool_diameter.valueChanged.connect(self._on_pocket_changed)
        f.cut_depth.valueChanged.connect(self._on_pocket_changed)
        f.stepover.valueChanged.connect(self._on_pocket_changed)
        f.angle_deg.valueChanged.connect(self._on_pocket_changed)
        f.multi_depth.toggled.connect(self._on_pocket_changed)
        f.stepdown.valueChanged.connect(self._on_pocket_changed)
        f.ramp_strategy.currentTextChanged.connect(self._on_pocket_changed)
        f.ramp_angle.valueChanged.connect(self._on_pocket_changed)
        f.ramp_radius.valueChanged.connect(self._on_pocket_changed)

    def set_operation(
        self,
        operation: ProfileOp | PocketOp | None,
        tool_controller: ToolController | None = None,
    ) -> None:
        self._operation = operation
        self._tool_controller = tool_controller
        if operation is None:
            self._stack.setCurrentWidget(self._empty)
            return
        self._suspend_signals = True
        try:
            if isinstance(operation, ProfileOp):
                self._populate_profile(operation, tool_controller)
                self._stack.setCurrentWidget(self._profile_form)
            elif isinstance(operation, PocketOp):
                self._populate_pocket(operation, tool_controller)
                self._stack.setCurrentWidget(self._pocket_form)
            else:
                self._stack.setCurrentWidget(self._empty)
        finally:
            self._suspend_signals = False

    def _populate_profile(
        self, op: ProfileOp, tool_controller: ToolController | None
    ) -> None:
        f = self._profile_form
        f.name.setText(op.name)
        f.offset_side.setCurrentText(op.offset_side.value)
        f.direction.setCurrentText(op.direction.value)
        f.cut_depth.setValue(op.cut_depth)
        f.multi_depth.setChecked(op.multi_depth)
        f.stepdown.setValue(op.stepdown if op.stepdown is not None else 1.0)
        f.stepdown.setEnabled(op.multi_depth)
        override = op.chord_tolerance is not None
        f.chord_override.setChecked(override)
        f.chord_tolerance.setEnabled(override)
        if override:
            f.chord_tolerance.setValue(op.chord_tolerance or 0.05)
        if tool_controller is not None:
            diameter = float(tool_controller.tool.geometry.get("diameter", 3.0))
            f.tool_diameter.setValue(diameter)
            f.tool_diameter.setEnabled(True)
        else:
            f.tool_diameter.setEnabled(False)
        self._populate_lead(op.lead_in, f.lead_in_style, f.lead_in_length)
        self._populate_lead(op.lead_out, f.lead_out_style, f.lead_out_length)
        f.ramp_strategy.setCurrentText(op.ramp.strategy.value)
        f.ramp_angle.setValue(op.ramp.angle_deg)

    def _populate_pocket(
        self, op: PocketOp, tool_controller: ToolController | None
    ) -> None:
        f = self._pocket_form
        f.name.setText(op.name)
        f.strategy.setCurrentText(op.strategy.value)
        f.direction.setCurrentText(op.direction.value)
        f.cut_depth.setValue(op.cut_depth)
        f.stepover.setValue(op.stepover)
        f.angle_deg.setValue(op.angle_deg)
        f.angle_deg.setEnabled(op.strategy is PocketStrategy.ZIGZAG)
        f.multi_depth.setChecked(op.multi_depth)
        f.stepdown.setValue(op.stepdown if op.stepdown is not None else 1.0)
        f.stepdown.setEnabled(op.multi_depth)
        f.ramp_strategy.setCurrentText(op.ramp.strategy.value)
        f.ramp_angle.setValue(op.ramp.angle_deg)
        f.ramp_radius.setValue(op.ramp.radius)
        if tool_controller is not None:
            diameter = float(tool_controller.tool.geometry.get("diameter", 3.0))
            f.tool_diameter.setValue(diameter)
            f.tool_diameter.setEnabled(True)
        else:
            f.tool_diameter.setEnabled(False)

    @staticmethod
    def _populate_lead(
        config: LeadConfig,
        style: QComboBox,
        length: QDoubleSpinBox,
    ) -> None:
        style.setCurrentText(config.style.value)
        length.setValue(config.length)

    def _on_profile_changed(self) -> None:
        if self._suspend_signals or not isinstance(self._operation, ProfileOp):
            return
        op = self._operation
        f = self._profile_form
        op.name = f.name.text()
        op.offset_side = OffsetSide(f.offset_side.currentText())
        op.direction = MillingDirection(f.direction.currentText())
        op.cut_depth = f.cut_depth.value()
        op.multi_depth = f.multi_depth.isChecked()
        f.stepdown.setEnabled(op.multi_depth)
        op.stepdown = f.stepdown.value() if op.multi_depth else None
        override = f.chord_override.isChecked()
        f.chord_tolerance.setEnabled(override)
        op.chord_tolerance = f.chord_tolerance.value() if override else None
        if self._tool_controller is not None:
            self._tool_controller.tool.geometry["diameter"] = (
                f.tool_diameter.value()
            )
        op.lead_in = LeadConfig(
            style=LeadStyle(f.lead_in_style.currentText()),
            length=f.lead_in_length.value(),
        )
        op.lead_out = LeadConfig(
            style=LeadStyle(f.lead_out_style.currentText()),
            length=f.lead_out_length.value(),
        )
        op.ramp = RampConfig(
            strategy=RampStrategy(f.ramp_strategy.currentText()),
            angle_deg=f.ramp_angle.value(),
            radius=op.ramp.radius,
        )
        self.operation_changed.emit()

    def _on_pocket_changed(self) -> None:
        if self._suspend_signals or not isinstance(self._operation, PocketOp):
            return
        op = self._operation
        f = self._pocket_form
        op.name = f.name.text()
        op.strategy = PocketStrategy(f.strategy.currentText())
        op.direction = MillingDirection(f.direction.currentText())
        op.cut_depth = f.cut_depth.value()
        op.stepover = f.stepover.value()
        op.angle_deg = f.angle_deg.value()
        f.angle_deg.setEnabled(op.strategy is PocketStrategy.ZIGZAG)
        op.multi_depth = f.multi_depth.isChecked()
        f.stepdown.setEnabled(op.multi_depth)
        op.stepdown = f.stepdown.value() if op.multi_depth else None
        op.ramp = RampConfig(
            strategy=RampStrategy(f.ramp_strategy.currentText()),
            angle_deg=f.ramp_angle.value(),
            radius=f.ramp_radius.value(),
        )
        if self._tool_controller is not None:
            self._tool_controller.tool.geometry["diameter"] = (
                f.tool_diameter.value()
            )
        self.operation_changed.emit()


class _ProfileForm(QWidget):
    """The profile form. Held in its own widget so its fields are typed."""

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


class _PocketForm(QWidget):
    """The pocket form. Mirrors the profile form style."""

    def __init__(self) -> None:
        super().__init__()
        self.name = QLineEdit()
        self.strategy = QComboBox()
        self.strategy.addItems([s.value for s in PocketStrategy])
        self.direction = QComboBox()
        self.direction.addItems([d.value for d in MillingDirection])
        self.tool_diameter = QDoubleSpinBox()
        self.tool_diameter.setRange(0.05, 100.0)
        self.tool_diameter.setDecimals(3)
        self.tool_diameter.setSingleStep(0.5)
        self.tool_diameter.setSuffix(" mm")
        self.cut_depth = QDoubleSpinBox()
        self.cut_depth.setRange(-1000.0, 1000.0)
        self.cut_depth.setDecimals(3)
        self.cut_depth.setSingleStep(0.5)
        self.cut_depth.setSuffix(" mm")
        self.stepover = QDoubleSpinBox()
        self.stepover.setRange(0.001, 100.0)
        self.stepover.setDecimals(3)
        self.stepover.setSingleStep(0.25)
        self.stepover.setSuffix(" mm")
        self.angle_deg = QDoubleSpinBox()
        self.angle_deg.setRange(-180.0, 180.0)
        self.angle_deg.setDecimals(2)
        self.angle_deg.setSingleStep(15.0)
        self.angle_deg.setSuffix(" °")
        self.multi_depth = QCheckBox("Multi-pass")
        self.stepdown = QDoubleSpinBox()
        self.stepdown.setRange(0.001, 100.0)
        self.stepdown.setDecimals(3)
        self.stepdown.setSingleStep(0.5)
        self.stepdown.setSuffix(" mm")
        self.ramp_strategy = QComboBox()
        self.ramp_strategy.addItems([s.value for s in RampStrategy])
        self.ramp_angle = QDoubleSpinBox()
        self.ramp_angle.setRange(0.01, 45.0)
        self.ramp_angle.setDecimals(2)
        self.ramp_angle.setSingleStep(0.5)
        self.ramp_angle.setSuffix(" °")
        self.ramp_radius = QDoubleSpinBox()
        self.ramp_radius.setRange(0.05, 100.0)
        self.ramp_radius.setDecimals(3)
        self.ramp_radius.setSingleStep(0.25)
        self.ramp_radius.setSuffix(" mm")

        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Tool diameter", self.tool_diameter)
        form.addRow("Strategy", self.strategy)
        form.addRow("Direction", self.direction)
        form.addRow("Cut depth", self.cut_depth)
        form.addRow("Stepover", self.stepover)
        form.addRow("Zigzag angle", self.angle_deg)
        form.addRow("", self.multi_depth)
        form.addRow("Stepdown", self.stepdown)
        form.addRow("Ramp strategy", self.ramp_strategy)
        form.addRow("Ramp angle", self.ramp_angle)
        form.addRow("Ramp radius", self.ramp_radius)


def _make_lead_widgets() -> tuple[QComboBox, QDoubleSpinBox]:
    style = QComboBox()
    style.addItems([s.value for s in LeadStyle])
    length = QDoubleSpinBox()
    length.setRange(0.0, 100.0)
    length.setDecimals(3)
    length.setSingleStep(0.5)
    length.setSuffix(" mm")
    return style, length
