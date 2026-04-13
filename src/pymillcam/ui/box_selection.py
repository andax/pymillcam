"""Directional box-selection logic.

Pure function — no UI, no Qt — so it's easily testable.

Two modes (AutoCAD convention):
- **Contained** (drag left → right, "window"): keep entities entirely
  inside the box.
- **Crossing** (drag right → left): keep entities that touch the box.
"""
from __future__ import annotations

from enum import StrEnum

from shapely.geometry import box as shapely_box
from shapely.geometry.base import BaseGeometry

from pymillcam.core.geometry import GeometryEntity, GeometryLayer


class BoxMode(StrEnum):
    CONTAINED = "contained"
    CROSSING = "crossing"


class SelectionCombine(StrEnum):
    """How a new pick should combine with the existing selection."""
    REPLACE = "replace"
    ADD = "add"
    TOGGLE = "toggle"


def combine_selection(
    current: list[tuple[str, str]],
    picked: list[tuple[str, str]],
    mode: SelectionCombine,
) -> list[tuple[str, str]]:
    """Apply a new pick to a current selection per `mode`.

    Order preserved: existing entries stay in place; new ones append at the
    end. Toggle mode removes anything in both lists (XOR semantics) — useful
    for Ctrl+click to deselect.
    """
    if mode is SelectionCombine.REPLACE:
        return list(picked)
    if mode is SelectionCombine.ADD:
        seen = set(current)
        out = list(current)
        for item in picked:
            if item not in seen:
                out.append(item)
                seen.add(item)
        return out
    # TOGGLE — XOR of current and picked.
    picked_set = set(picked)
    out = [c for c in current if c not in picked_set]
    out_set = set(out)
    current_set = set(current)
    for item in picked:
        if item not in current_set and item not in out_set:
            out.append(item)
            out_set.add(item)
    return out


def direction_from_drag(start_x: float, end_x: float) -> BoxMode:
    """L→R drag selects contained; R→L drag selects crossing."""
    return BoxMode.CONTAINED if end_x >= start_x else BoxMode.CROSSING


def select_in_box(
    layers: list[GeometryLayer],
    box: tuple[float, float, float, float],
    mode: BoxMode,
) -> list[tuple[str, str]]:
    """Return the (layer_name, entity_id) pairs matching `mode` within `box`.

    `box` is `(min_x, min_y, max_x, max_y)` in world (mm) coordinates.
    """
    min_x, min_y, max_x, max_y = box
    if min_x > max_x or min_y > max_y:
        # Caller passed an inverted rect; normalise rather than no-op so
        # geometry-level callers don't have to second-guess drag direction.
        min_x, max_x = sorted((min_x, max_x))
        min_y, max_y = sorted((min_y, max_y))
    polygon = shapely_box(min_x, min_y, max_x, max_y)

    out: list[tuple[str, str]] = []
    for layer in layers:
        if not layer.visible:
            continue
        for entity in layer.entities:
            if _matches(entity, polygon, mode, (min_x, min_y, max_x, max_y)):
                out.append((layer.name, entity.id))
    return out


def _matches(
    entity: GeometryEntity,
    polygon: BaseGeometry,
    mode: BoxMode,
    bounds: tuple[float, float, float, float],
) -> bool:
    if entity.point is not None:
        # Point entity: a strict containment check on the box.
        x, y = entity.point
        min_x, min_y, max_x, max_y = bounds
        return min_x <= x <= max_x and min_y <= y <= max_y

    geom = entity.geom
    if mode is BoxMode.CONTAINED:
        return bool(geom.within(polygon))
    return bool(geom.intersects(polygon))
