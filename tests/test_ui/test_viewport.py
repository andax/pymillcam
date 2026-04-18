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


def test_set_selection_updates_state_without_emitting(
    viewport: Viewport, qtbot: QtBot
) -> None:
    entity = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    viewport.set_layers([GeometryLayer(name="L", entities=[entity])])
    with qtbot.assertNotEmitted(viewport.selection_changed):
        viewport.set_selection([("L", entity.id)])
    assert viewport.selection == [("L", entity.id)]


def test_set_layers_drops_stale_selection(viewport: Viewport) -> None:
    entity = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    viewport.set_layers([GeometryLayer(name="L", entities=[entity])])
    viewport.set_selection([("L", entity.id)])
    # Now replace with an empty layer list — the selection target is gone.
    viewport.set_layers([])
    assert viewport.selection == []


def _drive_drag(
    viewport: Viewport,
    start: QPointF,
    end: QPointF,
    modifiers: object | None = None,
) -> None:
    """Replay a left-button press → move → release at the widget level."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent

    mods = modifiers if modifiers is not None else Qt.KeyboardModifier.NoModifier

    def _ev(kind, pos: QPointF, button: Qt.MouseButton) -> QMouseEvent:
        return QMouseEvent(
            kind,
            pos,
            viewport.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
            button,
            Qt.MouseButton.LeftButton if button == Qt.MouseButton.NoButton else button,
            mods,
        )

    left = Qt.MouseButton.LeftButton
    none = Qt.MouseButton.NoButton
    viewport.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, start, left))
    viewport.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, end, none))
    viewport.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, end, left))


def test_drag_left_to_right_picks_only_contained_entities(viewport: Viewport) -> None:
    inside = GeometryEntity(
        segments=[
            LineSegment(start=(2, 2), end=(8, 2)),
            LineSegment(start=(8, 2), end=(8, 8)),
            LineSegment(start=(8, 8), end=(2, 8)),
            LineSegment(start=(2, 8), end=(2, 2)),
        ],
        closed=True,
    )
    crossing_only = GeometryEntity(
        segments=[LineSegment(start=(-50, 5), end=(50, 5))]
    )
    viewport.set_layers(
        [GeometryLayer(name="L", entities=[inside, crossing_only])]
    )
    viewport.fit_to_view()

    p1 = viewport.world_to_widget(0, 10)   # top-left of box (world y=10 → smaller widget y)
    p2 = viewport.world_to_widget(10, 0)   # bottom-right (L→R drag)
    _drive_drag(viewport, p1, p2)
    assert viewport.selection == [("L", inside.id)]


def test_drag_right_to_left_picks_crossing_entities(viewport: Viewport) -> None:
    inside = GeometryEntity(
        segments=[
            LineSegment(start=(2, 2), end=(8, 2)),
            LineSegment(start=(8, 2), end=(8, 8)),
            LineSegment(start=(8, 8), end=(2, 8)),
            LineSegment(start=(2, 8), end=(2, 2)),
        ],
        closed=True,
    )
    crossing_only = GeometryEntity(
        segments=[LineSegment(start=(-50, 5), end=(50, 5))]
    )
    viewport.set_layers(
        [GeometryLayer(name="L", entities=[inside, crossing_only])]
    )
    viewport.fit_to_view()

    p1 = viewport.world_to_widget(10, 10)  # top-right
    p2 = viewport.world_to_widget(0, 0)    # bottom-left (R→L drag)
    _drive_drag(viewport, p1, p2)
    assert {pair[1] for pair in viewport.selection} == {inside.id, crossing_only.id}


def test_short_left_press_is_treated_as_click_not_drag(viewport: Viewport) -> None:
    line = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(100, 0))])
    viewport.set_layers([GeometryLayer(name="L", entities=[line])])
    viewport.fit_to_view()

    p = viewport.world_to_widget(50, 0)
    # Move only 1 px — under DRAG_THRESHOLD_PX, so still a click.
    _drive_drag(viewport, p, QPointF(p.x() + 1, p.y()))
    assert viewport.selection == [("L", line.id)]


def test_click_on_empty_space_clears_selection(viewport: Viewport) -> None:
    entity = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    viewport.set_layers([GeometryLayer(name="L", entities=[entity])])
    viewport.set_selection([("L", entity.id)])

    # Click far from any geometry.
    p = viewport.world_to_widget(500, 500)
    _drive_drag(viewport, p, p)
    assert viewport.selection == []


def test_ctrl_click_toggles_individual_entity(viewport: Viewport) -> None:
    from PySide6.QtCore import Qt

    a = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    b = GeometryEntity(segments=[LineSegment(start=(0, 5), end=(10, 5))])
    viewport.set_layers([GeometryLayer(name="L", entities=[a, b])])
    viewport.fit_to_view()
    viewport.set_selection([("L", a.id), ("L", b.id)])

    # Ctrl+click on `a` should remove just `a`.
    p = viewport.world_to_widget(5, 0)
    _drive_drag(viewport, p, p, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert viewport.selection == [("L", b.id)]

    # Ctrl+click again on `a` should add it back.
    _drive_drag(viewport, p, p, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert set(viewport.selection) == {("L", a.id), ("L", b.id)}


def test_shift_click_adds_to_selection(viewport: Viewport) -> None:
    from PySide6.QtCore import Qt

    a = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    b = GeometryEntity(segments=[LineSegment(start=(0, 5), end=(10, 5))])
    viewport.set_layers([GeometryLayer(name="L", entities=[a, b])])
    viewport.fit_to_view()
    viewport.set_selection([("L", a.id)])

    p = viewport.world_to_widget(5, 5)
    _drive_drag(viewport, p, p, modifiers=Qt.KeyboardModifier.ShiftModifier)
    assert set(viewport.selection) == {("L", a.id), ("L", b.id)}


def test_ctrl_drag_box_xors_entities_in_box(viewport: Viewport) -> None:
    from PySide6.QtCore import Qt

    inside = GeometryEntity(
        segments=[
            LineSegment(start=(2, 2), end=(8, 2)),
            LineSegment(start=(8, 2), end=(8, 8)),
            LineSegment(start=(8, 8), end=(2, 8)),
            LineSegment(start=(2, 8), end=(2, 2)),
        ],
        closed=True,
    )
    other = GeometryEntity(
        segments=[LineSegment(start=(50, 50), end=(60, 50))],
    )
    viewport.set_layers([GeometryLayer(name="L", entities=[inside, other])])
    viewport.fit_to_view()
    viewport.set_selection([("L", inside.id), ("L", other.id)])

    p1 = viewport.world_to_widget(0, 10)
    p2 = viewport.world_to_widget(10, 0)
    _drive_drag(viewport, p1, p2, modifiers=Qt.KeyboardModifier.ControlModifier)
    # Box covered `inside` only — toggled off; `other` untouched.
    assert viewport.selection == [("L", other.id)]


def test_ctrl_click_on_empty_space_keeps_existing(viewport: Viewport) -> None:
    from PySide6.QtCore import Qt

    entity = GeometryEntity(segments=[LineSegment(start=(0, 0), end=(10, 0))])
    viewport.set_layers([GeometryLayer(name="L", entities=[entity])])
    viewport.set_selection([("L", entity.id)])

    p = viewport.world_to_widget(500, 500)
    _drive_drag(viewport, p, p, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert viewport.selection == [("L", entity.id)]


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


def test_right_click_emits_context_menu_requested(
    viewport: Viewport, qtbot: QtBot
) -> None:
    """Right-click should fire ``context_menu_requested`` with the
    widget-space position, so MainWindow can build a context menu
    with project-specific actions (Select Similar, etc.)."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent

    with qtbot.waitSignal(viewport.context_menu_requested, timeout=500) as sig:
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(42, 17),
            viewport.mapToGlobal(QPoint(42, 17)),
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        )
        viewport.mousePressEvent(event)

    # Payload carries the widget-space position, not the global one.
    (emitted_pos,) = sig.args
    assert emitted_pos.x() == pytest.approx(42)
    assert emitted_pos.y() == pytest.approx(17)
