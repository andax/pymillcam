"""Weld open contour entities whose endpoints meet into single contours.

Used in two places:
- Optional pass after DXF import (controlled by `AppPreferences.auto_stitch_on_import`).
- The Operations > Join Paths UI action (operating on the user's selection).

Pure function, no UI / no I/O. Conservative on ambiguity: if a vertex has
more than one stitchable neighbour, leave that connection alone — the
user can split the selection and try again.
"""
from __future__ import annotations

import math
from typing import Literal

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.segments import ArcSegment, LineSegment, Segment

_End = Literal["start", "end"]


def stitch_entities(
    entities: list[GeometryEntity], tolerance: float
) -> list[GeometryEntity]:
    """Return a new list where contours sharing endpoints have been merged.

    Closed and point entities pass through unchanged. Open contour entities
    whose endpoints meet within `tolerance` (mm) are joined; segment
    direction is reversed where needed so each chain reads start→end.
    A chain that closes back on itself is marked `closed=True`.
    """
    if tolerance <= 0:
        raise ValueError(f"tolerance must be positive, got {tolerance}")

    passthrough: list[GeometryEntity] = []
    open_entities: list[GeometryEntity] = []
    for entity in entities:
        if entity.point is not None or entity.closed or not entity.segments:
            passthrough.append(entity)
        else:
            open_entities.append(entity)

    if not open_entities:
        return list(entities)

    used: set[int] = set()
    stitched: list[GeometryEntity] = []
    for seed in range(len(open_entities)):
        if seed in used:
            continue
        chain = list(open_entities[seed].segments)
        used.add(seed)

        while True:
            match = _find_unique_match(
                open_entities, used, chain[-1].end, tolerance
            )
            if match is None:
                break
            idx, end = match
            other = list(open_entities[idx].segments)
            if end == "end":
                other = _reverse_segments(other)
            chain.extend(other)
            used.add(idx)

        while True:
            match = _find_unique_match(
                open_entities, used, chain[0].start, tolerance
            )
            if match is None:
                break
            idx, end = match
            other = list(open_entities[idx].segments)
            if end == "start":
                other = _reverse_segments(other)
            chain = other + chain
            used.add(idx)

        is_closed = _within_tol(chain[0].start, chain[-1].end, tolerance)
        if is_closed:
            chain = _snap_closure(chain)

        # Don't carry the seed entity's `dxf_entity_type` (e.g. "line") onto a
        # stitched chain — the composite isn't a DXF LINE any more. "path"
        # matches the user-facing action name ("Join paths") and reads cleanly
        # in the layers tree whether the chain ended up open or closed.
        if len(chain) == len(open_entities[seed].segments):
            # Single entity, no stitching happened — preserve original label.
            entity_type = open_entities[seed].dxf_entity_type
        else:
            entity_type = "path"
        stitched.append(
            GeometryEntity(
                segments=chain,
                closed=is_closed,
                source=open_entities[seed].source,
                dxf_entity_type=entity_type,
            )
        )

    return passthrough + stitched


def _find_unique_match(
    entities: list[GeometryEntity],
    used: set[int],
    point: tuple[float, float],
    tolerance: float,
) -> tuple[int, _End] | None:
    """Find the single unused entity whose start or end touches `point`.

    Counts ALL entity endpoints at the point (used or not) so that a
    Y/X-junction stays unstitched even after one of its legs is in
    another chain.
    """
    legs_at_point = 0
    found: tuple[int, _End] | None = None
    for i, entity in enumerate(entities):
        starts = _within_tol(entity.segments[0].start, point, tolerance)
        ends = _within_tol(entity.segments[-1].end, point, tolerance)
        if starts:
            legs_at_point += 1
        if ends:
            legs_at_point += 1
        if i in used:
            continue
        if (starts or ends) and found is None:
            found = (i, "start" if starts else "end")
    # The current chain owns one of the legs (start or end of the seed
    # entity at this point). A clean continuation has exactly two legs
    # (mine + one neighbour). Three or more = junction → bail.
    if legs_at_point > 2:
        return None
    return found


def _within_tol(
    a: tuple[float, float], b: tuple[float, float], tolerance: float
) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance


def _reverse_segments(segments: list[Segment]) -> list[Segment]:
    return [_reverse_segment(s) for s in reversed(segments)]


def _reverse_segment(seg: Segment) -> Segment:
    if isinstance(seg, LineSegment):
        return LineSegment(start=seg.end, end=seg.start)
    # Arc: walk it the other way — same circle, sweep negated, new start
    # is the old end.
    return ArcSegment(
        center=seg.center,
        radius=seg.radius,
        start_angle_deg=seg.start_angle_deg + seg.sweep_deg,
        sweep_deg=-seg.sweep_deg,
    )


def _snap_closure(chain: list[Segment]) -> list[Segment]:
    """Replace the last segment's end with the chain's exact start point."""
    last = chain[-1]
    target = chain[0].start
    if isinstance(last, LineSegment):
        return chain[:-1] + [LineSegment(start=last.start, end=target)]
    # For arcs we leave precision to the source — see segments.py for the
    # full-circle floating-point hazard and how Polygon.buffer handles it.
    return chain
