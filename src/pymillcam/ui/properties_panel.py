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

from uuid import uuid4

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pymillcam.core.operations import (
    DrillCycle,
    DrillOp,
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
from pymillcam.core.tool_library import ToolLibrary
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

    def set_tool_editable(self, editable: bool) -> None:
        """Enable or disable tool-related form widgets.

        Called by :class:`PropertiesPanel` to lock tool fields when the
        bound op is pinned to a library tool — prevents the dropdown
        label and the actual tool geometry from silently diverging.
        Re-enabled when the user picks ``(Custom)``.

        Default implementation toggles ``self.tool_diameter`` if the
        subclass exposes one. Subclasses with richer tool UIs (shape
        picker, flute count, etc.) override to cover them too.
        """
        widget = getattr(self, "tool_diameter", None)
        if widget is not None:
            widget.setEnabled(editable)

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


_CUSTOM_TOOL_LABEL = "(Custom)"


class PropertiesPanel(QWidget):
    """Hosts an editable form for the currently selected operation."""

    operation_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._operation: Operation | None = None
        self._tool_controller: ToolController | None = None
        self._tool_library = ToolLibrary()
        # Suppress programmatic combo changes from firing the user-edit
        # handler when we repopulate or sync to a new op.
        self._suspend_tool_signals = False

        # -------- Tool picker (panel-level, common to all op types) ----
        self._tool_combo = QComboBox()
        self._tool_combo.setEnabled(False)  # no op bound yet
        self._tool_combo.currentTextChanged.connect(self._on_tool_selected)

        tool_row = QHBoxLayout()
        tool_row.setContentsMargins(8, 8, 8, 0)
        tool_row.addWidget(QLabel("Tool:"))
        tool_row.addWidget(self._tool_combo, stretch=1)

        # -------- Stack of per-op-type forms ---------------------------
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
        layout.addLayout(tool_row)
        layout.addWidget(self._stack, stretch=1)

        self._rebuild_tool_combo()

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

    def set_tool_library(self, library: ToolLibrary) -> None:
        """Inform the panel about the current ToolLibrary.

        Called by MainWindow at startup and whenever the library is
        edited via ``Tools > Library…``. Rebuilds the Tool combo so its
        entries reflect the new library, then re-selects the entry that
        matches the currently-bound op's tool (if any).
        """
        self._tool_library = library
        self._rebuild_tool_combo()
        self._sync_tool_combo_to_op()

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
            self._tool_combo.setEnabled(False)
            self._sync_tool_combo_to_op()
            return
        form = self._forms.get(type(operation))
        if form is None:
            self._stack.setCurrentWidget(self._empty)
            self._tool_combo.setEnabled(False)
            self._sync_tool_combo_to_op()
            return
        form.bind(operation, tool_controller)
        self._stack.setCurrentWidget(form)
        self._tool_combo.setEnabled(True)
        self._sync_tool_combo_to_op()

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

    def _on_tool_selected(self, _display_text: str) -> None:
        """Handle a user pick in the Tool combo.

        Picking a library tool replaces the bound op's ToolController
        fields in place:

        * ``tc.tool`` becomes a deep copy of the library entry with a
          fresh ``id`` (per-op identity distinct from the library's)
          and ``library_id`` set to the library tool's ``id`` so the
          pinning survives save / load.
        * ``spindle_rpm`` / ``feed_xy`` / ``feed_z`` are seeded from
          the library tool's ``cutting_data["default"]`` when present.
        * The form is re-bound so tool-dependent widgets refresh, then
          the tool widgets are **locked** — a library-backed op's
          tool can't be tweaked ad-hoc without explicitly switching to
          ``(Custom)`` first.

        Picking ``(Custom)`` clears ``library_id`` on the op's tool and
        unlocks the tool widgets so the user can edit freely. The tool
        values themselves are left untouched — the user starts editing
        from whatever state the op was in.
        """
        if self._suspend_tool_signals:
            return
        tc = self._tool_controller
        op = self._operation
        if tc is None or op is None:
            return
        form = self._forms.get(type(op))
        lib_id = self._tool_combo.currentData()

        if lib_id is None:
            # (Custom) — unpin so the user can edit, but don't mutate
            # tool values. Only emit operation_changed if state actually
            # changed so a no-op reselection doesn't churn the undo
            # stack / preview.
            changed = tc.tool.library_id is not None
            tc.tool.library_id = None
            if form is not None:
                form.set_tool_editable(True)
            if changed:
                self.operation_changed.emit()
            return

        lib_tool = self._tool_library.find(lib_id)
        if lib_tool is None:
            return  # stale combo data — shouldn't happen in practice
        tc.tool = lib_tool.model_copy(
            deep=True,
            update={"id": uuid4().hex, "library_id": lib_tool.id},
        )
        cd = tc.tool.cutting_data.get("default")
        if cd is not None:
            tc.spindle_rpm = cd.spindle_rpm
            tc.feed_xy = cd.feed_xy
            tc.feed_z = cd.feed_z
        if form is not None:
            form.bind(op, tc)
            form.set_tool_editable(False)
        self.operation_changed.emit()

    # -- Tool combo maintenance --------------------------------------

    def _rebuild_tool_combo(self) -> None:
        """Populate the combo with library entries + ``(Custom)`` trailer.

        Each combo item stores the library tool's ``id`` as userData;
        lookups match on that id so two library tools with the same
        name coexist without ambiguity, and a tool rename doesn't
        break the op's dropdown selection.
        """
        self._suspend_tool_signals = True
        try:
            self._tool_combo.clear()
            for tool in self._tool_library.tools:
                diameter = float(tool.geometry.get("diameter", 0.0))
                display = f"{tool.name}  ({diameter:g} mm)"
                self._tool_combo.addItem(display, userData=tool.id)
            self._tool_combo.addItem(_CUSTOM_TOOL_LABEL, userData=None)
        finally:
            self._suspend_tool_signals = False

    def _sync_tool_combo_to_op(self) -> None:
        """Match combo selection to the bound op's ``tool.library_id``.

        Falls through to ``(Custom)`` when the op's tool has no
        ``library_id``, or when that id points at a tool that's no
        longer in the library (e.g. user deleted it). Also toggles the
        form's tool-field editability: library-backed → locked,
        custom → editable.
        """
        self._suspend_tool_signals = True
        try:
            tc = self._tool_controller
            op = self._operation
            form = self._forms.get(type(op)) if op is not None else None

            if tc is None:
                self._tool_combo.setCurrentIndex(
                    self._tool_combo.count() - 1  # (Custom)
                )
                if form is not None:
                    # No controller — ``populate`` already disabled the
                    # tool widgets; leave them that way.
                    pass
                return

            lib_id = tc.tool.library_id
            if lib_id is not None:
                idx = self._find_combo_index_by_library_id(lib_id)
                if idx >= 0:
                    self._tool_combo.setCurrentIndex(idx)
                    if form is not None:
                        form.set_tool_editable(False)
                    return
                # library_id set but the library no longer has that
                # tool — fall through to (Custom).

            # Custom mode: last combo entry, editable fields.
            self._tool_combo.setCurrentIndex(self._tool_combo.count() - 1)
            if form is not None:
                form.set_tool_editable(True)
        finally:
            self._suspend_tool_signals = False

    def _find_combo_index_by_library_id(self, library_id: str) -> int:
        """Return the combo index whose userData equals ``library_id``, or -1."""
        for i in range(self._tool_combo.count()):
            if self._tool_combo.itemData(i) == library_id:
                return i
        return -1


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


# ------------------------------------------------------------------ drill form


@register_form(DrillOp)
class _DrillForm(OperationFormBase):
    """Form widgets + populate/write-back for DrillOp.

    Peck-specific fields (``peck_depth``, ``chip_break_retract``) are
    always visible but disabled for cycles that don't use them, so the
    user sees why a given value is greyed out rather than having the
    field disappear on cycle switch.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = QLineEdit()
        self.cycle = QComboBox()
        self.cycle.addItems([c.value for c in DrillCycle])
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
        self.peck_depth_override = QCheckBox("Override default peck")
        self.peck_depth = QDoubleSpinBox()
        self.peck_depth.setRange(0.01, 100.0)
        self.peck_depth.setDecimals(3)
        self.peck_depth.setSingleStep(0.25)
        self.peck_depth.setSuffix(" mm")
        self.chip_break_retract = QDoubleSpinBox()
        self.chip_break_retract.setRange(0.01, 10.0)
        self.chip_break_retract.setDecimals(3)
        self.chip_break_retract.setSingleStep(0.1)
        self.chip_break_retract.setSuffix(" mm")
        self.dwell_at_bottom = QDoubleSpinBox()
        self.dwell_at_bottom.setRange(0.0, 60.0)
        self.dwell_at_bottom.setDecimals(2)
        self.dwell_at_bottom.setSingleStep(0.1)
        self.dwell_at_bottom.setSuffix(" s")

        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("Tool diameter", self.tool_diameter)
        form.addRow("Cycle", self.cycle)
        form.addRow("Cut depth", self.cut_depth)
        form.addRow("Peck depth", self.peck_depth_override)
        form.addRow("", self.peck_depth)
        form.addRow("Chip-break retract", self.chip_break_retract)
        form.addRow("Dwell at bottom", self.dwell_at_bottom)

        self._wire(
            self.name.textEdited,
            self.cycle.currentTextChanged,
            self.tool_diameter.valueChanged,
            self.cut_depth.valueChanged,
            self.peck_depth_override.toggled,
            self.peck_depth.valueChanged,
            self.chip_break_retract.valueChanged,
            self.dwell_at_bottom.valueChanged,
        )

    def populate(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        assert isinstance(op, DrillOp)
        self.name.setText(op.name)
        self.cycle.setCurrentText(op.cycle.value)
        self.cut_depth.setValue(op.cut_depth)
        override = op.peck_depth is not None
        self.peck_depth_override.setChecked(override)
        self.peck_depth.setValue(op.peck_depth if op.peck_depth is not None else 1.0)
        self.chip_break_retract.setValue(op.chip_break_retract)
        self.dwell_at_bottom.setValue(op.dwell_at_bottom_s)
        self._update_cycle_dependent_enablement(op.cycle, override)
        if tool_controller is not None:
            diameter = float(tool_controller.tool.geometry.get("diameter", 3.0))
            self.tool_diameter.setValue(diameter)
            self.tool_diameter.setEnabled(True)
        else:
            self.tool_diameter.setEnabled(False)

    def write_back(
        self, op: Operation, tool_controller: ToolController | None
    ) -> None:
        assert isinstance(op, DrillOp)
        op.name = self.name.text()
        op.cycle = DrillCycle(self.cycle.currentText())
        op.cut_depth = self.cut_depth.value()
        override = self.peck_depth_override.isChecked()
        op.peck_depth = self.peck_depth.value() if override else None
        op.chip_break_retract = self.chip_break_retract.value()
        op.dwell_at_bottom_s = self.dwell_at_bottom.value()
        self._update_cycle_dependent_enablement(op.cycle, override)
        if tool_controller is not None:
            tool_controller.tool.geometry["diameter"] = self.tool_diameter.value()

    def _update_cycle_dependent_enablement(
        self, cycle: DrillCycle, peck_override: bool
    ) -> None:
        uses_peck = cycle in (DrillCycle.PECK, DrillCycle.CHIP_BREAK)
        self.peck_depth_override.setEnabled(uses_peck)
        self.peck_depth.setEnabled(uses_peck and peck_override)
        self.chip_break_retract.setEnabled(cycle is DrillCycle.CHIP_BREAK)


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
