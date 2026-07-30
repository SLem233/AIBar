"""Always-on-top desktop widget: a row or column of per-provider radial gauges.

Behaviour (v0.6.0):
- Orientation is live: while dragging, as soon as the frame touches a screen
  edge it snaps to that edge's orientation (vertical at left/right, horizontal
  at top/bottom) and the position is clamped to the screen — it can't be
  dragged off-screen.
- On an orientation change the *frame* itself transposes (w <-> h) around its
  center, so the gauges keep their size; only their layout (row vs column)
  changes.
- Left-click opens the full dashboard popup; right-click opens the menu.
- "Скрывать виджет": after 5s idle the widget slides off the nearest screen
  edge, leaving ~15% of its frame as a "bookmark" tab; hovering that tab
  slides it back and docks it flush to the edge.
- The soonest-reset countdown across visible tiles is shown on the widget.
- A context-menu slider (50-150%) scales the whole widget.
"""

import time
from datetime import datetime, timezone

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizeGrip,
    QSlider,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .. import __version__, theme
from ..geoblock import PAUSED_MESSAGE
from ..providers.base import ProviderSnapshot
from ..update import RELEASES_URL
from .dashboard import ProviderCard
from .gauge import RadialGauge

WIDGET_FLAGS = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool

# Distance (px) from a screen edge at which the widget is "docked".
EDGE_THRESHOLD = 0  # touching counts as docked
# Fraction of the frame left poking out when auto-hidden (Feature 3).
HIDE_VISIBLE_FRACTION = 0.15
# Auto-hide delay (ms) after the mouse leaves the widget.
HIDE_DELAY = 5000
# A mouse move shorter than this (px) is treated as a click, not a drag.
CLICK_THRESHOLD = 4

DEFAULT_SCALE = 1.0
MIN_SCALE = 0.5
MAX_SCALE = 1.5


class UpdateBadge(QLabel):
    """Clickable pill shown when a newer release is published."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"""
            background: rgba(250, 178, 25, 38);
            color: {theme.WARNING};
            border: 1px solid {theme.WARNING};
            border-radius: 7px;
            padding: 2px 6px;
            font-family: "{theme.FONT_FAMILY}";
            font-size: 10px;
            font-weight: 600;
            """
        )
        self.hide()

    def show_version(self, version: str) -> None:
        self._version = version
        self._relabel()
        self.show()

    def _relabel(self) -> None:
        wide = self.parentWidget() is None or self.parentWidget().width() >= 90
        self.setText("↓ Update available" if wide else "↓ Update")
        self.setToolTip(
            f"Доступна версия {getattr(self, '_version', '')} — нажмите, чтобы открыть страницу загрузки"
        )

    def relabel_for_width(self) -> None:
        if self.isVisible():
            self._relabel()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            QDesktopServices.openUrl(QUrl(RELEASES_URL))
        super().mousePressEvent(event)


class VpnBadge(QLabel):
    """Pill shown while polling is paused because the VPN is off."""

    def __init__(self, parent=None):
        super().__init__("⏸ нет VPN", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(PAUSED_MESSAGE)
        self.setStyleSheet(
            f"""
            background: rgba(250, 178, 25, 38);
            color: {theme.WARNING};
            border: 1px solid {theme.WARNING};
            border-radius: 7px;
            padding: 2px 6px;
            font-family: "{theme.FONT_FAMILY}";
            font-size: 10px;
            font-weight: 600;
            """
        )
        self.hide()


class GaugeTile(QWidget):
    """One provider's gauge with a caption underneath."""

    def __init__(self, provider: str, parent=None):
        super().__init__(parent)
        self.gauge = RadialGauge(scalable=True)
        self.caption = QLabel(provider)
        self.caption.setAlignment(Qt.AlignHCenter)
        self.caption.setStyleSheet(
            f'color: {theme.TEXT_SECONDARY}; font-family: "{theme.FONT_FAMILY}"; font-size: 11px;'
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.gauge, stretch=1)
        layout.addWidget(self.caption)

    def update_snapshot(self, snap: ProviderSnapshot) -> None:
        self.gauge.set_percents([w.used_percent for w in snap.windows])
        suffix = " ⏸" if snap.paused else (" ⚠" if snap.error else "")
        self.caption.setText(f"{snap.provider}{suffix}")

    def resizeEvent(self, event) -> None:
        # captions don't fit below ~60px — the tooltip still names the provider
        self.caption.setVisible(self.width() >= 60)
        self.setToolTip(self.caption.text() if self.width() < 60 else "")
        super().resizeEvent(event)


class DesktopWidget(QWidget):
    """The floating always-on-top row/column of gauges."""

    geometry_changed = Signal()
    refresh_requested = Signal()
    settings_requested = Signal()
    help_requested = Signal()
    hide_requested = Signal()  # hide the widget entirely (stays in tray)
    hide_on_idle_changed = Signal(bool)
    scale_changed = Signal(float)  # widget zoom factor (0.5..1.5)
    check_updates_requested = Signal()
    quit_requested = Signal()
    mode_changed = Signal(str)  # "full" | "mini"
    dashboard_requested = Signal()  # left-click: show the full dashboard

    # Base (100% scale) frame size in each orientation.
    BASE_W = 120
    BASE_H = 260

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(WIDGET_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(self.BASE_W, self.BASE_H)

        self._scale = DEFAULT_SCALE
        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None  # to tell click from drag
        self._dragging = False
        self._tiles: dict[str, GaugeTile] = {}
        self._context_menu: QMenu | None = None
        self._snapshots: list[ProviderSnapshot] = []
        self._mode = "full"  # full | mini
        self._mini_threshold = 70.0
        # (timestamp, {provider: summed window percents}) for the last 15 min
        self._activity: list[tuple[float, dict[str, float]]] = []
        self._last_solo: str | None = None

        # Tiles live in a container whose layout swaps vertical/horizontal.
        self.tiles_vbox = QVBoxLayout()
        self.tiles_vbox.setSpacing(6)
        self.tiles_hbox = QHBoxLayout()
        self.tiles_hbox.setSpacing(6)
        self._horizontal = False  # vertical by default

        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent; width: 14px; height: 14px;")

        self.update_badge = UpdateBadge(self)
        self.vpn_badge = VpnBadge(self)

        # The soonest-reset countdown label, shown on the widget itself.
        self.countdown_label = QLabel("")
        self.countdown_label.setAlignment(Qt.AlignCenter)
        self.countdown_label.setStyleSheet(
            f'color: {theme.TEXT_MUTED}; font-family: "{theme.FONT_FAMILY}"; font-size: 10px;'
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 2)
        layout.addWidget(self.update_badge)
        layout.addWidget(self.vpn_badge)
        layout.addLayout(self.tiles_vbox, stretch=1)  # vertical is active
        layout.addWidget(self.countdown_label)
        layout.addWidget(grip, alignment=Qt.AlignBottom | Qt.AlignRight)

        # Auto-hide state.
        self._hide_on_idle = False
        self._hidden = False
        self._hidden_anchor: str | None = None  # which edge we slid behind
        self._docked_pos: QPoint | None = None  # flush-to-edge visible position
        self._idle_hide_timer = QTimer(self)
        self._idle_hide_timer.setSingleShot(True)
        self._idle_hide_timer.setInterval(HIDE_DELAY)
        self._idle_hide_timer.timeout.connect(self._slide_off)

        # Debounced geometry persistence.
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(800)
        self._geometry_timer.timeout.connect(self.geometry_changed)

        self._apply_scale(DEFAULT_SCALE)

    # ---- helpers --------------------------------------------------------
    def _available_geometry(self) -> QRect:
        return self.screen().availableGeometry()

    def _clamp_to_screen(self, pos: QPoint) -> QPoint:
        """Keep the whole frame inside the screen."""
        screen = self._available_geometry()
        x = min(max(pos.x(), screen.left()), screen.right() - self.width() + 1)
        y = min(max(pos.y(), screen.top()), screen.bottom() - self.height() + 1)
        return QPoint(x, y)

    def _center(self) -> QPoint:
        g = self.geometry()
        return QPoint(g.left() + g.width() // 2, g.top() + g.height() // 2)

    # ---- data -----------------------------------------------------------
    def update_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        for snap in snapshots:
            tile = self._tiles.get(snap.provider)
            if tile is None:
                tile = GaugeTile(snap.provider)
                self._tiles[snap.provider] = tile
                self._active_tiles_layout().addWidget(tile, stretch=1)
            tile.update_snapshot(snap)
        self._snapshots = snapshots
        self.vpn_badge.setVisible(any(s.paused for s in snapshots))
        self._record_activity(snapshots)
        self._apply_visibility()
        self._update_countdown()

    def _update_countdown(self) -> None:
        """Show the soonest reset countdown among visible tiles' windows."""
        now = datetime.now(timezone.utc)
        soonest = None
        soonest_dt = None
        for name in self._visible_providers():
            tile_snap = next((s for s in self._snapshots if s.provider == name), None)
            if not tile_snap:
                continue
            for w in tile_snap.windows:
                if w.resets_at and (soonest_dt is None or w.resets_at < soonest_dt):
                    soonest_dt = w.resets_at
                    soonest = w
        if soonest is not None:
            self.countdown_label.setText(f"↺ {soonest.reset_countdown(now)}")
            self.countdown_label.show()
        else:
            self.countdown_label.hide()

    def _active_tiles_layout(self):
        return self.tiles_hbox if self._horizontal else self.tiles_vbox

    def clear_tiles(self) -> None:
        for tile in self._tiles.values():
            self.tiles_vbox.removeWidget(tile)
            self.tiles_hbox.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()
        self._activity.clear()
        self._last_solo = None

    def set_update_available(self, version: str) -> None:
        self.update_badge.show_version(version)

    # ---- scale (Feature 2) ---------------------------------------------
    def set_scale(self, scale: float) -> None:
        scale = max(MIN_SCALE, min(MAX_SCALE, float(scale)))
        if abs(scale - self._scale) < 1e-3:
            return
        self.scale_changed.emit(scale)
        self._apply_scale(scale)

    def _apply_scale(self, scale: float) -> None:
        """Resize the frame around its center, keeping orientation."""
        self._scale = scale
        center = self._center()
        if self._horizontal:
            w = int(self.BASE_H * scale)
            h = int(self.BASE_W * scale)
        else:
            w = int(self.BASE_W * scale)
            h = int(self.BASE_H * scale)
        self.setMinimumSize(w, h)
        self.resize(w, h)
        # re-center, clamped to screen
        pos = QPoint(center.x() - w // 2, center.y() - h // 2)
        self.move(self._clamp_to_screen(pos))
        self.update_badge.relabel_for_width()

    # ---- orientation (Feature 1) ---------------------------------------
    def _transpose_frame(self) -> None:
        """Swap the frame's width and height around its center.

        Gauges keep their size; only the row/column arrangement changes.
        """
        center = self._center()
        new_w, new_h = self.height(), self.width()
        scale = self._scale
        # Base size depends on orientation so the frame is always consistent.
        if self._horizontal:
            new_w = int(self.BASE_H * scale)
            new_h = int(self.BASE_W * scale)
        else:
            new_w = int(self.BASE_W * scale)
            new_h = int(self.BASE_H * scale)
        self.setMinimumSize(new_w, new_h)
        self.resize(new_w, new_h)
        pos = QPoint(center.x() - new_w // 2, center.y() - new_h // 2)
        self.move(self._clamp_to_screen(pos))

    def _set_orientation(self, horizontal: bool) -> None:
        """Switch tiles between the vertical and horizontal layouts and transpose
        the frame so gauges keep their size."""
        if horizontal == self._horizontal:
            return
        old = self._active_tiles_layout()
        new = self.tiles_hbox if horizontal else self.tiles_vbox
        for tile in list(self._tiles.values()):
            old.removeWidget(tile)
            new.addWidget(tile, stretch=1)
        # Install the new layout where the old one sat in the main layout.
        main = self.layout()
        for i in range(main.count()):
            item = main.itemAt(i)
            if item is old or item.layout() is old:
                main.removeItem(item)
                main.insertLayout(i, new)
                break
        self._horizontal = horizontal
        self._transpose_frame()

    def _nearest_docked_edge(self) -> str | None:
        """Return 'top'|'bottom'|'left'|'right' if the frame touches that edge."""
        geo = self.frameGeometry()
        screen = self._available_geometry()
        if geo.top() - screen.top() <= EDGE_THRESHOLD:
            return "top"
        if screen.bottom() - geo.bottom() <= EDGE_THRESHOLD:
            return "bottom"
        if geo.left() - screen.left() <= EDGE_THRESHOLD:
            return "left"
        if screen.right() - geo.right() <= EDGE_THRESHOLD:
            return "right"
        return None

    def _orient_to_edge(self, edge: str | None) -> None:
        if edge in ("top", "bottom"):
            self._set_orientation(True)
        elif edge in ("left", "right"):
            self._set_orientation(False)

    def _dock_to_edge(self, edge: str) -> None:
        """Move the frame flush against the given screen edge (clamped)."""
        screen = self._available_geometry()
        geo = self.frameGeometry()
        if edge == "top":
            pos = QPoint(geo.left(), screen.top())
        elif edge == "bottom":
            pos = QPoint(geo.left(), screen.bottom() - self.height() + 1)
        elif edge == "left":
            pos = QPoint(screen.left(), geo.top())
        else:  # right
            pos = QPoint(screen.right() - self.width() + 1, geo.top())
        self.move(self._clamp_to_screen(pos))

    # ---- auto-hide (Feature 3) -----------------------------------------
    def set_hide_on_idle(self, enabled: bool) -> None:
        self._hide_on_idle = bool(enabled)
        if self._hide_on_idle:
            self._idle_hide_timer.start()
        else:
            self._idle_hide_timer.stop()
            self._slide_in()

    def _slide_off(self) -> None:
        """Slide the widget behind the nearest screen edge, leaving ~15% as a tab."""
        if not self._hide_on_idle or self._hidden:
            return
        edge = self._nearest_docked_edge()
        if edge is None:
            edge = self._nearest_edge_by_distance()
        if edge is None:
            return
        # Flush to that edge first (the visible position), then slide mostly off.
        self._dock_to_edge(edge)
        self._docked_pos = self.pos()
        visible = max(6, int(self._frame_extent(edge) * HIDE_VISIBLE_FRACTION))
        offset = self._frame_extent(edge) - visible
        screen = self._available_geometry()
        if edge == "top":
            self.move(self.pos().x(), screen.top() - offset)
        elif edge == "bottom":
            self.move(self.pos().x(), screen.bottom() - visible + 1)
        elif edge == "left":
            self.move(screen.left() - offset, self.pos().y())
        else:  # right
            self.move(screen.right() - visible + 1, self.pos().y())
        self._hidden_anchor = edge
        self._hidden = True

    def _nearest_edge_by_distance(self) -> str | None:
        """Closest screen edge by raw distance (for mid-screen auto-hide)."""
        geo = self.frameGeometry()
        screen = self._available_geometry()
        margins = {
            "top": geo.top() - screen.top(),
            "bottom": screen.bottom() - geo.bottom(),
            "left": geo.left() - screen.left(),
            "right": screen.right() - geo.right(),
        }
        return min(margins, key=margins.get)

    def _frame_extent(self, edge: str) -> int:
        """The frame's dimension perpendicular to the given edge."""
        return self.height() if edge in ("top", "bottom") else self.width()

    def _slide_in(self) -> None:
        """Slide back to the docked (flush) position against the anchor edge."""
        if not self._hidden:
            return
        if self._docked_pos is not None:
            self.move(self._clamp_to_screen(self._docked_pos))
        elif self._hidden_anchor:
            self._dock_to_edge(self._hidden_anchor)
        self._hidden = False

    # ---- mini mode -------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ("full", "mini") else "full"
        self._apply_visibility()
        self._update_countdown()

    def set_mini_threshold(self, percent: float) -> None:
        self._mini_threshold = percent
        self._apply_visibility()
        self._update_countdown()

    def _record_activity(self, snapshots: list[ProviderSnapshot], now: float | None = None) -> None:
        now = now if now is not None else time.time()
        usage = {
            s.provider: sum(w.used_percent for w in s.windows)
            for s in snapshots
            if not s.error and not s.paused and s.windows
        }
        self._activity.append((now, usage))
        cutoff = now - 15 * 60
        self._activity = [(ts, u) for ts, u in self._activity if ts >= cutoff]

    def _activity_delta(self) -> dict[str, float]:
        if len(self._activity) < 2:
            return {}
        _, oldest = self._activity[0]
        _, newest = self._activity[-1]
        return {
            name: newest[name] - oldest[name]
            for name in newest
            if name in oldest and newest[name] > oldest[name]
        }

    def _visible_providers(self) -> set[str]:
        if self._mode != "mini":
            return set(self._tiles)
        hot = {
            s.provider
            for s in self._snapshots
            if any(w.used_percent >= self._mini_threshold for w in s.windows)
        }
        if hot:
            return hot
        delta = self._activity_delta()
        if delta:
            self._last_solo = max(delta, key=delta.get)
        if self._last_solo not in self._tiles:
            self._last_solo = next(iter(self._tiles), None)
        return {self._last_solo} if self._last_solo else set()

    def _apply_visibility(self) -> None:
        if not self._tiles:
            return
        visible = self._visible_providers()
        for name, tile in self._tiles.items():
            tile.setVisible(name in visible)

    # ---- painting -------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor(255, 255, 255, 26))
        painter.setBrush(QColor(13, 13, 13, 217))  # theme.PAGE at 85% opacity
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 12, 12)
        painter.end()

    # ---- drag to move / click to open dashboard -------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._press_pos = event.globalPosition().toPoint()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            if self._hidden:
                self._slide_in()
            target = event.globalPosition().toPoint() - self._drag_offset
            # Clamp: the widget can't leave the screen.
            clamped = self._clamp_to_screen(target)
            self.move(clamped)
            self._dragging = True
            # Live orientation: as soon as the frame touches an edge, snap to
            # that edge's orientation and dock flush to it.
            edge = self._nearest_docked_edge()
            if edge is not None:
                self._orient_to_edge(edge)
                self._dock_to_edge(edge)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            moved = 0
            if self._press_pos is not None:
                moved = (
                    event.globalPosition().toPoint() - self._press_pos
                ).manhattanLength()
            self._drag_offset = None
            self._press_pos = None
            if not self._dragging and moved < CLICK_THRESHOLD:
                self.dashboard_requested.emit()
            self._dragging = False
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        version = QAction(f"AIBar v{__version__}", menu)
        version.setEnabled(False)
        menu.addAction(version)
        menu.addSeparator()
        refresh = QAction("Обновить", menu)
        refresh.triggered.connect(self.refresh_requested)
        mini = QAction("Мини-режим (только у лимита)", menu, checkable=True)
        mini.setChecked(self._mode == "mini")
        mini.toggled.connect(
            lambda on: self.mode_changed.emit("mini" if on else "full")
        )
        settings = QAction("Настройки…", menu)
        settings.triggered.connect(self.settings_requested)
        help_action = QAction("Справка", menu)
        help_action.triggered.connect(self.help_requested)
        hide_idle = QAction("Скрывать виджет при простое", menu, checkable=True)
        hide_idle.setChecked(self._hide_on_idle)
        hide_idle.toggled.connect(self.hide_on_idle_changed)

        # Scale submenu with a slider 50-150%.
        scale_menu = menu.addMenu("Масштаб")
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(int(MIN_SCALE * 100))
        slider.setMaximum(int(MAX_SCALE * 100))
        slider.setValue(int(self._scale * 100))
        slider.setTickInterval(10)
        slider.setTickPosition(QSlider.TicksBelow)
        pct_label = QLabel(f"{int(self._scale * 100)}%")
        slider.valueChanged.connect(
            lambda v: pct_label.setText(f"{v}%")
        )
        slider.sliderReleased.connect(
            lambda: self.set_scale(slider.value() / 100.0)
        )
        scale_row = QHBoxLayout()
        scale_row.setContentsMargins(8, 6, 8, 6)
        scale_row.addWidget(slider)
        scale_row.addWidget(pct_label)
        scale_widget = QWidget(scale_menu)
        scale_widget.setLayout(scale_row)
        scale_action = QWidgetAction(scale_menu)
        scale_action.setDefaultWidget(scale_widget)
        scale_menu.addAction(scale_action)

        check_updates = QAction("Проверить обновления…", menu)
        check_updates.triggered.connect(self.check_updates_requested)
        hide = QAction("Скрыть виджет (остаётся в трее)", menu)
        hide.triggered.connect(self.hide_requested)
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(refresh)
        menu.addAction(mini)
        menu.addAction(scale_menu.menuAction())
        menu.addAction(settings)
        menu.addAction(help_action)
        menu.addAction(hide_idle)
        menu.addAction(check_updates)
        menu.addAction(hide)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._idle_hide_timer.stop()
        menu.aboutToHide.connect(self._maybe_restart_idle_timer)
        self._context_menu = menu
        menu.popup(event.globalPos())

    def _maybe_restart_idle_timer(self) -> None:
        if self._hide_on_idle and not self._hidden:
            self._idle_hide_timer.start()

    # ---- hover: drives only the auto-hide timer -------------------------
    def enterEvent(self, event) -> None:
        self._idle_hide_timer.stop()
        if self._hidden:
            self._slide_in()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hide_on_idle:
            self._idle_hide_timer.start()
        super().leaveEvent(event)

    # ---- geometry persistence -------------------------------------------
    def moveEvent(self, event) -> None:
        if not self._hidden:
            self._geometry_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._geometry_timer.start()
        self.update_badge.relabel_for_width()
        super().resizeEvent(event)

    def hideEvent(self, event) -> None:
        self._idle_hide_timer.stop()
        super().hideEvent(event)
