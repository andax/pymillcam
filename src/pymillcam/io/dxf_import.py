"""DXF → GeometryLayer import via ezdxf.

Supports LINE, LWPOLYLINE (with bulges), POLYLINE (with bulges), CIRCLE,
ARC, and POINT. Splines, ellipses, hatches, and blocks are silently
skipped — they can be added as needed.

All coordinates are normalized to millimetres; if the DXF declares inch
units via $INSUNITS, entity coordinates are scaled on import. Arcs are
preserved analytically as `ArcSegment` objects — no chord approximation
happens in the importer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import Arc, Circle, DXFEntity, Line, LWPolyline, Point as DxfPoint, Polyline

from pymillcam.core.geometry import EntitySource, GeometryEntity, GeometryLayer
from pymillcam.core.path_stitching import stitch_entities
from pymillcam.core.segments import ArcSegment, LineSegment, Segment

INSUNITS_INCHES = 1
INSUNITS_MILLIMETERS = 4


class DxfImportError(Exception):
    """Raised when a DXF file can't be opened or parsed."""


@dataclass
class _EntityShape:
    """Intermediate result of converting one DXF entity."""
    segments: list[Segment] = field(default_factory=list)
    closed: bool = False
    point: tuple[float, float] | None = None


def import_dxf(
    path: str | Path,
    *,
    stitch_tolerance: float | None = None,
) -> list[GeometryLayer]:
    """Import `path` and return one GeometryLayer per non-empty DXF layer.

    If `stitch_tolerance` is set, runs `path_stitching.stitch_entities` per
    layer with that tolerance — useful for DXFs authored as separate LINE
    entities. Pass `None` (default) to leave entities as-imported.
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
        shape = _entity_to_shape(dxf_entity, scale)
        if shape is None:
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
                segments=shape.segments,
                closed=shape.closed,
                point=shape.point,
                source=EntitySource.DXF,
                dxf_entity_type=dxf_entity.dxftype().lower(),
            )
        )

    if stitch_tolerance is not None:
        for layer in layers.values():
            layer.entities = stitch_entities(layer.entities, stitch_tolerance)

    return list(layers.values())


def _unit_scale(doc: Drawing) -> float:
    units = doc.header.get("$INSUNITS", 0)
    if units == INSUNITS_INCHES:
        return 25.4
    return 1.0


def _entity_to_shape(entity: DXFEntity, scale: float) -> _EntityShape | None:
    # Arc must be checked before Circle: ezdxf's Arc inherits from Circle.
    if isinstance(entity, Arc):
        c = entity.dxf.center
        start = float(entity.dxf.start_angle)
        end = float(entity.dxf.end_angle)
        sweep = (end - start) % 360.0
        if sweep == 0.0:
            sweep = 360.0
        arc = ArcSegment(
            center=(c.x * scale, c.y * scale),
            radius=entity.dxf.radius * scale,
            start_angle_deg=start,
            sweep_deg=sweep,
        )
        return _EntityShape(segments=[arc], closed=arc.is_full_circle)

    if isinstance(entity, Circle):
        c = entity.dxf.center
        return _EntityShape(
            segments=[
                ArcSegment(
                    center=(c.x * scale, c.y * scale),
                    radius=entity.dxf.radius * scale,
                    start_angle_deg=0.0,
                    sweep_deg=360.0,
                )
            ],
            closed=True,
        )

    if isinstance(entity, Line):
        s = entity.dxf.start
        e = entity.dxf.end
        return _EntityShape(
            segments=[
                LineSegment(
                    start=(s.x * scale, s.y * scale),
                    end=(e.x * scale, e.y * scale),
                )
            ],
            closed=False,
        )

    if isinstance(entity, LWPolyline):
        pts = [
            (float(x) * scale, float(y) * scale, float(bulge))
            for x, y, _w1, _w2, bulge in entity.get_points("xyseb")
        ]
        return _polyline_shape(pts, bool(entity.closed))

    if isinstance(entity, Polyline):
        pts = [
            (
                v.dxf.location.x * scale,
                v.dxf.location.y * scale,
                float(getattr(v.dxf, "bulge", 0.0) or 0.0),
            )
            for v in entity.vertices
        ]
        return _polyline_shape(pts, bool(entity.is_closed))

    if isinstance(entity, DxfPoint):
        loc = entity.dxf.location
        return _EntityShape(point=(loc.x * scale, loc.y * scale))

    return None


def _polyline_shape(
    points_with_bulge: list[tuple[float, float, float]],
    closed: bool,
) -> _EntityShape | None:
    """Convert an (x, y, bulge) sequence into a segment chain."""
    n = len(points_with_bulge)
    if n < 2:
        return None

    segments: list[Segment] = []
    for i in range(n):
        is_last_vertex = i == n - 1
        if is_last_vertex and not closed:
            break
        x1, y1, bulge = points_with_bulge[i]
        x2, y2, _ = points_with_bulge[(i + 1) % n]
        if (x1, y1) == (x2, y2):
            continue
        if bulge == 0.0:
            segments.append(LineSegment(start=(x1, y1), end=(x2, y2)))
        else:
            segments.append(_arc_from_bulge((x1, y1), (x2, y2), bulge))

    if not segments:
        return None
    return _EntityShape(segments=segments, closed=closed)


def _arc_from_bulge(
    start: tuple[float, float],
    end: tuple[float, float],
    bulge: float,
) -> ArcSegment:
    """Build an ArcSegment for a LWPOLYLINE bulge between two vertices.

    Bulge convention (AutoCAD):
      bulge = tan(θ / 4)
    where θ is the signed included angle (sweep). Positive bulge → CCW.
    The center sits at perpendicular offset R·cos(θ/2) from the chord
    midpoint, on the side of the chord determined by the bulge sign.
    """
    x1, y1 = start
    x2, y2 = end
    chord_dx = x2 - x1
    chord_dy = y2 - y1
    chord_length = math.hypot(chord_dx, chord_dy)
    if chord_length == 0.0:
        raise ValueError("Cannot build arc from degenerate zero-length chord")

    half_angle = 2.0 * math.atan(bulge)           # α, signed
    sweep_rad = 4.0 * math.atan(bulge)            # θ, signed
    radius = chord_length / (2.0 * abs(math.sin(half_angle)))
    perp_offset = radius * math.cos(half_angle)   # ≥ 0 since |α| < π/2

    # Perpendicular to chord, rotated +90° (CCW from chord direction).
    perp_x = -chord_dy / chord_length
    perp_y = chord_dx / chord_length

    mid_x = (x1 + x2) / 2.0
    mid_y = (y1 + y2) / 2.0
    sign = 1.0 if bulge > 0 else -1.0
    cx = mid_x + sign * perp_offset * perp_x
    cy = mid_y + sign * perp_offset * perp_y

    start_angle_deg = math.degrees(math.atan2(y1 - cy, x1 - cx))
    return ArcSegment(
        center=(cx, cy),
        radius=radius,
        start_angle_deg=start_angle_deg,
        sweep_deg=math.degrees(sweep_rad),
    )
