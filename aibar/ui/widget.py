"""Always-on-top desktop widget: a row or column of per-provider radial gauges.

Draggable anywhere, resizable via the bottom-right grip. Orientation is
derived from the nearest screen edge after a drag (vertical at the left/right
edge, horizontal at the top/bottom edge). Left-click opens the full
dashboard popup; right-click opens the settings menu.
"""

import time
from datetime import datetime

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizeGrip,
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

# Orientation: distance (px) from a screen edge at which the widget is
# considered "docked" to that edge.
EDGE_THRESHOLD = 12
# Auto-hide: visible sliver (px) left poking out when the widget slides off.
HIDE_SLIVER = 10
# Auto-hide delay (ms) after the mouse leaves the widget.
HIDE_DELAY = 5000
# A mouse move shorter than this (px) is treated as a click, not a drag.
CLICK_THRESHOLD = 4


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
    check_updates_requested = Signal()
    quit_requested = Signal()
    mode_changed = Signal(str)  # "full" | "mini"
    dashboard_requested = Signal()  # left-click: show the full dashboard

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(WIDGET_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(45, 60)
        self.resize(120, 260)

        self._drag_offset: QPoint | None = None
        self._press_pos: QPoint | None = None  # to tell click from drag
        self._tiles: dict[str, GaugeTile] = {}
        self._context_menu: QMenu | None = None
        self._snapshots: list[ProviderSnapshot] = []
        self._mode = "full"  # full | mini
        self._mini_threshold = 70.0
        # (timestamp, {provider: summed window percents}) for the last 15 min
        self._activity: list[tuple[float, dict[str, float]]] = []
        self._last_solo: str | None = None

        # Tiles live in a container whose layout we swap between vertical and
        # horizontal. Both layouts are kept as references so tests can inspect
        # either; only the active one is installed on the container.
        self.tiles_vbox = QVBoxLayout()
        self.tiles_vbox.setSpacing(6)
        self.tiles_hbox = QHBoxLayout()
        self.tiles_hbox.setSpacing(6)
        self._horizontal = False

        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent; width: 14px; height: 14px;")

        self.update_badge = UpdateBadge(self)
        self.vpn_badge = VpnBadge(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 2)
        layout.addWidget(self.update_badge)
        layout.addWidget(self.vpn_badge)
        # tiles_vbox is the active layout by default (vertical).
        layout.addLayout(self.tiles_vbox, stretch=1)
        layout.addWidget(grip, alignment=Qt.AlignBottom | Qt.AlignRight)

        # Auto-hide state (Feature 2): off unless hide_on_idle is enabled.
        self._hide_on_idle = False
        self._hidden = False
        self._pre_hide_pos: QPoint | None = None
        self._idle_hide_timer = QTimer(self)
        self._idle_hide_timer.setSingleShot(True)
        self._idle_hide_timer.setInterval(HIDE_DELAY)
        self._idle_hide_timer.timeout.connect(self._slide_off)

        # Debounced geometry persistence
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(800)
        self._geometry_timer.timeout.connect(self.geometry_changed)

    # ---- data -----------------------------------------------------------
    def update_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        for snap in snapshots:
            tile = self._tiles.get(snap.provider)
            if tile is None:
                tile = GaugeTile(snap.provider)
                self._tiles[snap.provider] = tile
                self._add_tile_to_active_layout(tile)
            tile.update_snapshot(snap)
        self._snapshots = snapshots
        self.vpn_badge.setVisible(any(s.paused for s in snapshots))
        self._record_activity(snapshots)
        self._apply_visibility()

    def _add_tile_to_active_layout(self, tile: GaugeTile, stretch: int = 1) -> None:
        layout = self.tiles_hbox if self._horizontal else self.tiles_vbox
        layout.addWidget(tile, stretch=stretch)

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

    # ---- orientation (Feature 1) ----------------------------------------
    def _active_tiles_layout(self):
        """The layout currently installed in the widget (vertical by default)."""
        return self.tiles_hbox if self._horizontal else self.tiles_vbox

    def _set_orientation(self, horizontal: bool) -> None:
        """Switch tiles between the vertical and horizontal layouts.

        Both layouts are kept as instance attributes; the one matching the new
        orientation becomes the *active* (installed) layout and owns the tiles.
        """
        if horizontal == self._horizontal:
            return
        old = self._active_tiles_layout()
        new = self.tiles_hbox if horizontal else self.tiles_vbox
        # Detach every tile widget from the old layout, then attach to the new.
        tiles = list(self._tiles.values())
        for tile in tiles:
            old.removeWidget(tile)
            new.addWidget(tile, stretch=1)
        # Swap which layout is installed on the widget's main layout. We find
        # the index where the old tile-layout sat and replace it with the new.
        main = self.layout()
        replaced = False
        for i in range(main.count()):
            item = main.itemAt(i)
            if item is old or item.layout() is old:
                main.removeItem(item)
                main.insertLayout(i, new)
                replaced = True
                break
        if not replaced:
            # Fallback: just append (shouldn't normally happen).
            main.addLayout(new)
        self._horizontal = horizontal
        # Reshape the minimum size sensibly per orientation.
        if horizontal:
            self.setMinimumSize(60, 45)
        else:
            self.setMinimumSize(45, 60)

    def _edge_orientation(self) -> bool | None:
        """Return desired horizontal flag from the nearest edge, or None if free."""
        screen = self.screen().availableGeometry()
        geo = self.frameGeometry()
        if geo.top() - screen.top() <= EDGE_THRESHOLD:
            return True  # top edge -> horizontal
        if screen.bottom() - geo.bottom() <= EDGE_THRESHOLD:
            return True  # bottom edge -> horizontal
        if geo.left() - screen.left() <= EDGE_THRESHOLD:
            return False  # left edge -> vertical
        if screen.right() - geo.right() <= EDGE_THRESHOLD:
            return False  # right edge -> vertical
        return None  # mid-screen: keep current

    def _apply_edge_orientation(self) -> None:
        desired = self._edge_orientation()
        if desired is not None:
            self._set_orientation(desired)

    # ---- auto-hide (Feature 2) ------------------------------------------
    def set_hide_on_idle(self, enabled: bool) -> None:
        self._hide_on_idle = bool(enabled)
        if self._hide_on_idle:
            self._idle_hide_timer.start()  # begin the countdown now
        else:
            self._idle_hide_timer.stop()
            self._slide_in()  # make sure it's fully visible again

    def _nearest_edge_offset(self) -> tuple[int, int, str]:
        """Return (dx, dy, which) to slide the widget off its nearest edge."""
        screen = self.screen().availableGeometry()
        geo = self.frameGeometry()
        margins = {
            "top": geo.top() - screen.top(),
            "bottom": screen.bottom() - geo.bottom(),
            "left": geo.left() - screen.left(),
            "right": screen.right() - geo.right(),
        }
        which = min(margins, key=margins.get)
        w, h = geo.width(), geo.height()
        if which == "top":
            return (0, -(h - HIDE_SLIVER), "top")
        if which == "bottom":
            return (0, h - HIDE_SLIVER, "bottom")
        if which == "left":
            return (-(w - HIDE_SLIVER), 0, "left")
        return (w - HIDE_SLIVER, 0, "right")

    def _slide_off(self) -> None:
        if not self._hide_on_idle or self._hidden:
            return
        dx, dy, _ = self._nearest_edge_offset()
        self._pre_hide_pos = self.pos()
        self.move(self.pos().x() + dx, self.pos().y() + dy)
        self._hidden = True

    def _slide_in(self) -> None:
        if not self._hidden:
            return
        if self._pre_hide_pos is not None:
            self.move(self._pre_hide_pos)
        self._hidden = False

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
        """Positive usage growth per provider over the retained window."""
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
        # nobody is close to the limit — show the most recently active provider
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
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            # If it was hidden (auto-hide), reveal it at the cursor as we drag.
            if self._hidden:
                self._slide_in()
            self.move(event.globalPosition().toPoint() - self._drag_offset)
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
            # A press-release with negligible movement is a click -> dashboard.
            if moved < CLICK_THRESHOLD:
                self.dashboard_requested.emit()
            else:
                # Reorient after a real drag based on the nearest screen edge.
                self._apply_edge_orientation()
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
        # While the context menu is open, don't let the widget auto-hide.
        self._idle_hide_timer.stop()
        menu.aboutToHide.connect(self._maybe_restart_idle_timer)
        self._context_menu = menu  # keep alive: popup() is non-blocking
        menu.popup(event.globalPos())

    def _maybe_restart_idle_timer(self) -> None:
        if self._hide_on_idle and not self._hidden:
            self._idle_hide_timer.start()

    # ---- hover: now only drives the auto-hide timer ---------------------
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
        if not self._hidden:  # don't persist the off-screen slide position
            self._geometry_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._geometry_timer.start()
        self.update_badge.relabel_for_width()
        super().resizeEvent(event)

    def hideEvent(self, event) -> None:
        self._idle_hide_timer.stop()
        super().hideEvent(event)
