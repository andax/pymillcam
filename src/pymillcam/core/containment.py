"""Geometric containment helpers for pocket islands.

Builds a containment tree from a list of closed contours: who's inside
who. The pocket engine uses this to split a flat list of selected
contours into "pocket regions" — each region is one boundary plus zero
or more islands. Even-depth contours are boundaries; odd-depth contours
are islands of their parent. Nested pockets (a boundary inside an
island) fall out for free as separate top-level regions.

The UI uses the same module to find candidate islands inside a
user-picked boundary (the "auto-detect islands" affordance).
"""
from __future__ import annotations

from shapely.geometry import Polygon

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.segments import segments_to_shapely

# Tighter than the user-facing chord_tolerance — containment is an
# internal check and benefits from a denser polygon discretization.
_CONTAINMENT_TOLERANCE_MM = 0.01


def build_pocket_regions(
    entities: list[GeometryEntity],
) -> list[tuple[GeometryEntity, list[GeometryEntity]]]:
    """Group closed entities into (boundary, [islands]) pocket regions.

    Open entities are skipped — pocket boundaries and islands must be
    closed. The containment tree is built by Shapely polygon-in-polygon
    tests; each contour's parent is the smallest polygon strictly
    containing it. Top-level (parentless) contours are pocket
    boundaries; their direct children are islands. Grandchildren become
    their own top-level boundaries — i.e., a recursive nested-pocket
    pattern handled by the depth-parity rule.
    """
    closed = [e for e in entities if e.closed and e.segments]
    if not closed:
        return []
    polygons: list[Polygon] = []
    valid_entities: list[GeometryEntity] = []
    for entity in closed:
        shape = segments_to_shapely(
            entity.segments, closed=True, tolerance=_CONTAINMENT_TOLERANCE_MM
        )
        if isinstance(shape, Polygon):
            polygons.append(shape)
            valid_entities.append(entity)
    if not polygons:
        return []
    closed = valid_entities
    parents = _compute_parents(polygons)
    depths = _compute_depths(parents)
    regions: list[tuple[GeometryEntity, list[GeometryEntity]]] = []
    for i, entity in enumerate(closed):
        if depths[i] % 2 != 0:
            continue
        islands = [closed[j] for j, p in enumerate(parents) if p == i]
        regions.append((entity, islands))
    return regions


def find_contained_entities(
    boundary: GeometryEntity,
    candidates: list[GeometryEntity],
) -> list[GeometryEntity]:
    """Return candidates strictly contained inside `boundary`.

    Used by the UI's "auto-detect islands" affordance: the user picks
    a boundary, and we surface any closed contour from the project that
    sits inside it. The boundary itself is excluded from results.
    """
    if not boundary.closed or not boundary.segments:
        return []
    boundary_poly = segments_to_shapely(
        boundary.segments, closed=True, tolerance=_CONTAINMENT_TOLERANCE_MM
    )
    out: list[GeometryEntity] = []
    for cand in candidates:
        if cand is boundary or not cand.closed or not cand.segments:
            continue
        cand_poly = segments_to_shapely(
            cand.segments, closed=True, tolerance=_CONTAINMENT_TOLERANCE_MM
        )
        if boundary_poly.contains(cand_poly):
            out.append(cand)
    return out


def _compute_parents(polygons: list[Polygon]) -> list[int | None]:
    """For each polygon, return the index of the smallest polygon strictly
    containing it, or None if it has no parent. Smallest = least area
    among all containing polygons (the "immediate" parent in the tree).
    """
    parents: list[int | None] = []
    for i, child in enumerate(polygons):
        candidates = [
            j for j, parent in enumerate(polygons)
            if j != i and parent.contains(child)
        ]
        if not candidates:
            parents.append(None)
            continue
        parents.append(min(candidates, key=lambda k: polygons[k].area))
    return parents


def _compute_depths(parents: list[int | None]) -> list[int]:
    """Chain length from each node to its root via parent links."""
    depths = [0] * len(parents)
    for i in range(len(parents)):
        d = 0
        p = parents[i]
        seen = {i}
        while p is not None:
            if p in seen:
                # Cycle — shouldn't happen with strict polygon containment,
                # but guard anyway.
                break
            seen.add(p)
            d += 1
            p = parents[p]
        depths[i] = d
    return depths
