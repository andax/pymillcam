"""Rest-machining cleanup pass for OFFSET pockets.

After the regular + adaptive passes finish, we compare the swept area
(each emitted ring's centerline buffered by tool_radius) against the
cuttable area. The residual inside `tool_center_space` is emitted as
one or more cleanup ring-groups, which handles V-notch corners where
an island grows close to the boundary.
"""
from __future__ import annotations

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from pymillcam.core.operations import MillingDirection
from pymillcam.core.segments import Segment

from ._shared import _extract_polygons, _polygon_to_ring_group


def _polygon_centerlines(poly: Polygon) -> list[LineString]:
    """Extract exterior + interior rings as LineStrings (the cutter-
    centerline paths for a single emitted ring-group)."""
    lines: list[LineString] = [LineString(poly.exterior.coords)]
    for interior in poly.interiors:
        lines.append(LineString(interior.coords))
    return lines


def _rest_machining_groups(
    machinable: Polygon,
    centerlines: list[LineString],
    tool_radius: float,
    direction: MillingDirection,
) -> list[list[list[Segment]]]:
    """Emit cleanup ring-groups for uncut-but-cuttable residual area.

    `cuttable` is the material the tool can physically reach given its
    radius: `machinable.buffer(-r).buffer(+r)`. `swept` is the material
    the regular passes already removed, approximated by each emitted
    centerline buffered by tool_radius. `residual = cuttable − swept` is
    the uncut material.

    For the tool to clean up residual component `r` without gouging
    walls, its center must stay inside `tool_center_space`. The valid
    walking area is therefore `r ∩ tool_center_space` — the part of the
    residual the tool center can actually reach. Walking this shape's
    exterior traces the border between residual and swept on one side
    and along the walls on the other, naturally descending into V-notch
    corridors.

    Note: a single exterior walk only covers residuals up to ~2·tool_radius
    wide. Wider residual chunks would need multiple rings inside the
    component — revisit when a test case surfaces them. Skips residuals
    below `(0.2·tool_radius)²` (below kerf/deflection on a typical router).
    """
    tool_center_space = machinable.buffer(-tool_radius)
    if tool_center_space.is_empty:
        return []
    swept = unary_union([line.buffer(tool_radius) for line in centerlines])
    cuttable = tool_center_space.buffer(+tool_radius)
    residual = cuttable.difference(swept)
    if residual.is_empty:
        return []

    min_residual_area = (tool_radius * 0.2) ** 2

    groups: list[list[list[Segment]]] = []
    for r in _extract_polygons(residual):
        if r.area < min_residual_area:
            continue
        target = r.intersection(tool_center_space)
        for t in _extract_polygons(target):
            if t.area < min_residual_area:
                continue
            g = _polygon_to_ring_group(t, direction)
            if g:
                groups.append(g)
    return groups
