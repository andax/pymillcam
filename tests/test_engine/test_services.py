"""Behaviour tests for ToolpathService.

Verifies the dispatch / registration contract: registering a new op
type plugs in without changing the service class, unregistered op
types return empty previews and None toolpaths (rather than raising),
and ``generate_program`` assembles the full post-processor pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import (
    GeometryRef,
    OffsetSide,
    PocketOp,
    ProfileOp,
)
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment, Segment
from pymillcam.core.tools import Tool, ToolController
from pymillcam.engine.common import EngineError
from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath
from pymillcam.engine.services import ToolpathService


def _rect_entity() -> GeometryEntity:
    pts = [(0.0, 0.0), (20.0, 0.0), (20.0, 15.0), (0.0, 15.0)]
    segments = [
        LineSegment(start=pts[i], end=pts[(i + 1) % 4]) for i in range(4)
    ]
    return GeometryEntity(segments=segments, closed=True)


def _project_with_rect(op) -> Project:
    """Boilerplate: a closed rect + a ToolController + one op referencing it."""
    project = Project()
    entity = _rect_entity()
    layer = GeometryLayer(name="L", entities=[entity])
    project.geometry_layers.append(layer)
    tc = ToolController(tool_number=1, tool=Tool(name="3mm"))
    tc.tool.geometry["diameter"] = 3.0
    project.tool_controllers.append(tc)
    op.tool_controller_id = 1
    op.geometry_refs.append(GeometryRef(layer_name="L", entity_id=entity.id))
    project.operations.append(op)
    return project


# -------------------------------------------------------------- built-ins


def test_profileop_and_pocketop_are_supported_out_of_the_box() -> None:
    svc = ToolpathService()

    assert svc.supports(ProfileOp(name="p"))
    assert svc.supports(PocketOp(name="q"))


def test_compute_preview_returns_segments_for_profile_op() -> None:
    project = _project_with_rect(
        ProfileOp(name="P", cut_depth=-1.0, offset_side=OffsetSide.OUTSIDE)
    )
    svc = ToolpathService()

    preview = svc.compute_preview(project.operations[0], project)

    # Not checking exact coordinates — just that we got a non-empty
    # chain of segments, which is the contract ``compute_preview`` has
    # with the UI (empty list means "no preview available").
    assert preview
    assert all(isinstance(s, (LineSegment, ArcSegment)) for s in preview)


def test_generate_toolpath_returns_irtoolpath_for_profile_op() -> None:
    project = _project_with_rect(ProfileOp(name="P", cut_depth=-1.0))
    svc = ToolpathService()

    tp = svc.generate_toolpath(project.operations[0], project)

    assert isinstance(tp, Toolpath)
    assert tp.operation_name == "P"
    # Every toolpath ends by retracting to safe height.
    assert any(
        i.type is MoveType.RAPID and i.z == project.settings.safe_height
        for i in tp.instructions
    )


def test_generate_toolpath_returns_none_for_disabled_op() -> None:
    project = _project_with_rect(ProfileOp(name="P", cut_depth=-1.0))
    project.operations[0].enabled = False
    svc = ToolpathService()

    assert svc.generate_toolpath(project.operations[0], project) is None


# -------------------------------------------------------------- unknown op types


@dataclass
class _UnknownOp:
    """Not a registered op type — service should skip it, not raise."""

    name: str = "unknown"
    enabled: bool = True
    type: str = "unknown"
    geometry_refs: list = field(default_factory=list)


def test_unsupported_op_type_yields_empty_preview() -> None:
    assert ToolpathService().compute_preview(_UnknownOp(), Project()) == []


def test_unsupported_op_type_yields_none_toolpath() -> None:
    assert ToolpathService().generate_toolpath(_UnknownOp(), Project()) is None


def test_unsupported_op_type_is_not_in_supports_set() -> None:
    assert not ToolpathService().supports(_UnknownOp())


# -------------------------------------------------------------- registration


def test_register_preview_plugs_in_new_op_type() -> None:
    svc = ToolpathService()
    recorded: list[str] = []

    def preview_fn(op, project) -> list[Segment]:
        recorded.append(op.name)
        return [LineSegment(start=(0, 0), end=(1, 0))]

    svc.register_preview(_UnknownOp, preview_fn)
    result = svc.compute_preview(_UnknownOp(name="newtype"), Project())

    assert recorded == ["newtype"]
    assert len(result) == 1


def test_register_toolpath_plugs_in_new_op_type() -> None:
    svc = ToolpathService()

    def toolpath_fn(op, project) -> Toolpath:
        return Toolpath(
            operation_name=op.name,
            tool_number=0,
            instructions=[IRInstruction(type=MoveType.COMMENT, comment="hi")],
        )

    svc.register_toolpath(_UnknownOp, toolpath_fn)
    tp = svc.generate_toolpath(_UnknownOp(name="custom"), Project())

    assert tp is not None
    assert tp.operation_name == "custom"


# -------------------------------------------------------------- generate_program


class _RecordingPost:
    """Minimal PostProcessor: records the toolpaths it received."""

    name = "recorder"

    def __init__(self) -> None:
        self.received: list[Toolpath] = []

    def post_program(self, toolpaths: list[Toolpath]) -> str:
        self.received = list(toolpaths)
        return "(recorded)"


def test_generate_program_feeds_all_enabled_ops_to_post() -> None:
    project = _project_with_rect(ProfileOp(name="P1", cut_depth=-1.0))
    entity = project.geometry_layers[0].entities[0]
    project.operations.append(
        ProfileOp(
            name="P2",
            cut_depth=-1.0,
            tool_controller_id=1,
            geometry_refs=[GeometryRef(layer_name="L", entity_id=entity.id)],
        )
    )
    svc = ToolpathService()
    post = _RecordingPost()

    gcode, toolpaths = svc.generate_program(project, post)

    assert gcode == "(recorded)"
    assert [tp.operation_name for tp in post.received] == ["P1", "P2"]
    assert toolpaths == post.received


def test_generate_program_skips_disabled_ops() -> None:
    project = _project_with_rect(ProfileOp(name="P1", cut_depth=-1.0))
    entity = project.geometry_layers[0].entities[0]
    project.operations.append(
        ProfileOp(
            name="P2",
            cut_depth=-1.0,
            enabled=False,
            tool_controller_id=1,
            geometry_refs=[GeometryRef(layer_name="L", entity_id=entity.id)],
        )
    )
    svc = ToolpathService()
    post = _RecordingPost()

    _, toolpaths = svc.generate_program(project, post)

    assert [tp.operation_name for tp in toolpaths] == ["P1"]


def test_generate_program_propagates_engine_errors() -> None:
    """A generation failure aborts the whole program — we don't want
    partial G-code to ship with an operation silently missing."""
    project = Project()
    # No ToolController registered → resolve_tool_controller raises.
    layer = GeometryLayer(name="L", entities=[_rect_entity()])
    project.geometry_layers.append(layer)
    project.operations.append(
        ProfileOp(
            name="Broken",
            cut_depth=-1.0,
            tool_controller_id=1,  # not in project
            geometry_refs=[
                GeometryRef(
                    layer_name="L", entity_id=layer.entities[0].id
                )
            ],
        )
    )
    svc = ToolpathService()

    with pytest.raises(EngineError):
        svc.generate_program(project, _RecordingPost())
