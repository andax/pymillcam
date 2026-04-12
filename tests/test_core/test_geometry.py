"""Tests for pymillcam.core.geometry."""
from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point, Polygon

from pymillcam.core.geometry import EntitySource, GeometryEntity, GeometryLayer
from pymillcam.core.segments import ArcSegment, LineSegment


def _rect_segments(w: float = 10.0, h: float = 10.0) -> list:
    return [
        LineSegment(start=(0, 0), end=(w, 0)),
        LineSegment(start=(w, 0), end=(w, h)),
        LineSegment(start=(w, h), end=(0, h)),
        LineSegment(start=(0, h), end=(0, 0)),
    ]


def test_entity_rejects_empty_shape() -> None:
    with pytest.raises(ValueError, match="must have either"):
        GeometryEntity()


def test_entity_rejects_point_and_segments() -> None:
    with pytest.raises(ValueError, match="cannot hold both"):
        GeometryEntity(
            point=(0, 0),
            segments=[LineSegment(start=(0, 0), end=(1, 0))],
        )


def test_entity_point_yields_shapely_point() -> None:
    entity = GeometryEntity(point=(3.0, 4.0))
    assert isinstance(entity.geom, Point)
    assert (entity.geom.x, entity.geom.y) == (3.0, 4.0)
    assert entity.is_closed is False


def test_entity_open_chain_yields_linestring() -> None:
    entity = GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(10, 0)),
            LineSegment(start=(10, 0), end=(10, 5)),
        ],
        closed=False,
    )
    assert isinstance(entity.geom, LineString)
    assert entity.is_closed is False


def test_entity_closed_chain_yields_polygon() -> None:
    entity = GeometryEntity(segments=_rect_segments(), closed=True)
    assert isinstance(entity.geom, Polygon)
    assert entity.is_closed is True
    assert math.isclose(entity.geom.area, 100.0)


def test_entity_full_circle_arc_yields_polygon() -> None:
    entity = GeometryEntity(
        segments=[ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    assert isinstance(entity.geom, Polygon)
    # Default shadow tolerance (0.01 mm) gives ~0.3% area error at r=5.
    assert math.isclose(entity.geom.area, math.pi * 25, rel_tol=1e-2)


def test_entity_round_trips_via_json_with_arcs() -> None:
    original = GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(10, 0)),
            ArcSegment(center=(10, 5), radius=5, start_angle_deg=-90, sweep_deg=180),
            LineSegment(start=(10, 10), end=(0, 10)),
            ArcSegment(center=(0, 5), radius=5, start_angle_deg=90, sweep_deg=180),
        ],
        closed=True,
        source=EntitySource.DXF,
        dxf_entity_type="lwpolyline",
    )
    restored = GeometryEntity.model_validate_json(original.model_dump_json())
    assert restored.id == original.id
    assert restored.closed is True
    assert len(restored.segments) == 4
    # Discriminated union preserves concrete types.
    assert restored.segments[1].type == "arc"
    assert isinstance(restored.geom, Polygon)


def test_layer_find_entity() -> None:
    a = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(1, 1))])
    b = GeometryEntity(segments=[LineSegment(start=(2, 2), end=(3, 3))])
    layer = GeometryLayer(name="Profile", entities=[a, b])
    assert layer.find_entity(a.id) is a
    assert layer.find_entity(b.id) is b
    assert layer.find_entity("missing") is None


def test_layer_round_trips_via_json() -> None:
    layer = GeometryLayer(
        name="Profile_Outside",
        color="#ff0000",
        entities=[GeometryEntity(segments=_rect_segments(), closed=True)],
        source_dxf_path="/tmp/part.dxf",
    )
    restored = GeometryLayer.model_validate_json(layer.model_dump_json())
    assert restored.name == "Profile_Outside"
    assert restored.color == "#ff0000"
    assert restored.source_dxf_path == "/tmp/part.dxf"
    assert len(restored.entities) == 1
    assert math.isclose(restored.entities[0].geom.area, 100.0)
