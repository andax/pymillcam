"""Properties panel for editing the currently selected operation.

Architecture:

- ``OperationFormBase`` is the abstract QWidget each op-type's form
  inherits from. It owns the widgets, the populate / writeback logic,
  and a single ``field_changed`` signal. Subclasses wire their input
  widgets with ``self._wire(...)``; everything downstream is uniform.

- ``FORM_REGISTRY`` maps op type → form class. Adding a new op type
  (DrillOp, SurfaceOp, ...) means writing one form class and registering
  it; ``PropertiesPanel`` itself doesn't change.

- ``PropertiesPanel`` is just the host: a QStackedWidget with the empty
  placeholder plus one widget per registered form, with ``set_operation``
  looking up the form for the op's type and binding the data.

This shape is meant to scale past 2 op types cleanly — the old pattern
(one ``_populate_X`` + one ``_on_X_changed`` + one per-field signal wire
per type, dispatched via ``isinstance``) would have multiplied linearly
with each op type.
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
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.operations import (
    LeadConfig,
    LeadStyle,
    MillingDirection,
    OffsetSide,
    Operation,
    PocketOp,
    PocketStrategy,
    ProfileOp,
    RampConfig,
    RampStrategy,
    TabConfig,
)
from pymillcam.core.tools import ToolController


class OperationFormBase(QWidget):
    """Abstract form for one operation type.

    Subclasses:

    1. Build their input widgets as attributes in ``__init__``.
    2. Call ``self._wire(widget.changeSignal, ...)`` once per widget so
       the base emits ``field_changed`` on any user edit.
    3. Implement ``populate(op, tc)`` — read the op model into the
       widgets. Called while signals are suspended; will NOT re-emit.
    4. Implement ``write_back(op, tc)`` — read the widgets into the op
       model. ``field_changed`` has already fired by the time this runs.
    """

    field_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._suspend_signals = False

    def _wire(self, *signals: object) -> None:
        """Hook input-widget change signals to ``field_changed``.

        Accepts Qt ``Signal`` bound methods — ``widget.textEdited``,
        ``widget.valueChanged``, etc. Keeps every subclass from having
        to write its own ``widget.sig.connect(self._emit)`` boilerplate.
        """
        for sig in signals:
            sig.connect(self._maybe_emit)  # type: ignore[attr-defined]

    def _maybe_emit(self, *args: object, **kwargs: object) -> None:
        if not self._suspend_signals:
            self.field_changed.emit()

    # -- Bind / unbind -------------------------------------------------

    def bind(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        """Populate the form for ``op`` without triggering ``field_changed``."""
        self._suspend_signals = True
        try:
            self.populate(op, tool_controller)
        finally:
            self._suspend_signals = False

    # -- Abstract -----------------------------------------------------

    def populate(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        """Read ``op`` into the form widgets. Override in subclasses."""
        raise NotImplementedError

    def write_back(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        """Read the form widgets into ``op``. Override in subclasses."""
        raise NotImplementedError


FORM_REGISTRY: dict[type[Operation], type[OperationFormBase]] = {}


def register_form(op_type: type[Operation]):
    """Class decorator: register a form for an op type.

    Usage::

        @register_form(DrillOp)
        class _DrillForm(OperationFormBase):
            ...

    Registration happens at module import. PropertiesPanel instances
    created after the module is imported see the form automatically.
    """

    def decorator(form_cls: type[OperationFormBase]) -> type[OperationFormBase]:
        FORM_REGISTRY[op_type] = form_cls
        return form_cls

    return decorator


class PropertiesPanel(QWidget):
    """Hosts an editable form for the currently selected operation."""

    operation_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._operation: Operation | None = None
        self._tool_controller: ToolController | None = None

        self._stack = QStackedWidget(self)
        self._empty = QLabel("Select an operation to edit its parameters.")
        self._empty.setMargin(12)
        self._stack.addWidget(self._empty)

        # One form instance per registered type, eagerly built so test
        # code and toolpath previews can reach into the widgets before
        # any op is bound. New op types register after import and will
        # be picked up on the next PropertiesPanel instance.
        self._forms: dict[type[Operation], OperationFormBase] = {}
        for op_type, form_cls in FORM_REGISTRY.items():
            form = form_cls()
            form.field_changed.connect(self._on_form_field_changed)
            self._stack.addWidget(form)
            self._forms[op_type] = form

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

    # -- Back-compat shims -------------------------------------------

    @property
    def _profile_form(self) -> _ProfileForm:
        """Deprecated direct accessor used by tests and older callers.

        Prefer ``panel.form_for(ProfileOp)`` in new code.
        """
        return self._forms[ProfileOp]  # type: ignore[return-value]

    @property
    def _pocket_form(self) -> _PocketForm:
        return self._forms[PocketOp]  # type: ignore[return-value]

    # -- Public API ---------------------------------------------------

    def form_for(
        self, op_type: type[Operation]
    ) -> OperationFormBase | None:
        """Return the form widget for an op type, or None if unregistered."""
        return self._forms.get(op_type)

    def set_operation(
        self,
        operation: Operation | None,
        tool_controller: ToolController | None = None,
    ) -> None:
        """Bind the panel to ``operation``. ``None`` shows the empty state."""
        self._operation = operation
        self._tool_controller = tool_controller
        if operation is None:
            self._stack.setCurrentWidget(self._empty)
            return
        form = self._forms.get(type(operation))
        if form is None:
            self._stack.setCurrentWidget(self._empty)
            return
        form.bind(operation, tool_controller)
        self._stack.setCurrentWidget(form)

    # -- Signal plumbing ---------------------------------------------

    def _on_form_field_changed(self) -> None:
        """Route a form-level edit to the bound op, then re-emit at panel level."""
        op = self._operation
        if op is None:
            return
        form = self._forms.get(type(op))
        if form is None:
            return
        form.write_back(op, self._tool_controller)
        self.operation_changed.emit()


# ---------------------------------------------------------------- profile form


@register_form(ProfileOp)
class _ProfileForm(OperationFormBase):
    """Form widgets + populate/write-back for ProfileOp."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.tabs_enabled = QCheckBox("Enable tabs")
        self.tabs_count = QSpinBox()
        self.tabs_count.setRange(1, 50)
        self.tabs_count.setSingleStep(1)
        self.tabs_height = QDoubleSpinBox()
        self.tabs_height.setRange(0.05, 50.0)
        self.tabs_height.setDecimals(3)
        self.tabs_height.setSingleStep(0.1)
        self.tabs_height.setSuffix(" mm")
        self.tabs_width = QDoubleSpinBox()
        self.tabs_width.setRange(0.1, 100.0)
        self.tabs_width.setDecimals(3)
        self.tabs_width.setSingleStep(0.5)
        self.tabs_width.setSuffix(" mm")
        self.tabs_ramp_length = QDoubleSpinBox()
        self.tabs_ramp_length.setRange(0.0, 100.0)
        self.tabs_ramp_length.setDecimals(3)
        self.tabs_ramp_length.setSingleStep(0.5)
        self.tabs_ramp_length.setSuffix(" mm")

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
        form.addRow("", self.tabs_enabled)
        form.addRow("Tab count", self.tabs_count)
        form.addRow("Tab height", self.tabs_height)
        form.addRow("Tab width", self.tabs_width)
        form.addRow("Tab ramp length", self.tabs_ramp_length)

        self._wire(
            self.name.textEdited,
            self.offset_side.currentTextChanged,
            self.direction.currentTextChanged,
            self.cut_depth.valueChanged,
            self.multi_depth.toggled,
            self.stepdown.valueChanged,
            self.chord_tolerance.valueChanged,
            self.chord_override.toggled,
            self.tool_diameter.valueChanged,
            self.lead_in_style.currentTextChanged,
            self.lead_in_length.valueChanged,
            self.lead_out_style.currentTextChanged,
            self.lead_out_length.valueChanged,
            self.ramp_strategy.currentTextChanged,
            self.ramp_angle.valueChanged,
            self.tabs_enabled.toggled,
            self.tabs_count.valueChanged,
            self.tabs_height.valueChanged,
            self.tabs_width.valueChanged,
            self.tabs_ramp_length.valueChanged,
        )

    def populate(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        assert isinstance(op, ProfileOp)
        self.name.setText(op.name)
        self.offset_side.setCurrentText(op.offset_side.value)
        self.direction.setCurrentText(op.direction.value)
        self.cut_depth.setValue(op.cut_depth)
        self.multi_depth.setChecked(op.multi_depth)
        self.stepdown.setValue(op.stepdown if op.stepdown is not None else 1.0)
        self.stepdown.setEnabled(op.multi_depth)
        override = op.chord_tolerance is not None
        self.chord_override.setChecked(override)
        self.chord_tolerance.setEnabled(override)
        if override:
            self.chord_tolerance.setValue(op.chord_tolerance or 0.05)
        if tool_controller is not None:
            diameter = float(tool_controller.tool.geometry.get("diameter", 3.0))
            self.tool_diameter.setValue(diameter)
            self.tool_diameter.setEnabled(True)
        else:
            self.tool_diameter.setEnabled(False)
        _populate_lead(op.lead_in, self.lead_in_style, self.lead_in_length)
        _populate_lead(op.lead_out, self.lead_out_style, self.lead_out_length)
        self.ramp_strategy.setCurrentText(op.ramp.strategy.value)
        self.ramp_angle.setValue(op.ramp.angle_deg)
        self.tabs_enabled.setChecked(op.tabs.enabled)
        self.tabs_count.setValue(op.tabs.count)
        self.tabs_height.setValue(op.tabs.height)
        self.tabs_width.setValue(op.tabs.width)
        self.tabs_ramp_length.setValue(op.tabs.ramp_length)
        for w in (self.tabs_count, self.tabs_height, self.tabs_width, self.tabs_ramp_length):
            w.setEnabled(op.tabs.enabled)

    def write_back(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        assert isinstance(op, ProfileOp)
        op.name = self.name.text()
        op.offset_side = OffsetSide(self.offset_side.currentText())
        op.direction = MillingDirection(self.direction.currentText())
        op.cut_depth = self.cut_depth.value()
        op.multi_depth = self.multi_depth.isChecked()
        self.stepdown.setEnabled(op.multi_depth)
        op.stepdown = self.stepdown.value() if op.multi_depth else None
        override = self.chord_override.isChecked()
        self.chord_tolerance.setEnabled(override)
        op.chord_tolerance = self.chord_tolerance.value() if override else None
        if tool_controller is not None:
            tool_controller.tool.geometry["diameter"] = self.tool_diameter.value()
        op.lead_in = LeadConfig(
            style=LeadStyle(self.lead_in_style.currentText()),
            length=self.lead_in_length.value(),
        )
        op.lead_out = LeadConfig(
            style=LeadStyle(self.lead_out_style.currentText()),
            length=self.lead_out_length.value(),
        )
        op.ramp = RampConfig(
            strategy=RampStrategy(self.ramp_strategy.currentText()),
            angle_deg=self.ramp_angle.value(),
            radius=op.ramp.radius,
        )
        op.tabs = TabConfig(
            enabled=self.tabs_enabled.isChecked(),
            style=op.tabs.style,
            count=self.tabs_count.value(),
            width=self.tabs_width.value(),
            height=self.tabs_height.value(),
            ramp_length=self.tabs_ramp_length.value(),
            auto_place=op.tabs.auto_place,
        )
        for w in (self.tabs_count, self.tabs_height, self.tabs_width, self.tabs_ramp_length):
            w.setEnabled(op.tabs.enabled)


# ----------------------------------------------------------------- pocket form


@register_form(PocketOp)
class _PocketForm(OperationFormBase):
    """Form widgets + populate/write-back for PocketOp."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.rest_machining = QCheckBox("Rest machining (V-notch cleanup)")

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
        form.addRow("", self.rest_machining)

        self._wire(
            self.name.textEdited,
            self.strategy.currentTextChanged,
            self.direction.currentTextChanged,
            self.tool_diameter.valueChanged,
            self.cut_depth.valueChanged,
            self.stepover.valueChanged,
            self.angle_deg.valueChanged,
            self.multi_depth.toggled,
            self.stepdown.valueChanged,
            self.ramp_strategy.currentTextChanged,
            self.ramp_angle.valueChanged,
            self.ramp_radius.valueChanged,
            self.rest_machining.toggled,
        )

    def populate(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        assert isinstance(op, PocketOp)
        self.name.setText(op.name)
        self.strategy.setCurrentText(op.strategy.value)
        self.direction.setCurrentText(op.direction.value)
        self.cut_depth.setValue(op.cut_depth)
        self.stepover.setValue(op.stepover)
        self.angle_deg.setValue(op.angle_deg)
        self.angle_deg.setEnabled(op.strategy is PocketStrategy.ZIGZAG)
        self.multi_depth.setChecked(op.multi_depth)
        self.stepdown.setValue(op.stepdown if op.stepdown is not None else 1.0)
        self.stepdown.setEnabled(op.multi_depth)
        self.ramp_strategy.setCurrentText(op.ramp.strategy.value)
        self.ramp_angle.setValue(op.ramp.angle_deg)
        self.ramp_radius.setValue(op.ramp.radius)
        self.rest_machining.setChecked(op.rest_machining)
        if tool_controller is not None:
            diameter = float(tool_controller.tool.geometry.get("diameter", 3.0))
            self.tool_diameter.setValue(diameter)
            self.tool_diameter.setEnabled(True)
        else:
            self.tool_diameter.setEnabled(False)

    def write_back(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        assert isinstance(op, PocketOp)
        op.name = self.name.text()
        op.strategy = PocketStrategy(self.strategy.currentText())
        op.direction = MillingDirection(self.direction.currentText())
        op.cut_depth = self.cut_depth.value()
        op.stepover = self.stepover.value()
        op.angle_deg = self.angle_deg.value()
        self.angle_deg.setEnabled(op.strategy is PocketStrategy.ZIGZAG)
        op.multi_depth = self.multi_depth.isChecked()
        self.stepdown.setEnabled(op.multi_depth)
        op.stepdown = self.stepdown.value() if op.multi_depth else None
        op.ramp = RampConfig(
            strategy=RampStrategy(self.ramp_strategy.currentText()),
            angle_deg=self.ramp_angle.value(),
            radius=self.ramp_radius.value(),
        )
        op.rest_machining = self.rest_machining.isChecked()
        if tool_controller is not None:
            tool_controller.tool.geometry["diameter"] = self.tool_diameter.value()


# ------------------------------------------------------------------- helpers


def _populate_lead(
    config: LeadConfig,
    style: QComboBox,
    length: QDoubleSpinBox,
) -> None:
    style.setCurrentText(config.style.value)
    length.setValue(config.length)


def _make_lead_widgets() -> tuple[QComboBox, QDoubleSpinBox]:
    style = QComboBox()
    style.addItems([s.value for s in LeadStyle])
    length = QDoubleSpinBox()
    length.setRange(0.0, 100.0)
    length.setDecimals(3)
    length.setSingleStep(0.5)
    length.setSuffix(" mm")
    return style, length
