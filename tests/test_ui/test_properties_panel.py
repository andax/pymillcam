"""Unit tests for the properties panel (Profile + Pocket forms).

These tests drive the panel in isolation, without a full MainWindow, so
the field-to-model binding is covered even if downstream wiring changes.
"""
from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from pymillcam.core.operations import (
    DrillCycle,
    DrillOp,
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


# ---------- DrillOp form --------------------------------------------------


def test_setting_drill_op_shows_drill_form(panel: PropertiesPanel) -> None:
    drill_form = panel.form_for(DrillOp)
    panel.set_operation(DrillOp(name="D"))
    assert panel._stack.currentWidget() is drill_form


def test_drill_fields_populate_from_op(panel: PropertiesPanel) -> None:
    op = DrillOp(
        name="D",
        cycle=DrillCycle.PECK,
        cut_depth=-6.0,
        peck_depth=1.5,
        chip_break_retract=0.3,
        dwell_at_bottom_s=0.2,
    )
    panel.set_operation(op)
    form = panel.form_for(DrillOp)
    assert form.name.text() == "D"
    assert form.cycle.currentText() == "peck"
    assert form.cut_depth.value() == pytest.approx(-6.0)
    assert form.peck_depth_override.isChecked()
    assert form.peck_depth.value() == pytest.approx(1.5)
    assert form.chip_break_retract.value() == pytest.approx(0.3)
    assert form.dwell_at_bottom.value() == pytest.approx(0.2)


def test_drill_cycle_write_back_round_trips(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    op = DrillOp(name="D", cycle=DrillCycle.SIMPLE)
    panel.set_operation(op)
    form = panel.form_for(DrillOp)
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        form.cycle.setCurrentText("chip_break")
    assert op.cycle is DrillCycle.CHIP_BREAK


def test_peck_depth_disabled_for_simple_cycle(panel: PropertiesPanel) -> None:
    """The peck-depth override fields make no sense for a simple cycle;
    the form should grey them out so the user sees why."""
    panel.set_operation(DrillOp(name="D", cycle=DrillCycle.SIMPLE))
    form = panel.form_for(DrillOp)
    assert not form.peck_depth_override.isEnabled()
    assert not form.peck_depth.isEnabled()
    # Chip-break retract only meaningful for CHIP_BREAK cycle.
    assert not form.chip_break_retract.isEnabled()


def test_chip_break_retract_enabled_only_for_chip_break_cycle(
    panel: PropertiesPanel,
) -> None:
    panel.set_operation(DrillOp(name="D", cycle=DrillCycle.CHIP_BREAK))
    assert panel.form_for(DrillOp).chip_break_retract.isEnabled()
    panel.set_operation(DrillOp(name="D", cycle=DrillCycle.PECK))
    assert not panel.form_for(DrillOp).chip_break_retract.isEnabled()


def test_disabling_peck_override_sets_peck_depth_to_none(
    panel: PropertiesPanel,
) -> None:
    op = DrillOp(name="D", cycle=DrillCycle.PECK, peck_depth=2.0)
    panel.set_operation(op)
    form = panel.form_for(DrillOp)
    form.peck_depth_override.setChecked(False)
    assert op.peck_depth is None


# ---------- Tool dropdown -----------------------------------------------


def _library_with(*tools):
    from pymillcam.core.tool_library import ToolLibrary

    lib = ToolLibrary(tools=list(tools))
    if tools:
        lib.default_tool_id = tools[0].id
    return lib


def _lib_tool(name: str, *, diameter: float = 3.0, rpm: int = 18000,
              feed_xy: float = 1200.0, feed_z: float = 300.0):
    from pymillcam.core.tools import CuttingData, Tool, ToolShape

    t = Tool(name=name, shape=ToolShape.ENDMILL)
    t.geometry["diameter"] = diameter
    t.cutting_data["default"] = CuttingData(
        spindle_rpm=rpm, feed_xy=feed_xy, feed_z=feed_z,
    )
    return t


def test_tool_combo_is_disabled_when_no_op_bound(panel: PropertiesPanel) -> None:
    """The combo would have nothing to act on without a bound op; keep
    it disabled so a stray click can't try to mutate a None controller."""
    assert not panel._tool_combo.isEnabled()


def test_tool_combo_lists_library_tools(panel: PropertiesPanel) -> None:
    t1 = _lib_tool("3mm flat")
    t2 = _lib_tool("6mm rougher", diameter=6.0)
    panel.set_tool_library(_library_with(t1, t2))
    # Two library tools + the trailing "(Custom)" entry.
    assert panel._tool_combo.count() == 3
    ids = [
        panel._tool_combo.itemData(i)
        for i in range(panel._tool_combo.count())
    ]
    # Combo keys off each library tool's id, not name — that way two
    # same-named tools still land in distinct rows and renames don't
    # break the pinning.
    assert ids[:2] == [t1.id, t2.id]
    # Custom entry carries userData=None so library lookups skip it.
    assert ids[2] is None


def test_tool_combo_selects_entry_matching_bound_op(
    panel: PropertiesPanel,
) -> None:
    """Binding an op whose tool carries the library entry's id in its
    ``library_id`` should auto-select that library entry in the combo."""
    from pymillcam.core.tools import Tool, ToolController

    lib_tool = _lib_tool("3mm flat")
    panel.set_tool_library(_library_with(lib_tool))
    tc = ToolController(
        tool_number=1,
        tool=Tool(name="3mm flat", library_id=lib_tool.id),
    )
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    assert panel._tool_combo.currentData() == lib_tool.id


def test_tool_combo_shows_custom_when_op_tool_has_no_library_id(
    panel: PropertiesPanel,
) -> None:
    """A tool without ``library_id`` is by definition not linked to any
    library entry — show (Custom) regardless of name collisions."""
    from pymillcam.core.tools import Tool, ToolController

    panel.set_tool_library(_library_with(_lib_tool("3mm flat")))
    # Same name as a library entry but no library_id → still (Custom).
    tc = ToolController(
        tool_number=1, tool=Tool(name="3mm flat", library_id=None)
    )
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    assert panel._tool_combo.currentData() is None


def test_tool_combo_falls_back_to_custom_when_library_id_stale(
    panel: PropertiesPanel,
) -> None:
    """If ``library_id`` points at a tool that's been deleted from the
    library, show (Custom) rather than misleadingly selecting
    something else."""
    from pymillcam.core.tools import Tool, ToolController

    panel.set_tool_library(_library_with(_lib_tool("current")))
    tc = ToolController(
        tool_number=1,
        tool=Tool(name="old", library_id="deleted-uuid-does-not-exist"),
    )
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    assert panel._tool_combo.currentData() is None


def test_picking_library_tool_replaces_tool_controller_tool(
    panel: PropertiesPanel, qtbot: QtBot
) -> None:
    """Primary workflow: switch the bound op to a different library
    tool via the dropdown — the op's ToolController ends up with the
    library tool's geometry, cutting data, and library_id backlink."""
    from pymillcam.core.tools import Tool, ToolController

    starter = _lib_tool("starter", diameter=3.0)
    rougher = _lib_tool(
        "rougher", diameter=8.0, rpm=20000, feed_xy=2500.0, feed_z=800.0,
    )
    panel.set_tool_library(_library_with(starter, rougher))
    tc = ToolController(
        tool_number=1,
        tool=Tool(name="starter", library_id=starter.id),
    )
    tc.tool.geometry["diameter"] = 3.0
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    target_idx = panel._find_combo_index_by_library_id(rougher.id)
    assert target_idx >= 0
    with qtbot.waitSignal(panel.operation_changed, timeout=500):
        panel._tool_combo.setCurrentIndex(target_idx)

    assert tc.tool.name == "rougher"
    assert tc.tool.library_id == rougher.id
    assert tc.tool.geometry["diameter"] == pytest.approx(8.0)
    assert tc.spindle_rpm == 20000
    assert tc.feed_xy == pytest.approx(2500.0)
    assert tc.feed_z == pytest.approx(800.0)


def test_picking_library_tool_refreshes_form_diameter(
    panel: PropertiesPanel,
) -> None:
    from pymillcam.core.tools import Tool, ToolController

    starter = _lib_tool("starter", diameter=3.0)
    rougher = _lib_tool("rougher", diameter=8.0)
    panel.set_tool_library(_library_with(starter, rougher))
    tc = ToolController(
        tool_number=1,
        tool=Tool(name="starter", library_id=starter.id),
    )
    tc.tool.geometry["diameter"] = 3.0
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    panel._tool_combo.setCurrentIndex(
        panel._find_combo_index_by_library_id(rougher.id)
    )

    assert panel._profile_form.tool_diameter.value() == pytest.approx(8.0)


def test_picking_custom_is_a_noop(panel: PropertiesPanel) -> None:
    """Selecting the ``(Custom)`` entry must not mutate the bound
    ToolController — it's a display state, not an action."""
    from pymillcam.core.tools import Tool, ToolController

    panel.set_tool_library(_library_with(_lib_tool("starter")))
    tc = ToolController(tool_number=1, tool=Tool(name="starter"))
    tc.tool.geometry["diameter"] = 3.0
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)
    before_id = tc.tool.id

    # Last entry is the (Custom) label.
    panel._tool_combo.setCurrentIndex(panel._tool_combo.count() - 1)

    # Tool is unchanged — same identity, same diameter.
    assert tc.tool.id == before_id
    assert tc.tool.geometry["diameter"] == pytest.approx(3.0)


def test_picking_library_tool_gives_distinct_id(
    panel: PropertiesPanel,
) -> None:
    """Projects stay self-contained: the dropdown copies the library
    tool (fresh ``id``, ``library_id`` backlink) rather than
    referencing it, so editing the library later can't retroactively
    mutate the op's tool."""
    from pymillcam.core.tools import Tool, ToolController

    lib_tool = _lib_tool("starter")
    panel.set_tool_library(_library_with(lib_tool))
    tc = ToolController(tool_number=1, tool=Tool(name="placeholder"))
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    panel._tool_combo.setCurrentIndex(
        panel._find_combo_index_by_library_id(lib_tool.id)
    )

    # ``id`` is fresh (per-op identity), ``library_id`` points at source.
    assert tc.tool.id != lib_tool.id
    assert tc.tool.library_id == lib_tool.id


def test_set_tool_library_after_bind_refreshes_combo(
    panel: PropertiesPanel,
) -> None:
    """When the user edits the library via Tools > Library…, the new
    set_tool_library call should update the combo to show the edited
    list. Newly-added tools appear, deleted tools vanish, and an op
    pinned to a now-missing tool falls back to (Custom)."""
    from pymillcam.core.tools import Tool, ToolController

    starter = _lib_tool("starter")
    panel.set_tool_library(_library_with(starter))
    tc = ToolController(
        tool_number=1, tool=Tool(name="starter", library_id=starter.id)
    )
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    # Library version 2 replaces "starter" with "rougher" — the op's
    # library_id now points at a missing tool.
    rougher = _lib_tool("rougher", diameter=8.0)
    panel.set_tool_library(_library_with(rougher))

    ids = [
        panel._tool_combo.itemData(i)
        for i in range(panel._tool_combo.count() - 1)  # skip (Custom)
    ]
    assert ids == [rougher.id]
    assert panel._tool_combo.currentData() is None  # (Custom) fallback


# ---------- Lock behaviour: tool fields follow combo state --------------


def test_library_tool_selection_locks_tool_diameter(
    panel: PropertiesPanel,
) -> None:
    """When a library tool is selected, tool_diameter must be
    read-only — otherwise the user could silently make the op's tool
    diverge from the library tool the dropdown claims it's using."""
    from pymillcam.core.tools import Tool, ToolController

    lib_tool = _lib_tool("3mm flat")
    panel.set_tool_library(_library_with(lib_tool))
    tc = ToolController(
        tool_number=1,
        tool=Tool(name="3mm flat", library_id=lib_tool.id),
    )
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)

    assert not panel._profile_form.tool_diameter.isEnabled()


def test_picking_library_tool_locks_tool_diameter(
    panel: PropertiesPanel,
) -> None:
    """Switching to a library tool via the combo transitions the
    tool_diameter widget from editable to locked in a single action."""
    from pymillcam.core.tools import Tool, ToolController

    lib_tool = _lib_tool("3mm flat")
    panel.set_tool_library(_library_with(lib_tool))
    tc = ToolController(
        tool_number=1, tool=Tool(name="custom", library_id=None)
    )
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)
    # Starts editable (custom tool).
    assert panel._profile_form.tool_diameter.isEnabled()

    panel._tool_combo.setCurrentIndex(
        panel._find_combo_index_by_library_id(lib_tool.id)
    )

    assert not panel._profile_form.tool_diameter.isEnabled()


def test_picking_custom_unlocks_tool_diameter(
    panel: PropertiesPanel,
) -> None:
    """Switching to (Custom) unpins the library link and re-enables
    the tool_diameter widget — the user starts editing from whatever
    state the library tool left them in."""
    from pymillcam.core.tools import Tool, ToolController

    lib_tool = _lib_tool("3mm flat")
    panel.set_tool_library(_library_with(lib_tool))
    tc = ToolController(
        tool_number=1,
        tool=Tool(name="3mm flat", library_id=lib_tool.id),
    )
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)
    assert not panel._profile_form.tool_diameter.isEnabled()

    panel._tool_combo.setCurrentIndex(panel._tool_combo.count() - 1)

    assert panel._profile_form.tool_diameter.isEnabled()
    # library_id was cleared — future syncs will show (Custom) not the
    # prior library tool.
    assert tc.tool.library_id is None


def test_picking_custom_preserves_tool_values(
    panel: PropertiesPanel,
) -> None:
    """Switching to (Custom) must not mutate tool geometry or cutting
    fields — it only unpins and unlocks. Lets the user start editing
    from the library tool's current state rather than a default."""
    from pymillcam.core.tools import Tool, ToolController

    lib_tool = _lib_tool("3mm flat", diameter=3.0, rpm=18000, feed_xy=1200.0)
    panel.set_tool_library(_library_with(lib_tool))
    tc = ToolController(
        tool_number=1,
        tool=Tool(name="3mm flat", library_id=lib_tool.id),
        spindle_rpm=18000, feed_xy=1200.0,
    )
    tc.tool.geometry["diameter"] = 3.0
    op = ProfileOp(name="P", tool_controller_id=1)
    panel.set_operation(op, tool_controller=tc)
    before_id = tc.tool.id

    panel._tool_combo.setCurrentIndex(panel._tool_combo.count() - 1)

    assert tc.tool.id == before_id
    assert tc.tool.geometry["diameter"] == pytest.approx(3.0)
    assert tc.spindle_rpm == 18000
    assert tc.feed_xy == pytest.approx(1200.0)


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
