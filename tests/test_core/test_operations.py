"""Tests for pymillcam.core.operations."""
from __future__ import annotations

from pymillcam.core.operations import (
    GeometryRef,
    MillingDirection,
    OffsetSide,
    ProfileOp,
    TabStyle,
)


def test_profile_op_defaults() -> None:
    op = ProfileOp(name="Outer profile")
    assert op.type == "profile"
    assert op.enabled is True
    assert op.offset_side is OffsetSide.OUTSIDE
    assert op.direction is MillingDirection.CLIMB
    assert op.multi_depth is True
    assert op.stepdown is None
    assert op.safe_height is None
    assert op.tabs.enabled is False
    assert op.tabs.style is TabStyle.RECTANGULAR
    assert op.id  # uuid populated


def test_profile_op_with_geometry_refs() -> None:
    op = ProfileOp(
        name="Cutouts",
        geometry_refs=[
            GeometryRef(layer_name="Profile_Outside", entity_id="abc"),
            GeometryRef(layer_name="Profile_Outside", entity_id="def"),
        ],
        cut_depth=-6.0,
        tool_controller_id=1,
    )
    assert len(op.geometry_refs) == 2
    assert op.cut_depth == -6.0
    assert op.tool_controller_id == 1


def test_profile_op_round_trips_via_json() -> None:
    original = ProfileOp(
        name="Outer profile",
        offset_side=OffsetSide.INSIDE,
        direction=MillingDirection.CONVENTIONAL,
        cut_depth=-12.0,
        stepdown=2.0,
        geometry_refs=[GeometryRef(layer_name="L", entity_id="e1")],
    )
    restored = ProfileOp.model_validate_json(original.model_dump_json())
    assert restored == original
