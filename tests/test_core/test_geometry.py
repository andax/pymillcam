"""Tests for pymillcam.core.geometry."""
from __future__ import annotations

from shapely.geometry import LineString, Polygon

from pymillcam.core.geometry import EntitySource, GeometryEntity, GeometryLayer


def test_entity_defaults() -> None:
    entity = GeometryEntity(geom=Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]))
    assert entity.source is EntitySource.DXF
    assert entity.dxf_entity_type is None
    assert entity.id  # uuid populated
    assert entity.is_closed is True


def test_entity_open_linestring_is_not_closed() -> None:
    entity = GeometryEntity(geom=LineString([(0, 0), (10, 0), (10, 10)]))
    assert entity.is_closed is False


def test_entity_closed_linestring_is_closed() -> None:
    entity = GeometryEntity(geom=LineString([(0, 0), (10, 0), (10, 10), (0, 0)]))
    assert entity.is_closed is True


def test_entity_round_trips_via_wkt_json() -> None:
    original = GeometryEntity(
        geom=Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
        source=EntitySource.DXF,
        dxf_entity_type="lwpolyline",
    )
    restored = GeometryEntity.model_validate_json(original.model_dump_json())
    assert restored.id == original.id
    assert restored.source is EntitySource.DXF
    assert restored.dxf_entity_type == "lwpolyline"
    assert restored.geom.equals(original.geom)


def test_layer_find_entity() -> None:
    a = GeometryEntity(geom=LineString([(0, 0), (1, 1)]))
    b = GeometryEntity(geom=LineString([(2, 2), (3, 3)]))
    layer = GeometryLayer(name="Profile", entities=[a, b])
    assert layer.find_entity(a.id) is a
    assert layer.find_entity(b.id) is b
    assert layer.find_entity("missing") is None


def test_layer_round_trips_via_json() -> None:
    layer = GeometryLayer(
        name="Profile_Outside",
        color="#ff0000",
        entities=[GeometryEntity(geom=Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))],
        source_dxf_path="/tmp/part.dxf",
    )
    restored = GeometryLayer.model_validate_json(layer.model_dump_json())
    assert restored.name == "Profile_Outside"
    assert restored.color == "#ff0000"
    assert restored.source_dxf_path == "/tmp/part.dxf"
    assert len(restored.entities) == 1
    assert restored.entities[0].geom.equals(layer.entities[0].geom)
