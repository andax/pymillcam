"""Smoke + unit tests for the 2D viewport.

Visual correctness — whether arcs render smoothly, whether the grid looks
right, whether axes point the correct way — can only be verified by a
human. These tests guard the invariants that *can* be asserted: the
coordinate transform, the bounds calculation, the zoom-centered-on-cursor
behaviour, and basic instantiation with mixed line/arc content.
"""
from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QPointF
from pytestqt.qtbot import QtBot

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.segments import ArcSegment, LineSegment
from pymillcam.ui.viewport import (
    Viewport,
    _angle_within_sweep,
    _distance_to_entity,
    _grid_spacings,
)


@pytest.fixture
def viewport(qtbot: QtBot) -> Viewport:
    vp = Viewport()
    qtbot.addWidget(vp)
    vp.resize(800, 600)
    # Force the resizeEvent to run so _origin gets initialised to widget centre.
    vp.show()
    qtbot.waitExposed(vp)
    return vp


def _mixed_layer() -> GeometryLayer:
    contour = GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(50, 0)),
            LineSegment(start=(50, 0), end=(50, 30)),
            ArcSegment(center=(45, 30), radius=5, start_angle_deg=0, sweep_deg=180),
            LineSegment(start=(40, 30), end=(0, 30)),
            LineSegment(start=(0, 30), end=(0, 0)),
        ],
        closed=True,
    )
    full_circle = GeometryEntity(
        segments=[ArcSegment(center=(25, 15), radius=5, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    point = GeometryEntity(point=(10.0, 10.0))
    return GeometryLayer(name="mixed", entities=[contour, full_circle, point])


def test_instantiates_without_geometry(viewport: Viewport) -> None:
    assert viewport.scale > 0


def test_set_layers_with_mixed_content_does_not_crash(viewport: Viewport) -> None:
    viewport.set_layers([_mixed_layer()])
    viewport.repaint()  # force a paint cycle to catch render-time errors


def test_world_widget_round_trip(viewport: Viewport) -> None:
    for x, y in [(0.0, 0.0), (10.0, 20.0), (-15.5, 7.25)]:
        widget_pt = viewport.world_to_widget(x, y)
        back = viewport.widget_to_world(widget_pt)
        assert back[0] == pytest.approx(x, abs=1e-9)
        assert back[1] == pytest.approx(y, abs=1e-9)


def test_world_y_up_convention(viewport: Viewport) -> None:
    # Point with larger world Y should render higher on the widget
    # (smaller widget-space Y).
    low = viewport.world_to_widget(0, 0)
    high = viewport.world_to_widget(0, 10)
    assert high.y() < low.y()


def test_fit_to_view_centres_geometry(viewport: Viewport, qtbot: QtBot) -> None:
    # Rectangle 0..50 x 0..30 — centre at (25, 15).
    entity = GeometryEntity(
        segments=[
            LineSegment(start=(0, 0), end=(50, 0)),
            LineSegment(start=(50, 0), end=(50, 30)),
            LineSegment(start=(50, 30), end=(0, 30)),
            LineSegment(start=(0, 30), end=(0, 0)),
        ],
        closed=True,
    )
    viewport.set_layers([GeometryLayer(name="rect", entities=[entity])])
    viewport.fit_to_view()
    centre_widget = viewport.world_to_widget(25, 15)
    assert centre_widget.x() == pytest.approx(viewport.width() / 2, abs=1.0)
    assert centre_widget.y() == pytest.approx(viewport.height() / 2, abs=1.0)


def test_fit_to_view_on_empty_is_noop(viewport: Viewport) -> None:
    scale_before = viewport.scale
    viewport.fit_to_view()
    assert viewport.scale == scale_before


def test_wheel_zoom_keeps_cursor_world_point_pinned(viewport: Viewport, qtbot: QtBot) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QWheelEvent

    cursor = QPointF(200, 150)
    before = viewport.widget_to_world(cursor)
    event = QWheelEvent(
        cursor,
        viewport.mapToGlobal(cursor),
        QPoint(0, 120),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    viewport.wheelEvent(event)
    after = viewport.widget_to_world(cursor)
    assert after[0] == pytest.approx(before[0], abs=1e-6)
    assert after[1] == pytest.approx(before[1], abs=1e-6)
    assert viewport.scale > 1.0  # zoomed in


def test_grid_spacings_pick_reasonable_values() -> None:
    # At 2 px/mm, target 10px → 5 mm minor, 50 mm major.
    assert _grid_spacings(2.0) == pytest.approx((5.0, 50.0))
    # At 20 px/mm, target 0.5 mm → 0.5 mm minor, 5 mm major.
    assert _grid_spacings(20.0) == pytest.approx((0.5, 5.0))
    # At 0.5 px/mm, target 20 mm → 20 mm minor, 200 mm major.
    assert _grid_spacings(0.5) == pytest.approx((20.0, 200.0))


def test_hit_test_picks_nearest_entity_within_tolerance(viewport: Viewport) -> None:
    line = GeometryEntity(
        segments=[LineSegment(start=(0, 0), end=(100, 0))],
    )
    arc = GeometryEntity(
        segments=[ArcSegment(center=(50, 50), radius=10, start_angle_deg=0, sweep_deg=360)],
        closed=True,
    )
    viewport.set_layers([GeometryLayer(name="L", entities=[line, arc])])
    viewport.fit_to_view()

    # Close to the line at its midpoint.
    layer, entity_id = viewport._hit_test((50.0, 0.02))
    assert (layer, entity_id) == ("L", line.id)

    # Near the circle's rim.
    layer, entity_id = viewport._hit_test((60.0, 50.0))
    assert (layer, entity_id) == ("L", arc.id)

    # Empty space far from both.
    layer, entity_id = viewport._hit_test((500.0, 500.0))
    assert (layer, entity_id) == (None, None)


def test_set_selected_updates_state_without_emitting(
    viewport: Viewport, qtbot: QtBot
) -> None:
    entity = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    viewport.set_layers([GeometryLayer(name="L", entities=[entity])])
    with qtbot.assertNotEmitted(viewport.selection_changed):
        viewport.set_selected("L", entity.id)
    assert viewport.selected == ("L", entity.id)


def test_set_layers_drops_stale_selection(viewport: Viewport) -> None:
    entity = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    viewport.set_layers([GeometryLayer(name="L", entities=[entity])])
    viewport.set_selected("L", entity.id)
    # Now replace with an empty layer list — the selection target is gone.
    viewport.set_layers([])
    assert viewport.selected == (None, None)


def test_distance_to_arc_handles_angles_outside_sweep() -> None:
    # Quarter arc from 0° to 90° at the origin with radius 10.
    arc_entity = GeometryEntity(
        segments=[
            ArcSegment(center=(0, 0), radius=10, start_angle_deg=0, sweep_deg=90),
        ],
    )
    # A point at 180° (west of origin) is *outside* the sweep — distance
    # should fall back to the nearest endpoint. Endpoints are (10, 0) and
    # (0, 10); from (-5, 0) the latter is closer.
    d = _distance_to_entity((-5, 0), arc_entity)
    assert d == pytest.approx(math.hypot(-5, -10), abs=1e-6)
    # A point inside the sweep angle range — distance is |d_center - r|.
    d = _distance_to_entity((3, 4), arc_entity)  # d_center = 5, radius=10 → 5
    assert d == pytest.approx(5.0, abs=1e-6)


def test_angle_within_sweep_cw_arc() -> None:
    cw_quarter = ArcSegment(center=(0, 0), radius=1, start_angle_deg=0, sweep_deg=-90)
    # 0° is the start — boundary case.
    assert _angle_within_sweep(0, cw_quarter)
    # -45° (or equivalently 315°) is inside the CW sweep.
    assert _angle_within_sweep(-45, cw_quarter)
    # 45° is on the other (CCW) side, outside the sweep.
    assert not _angle_within_sweep(45, cw_quarter)


def test_set_profile_preview_then_clear(viewport: Viewport) -> None:
    viewport.set_profile_preview([LineSegment(start=(0, 0), end=(10, 0))])
    assert viewport._profile_preview
    viewport.clear_profile_preview()
    assert viewport._profile_preview == []


def test_show_toggles_do_not_drop_state(viewport: Viewport) -> None:
    viewport.set_profile_preview([LineSegment(start=(0, 0), end=(10, 0))])
    viewport.set_show_profile_preview(False)
    # Hiding doesn't clear — re-showing should reveal the same data.
    assert viewport._profile_preview
    viewport.set_show_profile_preview(True)
    assert viewport._profile_preview


def test_mouse_position_signal_fires_on_move(viewport: Viewport, qtbot: QtBot) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent

    with qtbot.waitSignal(viewport.mouse_position_changed, timeout=500):
        event = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(100, 100),
            viewport.mapToGlobal(QPoint(100, 100)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        viewport.mouseMoveEvent(event)
