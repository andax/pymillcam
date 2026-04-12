"""Tests for pymillcam.core.project integration with geometry, tools, operations."""
from __future__ import annotations

import math

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import GeometryRef, ProfileOp
from pymillcam.core.project import Project
from pymillcam.core.segments import LineSegment
from pymillcam.core.tools import Tool, ToolController


def test_project_defaults_are_empty() -> None:
    project = Project()
    assert project.name == "Untitled"
    assert project.geometry_layers == []
    assert project.operations == []
    assert project.tool_controllers == []


def test_project_round_trips_with_populated_fields() -> None:
    entity = GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(50, 0)),
            LineSegment(start=(50, 0), end=(50, 30)),
            LineSegment(start=(50, 30), end=(0, 30)),
            LineSegment(start=(0, 30), end=(0, 0)),
        ],
        closed=True,
    )
    layer = GeometryLayer(name="Profile_Outside", entities=[entity])
    tool_controller = ToolController(tool_number=1, tool=Tool(name="3mm flat"))
    op = ProfileOp(
        name="Outer profile",
        tool_controller_id=1,
        cut_depth=-6.0,
        geometry_refs=[GeometryRef(layer_name=layer.name, entity_id=entity.id)],
    )

    project = Project(
        name="Test",
        geometry_layers=[layer],
        tool_controllers=[tool_controller],
        operations=[op],
    )

    restored = Project.model_validate_json(project.model_dump_json())
    assert restored.name == "Test"
    assert len(restored.geometry_layers) == 1
    assert math.isclose(restored.geometry_layers[0].entities[0].geom.area, 50 * 30)
    assert len(restored.tool_controllers) == 1
    assert restored.tool_controllers[0].tool.name == "3mm flat"
    assert len(restored.operations) == 1
    assert restored.operations[0].geometry_refs[0].entity_id == entity.id
