"""Arc-aware offset of a closed contour.

Computes the parallel offset of a chain of `LineSegment`/`ArcSegment` while
preserving arcs (the long-standing `Polygon.buffer` fallback collapsed them
to chords). Three building blocks:

1. Offset each segment in isolation:
   - Lines shift along the right-of-travel normal by the signed distance.
   - Arcs change radius — outward for CCW arcs, inward for CW — keeping
     the same centre, start angle, and sweep.
2. Join consecutive offset segments at the original vertex:
   - **Convex** (offset side gains a gap): fill with a tangent arc of
     radius `|distance|` centred on the original vertex.
   - **Concave** (offset side overlaps): trim both segments to their
     line-line intersection.
3. Tangent joins (segments collinear or smoothly meeting) fall through
   without modification.

What the MVP doesn't cover (raises `OffsetError`):
- Non-tangent line↔arc transitions in concave joins (would need
  line-arc / arc-arc intersection).
- Self-intersecting offsets (inside offset larger than the smallest
  feature).
- Multi-loop or polygon-with-holes contours.

Callers should fall back to a chord-based offset (e.g. `Polygon.buffer`)
when this raises so nothing regresses.
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon

from pymillcam.core.segments import (
    DEFAULT_SHADOW_TOLERANCE_MM,
    ArcSegment,
    LineSegment,
    Segment,
    reverse_segment_chain,
    segments_to_shapely,
)

# Vector-magnitude epsilon. Used to call segments collinear / endpoints
# coincident. Tighter than chord-tolerance defaults — these checks are
# about analytical equality, not machining accuracy.
_EPS = 1e-9


class OffsetError(Exception):
    """Raised when the analytical offsetter cannot produce a valid result."""


def offset_closed_contour(
    segments: list[Segment], distance: float, *, outside: bool
) -> list[Segment]:
    """Offset a closed segment chain by `distance` to the chosen side.

    Returns a new chain of segments preserving arcs. The input chain may
    be CW or CCW — the offsetter normalises to CCW internally, then maps
    `outside=True` to "right of CCW travel" and `outside=False` to "left".
    """
    if not segments:
        raise OffsetError("Cannot offset an empty contour")
    if distance <= 0:
        raise OffsetError(f"distance must be positive, got {distance}")

    if _is_clockwise(segments):
        segments = reverse_segment_chain(list(segments))

    d_signed = distance if outside else -distance

    if len(segments) == 1 and isinstance(segments[0], ArcSegment) and segments[0].is_full_circle:
        return [_offset_full_circle(segments[0], d_signed)]

    n = len(segments)
    offset_segs = [_offset_segment(s, d_signed) for s in segments]

    # One join per vertex i (between segments[(i-1) % n] and segments[i]).
    # Each join stashes the modifications it needs to make on the two
    # adjacent offset segments plus any filler arcs.
    joins = [
        _compute_join_at_vertex(
            offset_segs[(i - 1) % n],
            offset_segs[i],
            segments[i].start,
            _tangent_at_end(segments[(i - 1) % n]),
            _tangent_at_start(segments[i]),
            d_signed,
        )
        for i in range(n)
    ]

    result: list[Segment] = []
    for i in range(n):
        seg = offset_segs[i]
        join_in = joins[i]
        join_out = joins[(i + 1) % n]
        seg = _retarget(seg, new_start=join_in.new_cur_start, new_end=join_out.new_prev_end)
        result.append(seg)
        result.extend(join_out.fillers)

    _validate_result_is_simple(result, segments, outside=outside)
    return result


def _validate_result_is_simple(
    segments: list[Segment],
    original_segments: list[Segment],
    *,
    outside: bool,
) -> None:
    """Sanity-check the offset polygon.

    A line-line intersection trim can leave the result *looking* like a
    valid CCW polygon while actually being the wrong shape (for example,
    inside-offsetting a 50×30 rectangle by 50 mm produces a CCW
    rectangle that's *bigger* than the original — geometrically
    nonsense). Comparing areas catches both that and the simpler
    "flipped orientation" case.
    """
    try:
        shadow = segments_to_shapely(
            segments, closed=True, tolerance=DEFAULT_SHADOW_TOLERANCE_MM
        )
    except (ValueError, Exception) as exc:  # noqa: BLE001 — Shapely may raise broadly
        raise OffsetError(f"Offset produced invalid geometry: {exc}") from exc
    if not isinstance(shadow, Polygon) or shadow.is_empty:
        raise OffsetError("Offset produced an empty or non-polygonal result")
    if not shadow.exterior.is_ccw:
        raise OffsetError("Offset self-intersected (orientation flipped)")
    if shadow.area < _EPS:
        raise OffsetError("Offset collapsed to zero area — distance too large")

    original = segments_to_shapely(
        original_segments, closed=True, tolerance=DEFAULT_SHADOW_TOLERANCE_MM
    )
    if isinstance(original, Polygon):
        if outside and shadow.area < original.area - _EPS:
            raise OffsetError(
                "Outside offset produced a smaller polygon — likely self-intersection"
            )
        if not outside and shadow.area > original.area + _EPS:
            raise OffsetError(
                "Inside offset produced a larger polygon — likely self-intersection"
            )


# --------------------------------------------------------- orientation helpers


def _is_clockwise(segments: list[Segment]) -> bool:
    """True if the chord-approximated polygon is wound CW."""
    shadow = segments_to_shapely(
        segments, closed=True, tolerance=DEFAULT_SHADOW_TOLERANCE_MM
    )
    if not isinstance(shadow, Polygon):
        raise OffsetError(
            f"Expected a Polygon shadow for orientation check; got {shadow.geom_type}"
        )
    return not shadow.exterior.is_ccw


# -------------------------------------------------------- per-segment offsets


def _offset_segment(seg: Segment, d_signed: float) -> Segment:
    if isinstance(seg, LineSegment):
        return _offset_line(seg, d_signed)
    return _offset_arc(seg, d_signed)


def _offset_line(seg: LineSegment, d_signed: float) -> LineSegment:
    sx, sy = seg.start
    ex, ey = seg.end
    dx, dy = ex - sx, ey - sy
    length = math.hypot(dx, dy)
    if length < _EPS:
        raise OffsetError("Cannot offset a zero-length line segment")
    # Right normal of (dx, dy) is (dy, -dx) (rotate −90°).
    nx, ny = dy / length, -dx / length
    ox, oy = nx * d_signed, ny * d_signed
    return LineSegment(start=(sx + ox, sy + oy), end=(ex + ox, ey + oy))


def _offset_arc(seg: ArcSegment, d_signed: float) -> ArcSegment:
    sign = 1.0 if seg.sweep_deg > 0 else -1.0
    new_radius = seg.radius + d_signed * sign
    if new_radius <= _EPS:
        raise OffsetError(
            f"Offset by {d_signed:+.4f} mm shrinks an arc of radius "
            f"{seg.radius} mm to {new_radius:.4f} mm"
        )
    return ArcSegment(
        center=seg.center,
        radius=new_radius,
        start_angle_deg=seg.start_angle_deg,
        sweep_deg=seg.sweep_deg,
    )


def _offset_full_circle(seg: ArcSegment, d_signed: float) -> ArcSegment:
    return _offset_arc(seg, d_signed)


# --------------------------------------------------------- tangents at endpoints


def _tangent_at_end(seg: Segment) -> tuple[float, float]:
    if isinstance(seg, LineSegment):
        return _line_tangent(seg)
    return _arc_tangent(seg, at_end=True)


def _tangent_at_start(seg: Segment) -> tuple[float, float]:
    if isinstance(seg, LineSegment):
        return _line_tangent(seg)
    return _arc_tangent(seg, at_end=False)


def _line_tangent(seg: LineSegment) -> tuple[float, float]:
    sx, sy = seg.start
    ex, ey = seg.end
    dx, dy = ex - sx, ey - sy
    length = math.hypot(dx, dy)
    return (dx / length, dy / length)


def _arc_tangent(seg: ArcSegment, *, at_end: bool) -> tuple[float, float]:
    angle_deg = seg.start_angle_deg + (seg.sweep_deg if at_end else 0.0)
    rad = math.radians(angle_deg)
    sign = 1.0 if seg.sweep_deg > 0 else -1.0
    # CCW tangent at angle θ on a circle is (−sin θ, cos θ); CW is its negation.
    return (-sign * math.sin(rad), sign * math.cos(rad))


# ------------------------------------------------------------------ joins


class _JoinResult:
    """How a single vertex transition modifies the two adjacent offset segments."""

    __slots__ = ("new_prev_end", "new_cur_start", "fillers")

    def __init__(
        self,
        *,
        new_prev_end: tuple[float, float],
        new_cur_start: tuple[float, float],
        fillers: list[Segment],
    ) -> None:
        self.new_prev_end = new_prev_end
        self.new_cur_start = new_cur_start
        self.fillers = fillers


def _compute_join_at_vertex(
    prev_off: Segment,
    cur_off: Segment,
    original_vertex: tuple[float, float],
    prev_tan_out: tuple[float, float],
    cur_tan_in: tuple[float, float],
    d_signed: float,
) -> _JoinResult:
    cross = prev_tan_out[0] * cur_tan_in[1] - prev_tan_out[1] * cur_tan_in[0]
    dot = prev_tan_out[0] * cur_tan_in[0] + prev_tan_out[1] * cur_tan_in[1]

    if abs(cross) < _EPS:
        if dot < 0:
            raise OffsetError("U-turn at vertex — offset is degenerate")
        # Tangent (collinear or smooth meeting): no fix-up needed.
        return _JoinResult(
            new_prev_end=prev_off.end,
            new_cur_start=cur_off.start,
            fillers=[],
        )

    if cross * d_signed > 0:
        # Convex offset: fill the gap with a tangent arc on the original vertex.
        fill = _make_fill_arc(prev_off.end, cur_off.start, original_vertex, d_signed)
        return _JoinResult(
            new_prev_end=prev_off.end,
            new_cur_start=cur_off.start,
            fillers=[fill],
        )

    # Concave offset: extend both offset segments to their intersection.
    intersection = _intersect_offset_pair(prev_off, cur_off)
    if intersection is None:
        raise OffsetError(
            "Cannot resolve concave join — non-line segments not supported in MVP"
        )
    return _JoinResult(
        new_prev_end=intersection,
        new_cur_start=intersection,
        fillers=[],
    )


def _make_fill_arc(
    start_pt: tuple[float, float],
    end_pt: tuple[float, float],
    center: tuple[float, float],
    d_signed: float,
) -> ArcSegment:
    cx, cy = center
    sx, sy = start_pt
    ex, ey = end_pt
    radius = abs(d_signed)
    start_angle = math.degrees(math.atan2(sy - cy, sx - cx))
    end_angle = math.degrees(math.atan2(ey - cy, ex - cx))
    sweep = end_angle - start_angle
    if d_signed > 0:
        # CCW (positive) sweep, normalised to (0, 360).
        if sweep <= 0:
            sweep += 360.0
    else:
        if sweep >= 0:
            sweep -= 360.0
    return ArcSegment(
        center=center,
        radius=radius,
        start_angle_deg=start_angle,
        sweep_deg=sweep,
    )


def _intersect_offset_pair(
    prev_off: Segment, cur_off: Segment
) -> tuple[float, float] | None:
    if isinstance(prev_off, LineSegment) and isinstance(cur_off, LineSegment):
        return _line_line_intersection(prev_off, cur_off)
    return None


def _line_line_intersection(
    a: LineSegment, b: LineSegment
) -> tuple[float, float] | None:
    ax1, ay1 = a.start
    ax2, ay2 = a.end
    bx1, by1 = b.start
    bx2, by2 = b.end
    dax, day = ax2 - ax1, ay2 - ay1
    dbx, dby = bx2 - bx1, by2 - by1
    denom = dax * dby - day * dbx
    if abs(denom) < _EPS:
        return None  # parallel (already handled by tangent branch — defensive)
    t = ((bx1 - ax1) * dby - (by1 - ay1) * dbx) / denom
    return (ax1 + t * dax, ay1 + t * day)


# ------------------------------------------------------------------ retarget


def _retarget(
    seg: Segment,
    *,
    new_start: tuple[float, float],
    new_end: tuple[float, float],
) -> Segment:
    if isinstance(seg, LineSegment):
        return LineSegment(start=new_start, end=new_end)
    # Arcs: only no-op retargeting is supported. If the join modified the
    # endpoints, that means a non-tangent transition involving an arc, which
    # the MVP punts on.
    if not _approx(new_start, seg.start) or not _approx(new_end, seg.end):
        raise OffsetError(
            "Trimming an arc segment is not supported — non-tangent line↔arc "
            "transitions need a richer intersector"
        )
    return seg


def _approx(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) < _EPS
