"""Tests for the directional box-select logic.

Pure tests — no UI, no Qt — so they describe the contract the viewport
relies on without coupling to widget mechanics.
"""
from __future__ import annotations

import pytest

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.ui.box_selection import (
    BoxMode,
    SelectionCombine,
    combine_selection,
    direction_from_drag,
    select_in_box,
)


def _line_layer(name: str = "L") -> tuple[GeometryLayer, GeometryEntity, GeometryEntity]:
    inside = GeometryEntity(
        segments=[
            LineSegment(start=(2, 2), end=(8, 2)),
            LineSegment(start=(8, 2), end=(8, 8)),
            LineSegment(start=(8, 8), end=(2, 8)),
            LineSegment(start=(2, 8), end=(2, 2)),
        ],
        closed=True,
    )
    crossing = GeometryEntity(
        segments=[LineSegment(start=(-5, 5), end=(15, 5))],
    )
    layer = GeometryLayer(name=name, entities=[inside, crossing])
    return layer, inside, crossing


def test_direction_from_drag_distinguishes_modes() -> None:
    assert direction_from_drag(0, 100) is BoxMode.CONTAINED
    assert direction_from_drag(100, 0) is BoxMode.CROSSING
    # Equal x — treat as contained (degenerate case).
    assert direction_from_drag(50, 50) is BoxMode.CONTAINED


def test_contained_mode_only_picks_entities_fully_inside() -> None:
    layer, inside, crossing = _line_layer()
    picked = select_in_box([layer], (0, 0, 10, 10), BoxMode.CONTAINED)
    assert picked == [(layer.name, inside.id)]


def test_crossing_mode_picks_anything_that_touches() -> None:
    layer, inside, crossing = _line_layer()
    picked = select_in_box([layer], (0, 0, 10, 10), BoxMode.CROSSING)
    ids = {p[1] for p in picked}
    assert ids == {inside.id, crossing.id}


def test_box_outside_all_geometry_returns_nothing() -> None:
    layer, _, _ = _line_layer()
    picked = select_in_box([layer], (100, 100, 110, 110), BoxMode.CROSSING)
    assert picked == []


def test_inverted_box_is_normalised() -> None:
    layer, inside, _ = _line_layer()
    # Caller passed max-then-min — we should still find the contained square.
    picked = select_in_box([layer], (10, 10, 0, 0), BoxMode.CONTAINED)
    assert picked == [(layer.name, inside.id)]


def test_arc_entity_contained_check_uses_full_geometry() -> None:
    arc_entity = GeometryEntity(
        segments=[ArcSegment(center=(0, 0), radius=5, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    layer = GeometryLayer(name="A", entities=[arc_entity])
    # Box big enough to contain the disk → contained match.
    assert select_in_box([layer], (-6, -6, 6, 6), BoxMode.CONTAINED) == [
        ("A", arc_entity.id)
    ]
    # Box that clips the disk → only crossing match.
    assert select_in_box([layer], (-2, -2, 2, 2), BoxMode.CONTAINED) == []
    assert select_in_box([layer], (-2, -2, 2, 2), BoxMode.CROSSING) == [
        ("A", arc_entity.id)
    ]


def test_point_entity_contained_check_uses_box_bounds() -> None:
    point_inside = GeometryEntity(point=(5.0, 5.0))
    point_outside = GeometryEntity(point=(50.0, 50.0))
    layer = GeometryLayer(name="P", entities=[point_inside, point_outside])
    picked = select_in_box([layer], (0, 0, 10, 10), BoxMode.CONTAINED)
    assert picked == [("P", point_inside.id)]


def test_invisible_layer_is_skipped() -> None:
    layer, inside, _ = _line_layer()
    layer.visible = False
    picked = select_in_box([layer], (-100, -100, 100, 100), BoxMode.CROSSING)
    assert picked == []


@pytest.mark.parametrize("mode", [BoxMode.CONTAINED, BoxMode.CROSSING])
def test_empty_layer_list_is_safe(mode: BoxMode) -> None:
    assert select_in_box([], (0, 0, 1, 1), mode) == []


# ---------------------------------------------------------- combine_selection

def test_replace_drops_current() -> None:
    out = combine_selection(
        [("L", "a"), ("L", "b")], [("L", "c")], SelectionCombine.REPLACE
    )
    assert out == [("L", "c")]


def test_add_unions_preserving_order() -> None:
    out = combine_selection(
        [("L", "a"), ("L", "b")],
        [("L", "b"), ("L", "c")],
        SelectionCombine.ADD,
    )
    assert out == [("L", "a"), ("L", "b"), ("L", "c")]


def test_toggle_xors_current_and_picked() -> None:
    out = combine_selection(
        [("L", "a"), ("L", "b")],
        [("L", "b"), ("L", "c")],
        SelectionCombine.TOGGLE,
    )
    # b was in both → drops out; a stays; c is new → added.
    assert out == [("L", "a"), ("L", "c")]


def test_toggle_with_empty_pick_is_noop() -> None:
    out = combine_selection(
        [("L", "a")], [], SelectionCombine.TOGGLE
    )
    assert out == [("L", "a")]


def test_combine_does_not_duplicate_existing_entries() -> None:
    out = combine_selection(
        [("L", "a"), ("L", "b")], [("L", "a")], SelectionCombine.ADD
    )
    assert out == [("L", "a"), ("L", "b")]
