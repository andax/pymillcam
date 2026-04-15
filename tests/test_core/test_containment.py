"""Tests for pymillcam.core.containment."""
from __future__ import annotations

from pymillcam.core.containment import (
    build_pocket_regions,
    find_contained_entities,
)
from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.segments import ArcSegment, LineSegment


def _square(cx: float, cy: float, half: float) -> GeometryEntity:
    return GeometryEntity(
        segments=[
            LineSegment(start=(cx - half, cy - half), end=(cx + half, cy - half)),
            LineSegment(start=(cx + half, cy - half), end=(cx + half, cy + half)),
            LineSegment(start=(cx + half, cy + half), end=(cx - half, cy + half)),
            LineSegment(start=(cx - half, cy + half), end=(cx - half, cy - half)),
        ],
        closed=True,
    )


def _circle(cx: float, cy: float, r: float) -> GeometryEntity:
    return GeometryEntity(
        segments=[
            ArcSegment(
                center=(cx, cy), radius=r,
                start_angle_deg=0.0, sweep_deg=360.0,
            )
        ],
        closed=True,
    )


def test_single_boundary_no_islands_returns_one_region() -> None:
    boundary = _square(0, 0, 25)
    regions = build_pocket_regions([boundary])
    assert len(regions) == 1
    assert regions[0][0] is boundary
    assert regions[0][1] == []


def test_boundary_with_one_island() -> None:
    boundary = _square(0, 0, 25)
    island = _circle(0, 0, 5)
    regions = build_pocket_regions([boundary, island])
    assert len(regions) == 1
    assert regions[0][0] is boundary
    assert regions[0][1] == [island]


def test_boundary_with_multiple_islands() -> None:
    boundary = _square(0, 0, 30)
    island_a = _circle(-15, 0, 3)
    island_b = _circle(15, 0, 3)
    island_c = _square(0, 15, 2)
    regions = build_pocket_regions([boundary, island_a, island_b, island_c])
    assert len(regions) == 1
    island_ids = {id(i) for i in regions[0][1]}
    assert island_ids == {id(island_a), id(island_b), id(island_c)}


def test_multiple_disjoint_pockets_each_with_own_islands() -> None:
    b1 = _square(0, 0, 20)
    i1 = _circle(0, 0, 3)
    b2 = _square(100, 0, 20)
    i2 = _circle(100, 0, 3)
    regions = build_pocket_regions([b1, i1, b2, i2])
    assert len(regions) == 2
    boundary_ids = {id(r[0]) for r in regions}
    assert boundary_ids == {id(b1), id(b2)}
    for boundary, islands in regions:
        if boundary is b1:
            assert islands == [i1]
        else:
            assert islands == [i2]


def test_nested_pockets_via_depth_alternation() -> None:
    # Outer boundary, island, then a smaller boundary inside the island.
    outer = _square(0, 0, 30)
    island = _square(0, 0, 15)
    nested = _square(0, 0, 5)
    regions = build_pocket_regions([outer, island, nested])
    assert len(regions) == 2
    # Outer boundary owns the island.
    outer_region = next(r for r in regions if r[0] is outer)
    assert outer_region[1] == [island]
    # Nested square is its own top-level boundary, no islands.
    nested_region = next(r for r in regions if r[0] is nested)
    assert nested_region[1] == []


def test_open_contours_skipped() -> None:
    open_chain = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))], closed=False,
    )
    closed = _square(0, 0, 25)
    regions = build_pocket_regions([open_chain, closed])
    assert len(regions) == 1
    assert regions[0][0] is closed


def test_smallest_containing_chosen_as_parent() -> None:
    # outer ⊃ middle ⊃ inner. Inner's parent should be middle, not outer.
    outer = _square(0, 0, 50)
    middle = _square(0, 0, 30)
    inner = _square(0, 0, 5)
    regions = build_pocket_regions([outer, middle, inner])
    # Outer (depth 0) → its island is middle (depth 1).
    # Middle is depth 1 → its child inner (depth 2) becomes a top-level
    # boundary in its own right.
    outer_region = next(r for r in regions if r[0] is outer)
    inner_region = next(r for r in regions if r[0] is inner)
    assert outer_region[1] == [middle]
    assert inner_region[1] == []


def test_find_contained_entities_returns_inside_only() -> None:
    boundary = _square(0, 0, 25)
    inside = _circle(0, 0, 5)
    outside = _circle(100, 0, 5)
    candidates = [boundary, inside, outside]
    contained = find_contained_entities(boundary, candidates)
    assert contained == [inside]


def test_find_contained_entities_skips_open() -> None:
    boundary = _square(0, 0, 25)
    open_chain = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(10, 0))], closed=False,
    )
    contained = find_contained_entities(boundary, [open_chain])
    assert contained == []
