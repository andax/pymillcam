"""Pure helpers for "select similar" queries against a project.

The UI's Select Similar menu resolves to one of these functions: pick a
seed entity (what the user right-clicked on), pick a similarity mode
(same type / same layer / same diameter), get back the list of
matching (layer_name, entity_id) references that the viewport should
select.

Kept pure — no Qt, no engine dependencies — so it's trivially testable
and reusable from wizards / scripts that need the same query.
"""
from __future__ import annotations

from enum import StrEnum

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment

# Absolute tolerance when declaring two full-circle radii "the same"
# for SAME_DIAMETER matching. 0.01 mm is generous enough to absorb the
# rounding CAD tools often apply to circle parameters, while tight
# enough to distinguish adjacent standard sizes (e.g. 3 mm vs 3.175
# mm = 1/8"; 6 mm vs 6.35 mm = 1/4").
DIAMETER_MATCH_TOLERANCE_MM = 0.01


class SimilarityMode(StrEnum):
    """Criterion for matching entities to the seed."""

    SAME_TYPE = "same_type"
    SAME_LAYER = "same_layer"
    SAME_DIAMETER = "same_diameter"


def find_similar_entities(
    seed_layer: str,
    seed_entity_id: str,
    project: Project,
    mode: SimilarityMode,
) -> list[tuple[str, str]]:
    """Return ``(layer_name, entity_id)`` for every entity matching the seed.

    The seed itself is included in the result so the caller can just
    replace the viewport's selection wholesale without worrying about
    "also keep the seed selected" bookkeeping. Sorted stably by layer
    order then original entity order so results are deterministic.

    Returns an empty list when:

    * The seed can't be located (layer or entity id doesn't exist).
    * The mode can't apply to this seed (e.g. ``SAME_DIAMETER`` on a
      line — "diameter" is meaningless there).
    """
    seed = _find_entity(seed_layer, seed_entity_id, project)
    if seed is None:
        return []

    if mode is SimilarityMode.SAME_LAYER:
        return [
            (seed_layer, e.id)
            for e in _layer_entities(seed_layer, project)
        ]
    if mode is SimilarityMode.SAME_TYPE:
        seed_kind = entity_kind(seed)
        return [
            (layer.name, e.id)
            for layer in project.geometry_layers
            for e in layer.entities
            if entity_kind(e) == seed_kind
        ]
    if mode is SimilarityMode.SAME_DIAMETER:
        seed_radius = full_circle_radius(seed)
        if seed_radius is None:
            return []  # seed isn't a circle; "same diameter" is N/A
        return [
            (layer.name, e.id)
            for layer in project.geometry_layers
            for e in layer.entities
            if _matches_radius(e, seed_radius)
        ]
    return []


# ------------------------------------------------------------------ helpers


def entity_kind(entity: GeometryEntity) -> str:
    """Coarse categorisation for SAME_TYPE matching.

    Four buckets: points, full-circles (single-arc closed), closed
    contours, open contours. Uses the entity's own shape rather than
    the original DXF ``dxf_entity_type`` so generated / stitched
    entities (whose DXF type is ``None`` or ``"path"``) still group
    consistently with their DXF-sourced equivalents.

    Exposed (not private) because MainWindow uses it to decide which
    menu items to enable for a given seed.
    """
    if entity.point is not None:
        return "point"
    if full_circle_radius(entity) is not None:
        return "circle"
    if entity.closed:
        return "closed_contour"
    return "open_contour"


def full_circle_radius(entity: GeometryEntity) -> float | None:
    """Return ``radius`` if ``entity`` is a single full-circle arc, else None.

    The full-circle-arc check matches exactly how DXF CIRCLE entities
    land in the project (imported as one 360° ArcSegment). Multi-arc
    approximations of a circle don't qualify — they're a closed
    contour, and ``SAME_DIAMETER`` won't find them. That's the right
    trade-off for the user's intent ("select all identical holes");
    a polygon that happens to be circle-ish isn't a hole.
    """
    if not entity.closed or len(entity.segments) != 1:
        return None
    seg = entity.segments[0]
    if not isinstance(seg, ArcSegment) or not seg.is_full_circle:
        return None
    return seg.radius


def _find_entity(
    layer_name: str, entity_id: str, project: Project
) -> GeometryEntity | None:
    for layer in project.geometry_layers:
        if layer.name == layer_name:
            for entity in layer.entities:
                if entity.id == entity_id:
                    return entity
    return None


def _layer_entities(
    layer_name: str, project: Project
) -> list[GeometryEntity]:
    for layer in project.geometry_layers:
        if layer.name == layer_name:
            return list(layer.entities)
    return []


def _matches_radius(entity: GeometryEntity, target_radius: float) -> bool:
    r = full_circle_radius(entity)
    if r is None:
        return False
    return abs(r - target_radius) <= DIAMETER_MATCH_TOLERANCE_MM
