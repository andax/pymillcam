"""Generic visit-order optimizer.

Many CAM problems reduce to "given a set of things to cut, each with an
entry point and an exit point, in what order should we cut them to
minimise rapid travel?". Drill holes, pocket regions, contour starts —
all the same shape of problem. This module solves it once.

API
---
* ``VisitItem`` — a single cuttable item with ``entry`` (where the cut
  starts) and ``exit`` (where the cut ends). For drill points the two
  coincide.
* ``order_nearest_neighbour(items, start)`` — greedy seed: at each step
  pick the unvisited item whose entry is closest to the current
  position. O(n^2). Deterministic.
* ``two_opt(items, order, start, *, assume_symmetric)`` — greedy 2-opt
  polish: repeatedly reverse a sub-sequence if doing so shortens the
  tour. Stops when a full pass finds no improvement. With
  ``assume_symmetric=True`` (the default), uses an O(1) 4-edge local
  check correct only when ``entry == exit`` (e.g. drill points). With
  ``assume_symmetric=False``, recomputes the full tour distance per
  candidate swap — slower (O(n) per swap), correct for asymmetric
  items (pocket regions, open contours).
* ``optimize_visit_order(items, start, *, assume_symmetric)`` —
  convenience wrapper: NN seed + 2-opt polish.
* ``total_rapid_distance(items, order, start)`` — sum of rapid
  distances along the tour. Test helper and tie-breaker.

The optimizer never mutates inputs; it returns a permutation of indices
into ``items``. Callers map the indices back to their own data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]

# Distances below this are treated as ties — guards against
# floating-point churn falsely "improving" a tour by 1e-15 forever.
_IMPROVEMENT_EPS = 1e-9


@dataclass(frozen=True)
class VisitItem:
    """A single cuttable item the optimizer can order.

    For drill points, ``entry == exit``. For pocket regions and open
    contours, they differ.
    """
    entry: Point
    exit: Point


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def total_rapid_distance(
    items: list[VisitItem], order: list[int], start: Point
) -> float:
    """Sum of rapid distances along the tour.

    For each item, adds the distance from the previous exit (or
    ``start`` for the first) to the item's entry. Travel *during* the
    cut is the engine's concern — we only price the rapids between
    items.
    """
    total = 0.0
    cur = start
    for idx in order:
        total += _dist(cur, items[idx].entry)
        cur = items[idx].exit
    return total


def order_nearest_neighbour(
    items: list[VisitItem], start: Point
) -> list[int]:
    """Greedy: at each step pick the unvisited item nearest to the
    current position. O(n^2), deterministic."""
    n = len(items)
    if n == 0:
        return []
    remaining = set(range(n))
    order: list[int] = []
    cur = start
    while remaining:
        best = min(remaining, key=lambda i: _dist(cur, items[i].entry))
        order.append(best)
        cur = items[best].exit
        remaining.remove(best)
    return order


def two_opt(
    items: list[VisitItem],
    order: list[int],
    start: Point,
    *,
    max_passes: int = 20,
    assume_symmetric: bool = True,
) -> list[int]:
    """Greedy 2-opt polish on an existing order.

    Within each pass, scans every (i, j) sub-sequence and reverses it
    if doing so shortens the tour. Stops early when a full pass finds
    no improvement; otherwise caps at ``max_passes`` (rarely reached
    for sane inputs).

    With ``assume_symmetric=True`` (default), uses an O(1) 4-edge
    local check — correct only when ``entry == exit`` for every item
    (drill points). With ``assume_symmetric=False``, computes the full
    tour distance for each candidate swap, which is O(n) per swap but
    correct for asymmetric items (pocket regions, open contours).
    """
    n = len(items)
    if n < 3:
        return list(order)
    result = list(order)
    if assume_symmetric:
        for _ in range(max_passes):
            improved = False
            for i in range(n - 1):
                for j in range(i + 1, n):
                    if _two_opt_swap_improves(items, result, start, i, j):
                        result[i:j + 1] = reversed(result[i:j + 1])
                        improved = True
            if not improved:
                break
        return result
    # Asymmetric path: compare full tour distances at each candidate
    # swap. For the small N typical of pocket region ordering (< 50)
    # the O(n^3) per pass is trivial.
    current_dist = total_rapid_distance(items, result, start)
    for _ in range(max_passes):
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                candidate = result[:i] + list(reversed(result[i:j + 1])) + result[j + 1:]
                cand_dist = total_rapid_distance(items, candidate, start)
                if cand_dist < current_dist - _IMPROVEMENT_EPS:
                    result = candidate
                    current_dist = cand_dist
                    improved = True
        if not improved:
            break
    return result


def _two_opt_swap_improves(
    items: list[VisitItem],
    order: list[int],
    start: Point,
    i: int,
    j: int,
) -> bool:
    """Local 4-edge check: would reversing order[i:j+1] shorten the tour?

    Only the entry into the slice (prev_exit -> items[order[i]].entry)
    and the exit from it (items[order[j]].exit -> next_entry) change
    when items are symmetric. We compare those four edges directly,
    O(1) per call.
    """
    n = len(order)
    prev_exit = start if i == 0 else items[order[i - 1]].exit
    has_next = j < n - 1
    next_entry = items[order[j + 1]].entry if has_next else None

    old_left = _dist(prev_exit, items[order[i]].entry)
    new_left = _dist(prev_exit, items[order[j]].entry)
    if next_entry is not None:
        old_right = _dist(items[order[j]].exit, next_entry)
        new_right = _dist(items[order[i]].exit, next_entry)
    else:
        old_right = 0.0
        new_right = 0.0
    return (new_left + new_right) < (old_left + old_right) - _IMPROVEMENT_EPS


def optimize_visit_order(
    items: list[VisitItem],
    start: Point,
    *,
    polish: bool = True,
    assume_symmetric: bool = True,
) -> list[int]:
    """Nearest-neighbour seed + optional 2-opt polish.

    Use this as the default entry point. Set
    ``assume_symmetric=False`` for asymmetric items (open contours,
    pocket regions with distinct entry/exit). Pass ``polish=False`` to
    skip 2-opt entirely.
    """
    order = order_nearest_neighbour(items, start)
    if polish:
        order = two_opt(
            items, order, start, assume_symmetric=assume_symmetric
        )
    return order


def points_to_items(points: list[Point]) -> list[VisitItem]:
    """Convenience: wrap a flat list of points as symmetric VisitItems.

    Useful for drill, where each hole is a single XY coordinate with no
    distinction between entry and exit.
    """
    return [VisitItem(entry=p, exit=p) for p in points]
