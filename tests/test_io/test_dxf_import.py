"""Tests for pymillcam.io.dxf_import.

DXF files are constructed in-memory via ezdxf and written to pytest's
tmp_path, so no checked-in fixtures are required.
"""
from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest

from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.io.dxf_import import DxfImportError, import_dxf


def _write_dxf(tmp_path: Path, build) -> Path:
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


def test_line_becomes_single_line_segment(tmp_path: Path) -> None:
    path = _write_dxf(tmp_path, lambda msp, doc: msp.add_line((0, 0), (10, 5)))
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.dxf_entity_type == "line"
    assert entity.closed is False
    assert len(entity.segments) == 1
    seg = entity.segments[0]
    assert isinstance(seg, LineSegment)
    assert seg.start == (0.0, 0.0)
    assert seg.end == (10.0, 5.0)


def test_closed_lwpolyline_becomes_closed_line_chain(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.closed is True
    assert len(entity.segments) == 4
    assert all(isinstance(s, LineSegment) for s in entity.segments)
    assert math.isclose(entity.geom.area, 100.0)


def test_open_lwpolyline_drops_closing_segment(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=False)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.closed is False
    assert len(entity.segments) == 2


def test_lwpolyline_bulge_becomes_arc_segment(tmp_path: Path) -> None:
    def build(msp, doc):
        # Quarter-circle bulge from (0,0) to (10,10): bulge = tan(90°/4) ≈ 0.4142.
        bulge = math.tan(math.radians(22.5))
        msp.add_lwpolyline(
            [(0, 0, 0, 0, bulge), (10, 10, 0, 0, 0)],
            format="xyseb",
            close=False,
        )

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert len(entity.segments) == 1
    arc = entity.segments[0]
    assert isinstance(arc, ArcSegment)
    # CCW quarter-circle from (0,0) to (10,10) has center at (0, 10) or (10, 0)
    # depending on sweep direction; bulge > 0 is CCW, center at (0, 10).
    assert math.isclose(arc.radius, 10.0, rel_tol=1e-9)
    assert math.isclose(arc.sweep_deg, 90.0, abs_tol=1e-9)
    cx, cy = arc.center
    assert math.isclose(cx, 0.0, abs_tol=1e-9)
    assert math.isclose(cy, 10.0, abs_tol=1e-9)
    # End point derived from arc should match the polyline's next vertex.
    ex, ey = arc.end
    assert math.isclose(ex, 10.0, abs_tol=1e-6)
    assert math.isclose(ey, 10.0, abs_tol=1e-6)


def test_lwpolyline_mixed_line_and_bulge(tmp_path: Path) -> None:
    def build(msp, doc):
        bulge = math.tan(math.radians(45))  # half-circle bulge (θ=180°)
        msp.add_lwpolyline(
            [
                (0, 0, 0, 0, 0),      # straight to next
                (10, 0, 0, 0, bulge), # half-circle to next
                (10, 10, 0, 0, 0),    # straight (closing)
            ],
            format="xyseb",
            close=True,
        )

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.closed is True
    assert len(entity.segments) == 3
    kinds = [s.type for s in entity.segments]
    assert kinds == ["line", "arc", "line"]


def test_circle_becomes_full_circle_arc_segment(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_circle(center=(5, 5), radius=2)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.closed is True
    assert len(entity.segments) == 1
    arc = entity.segments[0]
    assert isinstance(arc, ArcSegment)
    assert arc.is_full_circle is True
    assert arc.center == (5.0, 5.0)
    assert arc.radius == 2.0


def test_arc_becomes_partial_arc_segment(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_arc(center=(0, 0), radius=10, start_angle=0, end_angle=90)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.closed is False
    (arc,) = entity.segments
    assert isinstance(arc, ArcSegment)
    assert math.isclose(arc.sweep_deg, 90.0)
    assert arc.is_full_circle is False


def test_arc_crossing_seam_has_positive_sweep(tmp_path: Path) -> None:
    def build(msp, doc):
        # Arc from 350° to 10° crosses the 0° seam: sweep should be +20°.
        msp.add_arc(center=(0, 0), radius=5, start_angle=350, end_angle=10)

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    (arc,) = entity.segments
    assert math.isclose(arc.sweep_deg, 20.0)


def test_point_becomes_point_entity(tmp_path: Path) -> None:
    path = _write_dxf(tmp_path, lambda msp, doc: msp.add_point((3, 4)))
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    assert entity.point == (3.0, 4.0)
    assert entity.segments == []


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
        msp.add_line((0, 0), (1, 0))

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    (seg,) = entity.segments
    assert isinstance(seg, LineSegment)
    assert math.isclose(seg.end[0], 25.4)


def test_inch_units_scale_applies_to_bulge_arcs(tmp_path: Path) -> None:
    def build(msp, doc):
        doc.header["$INSUNITS"] = 1  # inches
        bulge = math.tan(math.radians(22.5))
        msp.add_lwpolyline(
            [(0, 0, 0, 0, bulge), (1, 1, 0, 0, 0)],
            format="xyseb",
            close=False,
        )

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    (entity,) = layer.entities
    (arc,) = entity.segments
    assert isinstance(arc, ArcSegment)
    # Original 1-inch-chord quarter circle → 25.4 mm chord → 25.4 mm radius.
    assert math.isclose(arc.radius, 25.4, rel_tol=1e-9)


def test_unsupported_entities_are_skipped(tmp_path: Path) -> None:
    def build(msp, doc):
        msp.add_line((0, 0), (1, 1))
        msp.add_text("hello", dxfattribs={"insert": (0, 0)})

    path = _write_dxf(tmp_path, build)
    (layer,) = import_dxf(path)
    assert len(layer.entities) == 1
    assert layer.entities[0].dxf_entity_type == "line"
