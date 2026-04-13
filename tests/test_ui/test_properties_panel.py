"""Unit tests for the ProfileOp properties panel.

These tests drive the panel in isolation, without a full MainWindow, so
the field-to-model binding is covered even if downstream wiring changes.
"""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from pymillcam.core.operations import MillingDirection, OffsetSide, ProfileOp
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
    assert panel._form.name.text() == "Test"
    assert panel._form.offset_side.currentText() == "inside"
    assert panel._form.cut_depth.value() == -4.5
    assert not panel._form.multi_depth.isChecked()
    assert panel._form.chord_override.isChecked()
    assert panel._form.chord_tolerance.value() == pytest.approx(0.02)


def test_editing_fields_updates_model_and_emits(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="Before", cut_depth=0.0)
    panel.set_operation(op)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._form.cut_depth.setValue(-5.0)
    assert op.cut_depth == -5.0

    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._form.offset_side.setCurrentText("on_line")
    assert op.offset_side is OffsetSide.ON_LINE


def test_disabling_multi_depth_clears_stepdown(panel: PropertiesPanel) -> None:
    op = ProfileOp(name="op", multi_depth=True, stepdown=2.0)
    panel.set_operation(op)
    panel._form.multi_depth.setChecked(False)
    assert op.multi_depth is False
    assert op.stepdown is None
    assert not panel._form.stepdown.isEnabled()


def test_disabling_chord_override_reverts_to_none(panel: PropertiesPanel) -> None:
    op = ProfileOp(name="op", chord_tolerance=0.02)
    panel.set_operation(op)
    panel._form.chord_override.setChecked(False)
    assert op.chord_tolerance is None
    assert not panel._form.chord_tolerance.isEnabled()


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
    assert not panel._form.tool_diameter.isEnabled()


def test_direction_field_round_trips_through_the_form(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = ProfileOp(name="op", direction=MillingDirection.CLIMB)
    panel.set_operation(op)
    assert panel._form.direction.currentText() == "climb"
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._form.direction.setCurrentText("conventional")
    assert op.direction is MillingDirection.CONVENTIONAL


def test_editing_tool_diameter_writes_back_to_tool_controller(
    panel: PropertiesPanel,
) -> None:
    tc = ToolController(tool_number=1, tool=Tool(name="3mm"))
    tc.tool.geometry["diameter"] = 3.0
    panel.set_operation(ProfileOp(name="op"), tool_controller=tc)
    panel._form.tool_diameter.setValue(6.5)
    assert tc.tool.geometry["diameter"] == 6.5
