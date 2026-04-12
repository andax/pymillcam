"""DXF → GeometryLayer import via ezdxf.

Supports LINE, LWPOLYLINE, POLYLINE, CIRCLE, ARC, POINT. Splines, ellipses,
hatches, and blocks are silently skipped — they can be added as needed.
All coordinates are normalized to millimetres; if the DXF declares inch
units via $INSUNITS, entity coordinates are scaled on import.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import Arc, Circle, DXFEntity, Line, LWPolyline, Polyline
from ezdxf.entities import Point as DxfPoint
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

from pymillcam.core.geometry import EntitySource, GeometryEntity, GeometryLayer

# AutoCAD $INSUNITS enum values we care about.
INSUNITS_INCHES = 1
INSUNITS_MILLIMETERS = 4

# Angular step used when discretizing circles/arcs into vertex polylines, in
# degrees. 5° is a reasonable default for preview; toolpath generation may
# re-sample later with tighter tolerances if needed.
ARC_SEGMENT_DEG = 5.0


class DxfImportError(Exception):
    """Raised when a DXF file can't be opened or parsed."""


def import_dxf(path: str | Path) -> list[GeometryLayer]:
    """Import `path` and return one GeometryLayer per non-empty DXF layer.

    Empty layers (no supported entities) are dropped so the caller only sees
    layers that actually contribute geometry.
    """
    path = Path(path)
    try:
        doc: Drawing = ezdxf.readfile(str(path))
    except OSError as e:
        raise DxfImportError(f"Cannot open {path}: {e}") from e
    except ezdxf.DXFStructureError as e:
        raise DxfImportError(f"Invalid DXF {path}: {e}") from e

    scale = _unit_scale(doc)
    timestamp = datetime.now(UTC)

    layers: dict[str, GeometryLayer] = {}
    for dxf_entity in doc.modelspace():
        geom = _entity_to_shapely(dxf_entity, scale)
        if geom is None:
            continue
        layer_name = dxf_entity.dxf.layer
        layer = layers.setdefault(
            layer_name,
            GeometryLayer(
                name=layer_name,
                source_dxf_path=str(path),
                import_timestamp=timestamp,
            ),
        )
        layer.entities.append(
            GeometryEntity(
                geom=geom,
                source=EntitySource.DXF,
                dxf_entity_type=dxf_entity.dxftype().lower(),
            )
        )

    return list(layers.values())


def _unit_scale(doc: Drawing) -> float:
    """Return the multiplier to bring DXF coordinates to millimetres."""
    units = doc.header.get("$INSUNITS", 0)
    if units == INSUNITS_INCHES:
        return 25.4
    return 1.0


def _entity_to_shapely(entity: DXFEntity, scale: float) -> BaseGeometry | None:
    # Arc must be checked before Circle: ezdxf's Arc inherits from Circle.
    if isinstance(entity, Arc):
        c = entity.dxf.center
        return LineString(_discretize_arc(
            c.x * scale, c.y * scale, entity.dxf.radius * scale,
            entity.dxf.start_angle, entity.dxf.end_angle,
        ))

    if isinstance(entity, Circle):
        c = entity.dxf.center
        return Polygon(_discretize_arc(
            c.x * scale, c.y * scale, entity.dxf.radius * scale, 0.0, 360.0,
        ))

    if isinstance(entity, Line):
        s = entity.dxf.start
        e = entity.dxf.end
        return LineString([(s.x * scale, s.y * scale), (e.x * scale, e.y * scale)])

    if isinstance(entity, LWPolyline):
        raw = entity.get_points("xy")
        points = [(float(x) * scale, float(y) * scale) for x, y in raw]
        if len(points) < 2:
            return None
        if entity.closed:
            if points[0] != points[-1]:
                points.append(points[0])
            return Polygon(points)
        return LineString(points)

    if isinstance(entity, Polyline):
        points = [
            (v.dxf.location.x * scale, v.dxf.location.y * scale) for v in entity.vertices
        ]
        if len(points) < 2:
            return None
        if entity.is_closed:
            if points[0] != points[-1]:
                points.append(points[0])
            return Polygon(points)
        return LineString(points)

    if isinstance(entity, DxfPoint):
        loc = entity.dxf.location
        return Point(loc.x * scale, loc.y * scale)

    return None


def _discretize_arc(
    cx: float, cy: float, r: float, start_deg: float, end_deg: float,
) -> list[tuple[float, float]]:
    """Sample an arc CCW from start_deg to end_deg (both in degrees).

    Handles the case where end_deg <= start_deg (arc crosses the 0° seam) by
    adding a 360° offset. A full circle (end_deg - start_deg == 360°) is
    sampled with the final vertex equal to the first, so callers can feed
    the result directly into a Polygon.
    """
    if end_deg <= start_deg:
        end_deg += 360.0
    span = end_deg - start_deg
    n = max(2, math.ceil(span / ARC_SEGMENT_DEG) + 1)
    return [
        (
            cx + r * math.cos(math.radians(start_deg + span * i / (n - 1))),
            cy + r * math.sin(math.radians(start_deg + span * i / (n - 1))),
        )
        for i in range(n)
    ]
