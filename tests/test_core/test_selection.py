"""Behaviour tests for core/selection find_similar_entities.

Focus on what each SimilarityMode actually selects, and on edge cases
where a mode can't apply (e.g. diameter matching on a line) or the
seed has been deleted.
"""
from __future__ import annotations

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.project import Project
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.core.selection import (
    SimilarityMode,
    entity_kind,
    find_similar_entities,
    full_circle_radius,
)


def _circle(radius: float, center: tuple[float, float] = (0.0, 0.0)) -> GeometryEntity:
    arc = ArcSegment(
        center=center, radius=radius, start_angle_deg=0.0, sweep_deg=360.0,
    )
    return GeometryEntity(segments=[arc], closed=True)


def _line(start: tuple[float, float], end: tuple[float, float]) -> GeometryEntity:
    return GeometryEntity(
        segments=[LineSegment(start=start, end=end)], closed=False,
    )


def _point(xy: tuple[float, float]) -> GeometryEntity:
    return GeometryEntity(point=xy)


def _rect(w: float = 10.0, h: float = 5.0) -> GeometryEntity:
    return GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(w, 0)),
            LineSegment(start=(w, 0), end=(w, h)),
            LineSegment(start=(w, h), end=(0, h)),
            LineSegment(start=(0, h), end=(0, 0)),
        ],
        closed=True,
    )


def _project_with(*layers: GeometryLayer) -> Project:
    return Project(geometry_layers=list(layers))


# -------------------------------------------------------- SAME_LAYER


def test_same_layer_returns_every_entity_on_that_layer() -> None:
    c1 = _circle(3.0)
    c2 = _circle(6.0)
    line = _line((0, 0), (10, 0))
    other_c = _circle(3.0)
    layer_a = GeometryLayer(name="A", entities=[c1, c2, line])
    layer_b = GeometryLayer(name="B", entities=[other_c])
    project = _project_with(layer_a, layer_b)

    matches = find_similar_entities(
        "A", c1.id, project, SimilarityMode.SAME_LAYER
    )

    assert set(matches) == {("A", c1.id), ("A", c2.id), ("A", line.id)}


def test_same_layer_excludes_other_layers_even_if_identical() -> None:
    c1 = _circle(3.0)
    c_on_other_layer = _circle(3.0)
    project = _project_with(
        GeometryLayer(name="A", entities=[c1]),
        GeometryLayer(name="B", entities=[c_on_other_layer]),
    )
    matches = find_similar_entities(
        "A", c1.id, project, SimilarityMode.SAME_LAYER
    )
    assert matches == [("A", c1.id)]


# --------------------------------------------------------- SAME_TYPE


def test_same_type_groups_points_circles_closed_open_separately() -> None:
    p = _point((0, 0))
    c = _circle(3.0)
    closed_rect = _rect()
    open_line = _line((0, 0), (10, 0))
    other_circle = _circle(6.0)
    project = _project_with(
        GeometryLayer(name="L", entities=[p, c, closed_rect, open_line, other_circle]),
    )

    by_point = find_similar_entities(
        "L", p.id, project, SimilarityMode.SAME_TYPE
    )
    by_circle = find_similar_entities(
        "L", c.id, project, SimilarityMode.SAME_TYPE
    )
    by_closed = find_similar_entities(
        "L", closed_rect.id, project, SimilarityMode.SAME_TYPE
    )
    by_open = find_similar_entities(
        "L", open_line.id, project, SimilarityMode.SAME_TYPE
    )

    assert by_point == [("L", p.id)]
    assert set(by_circle) == {("L", c.id), ("L", other_circle.id)}
    assert by_closed == [("L", closed_rect.id)]
    assert by_open == [("L", open_line.id)]


def test_same_type_crosses_layer_boundaries() -> None:
    c1 = _circle(3.0)
    c2 = _circle(6.0)
    project = _project_with(
        GeometryLayer(name="Holes", entities=[c1]),
        GeometryLayer(name="Vents", entities=[c2]),
    )
    matches = find_similar_entities(
        "Holes", c1.id, project, SimilarityMode.SAME_TYPE
    )
    assert set(matches) == {("Holes", c1.id), ("Vents", c2.id)}


# ------------------------------------------------------ SAME_DIAMETER


def test_same_diameter_matches_circles_within_tolerance() -> None:
    c_3mm_a = _circle(3.0)
    c_3mm_b = _circle(3.003)  # within 0.01 mm tolerance
    c_6mm = _circle(6.0)
    c_off = _circle(3.02)  # outside tolerance
    project = _project_with(
        GeometryLayer(
            name="L", entities=[c_3mm_a, c_3mm_b, c_6mm, c_off]
        ),
    )
    matches = find_similar_entities(
        "L", c_3mm_a.id, project, SimilarityMode.SAME_DIAMETER
    )
    assert set(matches) == {("L", c_3mm_a.id), ("L", c_3mm_b.id)}


def test_same_diameter_returns_empty_for_non_circle_seed() -> None:
    line = _line((0, 0), (10, 0))
    c = _circle(3.0)
    project = _project_with(
        GeometryLayer(name="L", entities=[line, c])
    )
    matches = find_similar_entities(
        "L", line.id, project, SimilarityMode.SAME_DIAMETER
    )
    assert matches == []


def test_same_diameter_ignores_non_circle_entities_of_same_numeric_value() -> None:
    """A rectangle with 'size' 3 isn't a circle with radius 3 — only
    full-circle arcs count as circles for diameter matching."""
    c = _circle(3.0)
    rect = _rect(w=6.0, h=6.0)  # bounding size happens to be 6 (= 2×3)
    project = _project_with(
        GeometryLayer(name="L", entities=[c, rect])
    )
    matches = find_similar_entities(
        "L", c.id, project, SimilarityMode.SAME_DIAMETER
    )
    assert matches == [("L", c.id)]


def test_same_diameter_crosses_layer_boundaries() -> None:
    c1 = _circle(3.0)
    c2 = _circle(3.0)
    project = _project_with(
        GeometryLayer(name="A", entities=[c1]),
        GeometryLayer(name="B", entities=[c2]),
    )
    matches = find_similar_entities(
        "A", c1.id, project, SimilarityMode.SAME_DIAMETER
    )
    assert set(matches) == {("A", c1.id), ("B", c2.id)}


# ------------------------------------------------------------ edge cases


def test_seed_not_found_returns_empty() -> None:
    project = _project_with(
        GeometryLayer(name="L", entities=[_circle(3.0)])
    )
    matches = find_similar_entities(
        "L", "not-an-id", project, SimilarityMode.SAME_TYPE
    )
    assert matches == []


def test_seed_on_missing_layer_returns_empty() -> None:
    project = _project_with(
        GeometryLayer(name="L", entities=[_circle(3.0)])
    )
    matches = find_similar_entities(
        "NoSuchLayer", "x", project, SimilarityMode.SAME_TYPE
    )
    assert matches == []


def test_seed_itself_is_included_in_result() -> None:
    """Lets callers replace the viewport selection wholesale without
    having to re-add the seed — matches AutoCAD/KiCad behaviour."""
    c = _circle(3.0)
    project = _project_with(GeometryLayer(name="L", entities=[c]))
    matches = find_similar_entities(
        "L", c.id, project, SimilarityMode.SAME_TYPE
    )
    assert ("L", c.id) in matches


# --------------------------------------------------- helpers exposed for UI


def test_entity_kind_labels() -> None:
    assert entity_kind(_point((0, 0))) == "point"
    assert entity_kind(_circle(3.0)) == "circle"
    assert entity_kind(_rect()) == "closed_contour"
    assert entity_kind(_line((0, 0), (1, 0))) == "open_contour"


def test_full_circle_radius_positive_cases() -> None:
    assert full_circle_radius(_circle(3.0)) == pytest.approx(3.0)


def test_full_circle_radius_rejects_non_circle_shapes() -> None:
    assert full_circle_radius(_rect()) is None
    assert full_circle_radius(_line((0, 0), (1, 0))) is None
    assert full_circle_radius(_point((0, 0))) is None


def test_full_circle_radius_rejects_partial_arc() -> None:
    """A 180° arc is NOT a circle — it's an open arc the user probably
    wants to select as "closed contour" via containing entity rather
    than as a diameter match."""
    arc = ArcSegment(
        center=(0, 0), radius=3.0, start_angle_deg=0.0, sweep_deg=180.0,
    )
    entity = GeometryEntity(segments=[arc], closed=False)
    assert full_circle_radius(entity) is None
