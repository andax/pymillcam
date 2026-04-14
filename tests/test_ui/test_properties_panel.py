"""Unit tests for the properties panel (Profile + Pocket forms).

These tests drive the panel in isolation, without a full MainWindow, so
the field-to-model binding is covered even if downstream wiring changes.
"""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

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
from pymillcam.core.tools import Tool, ToolController
from pymillcam.ui.properties_panel import PropertiesPanel


@pytest.fixture
def panel(qtbot: QtBot) -> PropertiesPanel:
    p = PropertiesPanel()
    qtbot.addWidget(p)
    return p


def test_empty_panel_shows_placeholder(panel: PropertiesPanel) -> None:
    assert panel._stack.currentWidget() is panel._empty


def test_set_operation_populates_fields(panel: PropertiesPanel) -> None:
    op = ProfileOp(
        name="Test",
        cut_depth=-4.5,
        offset_side=OffsetSide.INSIDE,
        multi_depth=False,
        chord_tolerance=0.02,
    )
    panel.set_operation(op)
    assert panel._profile_form.name.text() == "Test"
    assert panel._profile_form.offset_side.currentText() == "inside"
    assert panel._profile_form.cut_depth.value() == -4.5
    assert not panel._profile_form.multi_depth.isChecked()
    assert panel._profile_form.chord_override.isChecked()
    assert panel._profile_form.chord_tolerance.value() == pytest.approx(0.02)


def test_editing_fields_updates_model_and_emits(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="Before", cut_depth=0.0)
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._profile_form.cut_depth.setValue(-5.0)
    assert op.cut_depth == -5.0

    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._profile_form.offset_side.setCurrentText("on_line")
    assert op.offset_side is OffsetSide.ON_LINE


def test_disabling_multi_depth_clears_stepdown(panel: PropertiesPanel) -> None:
    op = ProfileOp(name="op", multi_depth=True, stepdown=2.0)
    panel.set_operation(op)
    panel._profile_form.multi_depth.setChecked(False)
    assert op.multi_depth is False
    assert op.stepdown is None
    assert not panel._profile_form.stepdown.isEnabled()


def test_disabling_chord_override_reverts_to_none(panel: PropertiesPanel) -> None:
    op = ProfileOp(name="op", chord_tolerance=0.02)
    panel.set_operation(op)
    panel._profile_form.chord_override.setChecked(False)
    assert op.chord_tolerance is None
    assert not panel._profile_form.chord_tolerance.isEnabled()


def test_setting_none_hides_form(panel: PropertiesPanel) -> None:
    op = ProfileOp(name="op")
    panel.set_operation(op)
    panel.set_operation(None)
    assert panel._stack.currentWidget() is panel._empty


def test_populating_does_not_re_emit(panel: PropertiesPanel, qtbot: QtBot) -> None:
    op = ProfileOp(name="stable", cut_depth=-2.0)
    with qtbot.assertNotEmitted(panel.operation_changed):
        panel.set_operation(op)


def test_tool_diameter_field_is_disabled_without_a_tool_controller(
    panel: PropertiesPanel,
) -> None:
    panel.set_operation(ProfileOp(name="op"), tool_controller=None)
    assert not panel._profile_form.tool_diameter.isEnabled()


def test_direction_field_round_trips_through_the_form(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="op", direction=MillingDirection.CLIMB)
    panel.set_operation(op)
    assert panel._profile_form.direction.currentText() == "climb"
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._profile_form.direction.setCurrentText("conventional")
    assert op.direction is MillingDirection.CONVENTIONAL


def test_editing_tool_diameter_writes_back_to_tool_controller(
    panel: PropertiesPanel,
) -> None:
    tc = ToolController(tool_number=1, tool=Tool(name="3mm"))
    tc.tool.geometry["diameter"] = 3.0
    panel.set_operation(ProfileOp(name="op"), tool_controller=tc)
    panel._profile_form.tool_diameter.setValue(6.5)
    assert tc.tool.geometry["diameter"] == 6.5


def test_lead_fields_populate_from_op(panel: PropertiesPanel) -> None:
    op = ProfileOp(
        name="op",
        lead_in=LeadConfig(style=LeadStyle.TANGENT, length=3.5),
        lead_out=LeadConfig(style=LeadStyle.ARC, length=2.5),
    )
    panel.set_operation(op)
    assert panel._profile_form.lead_in_style.currentText() == "tangent"
    assert panel._profile_form.lead_in_length.value() == pytest.approx(3.5)
    assert panel._profile_form.lead_out_style.currentText() == "arc"
    assert panel._profile_form.lead_out_length.value() == pytest.approx(2.5)


def test_editing_lead_style_writes_back_to_op(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="op", lead_in=LeadConfig(style=LeadStyle.ARC))
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._profile_form.lead_in_style.setCurrentText("direct")
    assert op.lead_in.style is LeadStyle.DIRECT


def test_editing_lead_length_writes_back_to_op(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="op")
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._profile_form.lead_out_length.setValue(7.25)
    assert op.lead_out.length == pytest.approx(7.25)


def test_ramp_fields_populate_from_op(panel: PropertiesPanel) -> None:
    op = ProfileOp(
        name="op",
        ramp=RampConfig(strategy=RampStrategy.PLUNGE, angle_deg=5.0),
    )
    panel.set_operation(op)
    assert panel._profile_form.ramp_strategy.currentText() == "plunge"
    assert panel._profile_form.ramp_angle.value() == pytest.approx(5.0)


def test_editing_ramp_angle_writes_back_to_op(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="op")
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._profile_form.ramp_angle.setValue(2.5)
    assert op.ramp.angle_deg == pytest.approx(2.5)


def test_editing_ramp_strategy_writes_back_to_op(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="op", ramp=RampConfig(strategy=RampStrategy.LINEAR))
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._profile_form.ramp_strategy.setCurrentText("plunge")
    assert op.ramp.strategy is RampStrategy.PLUNGE


# ---------- PocketOp form ------------------------------------------------


def test_setting_pocket_op_shows_pocket_form(panel: PropertiesPanel) -> None:
    panel.set_operation(PocketOp(name="P"))
    assert panel._stack.currentWidget() is panel._pocket_form


def test_pocket_fields_populate_from_op(panel: PropertiesPanel) -> None:
    op = PocketOp(
        name="Pocket",
        strategy=PocketStrategy.OFFSET,
        direction=MillingDirection.CONVENTIONAL,
        cut_depth=-4.0,
        stepover=1.25,
    )
    panel.set_operation(op)
    assert panel._pocket_form.name.text() == "Pocket"
    assert panel._pocket_form.strategy.currentText() == "offset"
    assert panel._pocket_form.direction.currentText() == "conventional"
    assert panel._pocket_form.cut_depth.value() == pytest.approx(-4.0)
    assert panel._pocket_form.stepover.value() == pytest.approx(1.25)


def test_editing_pocket_stepover_writes_back(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = PocketOp(name="P", stepover=2.0)
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._pocket_form.stepover.setValue(1.0)
    assert op.stepover == pytest.approx(1.0)


def test_editing_pocket_direction_writes_back(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = PocketOp(name="P", direction=MillingDirection.CLIMB)
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._pocket_form.direction.setCurrentText("conventional")
    assert op.direction is MillingDirection.CONVENTIONAL


def test_switching_between_profile_and_pocket_swaps_form(
    panel: PropertiesPanel,
) -> None:
    panel.set_operation(ProfileOp(name="Prof"))
    assert panel._stack.currentWidget() is panel._profile_form
    panel.set_operation(PocketOp(name="Pock"))
    assert panel._stack.currentWidget() is panel._pocket_form
    panel.set_operation(ProfileOp(name="Back"))
    assert panel._stack.currentWidget() is panel._profile_form


def test_pocket_ramp_fields_populate_from_op(panel: PropertiesPanel) -> None:
    op = PocketOp(
        name="P",
        ramp=RampConfig(
            strategy=RampStrategy.HELICAL, angle_deg=2.0, radius=1.25
        ),
    )
    panel.set_operation(op)
    assert panel._pocket_form.ramp_strategy.currentText() == "helical"
    assert panel._pocket_form.ramp_angle.value() == pytest.approx(2.0)
    assert panel._pocket_form.ramp_radius.value() == pytest.approx(1.25)


def test_editing_pocket_ramp_strategy_writes_back(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = PocketOp(name="P", ramp=RampConfig(strategy=RampStrategy.HELICAL))
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._pocket_form.ramp_strategy.setCurrentText("plunge")
    assert op.ramp.strategy is RampStrategy.PLUNGE


def test_pocket_multi_depth_fields_populate(panel: PropertiesPanel) -> None:
    op = PocketOp(name="P", multi_depth=True, stepdown=0.75)
    panel.set_operation(op)
    assert panel._pocket_form.multi_depth.isChecked()
    assert panel._pocket_form.stepdown.isEnabled()
    assert panel._pocket_form.stepdown.value() == pytest.approx(0.75)


def test_pocket_disabling_multi_depth_clears_stepdown(
    panel: PropertiesPanel,
) -> None:
    op = PocketOp(name="P", multi_depth=True, stepdown=1.5)
    panel.set_operation(op)
    panel._pocket_form.multi_depth.setChecked(False)
    assert op.multi_depth is False
    assert op.stepdown is None
    assert not panel._pocket_form.stepdown.isEnabled()


def test_pocket_profile_signals_are_routed_by_type(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    """Editing a pocket field on a bound ProfileOp must not mutate it, and
    vice versa — the type check in _on_*_changed guards against cross-talk."""
    prof = ProfileOp(name="Prof", cut_depth=-1.0)
    panel.set_operation(prof)
    # Now bind a pocket; leave the profile-form widgets alone.
    pock = PocketOp(name="Pock", cut_depth=-2.0, stepover=1.0)
    panel.set_operation(pock)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._pocket_form.cut_depth.setValue(-3.0)
    assert pock.cut_depth == pytest.approx(-3.0)
    # Profile op is untouched.
    assert prof.cut_depth == pytest.approx(-1.0)
