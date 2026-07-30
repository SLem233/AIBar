"""Always-on-top desktop widget: a row or column of per-provider radial gauges.

Behaviour:
- Orientation is live: while dragging the centre of the widget, as soon as the
  frame touches a screen edge it snaps to that edge's orientation (vertical at
  left/right, horizontal at top/bottom) and is clamped to the screen.
- On an orientation change the *frame* itself transposes (w <-> h) around its
  center, so the gauges keep their size; only their layout (row vs column)
  changes.
- Resize: the frame can be resized by dragging any of its four edges or four
  corners (hit-tested against a thin margin). The centre area drags the widget.
- Left-click on the centre opens the full dashboard popup; right-click opens
  the menu.
- "Скрывать виджет": when enabled, a polling timer watches the real cursor
  position; after a short idle the widget slides off the nearest screen edge,
  leaving ~15% of its frame as a "bookmark" tab; bringing the cursor over that
  tab slides it back and docks it flush to the edge.
- The soonest-reset countdown across visible tiles is shown on the widget.
"""

import time
from datetime import datetime, timezone

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
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
# Fraction of the frame left poking out when auto-hidden.
HIDE_VISIBLE_FRACTION = 0.15
# Grace period (s) after the cursor leaves before hiding kicks in.
HIDE_DELAY = 0.4
# Auto-hide slide animation duration (ms).
HIDE_ANIM_MS = 1000
# How often the auto-hide poller checks the cursor position (ms).
HIDE_POLL_MS = 350
# A mouse move shorter than this (px) is treated as a click, not a drag.
CLICK_THRESHOLD = 4
# Resize hit-test margin (px from each edge) and minimum frame size.
RESIZE_MARGIN = 8
MIN_W = 80
MIN_H = 80
DEFAULT_W = 120
DEFAULT_H = 260


def _widget_countdown(windows, now=None) -> str:
    """Two-unit countdown to the soonest window reset, shown on a tile."""
    now = now if now is not None else datetime.now(timezone.utc)
    soonest = min(
        (w for w in windows if w.resets_at),
        key=lambda w: w.resets_at,
        default=None,
    )
    if soonest is None:
        return ""
    delta = soonest.resets_at - now
    total = int(delta.total_seconds())
    if total <= 0:
        return "0м"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


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
        # Countdown to the next window reset, shown on its own line below the name.
        self.countdown_label = QLabel("")
        self.countdown_label.setAlignment(Qt.AlignHCenter)
        self.countdown_label.setStyleSheet(
            f'color: {theme.TEXT_MUTED}; font-family: "{theme.FONT_FAMILY}"; font-size: 10px;'
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.gauge, stretch=1)
        layout.addWidget(self.caption)
        layout.addWidget(self.countdown_label)

    def update_snapshot(self, snap: ProviderSnapshot) -> None:
        self.gauge.set_percents([w.used_percent for w in snap.windows])
        suffix = " ⏸" if snap.paused else (" ⚠" if snap.error else "")
        self.caption.setText(f"{snap.provider}{suffix}")
        countdown = _widget_countdown(snap.windows)
        self.countdown_label.setText(countdown)
        self.countdown_label.setVisible(bool(countdown))

    def resizeEvent(self, event) -> None:
        # captions don't fit below ~60px — the tooltip still names the provider
        narrow = self.width() < 60
        self.caption.setVisible(not narrow)
        if narrow:
            self.countdown_label.hide()
        else:
            # keep the countdown's own visibility rule (only if there is one)
            self.countdown_label.setVisible(bool(self.countdown_label.text()))
        self.setToolTip(self.caption.text() if narrow else "")
        super().resizeEvent(event)


class DesktopWidget(QWidget):
    """The floating always-on-top row/column of gauges."""

    geometry_changed = Signal()
    refresh_requested = Signal()
    settings_requested = Signal()
    help_requested = Signal()
    hide_requested = Signal()  # hide the widget entirely (stays in tray)
    hide_on_idle_changed = Signal(bool)
    check_updates_requested = Signal()
    quit_requested = Signal()
    mode_changed = Signal(str)  # "full" | "mini"
    dashboard_requested = Signal()  # left-click: show the full dashboard

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(WIDGET_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(MIN_W, MIN_H)
        self.resize(DEFAULT_W, DEFAULT_H)
        # Track mouse even without a button held, so we can set the resize
        # cursor when hovering an edge.
        self.setMouseTracking(True)

        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None  # to tell click from drag
        self._dragging = False
        # Edge-resize state.
        self._resize_edge: str = ""  # any combination of 'l','r','t','b'
        self._resize_start_geo: QRect | None = None
        self._resize_start_global: QPoint | None = None

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

        self.update_badge = UpdateBadge(self)
        self.vpn_badge = VpnBadge(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 2)
        layout.addWidget(self.update_badge)
        layout.addWidget(self.vpn_badge)
        layout.addLayout(self.tiles_vbox, stretch=1)  # vertical is active

        # Auto-hide state (polling-based for reliability on translucent windows).
        self._hide_on_idle = False
        self._hidden = False
        self._hidden_anchor: str | None = None  # which edge we slid behind
        self._docked_pos: QPoint | None = None  # flush-to-edge visible position
        self._last_active: float = time.time()
        self._idle_check_timer = QTimer(self)
        self._idle_check_timer.setInterval(HIDE_POLL_MS)
        self._idle_check_timer.timeout.connect(self._on_idle_check)
        # Smooth slide animation for hide/show (animates pos()).
        self._slide_anim: QPropertyAnimation | None = None

        # Debounced geometry persistence.
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(800)
        self._geometry_timer.timeout.connect(self.geometry_changed)

    # ---- helpers --------------------------------------------------------
    def _available_geometry(self) -> QRect:
        return self.screen().availableGeometry()

    def _clamp_to_screen(self, pos: QPoint) -> QPoint:
        """Keep the whole frame inside the screen."""
        screen = self._available_geometry()
        x = min(max(pos.x(), screen.left()), screen.right() - self.width() + 1)
        y = min(max(pos.y(), screen.top()), screen.bottom() - self.height() + 1)
        return QPoint(x, y)

    def _cursor_inside(self) -> bool:
        """True if the mouse cursor is actually over the widget's frame.

        Used to gate auto-hide: enter/leave events on a frameless translucent
        window are unreliable, so we check the real cursor position instead.
        """
        geo = self.frameGeometry()
        if geo.isNull() or geo.width() <= 0 or geo.height() <= 0:
            return False
        return geo.contains(QCursor.pos())

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

    # ---- orientation ----------------------------------------------------
    def _transpose_frame(self) -> None:
        """Swap the frame's width and height around its center.

        Gauges keep their size; only the row/column arrangement changes.
        """
        center = self._center()
        new_w, new_h = self.height(), self.width()
        self.setMinimumSize(min(MIN_W, new_w), min(MIN_H, new_h))
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

    # ---- auto-hide ------------------------------------------------------
    def set_hide_on_idle(self, enabled: bool) -> None:
        self._hide_on_idle = bool(enabled)
        if self._hide_on_idle:
            self._last_active = time.time()
            self._idle_check_timer.start()
        else:
            self._idle_check_timer.stop()
            # Option off: the widget MUST be fully visible again.
            self._slide_in()

    def _stop_slide_anim(self) -> None:
        if self._slide_anim is not None:
            self._slide_anim.stop()
            self._slide_anim = None

    def _animate_to(self, target: QPoint) -> None:
        """Smoothly animate the window position to ``target`` over HIDE_ANIM_MS."""
        self._stop_slide_anim()
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(HIDE_ANIM_MS)
        anim.setStartValue(self.pos())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._slide_anim = anim
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _hide_target(self, edge: str) -> QPoint:
        """Position where the widget sits ~15% poking out behind ``edge``."""
        visible = max(6, int(self._frame_extent(edge) * HIDE_VISIBLE_FRACTION))
        offset = self._frame_extent(edge) - visible
        screen = self._available_geometry()
        x, y = self.pos().x(), self.pos().y()
        if edge == "top":
            y = screen.top() - offset
        elif edge == "bottom":
            y = screen.bottom() - visible + 1
        elif edge == "left":
            x = screen.left() - offset
        else:  # right
            x = screen.right() - visible + 1
        return QPoint(x, y)

    def _slide_off(self) -> None:
        """Slide the widget behind the nearest screen edge, leaving ~15% as a tab."""
        if not self._hide_on_idle or self._hidden:
            return
        # Refuse while the cursor is genuinely over the widget (the poller
        # retries shortly).
        if self._cursor_inside():
            return
        edge = self._nearest_docked_edge()
        if edge is None:
            edge = self._nearest_edge_by_distance()
        if edge is None:
            return
        # Flush to that edge first (the visible position), then animate off.
        # Mark hidden BEFORE animating so moveEvent won't persist the off-screen
        # frames; the docked (visible) position is what we restore later.
        self._dock_to_edge(edge)
        self._docked_pos = self.pos()
        self._hidden_anchor = edge
        self._hidden = True
        self._animate_to(self._hide_target(edge))

    def _on_idle_check(self) -> None:
        """Polling-based auto-hide watcher.

        enter/leave events on a translucent frameless window are unreliable, so
        this checks the real cursor position on a timer and drives the hide/show
        transitions deterministically.
        """
        if not self._hide_on_idle:
            return
        if self._cursor_inside():
            self._last_active = time.time()
            if self._hidden:
                self._slide_in()
        else:
            if not self._hidden and (time.time() - self._last_active) >= HIDE_DELAY:
                self._slide_off()

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
            target = self._clamp_to_screen(self._docked_pos)
        elif self._hidden_anchor:
            self._dock_to_edge(self._hidden_anchor)
            target = self.pos()
        else:
            self._hidden = False
            return
        self._animate_to(target)
        self._hidden = False

    def ensure_visible(self) -> None:
        """Force the widget fully on-screen and not hidden (call at startup).

        Guards against a stale partially-hidden geometry left from a prior run.
        """
        self._stop_slide_anim()
        self._hidden = False
        self._hidden_anchor = None
        # If the stored position is off-screen, pull it back inside.
        self.move(self._clamp_to_screen(self.pos()))

    # ---- mini mode -------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ("full", "mini") else "full"
        self._apply_visibility()

    def set_mini_threshold(self, percent: float) -> None:
        self._mini_threshold = percent
        self._apply_visibility()

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

    # ---- resize (all four edges and corners) ----------------------------
    def _hit_test(self, local: QPoint) -> str:
        """Return the edge(s) under a local point: a string of 'l','r','t','b'.

        Empty string means the point is in the drag/click area.
        """
        on_l = local.x() <= RESIZE_MARGIN
        on_r = local.x() >= self.width() - 1 - RESIZE_MARGIN
        on_t = local.y() <= RESIZE_MARGIN
        on_b = local.y() >= self.height() - 1 - RESIZE_MARGIN
        edge = ""
        if on_t:
            edge += "t"
        if on_b:
            edge += "b"
        if on_l:
            edge += "l"
        if on_r:
            edge += "r"
        return edge

    @staticmethod
    def _cursor_for_edge(edge: str) -> Qt.CursorShape:
        # Corners first.
        if edge in ("tl", "br"):
            return Qt.SizeFDiagCursor
        if edge in ("tr", "bl"):
            return Qt.SizeBDiagCursor
        if edge in ("l", "r"):
            return Qt.SizeHorCursor
        if edge in ("t", "b"):
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def _update_hover_cursor(self, local: QPoint) -> None:
        edge = self._hit_test(local)
        self.setCursor(self._cursor_for_edge(edge))

    def _do_resize(self, global_pos: QPoint) -> None:
        g = self._resize_start_geo
        start = self._resize_start_global
        edge = self._resize_edge
        if g is None or start is None or not edge:
            return
        dg = global_pos - start
        left, top = g.left(), g.top()
        right, bottom = g.right() + 1, g.bottom() + 1
        if "l" in edge:
            left = min(g.left() + dg.x(), right - MIN_W)
        if "r" in edge:
            right = max(g.right() + 1 + dg.x(), left + MIN_W)
        if "t" in edge:
            top = min(g.top() + dg.y(), bottom - MIN_H)
        if "b" in edge:
            bottom = max(g.bottom() + 1 + dg.y(), top + MIN_H)
        new_w = right - left
        new_h = bottom - top
        # Keep the whole frame on-screen.
        screen = self._available_geometry()
        left = max(screen.left(), left)
        top = max(screen.top(), top)
        if left + new_w - 1 > screen.right():
            left = screen.right() - new_w + 1
        if top + new_h - 1 > screen.bottom():
            top = screen.bottom() - new_h + 1
        self.setGeometry(left, top, new_w, new_h)

    # ---- drag to move / click to open dashboard -------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._stop_slide_anim()
            local = event.position().toPoint()
            gpos = event.globalPosition().toPoint()
            edge = self._hit_test(local)
            if edge:
                # Resize from this edge/corner.
                self._resize_edge = edge
                self._resize_start_geo = self.geometry()
                self._resize_start_global = gpos
                self._dragging = False
                self._drag_offset = None
                self._press_pos = None
            else:
                self._drag_offset = gpos - self.frameGeometry().topLeft()
                self._press_pos = gpos
                self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.LeftButton):
            # Hover: just keep the resize cursor in sync.
            self._update_hover_cursor(event.position().toPoint())
            super().mouseMoveEvent(event)
            return

        gpos = event.globalPosition().toPoint()
        if self._resize_edge:
            self._do_resize(gpos)
            super().mouseMoveEvent(event)
            return

        if self._drag_offset is not None:
            # While dragging the widget is fully visible (no slide animation).
            if self._hidden:
                self._hidden = False
                self._hidden_anchor = None
            target = gpos - self._drag_offset
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
            resizing = bool(self._resize_edge)
            self._drag_offset = None
            self._press_pos = None
            self._resize_edge = ""
            self._resize_start_geo = None
            self._resize_start_global = None
            if not resizing and not self._dragging and moved < CLICK_THRESHOLD:
                self.dashboard_requested.emit()
            self._dragging = False
            # Refresh idle baseline after any interaction.
            if self._hide_on_idle:
                self._last_active = time.time()
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

        check_updates = QAction("Проверить обновления…", menu)
        check_updates.triggered.connect(self.check_updates_requested)
        hide = QAction("Скрыть виджет (остаётся в трее)", menu)
        hide.triggered.connect(self.hide_requested)
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(refresh)
        menu.addAction(mini)
        menu.addAction(settings)
        menu.addAction(help_action)
        menu.addAction(hide_idle)
        menu.addAction(check_updates)
        menu.addAction(hide)
        menu.addSeparator()
        menu.addAction(quit_action)
        if self._hide_on_idle:
            self._last_active = time.time()
        self._context_menu = menu
        menu.popup(event.globalPos())

    # ---- hover: drives only the auto-hide baseline ----------------------
    def enterEvent(self, event) -> None:
        # Mark active immediately so the poller won't hide on a stray leave.
        self._last_active = time.time()
        if self._hidden:
            self._slide_in()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        # The poller (not this event) decides when to hide; just note the time.
        super().leaveEvent(event)

    # ---- geometry persistence -------------------------------------------
    def moveEvent(self, event) -> None:
        # Don't persist positions while hidden or while a slide animation is
        # running (those are transient off-screen frames).
        if not self._hidden and self._slide_anim is None:
            self._geometry_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._geometry_timer.start()
        self.update_badge.relabel_for_width()
        super().resizeEvent(event)

    def hideEvent(self, event) -> None:
        self._idle_check_timer.stop()
        super().hideEvent(event)
