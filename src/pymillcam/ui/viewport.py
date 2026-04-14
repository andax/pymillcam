"""2D viewport widget — renders geometry layers with pan/zoom.

Coordinate system notes:
- World is in millimetres with +X right, +Y up (CAD convention).
- Widget pixels are the usual +X right, +Y down.
- Mapping is done manually (not via `QTransform`) so text/grid labels stay
  right-side up. `_world_to_widget` and `_widget_to_world` are the bridges.
- Arcs render via `QPainter.drawArc`, which uses mathematical CCW angle
  convention with 0° at 3 o'clock — the same as our `ArcSegment`. So
  `start_angle_deg` and `sweep_deg` pass through untouched (only converted
  to Qt's 1/16-degree units).
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.segments import ArcSegment, LineSegment, Segment
from pymillcam.engine.ir_walker import MoveKind, WalkedMove
from pymillcam.ui.box_selection import (
    BoxMode,
    SelectionCombine,
    combine_selection,
    direction_from_drag,
    select_in_box,
)

# Px per mm — chosen so that a ~400 mm part fits comfortably in an 800 px wide
# viewport before the user touches zoom.
DEFAULT_SCALE_PX_PER_MM = 2.0
MIN_SCALE_PX_PER_MM = 0.01
MAX_SCALE_PX_PER_MM = 10000.0
ZOOM_STEP = 1.2

# Desired pixel spacing for the minor grid; spacing adapts so this stays near.
TARGET_MINOR_SPACING_PX = 10.0

# Visual styling.
COLOR_BACKGROUND = QColor(30, 30, 32)
COLOR_GRID_MINOR = QColor(48, 48, 52)
COLOR_GRID_MAJOR = QColor(68, 68, 72)
COLOR_AXIS_X = QColor(190, 80, 80)
COLOR_AXIS_Y = QColor(80, 160, 80)
COLOR_GEOMETRY = QColor(230, 230, 230)
COLOR_POINT = QColor(240, 200, 80)
COLOR_SELECTED = QColor(90, 180, 255)
COLOR_PROFILE_PREVIEW = QColor(255, 160, 60)
COLOR_TOOLPATH_FEED = QColor(220, 90, 200)
COLOR_TOOLPATH_RAPID = QColor(80, 200, 220)
COLOR_BOX_CONTAINED_FILL = QColor(80, 220, 120, 40)
COLOR_BOX_CONTAINED_LINE = QColor(80, 220, 120)
COLOR_BOX_CROSSING_FILL = QColor(80, 160, 240, 40)
COLOR_BOX_CROSSING_LINE = QColor(80, 160, 240)

# Direction arrow size in widget pixels, and the minimum on-screen segment
# length below which we skip the arrow (avoids cluttering tiny chord runs).
ARROW_SIZE_PX = 7.0
ARROW_MIN_SEGMENT_PX = 24.0

# How close (in widget pixels) the click must be to an entity to hit it.
HIT_TEST_TOLERANCE_PX = 5.0
# Pixels of mouse movement required before a left-press becomes a drag.
DRAG_THRESHOLD_PX = 4.0


class Viewport(QWidget):
    """Pan/zoom/draw widget. Owns rendering state but no geometry model."""

    mouse_position_changed = Signal(float, float)
    # Emitted whenever the user changes the selection through the viewport.
    # Payload is the new selection: a list of (layer_name, entity_id) tuples,
    # possibly empty.
    selection_changed = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layers: list[GeometryLayer] = []
        self._scale: float = DEFAULT_SCALE_PX_PER_MM
        self._origin: QPointF = QPointF(0.0, 0.0)
        self._origin_initialized = False
        self._panning = False
        self._pan_start_widget: QPoint = QPoint()
        self._pan_start_origin: QPointF = QPointF()
        self._selection: list[tuple[str, str]] = []
        # Drag-to-box-select state (left button only; differentiated from a
        # click by `DRAG_THRESHOLD_PX` of movement).
        self._left_press_widget: QPointF | None = None
        self._dragging_box = False
        self._drag_current_widget: QPointF | None = None
        self._profile_preview: list[Segment] = []
        self._toolpath_preview: list[WalkedMove] = []
        self._show_profile_preview = True
        self._show_toolpath_preview = True

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(400, 300)

    def set_layers(self, layers: list[GeometryLayer]) -> None:
        self._layers = list(layers)
        # Drop any selected entity that's no longer in the project.
        self._selection = [
            (layer, entity_id)
            for layer, entity_id in self._selection
            if self._has_entity(layer, entity_id)
        ]
        self.update()

    def set_selection(self, items: list[tuple[str, str]]) -> None:
        """Programmatic selection change. Does NOT emit `selection_changed`."""
        self._selection = list(items)
        self.update()

    def set_profile_preview(self, segments: list[Segment]) -> None:
        self._profile_preview = list(segments)
        self.update()

    def clear_profile_preview(self) -> None:
        self._profile_preview = []
        self.update()

    def set_toolpath_preview(self, moves: list[WalkedMove]) -> None:
        self._toolpath_preview = list(moves)
        self.update()

    def clear_toolpath_preview(self) -> None:
        self._toolpath_preview = []
        self.update()

    def set_show_profile_preview(self, visible: bool) -> None:
        self._show_profile_preview = visible
        self.update()

    def set_show_toolpath_preview(self, visible: bool) -> None:
        self._show_toolpath_preview = visible
        self.update()

    @property
    def selection(self) -> list[tuple[str, str]]:
        return list(self._selection)

    def _has_entity(self, layer_name: str, entity_id: str) -> bool:
        for layer in self._layers:
            if layer.name != layer_name:
                continue
            return any(e.id == entity_id for e in layer.entities)
        return False

    def fit_to_view(self, margin: float = 0.1) -> None:
        bounds = self._compute_bounds()
        if bounds is None:
            return
        min_x, min_y, max_x, max_y = bounds
        width = max(max_x - min_x, 1e-6)
        height = max(max_y - min_y, 1e-6)
        pad_x = width * margin
        pad_y = height * margin
        width += 2 * pad_x
        height += 2 * pad_y
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2

        if self.width() <= 0 or self.height() <= 0:
            return
        scale = min(self.width() / width, self.height() / height)
        self._scale = max(MIN_SCALE_PX_PER_MM, min(scale, MAX_SCALE_PX_PER_MM))
        # Place (center_x, center_y) at the widget centre.
        self._origin = QPointF(
            self.width() / 2 - center_x * self._scale,
            self.height() / 2 + center_y * self._scale,
        )
        self._origin_initialized = True
        self.update()

    def world_to_widget(self, x: float, y: float) -> QPointF:
        return QPointF(self._origin.x() + x * self._scale, self._origin.y() - y * self._scale)

    def widget_to_world(self, point: QPointF) -> tuple[float, float]:
        return (
            (point.x() - self._origin.x()) / self._scale,
            -(point.y() - self._origin.y()) / self._scale,
        )

    @property
    def scale(self) -> float:
        return self._scale

    def _hit_test(
        self, world_point: tuple[float, float]
    ) -> tuple[str | None, str | None]:
        """Return (layer_name, entity_id) of the closest entity within tolerance."""
        tolerance_mm = HIT_TEST_TOLERANCE_PX / max(self._scale, 1e-9)
        best: tuple[str, str] | None = None
        best_dist = math.inf
        for layer in self._layers:
            if not layer.visible:
                continue
            for entity in layer.entities:
                d = _distance_to_entity(world_point, entity)
                if d <= tolerance_mm and d < best_dist:
                    best_dist = d
                    best = (layer.name, entity.id)
        if best is None:
            return (None, None)
        return best

    def _compute_bounds(self) -> tuple[float, float, float, float] | None:
        min_x = math.inf
        min_y = math.inf
        max_x = -math.inf
        max_y = -math.inf
        found = False
        for layer in self._layers:
            if not layer.visible:
                continue
            for entity in layer.entities:
                try:
                    b = entity.geom.bounds  # (minx, miny, maxx, maxy)
                except ValueError:
                    continue
                if not b:
                    continue
                min_x = min(min_x, b[0])
                min_y = min(min_y, b[1])
                max_x = max(max_x, b[2])
                max_y = max(max_y, b[3])
                found = True
        if not found:
            return None
        return (min_x, min_y, max_x, max_y)

    # ------------------------------------------------------------------ paint

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        if not self._origin_initialized and self.width() > 0 and self.height() > 0:
            self._origin = QPointF(self.width() / 2, self.height() / 2)
            self._origin_initialized = True
        super().resizeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), COLOR_BACKGROUND)
            self._draw_grid(painter)
            self._draw_axes(painter)
            self._draw_geometry(painter)
            # Draw the profile preview first, then the G-code toolpath on top
            # — the toolpath reflects the actual generated output, so it
            # should win when both layers are visible.
            if self._show_profile_preview and self._profile_preview:
                self._draw_profile_preview(painter)
            if self._show_toolpath_preview and self._toolpath_preview:
                self._draw_toolpath_preview(painter)
            if self._dragging_box:
                self._draw_drag_box(painter)
        finally:
            painter.end()

    def _draw_grid(self, painter: QPainter) -> None:
        minor, major = _grid_spacings(self._scale)
        w0 = self.widget_to_world(QPointF(0, self.height()))  # bottom-left world
        w1 = self.widget_to_world(QPointF(self.width(), 0))  # top-right world
        min_x_w, min_y_w = w0
        max_x_w, max_y_w = w1

        minor_pen = QPen(COLOR_GRID_MINOR, 1)
        major_pen = QPen(COLOR_GRID_MAJOR, 1)

        # Minor grid only if it won't collapse into a solid wash.
        if minor * self._scale >= 4.0:
            painter.setPen(minor_pen)
            self._draw_grid_lines(painter, minor, min_x_w, max_x_w, min_y_w, max_y_w, skip=major)

        painter.setPen(major_pen)
        self._draw_grid_lines(painter, major, min_x_w, max_x_w, min_y_w, max_y_w)

    def _draw_grid_lines(
        self,
        painter: QPainter,
        spacing: float,
        min_x: float,
        max_x: float,
        min_y: float,
        max_y: float,
        skip: float | None = None,
    ) -> None:
        start_x = math.floor(min_x / spacing) * spacing
        x = start_x
        top = self.world_to_widget(0, max_y).y()
        bottom = self.world_to_widget(0, min_y).y()
        while x <= max_x + spacing:
            if skip is None or not _is_multiple(x, skip):
                px = self.world_to_widget(x, 0).x()
                painter.drawLine(QPointF(px, top), QPointF(px, bottom))
            x += spacing

        start_y = math.floor(min_y / spacing) * spacing
        y = start_y
        left = self.world_to_widget(min_x, 0).x()
        right = self.world_to_widget(max_x, 0).x()
        while y <= max_y + spacing:
            if skip is None or not _is_multiple(y, skip):
                py = self.world_to_widget(0, y).y()
                painter.drawLine(QPointF(left, py), QPointF(right, py))
            y += spacing

    def _draw_axes(self, painter: QPainter) -> None:
        painter.setPen(QPen(COLOR_AXIS_X, 1.5))
        y_px = self._origin.y()
        painter.drawLine(QPointF(0, y_px), QPointF(self.width(), y_px))
        painter.setPen(QPen(COLOR_AXIS_Y, 1.5))
        x_px = self._origin.x()
        painter.drawLine(QPointF(x_px, 0), QPointF(x_px, self.height()))

        # Small origin crosshair square so you can still see (0,0) when axes
        # scroll off-screen.
        origin_visible = (
            0 <= self._origin.x() <= self.width()
            and 0 <= self._origin.y() <= self.height()
        )
        if origin_visible:
            painter.setPen(QPen(QColor(220, 220, 220), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(self._origin, 3, 3)

    def _draw_geometry(self, painter: QPainter) -> None:
        default_pen = QPen(COLOR_GEOMETRY, 1.4)
        selected_pen = QPen(COLOR_SELECTED, 2.2)
        selected_set = set(self._selection)
        deferred: list[GeometryEntity] = []
        for layer in self._layers:
            if not layer.visible:
                continue
            for entity in layer.entities:
                if (layer.name, entity.id) in selected_set:
                    # Draw selected entities last so they sit on top.
                    deferred.append(entity)
                    continue
                painter.setPen(default_pen)
                self._draw_entity(painter, entity)
        painter.setPen(selected_pen)
        for entity in deferred:
            self._draw_entity(painter, entity)

    def _draw_entity(self, painter: QPainter, entity: GeometryEntity) -> None:
        if entity.point is not None:
            self._draw_point(painter, entity.point)
            return
        for seg in entity.segments:
            self._draw_segment(painter, seg)

    def _draw_point(self, painter: QPainter, p: tuple[float, float]) -> None:
        prev_pen = painter.pen()
        painter.setPen(QPen(COLOR_POINT, 1.5))
        c = self.world_to_widget(*p)
        painter.drawLine(QPointF(c.x() - 4, c.y()), QPointF(c.x() + 4, c.y()))
        painter.drawLine(QPointF(c.x(), c.y() - 4), QPointF(c.x(), c.y() + 4))
        painter.setPen(prev_pen)

    def _draw_profile_preview(self, painter: QPainter) -> None:
        pen = QPen(COLOR_PROFILE_PREVIEW, 2.0)
        painter.setPen(pen)
        for seg in self._profile_preview:
            self._draw_segment(painter, seg)
        # Direction arrows on top, in the same colour.
        painter.setBrush(COLOR_PROFILE_PREVIEW)
        painter.setPen(Qt.PenStyle.NoPen)
        for seg in self._profile_preview:
            self._draw_direction_arrow(painter, seg)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_toolpath_preview(self, painter: QPainter) -> None:
        feed_pen = QPen(COLOR_TOOLPATH_FEED, 1.6)
        rapid_pen = QPen(COLOR_TOOLPATH_RAPID, 1.0, Qt.PenStyle.DashLine)
        for move in self._toolpath_preview:
            painter.setPen(feed_pen if move.kind is MoveKind.FEED else rapid_pen)
            self._draw_segment(painter, move.segment)
        # Arrows only on feed moves — rapids are positioning, not cutting.
        painter.setBrush(COLOR_TOOLPATH_FEED)
        painter.setPen(Qt.PenStyle.NoPen)
        for move in self._toolpath_preview:
            if move.kind is MoveKind.FEED:
                self._draw_direction_arrow(painter, move.segment)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_direction_arrow(
        self, painter: QPainter, seg: LineSegment | ArcSegment
    ) -> None:
        """Filled triangle at segment midpoint, pointing along travel."""
        if isinstance(seg, LineSegment):
            sx, sy = seg.start
            ex, ey = seg.end
            length_world = math.hypot(ex - sx, ey - sy)
            length_px = length_world * self._scale
            if length_px < ARROW_MIN_SEGMENT_PX:
                return
            mid = QPointF((sx + ex) / 2, (sy + ey) / 2)
            tan_x = (ex - sx) / length_world
            tan_y = (ey - sy) / length_world
        else:
            length_world = abs(math.radians(seg.sweep_deg)) * seg.radius
            length_px = length_world * self._scale
            if length_px < ARROW_MIN_SEGMENT_PX:
                return
            mid_angle_deg = seg.start_angle_deg + seg.sweep_deg / 2
            rad = math.radians(mid_angle_deg)
            cx, cy = seg.center
            mid = QPointF(
                cx + seg.radius * math.cos(rad),
                cy + seg.radius * math.sin(rad),
            )
            sign = 1.0 if seg.sweep_deg > 0 else -1.0
            tan_x = -sign * math.sin(rad)
            tan_y = sign * math.cos(rad)
        # World-space tangent → widget-space (Y flip).
        tip_widget = self.world_to_widget(mid.x(), mid.y())
        # In widget coords the X axis is the same; Y is flipped.
        wtx, wty = tan_x, -tan_y
        # Build the triangle in widget space.
        size = ARROW_SIZE_PX
        tip = QPointF(tip_widget.x() + wtx * size, tip_widget.y() + wty * size)
        # Two base points, perpendicular ±half-size from the back of the arrow.
        bx = tip_widget.x() - wtx * size * 0.4
        by = tip_widget.y() - wty * size * 0.4
        perp_x = -wty
        perp_y = wtx
        poly = QPolygonF(
            [
                tip,
                QPointF(bx + perp_x * size * 0.5, by + perp_y * size * 0.5),
                QPointF(bx - perp_x * size * 0.5, by - perp_y * size * 0.5),
            ]
        )
        painter.drawPolygon(poly)

    def _draw_drag_box(self, painter: QPainter) -> None:
        if self._left_press_widget is None or self._drag_current_widget is None:
            return
        start = self._left_press_widget
        end = self._drag_current_widget
        mode = direction_from_drag(start.x(), end.x())
        line_color = (
            COLOR_BOX_CONTAINED_LINE if mode is BoxMode.CONTAINED else COLOR_BOX_CROSSING_LINE
        )
        fill_color = (
            COLOR_BOX_CONTAINED_FILL if mode is BoxMode.CONTAINED else COLOR_BOX_CROSSING_FILL
        )
        rect = QRectF(start, end).normalized()
        # Crossing mode uses a dashed outline so the direction is unambiguous
        # even for colourblind users.
        pen_style = (
            Qt.PenStyle.SolidLine if mode is BoxMode.CONTAINED else Qt.PenStyle.DashLine
        )
        painter.setPen(QPen(line_color, 1.2, pen_style))
        painter.setBrush(fill_color)
        painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_segment(self, painter: QPainter, seg: LineSegment | ArcSegment) -> None:
        if isinstance(seg, LineSegment):
            p1 = self.world_to_widget(*seg.start)
            p2 = self.world_to_widget(*seg.end)
            painter.drawLine(p1, p2)
            return
        # Arc: draw via Qt's native arc primitive so it stays smooth at any zoom.
        c = self.world_to_widget(*seg.center)
        r_px = seg.radius * self._scale
        rect = QRectF(c.x() - r_px, c.y() - r_px, 2 * r_px, 2 * r_px)
        start = int(round(seg.start_angle_deg * 16))
        span = int(round(seg.sweep_deg * 16))
        painter.drawArc(rect, start, span)

    # ----------------------------------------------------------- interaction

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        cursor_widget = event.position()
        cursor_world = self.widget_to_world(cursor_widget)

        factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / ZOOM_STEP
        new_scale = max(MIN_SCALE_PX_PER_MM, min(self._scale * factor, MAX_SCALE_PX_PER_MM))
        self._scale = new_scale

        # Keep the world point under the cursor pinned to the same pixel.
        self._origin = QPointF(
            cursor_widget.x() - cursor_world[0] * self._scale,
            cursor_widget.y() + cursor_world[1] * self._scale,
        )
        self.update()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start_widget = event.position().toPoint()
            self._pan_start_origin = QPointF(self._origin)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            # Defer click vs drag decision until release / movement.
            self._left_press_widget = QPointF(event.position())
            self._dragging_box = False
            self._drag_current_widget = None
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self._panning:
            delta = pos.toPoint() - self._pan_start_widget
            self._origin = QPointF(
                self._pan_start_origin.x() + delta.x(),
                self._pan_start_origin.y() + delta.y(),
            )
            self.update()
        if self._left_press_widget is not None:
            dx = pos.x() - self._left_press_widget.x()
            dy = pos.y() - self._left_press_widget.y()
            if self._dragging_box or math.hypot(dx, dy) >= DRAG_THRESHOLD_PX:
                self._dragging_box = True
                self._drag_current_widget = QPointF(pos)
                self.update()
        x, y = self.widget_to_world(pos)
        self.mouse_position_changed.emit(x, y)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._left_press_widget is not None:
            combine = _modifier_combine(event.modifiers())
            if self._dragging_box and self._drag_current_widget is not None:
                self._finish_box_select(
                    self._left_press_widget, self._drag_current_widget, combine
                )
            else:
                world = self.widget_to_world(event.position())
                hit_layer, hit_id = self._hit_test(world)
                picked: list[tuple[str, str]] = (
                    [(hit_layer, hit_id)] if hit_layer and hit_id else []
                )
                # Empty pick + a modifier means "add nothing / toggle nothing"
                # — keep the existing selection. A plain click on empty space
                # still clears, matching CAD convention.
                if not picked and combine is not SelectionCombine.REPLACE:
                    pass
                else:
                    new_selection = combine_selection(self._selection, picked, combine)
                    self.set_selection(new_selection)
                    self.selection_changed.emit(new_selection)
            self._left_press_widget = None
            self._dragging_box = False
            self._drag_current_widget = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _finish_box_select(
        self, start: QPointF, end: QPointF, combine: SelectionCombine
    ) -> None:
        start_world = self.widget_to_world(start)
        end_world = self.widget_to_world(end)
        mode = direction_from_drag(start.x(), end.x())
        box = (
            min(start_world[0], end_world[0]),
            min(start_world[1], end_world[1]),
            max(start_world[0], end_world[0]),
            max(start_world[1], end_world[1]),
        )
        picked = select_in_box(self._layers, box, mode)
        new_selection = combine_selection(self._selection, picked, combine)
        self.set_selection(new_selection)
        self.selection_changed.emit(new_selection)


def _modifier_combine(modifiers: Qt.KeyboardModifier) -> SelectionCombine:
    """Map Qt keyboard modifiers to a selection-combine mode.

    Ctrl wins over Shift if both are held — toggle is the more useful action
    when the user is fixing up a selection.
    """
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        return SelectionCombine.TOGGLE
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        return SelectionCombine.ADD
    return SelectionCombine.REPLACE


def _grid_spacings(scale_px_per_mm: float) -> tuple[float, float]:
    """Pick sensible (minor, major) spacings in mm for a given zoom level."""
    target_mm = TARGET_MINOR_SPACING_PX / scale_px_per_mm
    exp = math.floor(math.log10(target_mm))
    mantissa = target_mm / (10**exp)
    if mantissa < 1.5:
        minor = 1 * 10**exp
    elif mantissa < 3.5:
        minor = 2 * 10**exp
    elif mantissa < 7.5:
        minor = 5 * 10**exp
    else:
        minor = 10 ** (exp + 1)
    return minor, minor * 10


def _is_multiple(value: float, step: float, eps: float = 1e-6) -> bool:
    if step <= 0:
        return False
    ratio = value / step
    return abs(ratio - round(ratio)) < eps


def _distance_to_entity(point: tuple[float, float], entity: GeometryEntity) -> float:
    if entity.point is not None:
        return math.hypot(point[0] - entity.point[0], point[1] - entity.point[1])
    if not entity.segments:
        return math.inf
    return min(_distance_to_segment(point, seg) for seg in entity.segments)


def _distance_to_segment(
    point: tuple[float, float], seg: LineSegment | ArcSegment
) -> float:
    if isinstance(seg, LineSegment):
        return _point_to_line_segment(point, seg.start, seg.end)
    return _point_to_arc(point, seg)


def _point_to_line_segment(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq <= 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(p[0] - cx, p[1] - cy)


def _point_to_arc(p: tuple[float, float], arc: ArcSegment) -> float:
    cx, cy = arc.center
    dx = p[0] - cx
    dy = p[1] - cy
    d_center = math.hypot(dx, dy)
    if d_center == 0.0:
        return arc.radius
    angle_deg = math.degrees(math.atan2(dy, dx))
    if _angle_within_sweep(angle_deg, arc):
        return abs(d_center - arc.radius)
    # Outside the arc's angular sweep — closest point is whichever endpoint
    # is nearest.
    sx, sy = arc.start
    ex, ey = arc.end
    return min(math.hypot(p[0] - sx, p[1] - sy), math.hypot(p[0] - ex, p[1] - ey))


def _angle_within_sweep(angle_deg: float, arc: ArcSegment) -> bool:
    if arc.is_full_circle:
        return True
    relative = (angle_deg - arc.start_angle_deg) % 360.0
    sweep = arc.sweep_deg
    eps = 1e-9
    if sweep >= 0:
        # CCW: visited range is [0, sweep].
        return relative <= sweep + eps
    # CW: visited range is {0} ∪ [360 + sweep, 360). The shared start
    # point (relative ≈ 0) needs its own branch because modulo wraps
    # it to 0, not to 360.
    return relative <= eps or relative >= 360.0 + sweep - eps
