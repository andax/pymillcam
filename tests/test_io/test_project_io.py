"""Tests for pymillcam.io.project_io."""
from __future__ import annotations

from pathlib import Path

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.operations import GeometryRef, OffsetSide, ProfileOp
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.core.tools import Tool, ToolController
from pymillcam.io.project_io import ProjectLoadError, load_project, save_project


def _populated_project() -> Project:
    entity = GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(50, 0)),
            LineSegment(start=(50, 0), end=(50, 30)),
            ArcSegment(center=(45, 30), radius=5, start_angle_deg=0, sweep_deg=180),
            LineSegment(start=(40, 30), end=(0, 30)),
            LineSegment(start=(0, 30), end=(0, 0)),
        ],
        closed=True,
    )
    layer = GeometryLayer(name="Profile_Outside", entities=[entity])
    tc = ToolController(tool_number=1, tool=Tool(name="3mm flat"))
    op = ProfileOp(
        name="Outer",
        tool_controller_id=1,
        cut_depth=-6.0,
        stepdown=2.0,
        offset_side=OffsetSide.OUTSIDE,
        chord_tolerance=0.02,
        geometry_refs=[GeometryRef(layer_name=layer.name, entity_id=entity.id)],
    )
    return Project(
        name="Demo",
        geometry_layers=[layer],
        tool_controllers=[tc],
        operations=[op],
    )


def test_round_trip_preserves_project(tmp_path: Path) -> None:
    original = _populated_project()
    path = tmp_path / "demo.pmc"
    save_project(original, path)
    restored = load_project(path)
    assert restored == original


def test_save_accepts_string_path(tmp_path: Path) -> None:
    original = _populated_project()
    path_str = str(tmp_path / "demo.pmc")
    save_project(original, path_str)
    assert load_project(path_str) == original


def test_save_writes_pretty_printed_json_by_default(tmp_path: Path) -> None:
    path = tmp_path / "demo.pmc"
    save_project(_populated_project(), path)
    text = path.read_text(encoding="utf-8")
    # Pretty-printed JSON contains newlines; compact form does not.
    assert "\n" in text


def test_save_compact_when_indent_none(tmp_path: Path) -> None:
    path = tmp_path / "demo.pmc"
    save_project(_populated_project(), path, indent=None)
    text = path.read_text(encoding="utf-8")
    assert "\n" not in text


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectLoadError, match="Cannot open"):
        load_project(tmp_path / "does_not_exist.pmc")


def test_load_malformed_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.pmc"
    path.write_text("{ this is not valid json", encoding="utf-8")
    with pytest.raises(ProjectLoadError, match="Invalid project"):
        load_project(path)


def test_load_wrong_schema_raises(tmp_path: Path) -> None:
    path = tmp_path / "wrong.pmc"
    # Valid JSON but missing required fields / wrong types.
    path.write_text('{"stock": "not-a-stock-object"}', encoding="utf-8")
    with pytest.raises(ProjectLoadError, match="Invalid project"):
        load_project(path)


def test_round_trip_empty_project(tmp_path: Path) -> None:
    path = tmp_path / "empty.pmc"
    save_project(Project(), path)
    restored = load_project(path)
    assert restored == Project()
