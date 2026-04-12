"""Tests for pymillcam.io.dxf_import.

DXF files are constructed in-memory via ezdxf and written to pytest's
tmp_path, so no checked-in fixtures are required.
"""
from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest
from shapely.geometry import LineString, Point, Polygon

from pymillcam.io.dxf_import import DxfImportError, import_dxf


def _write_dxf(tmp_path: Path, build) -> Path:
    """Create a minimal DXF, let `build(msp, doc)` add entities, save, return path."""
    doc = ezdxf.new()
    build(doc.modelspace(), doc)
    path = tmp_path / "part.dxf"
    doc.saveas(path)
    return path


def test_missing_file_raises() -> None:
    with pytest.raises(DxfImportError):
        import_dxf("/nonexistent/path.dxf")


def test_empty_drawing_returns_no_layers(tmp_path: Path) -> None:
    path = _write_dxf(tmp_path, lambda msp, doc: None)
    assert import_dxf(path) == []


def test_line_becomes_linestring(tmp_path: Path) -> None:
    path = _write_dxf(tmp_path, lambda msp, doc: msp.add_line((0, 0), (10, 5)))
    layers = import_dxf(path)
    assert len(layers) == 1
    (entity,) = layers[0].entities
    assert entity.dxf_entity_type == "line"
    assert isinstance(entity.geom, LineString)
    assert list(entity.geom.coords) == [(0.0, 0.0), (10.0, 5.0)]
    assert entity.is_closed is False


def test_closed_lwpolyline_becomes_polygon(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert isinstance(entity.geom, Polygon)
    assert entity.is_closed is True
    assert math.isclose(entity.geom.area, 100.0)


def test_open_lwpolyline_becomes_linestring(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=False)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert isinstance(entity.geom, LineString)
    assert entity.is_closed is False


def test_circle_becomes_closed_polygon(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_circle(center=(5, 5), radius=2)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert isinstance(entity.geom, Polygon)
    # π r² = 4π ≈ 12.566; discretization loses a tiny bit of area.
    assert math.isclose(entity.geom.area, math.pi * 4, rel_tol=1e-2)


def test_arc_becomes_open_linestring(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_arc(center=(0, 0), radius=10, start_angle=0, end_angle=90)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.dxf_entity_type == "arc"
    assert isinstance(entity.geom, LineString)
    assert entity.is_closed is False
    # First and last points are on the arc endpoints.
    coords = list(entity.geom.coords)
    assert math.isclose(coords[0][0], 10.0) and math.isclose(coords[0][1], 0.0, abs_tol=1e-9)
    assert math.isclose(coords[-1][0], 0.0, abs_tol=1e-9) and math.isclose(coords[-1][1], 10.0)


def test_point_becomes_point(tmp_path: Path) -> None:
    path = _write_dxf(tmp_path, lambda msp, doc: msp.add_point((3, 4)))
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert isinstance(entity.geom, Point)
    assert (entity.geom.x, entity.geom.y) == (3.0, 4.0)


def test_entities_grouped_by_dxf_layer(tmp_path: Path) -> None:
    def build(msp, doc):
        doc.layers.add("Profile_Outside")
        doc.layers.add("Drill_2mm")
        msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "Profile_Outside"})
        msp.add_line((5, 5), (6, 6), dxfattribs={"layer": "Profile_Outside"})
        msp.add_circle(center=(1, 1), radius=0.5, dxfattribs={"layer": "Drill_2mm"})

    path = _write_dxf(tmp_path, build)
    layers = import_dxf(path)
    by_name = {layer.name: layer for layer in layers}
    assert set(by_name) == {"Profile_Outside", "Drill_2mm"}
    assert len(by_name["Profile_Outside"].entities) == 2
    assert len(by_name["Drill_2mm"].entities) == 1


def test_records_source_path_and_timestamp(tmp_path: Path) -> None:
    path = _write_dxf(tmp_path, lambda msp, doc: msp.add_line((0, 0), (1, 0)))
    (layer,) = import_dxf(path)
    assert layer.source_dxf_path == str(path)
    assert layer.import_timestamp is not None


def test_inch_units_scale_to_mm(tmp_path: Path) -> None:
    def build(msp, doc):
        doc.header["$INSUNITS"] = 1  # inches
        msp.add_line((0, 0), (1, 0))  # 1 inch

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    coords = list(entity.geom.coords)
    assert math.isclose(coords[1][0], 25.4)


def test_unsupported_entities_are_skipped(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_line((0, 0), (1, 1))
        # TEXT is intentionally unsupported by the Phase 1 importer.
        msp.add_text("hello", dxfattribs={"insert": (0, 0)})

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    assert len(layer.entities) == 1
    assert layer.entities[0].dxf_entity_type == "line"
