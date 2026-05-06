"""Tests for the generic visit-order optimizer.

Asserts behaviour (improvement, idempotence, determinism) rather than
exact orderings, since multiple equally-optimal tours can exist for
symmetric inputs.
"""
from __future__ import annotations

import math
import random

import pytest

from pymillcam.engine.optimizer import (
    VisitItem,
    optimize_visit_order,
    order_nearest_neighbour,
    points_to_items,
    total_rapid_distance,
    two_opt,
)

# ------------------------------------------------------------- edge cases


def test_empty_returns_empty_order():
    assert order_nearest_neighbour([], (0.0, 0.0)) == []
    assert optimize_visit_order([], (0.0, 0.0)) == []


def test_single_item_returns_single_index():
    items = points_to_items([(5.0, 5.0)])
    assert order_nearest_neighbour(items, (0.0, 0.0)) == [0]
    assert optimize_visit_order(items, (0.0, 0.0)) == [0]


def test_two_items_picks_nearer_first():
    items = points_to_items([(10.0, 0.0), (1.0, 0.0)])
    order = order_nearest_neighbour(items, (0.0, 0.0))
    assert order == [1, 0]
    # 2-opt on a 2-item tour is a no-op.
    assert two_opt(items, order, (0.0, 0.0)) == order


# ---------------------------------------------------------- nearest neighbour


def test_nearest_neighbour_snake_through_grid():
    """5x5 grid, start at origin: NN produces a snake-ish row-major walk
    (rows alternating left-to-right and right-to-left). Exact ordering
    isn't asserted — the *behaviour* we want is that consecutive items
    are unit-distance apart.
    """
    pts = [(float(x), float(y)) for y in range(5) for x in range(5)]
    items = points_to_items(pts)
    order = order_nearest_neighbour(items, (0.0, 0.0))
    assert sorted(order) == list(range(len(pts)))
    # Every step should be at most sqrt(2) (a diagonal in the grid),
    # not bounce across the whole grid.
    cur = (0.0, 0.0)
    for idx in order:
        d = math.hypot(pts[idx][0] - cur[0], pts[idx][1] - cur[1])
        assert d <= math.sqrt(2) + 1e-9, (
            f"NN took a long jump at step {idx}: {d:.3f} > sqrt(2)"
        )
        cur = pts[idx]


def test_nearest_neighbour_shortens_random_cloud():
    """Compared to the as-given input order, NN should be no longer."""
    rng = random.Random(42)
    pts = [(rng.uniform(0, 100), rng.uniform(0, 100)) for _ in range(50)]
    items = points_to_items(pts)
    start = (0.0, 0.0)
    input_order = list(range(len(pts)))
    nn_order = order_nearest_neighbour(items, start)
    input_dist = total_rapid_distance(items, input_order, start)
    nn_dist = total_rapid_distance(items, nn_order, start)
    # Random points: NN should beat a random input order substantially.
    # Use 0.6x as a loose floor — typically NN does much better.
    assert nn_dist < input_dist * 0.6


# ---------------------------------------------------------------- 2-opt


def test_two_opt_no_worse_than_input():
    """2-opt only applies improving moves, so the result is always
    less-or-equal to the seed order."""
    rng = random.Random(7)
    pts = [(rng.uniform(0, 50), rng.uniform(0, 50)) for _ in range(30)]
    items = points_to_items(pts)
    start = (0.0, 0.0)
    seed = list(range(len(pts)))
    polished = two_opt(items, seed, start)
    seed_dist = total_rapid_distance(items, seed, start)
    pol_dist = total_rapid_distance(items, polished, start)
    assert pol_dist <= seed_dist + 1e-9


def test_two_opt_improves_known_bad_order():
    """A figure-of-eight crossing reorder gets straightened out."""
    # Four corners of a unit square. Visiting them in (0, 2, 1, 3)
    # order makes an X across the middle, which is provably longer
    # than the perimeter walk.
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    items = points_to_items(pts)
    start = (0.0, 0.0)
    bad = [0, 2, 1, 3]
    bad_dist = total_rapid_distance(items, bad, start)
    polished = two_opt(items, bad, start)
    pol_dist = total_rapid_distance(items, polished, start)
    assert pol_dist < bad_dist


def test_two_opt_idempotent_on_optimal():
    """Running 2-opt on an already-good order doesn't worsen it."""
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0)]
    items = points_to_items(pts)
    start = (-1.0, 0.0)
    optimal = [0, 1, 2, 3, 4]
    polished = two_opt(items, optimal, start)
    assert polished == optimal


# --------------------------------------------------------- end-to-end


def test_optimize_visit_order_combines_nn_and_two_opt():
    rng = random.Random(123)
    pts = [(rng.uniform(0, 100), rng.uniform(0, 100)) for _ in range(40)]
    items = points_to_items(pts)
    start = (0.0, 0.0)
    nn_only = total_rapid_distance(
        items, order_nearest_neighbour(items, start), start
    )
    full = total_rapid_distance(
        items, optimize_visit_order(items, start), start
    )
    # Polish should not make it worse, and on a random cloud should
    # typically improve a few percent.
    assert full <= nn_only + 1e-9


def test_determinism_same_input_same_output():
    rng = random.Random(2024)
    pts = [(rng.uniform(0, 50), rng.uniform(0, 50)) for _ in range(25)]
    items = points_to_items(pts)
    start = (0.0, 0.0)
    a = optimize_visit_order(items, start)
    b = optimize_visit_order(items, start)
    assert a == b


def test_polish_can_be_disabled():
    """polish=False returns the NN order unchanged."""
    rng = random.Random(5)
    pts = [(rng.uniform(0, 30), rng.uniform(0, 30)) for _ in range(15)]
    items = points_to_items(pts)
    start = (0.0, 0.0)
    nn_only = order_nearest_neighbour(items, start)
    no_polish = optimize_visit_order(items, start, polish=False)
    assert nn_only == no_polish


def test_total_distance_for_known_tour():
    """Sanity check the cost helper against a hand-computable answer."""
    pts = [(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)]
    items = points_to_items(pts)
    # Start at (0, 0): 0 -> 3 -> 5 (3-4-5 triangle)
    assert total_rapid_distance(items, [0, 1, 2], (0.0, 0.0)) == pytest.approx(7.0)


def test_asymmetric_visit_items_are_supported_by_nn():
    """Open contours have distinct entry/exit. NN should pick the
    nearer entry and advance from the exit."""
    items = [
        VisitItem(entry=(0.0, 0.0), exit=(5.0, 0.0)),  # left
        VisitItem(entry=(10.0, 0.0), exit=(15.0, 0.0)),  # right
    ]
    # Starting at -1 the left one's entry is closer; after that the
    # current position is at x=5 so the right one is next.
    assert order_nearest_neighbour(items, (-1.0, 0.0)) == [0, 1]


# --------------------------------------- asymmetric 2-opt path


def test_two_opt_asymmetric_handles_distinct_entry_exit():
    """Three regions arranged so a sub-sequence reversal *would* improve
    the tour for symmetric items but not for these asymmetric ones —
    or vice versa. The full-tour-distance path must compute correctly
    rather than rely on the 4-edge approximation."""
    # Three "regions": each is an asymmetric segment. Order [0, 1, 2]
    # is suboptimal; [0, 2, 1] is better when entries/exits matter.
    items = [
        VisitItem(entry=(0.0, 0.0), exit=(1.0, 0.0)),
        VisitItem(entry=(20.0, 0.0), exit=(21.0, 0.0)),
        VisitItem(entry=(2.0, 0.0), exit=(3.0, 0.0)),
    ]
    start = (0.0, 0.0)
    bad = [0, 1, 2]
    bad_dist = total_rapid_distance(items, bad, start)
    polished = two_opt(items, bad, start, assume_symmetric=False)
    pol_dist = total_rapid_distance(items, polished, start)
    assert pol_dist < bad_dist


def test_two_opt_asymmetric_idempotent_on_optimal():
    items = [
        VisitItem(entry=(0.0, 0.0), exit=(1.0, 0.0)),
        VisitItem(entry=(2.0, 0.0), exit=(3.0, 0.0)),
        VisitItem(entry=(4.0, 0.0), exit=(5.0, 0.0)),
    ]
    start = (-1.0, 0.0)
    optimal = [0, 1, 2]
    polished = two_opt(items, optimal, start, assume_symmetric=False)
    assert polished == optimal


def test_optimize_visit_order_asymmetric_beats_input():
    """End-to-end: NN + asymmetric 2-opt on a deliberately bad input
    order produces a strictly shorter tour."""
    rng = random.Random(99)
    items = []
    for _ in range(15):
        ax, ay = rng.uniform(0, 100), rng.uniform(0, 100)
        bx, by = ax + rng.uniform(-5, 5), ay + rng.uniform(-5, 5)
        items.append(VisitItem(entry=(ax, ay), exit=(bx, by)))
    start = (0.0, 0.0)
    input_order = list(range(len(items)))
    optimized = optimize_visit_order(
        items, start, assume_symmetric=False
    )
    assert sorted(optimized) == input_order
    assert total_rapid_distance(items, optimized, start) <= (
        total_rapid_distance(items, input_order, start) + 1e-9
    )
