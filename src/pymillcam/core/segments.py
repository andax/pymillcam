"""Segment-level 2D geometry primitives.

A `Segment` is either a straight line or a circular arc. Segments chain
end-to-end to form contours (polylines with arc support). Arcs are stored
analytically — center, radius, start angle, signed sweep — so they survive
DXF → toolpath → G-code without being collapsed into chord approximations.

`segments_to_shapely` is the one place that converts an arc-aware
representation into a chord-approximated Shapely geometry. Every Shapely-
facing operation should route through it so tolerance is controlled in a
single spot.
"""
from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

# Default chord-sag tolerance (mm) used when building the Shapely shadow for
# queries (contains, distance, etc.). Machining-output tolerance is a
# separate, user-facing setting that cascades via Project/Operation.
DEFAULT_SHADOW_TOLERANCE_MM = 0.01

# Angular "is-full-circle" epsilon, in degrees.
FULL_CIRCLE_EPSILON_DEG = 1e-9


class LineSegment(BaseModel):
    """Straight-line segment between two 2D points."""
    type: Literal["line"] = "line"
    start: tuple[float, float]
    end: tuple[float, float]

    @property
    def length(self) -> float:
        sx, sy = self.start
        ex, ey = self.end
        return math.hypot(ex - sx, ey - sy)


class ArcSegment(BaseModel):
    """Circular arc segment.

    `sweep_deg` carries both direction and extent: positive is CCW, negative
    is CW, and `abs(sweep_deg) == 360` represents a full circle. `start` and
    `end` are derived — for a full circle they coincide.
    """
    type: Literal["arc"] = "arc"
    center: tuple[float, float]
    radius: float
    start_angle_deg: float
    sweep_deg: float

    @property
    def ccw(self) -> bool:
        return self.sweep_deg > 0

    @property
    def is_full_circle(self) -> bool:
        return abs(abs(self.sweep_deg) - 360.0) < FULL_CIRCLE_EPSILON_DEG

    @property
    def end_angle_deg(self) -> float:
        return self.start_angle_deg + self.sweep_deg

    @property
    def start(self) -> tuple[float, float]:
        return _polar(self.center, self.radius, self.start_angle_deg)

    @property
    def end(self) -> tuple[float, float]:
        return _polar(self.center, self.radius, self.end_angle_deg)

    @property
    def length(self) -> float:
        return abs(math.radians(self.sweep_deg)) * self.radius


Segment = Annotated[LineSegment | ArcSegment, Field(discriminator="type")]


def segments_to_shapely(
    segments: list[LineSegment | ArcSegment],
    *,
    closed: bool,
    tolerance: float = DEFAULT_SHADOW_TOLERANCE_MM,
) -> BaseGeometry:
    """Discretize a chain of segments into a Shapely LineString or Polygon.

    `tolerance` is the maximum chord sag from the true arc, in mm — smaller
    means more vertices and a tighter fit. The chord between any two
    consecutive sampled points on an arc deviates from the true arc by at
    most this much.
    """
    if not segments:
        raise ValueError("segments_to_shapely requires at least one segment")
    if tolerance <= 0:
        raise ValueError(f"tolerance must be positive, got {tolerance}")

    points: list[tuple[float, float]] = [segments[0].start]
    for seg in segments:
        if isinstance(seg, LineSegment):
            points.append(seg.end)
        else:
            # Arc: sample beyond the shared start point already in `points`.
            points.extend(_sample_arc(seg, tolerance)[1:])

    if closed:
        if points[0] != points[-1]:
            points.append(points[0])
        return Polygon(points)
    return LineString(points)


def _polar(center: tuple[float, float], radius: float, angle_deg: float) -> tuple[float, float]:
    cx, cy = center
    theta = math.radians(angle_deg)
    return (cx + radius * math.cos(theta), cy + radius * math.sin(theta))


def _sample_arc(arc: ArcSegment, tolerance: float) -> list[tuple[float, float]]:
    """Sample an arc so each chord's sag from the true curve is ≤ tolerance."""
    if arc.radius <= 0 or arc.sweep_deg == 0:
        return [arc.start, arc.end]

    # Chord sag h = r·(1 − cos(Δθ/2)) ≤ tolerance  →  Δθ ≤ 2·acos(1 − t/r).
    # If tolerance ≥ radius, a single chord is already within spec.
    ratio = 1.0 - tolerance / arc.radius
    if ratio <= -1.0:
        return [arc.start, arc.end]
    theta_max = 2.0 * math.acos(max(ratio, -1.0))
    if theta_max <= 0.0:
        return [arc.start, arc.end]

    sweep_rad = math.radians(arc.sweep_deg)
    n = max(1, math.ceil(abs(sweep_rad) / theta_max))
    step = sweep_rad / n
    cx, cy = arc.center
    start_rad = math.radians(arc.start_angle_deg)
    return [
        (cx + arc.radius * math.cos(start_rad + step * i),
         cy + arc.radius * math.sin(start_rad + step * i))
        for i in range(n + 1)
    ]
