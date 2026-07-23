"""Always-on-top desktop widget: a column of per-provider radial gauges.

Draggable anywhere, resizable via the bottom-right grip; hovering shows a
panel with the full per-provider breakdown next to the widget.
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


class HoverPanel(QWidget):
    """Extended info shown while the mouse is over the widget (or the panel)."""

    hover_changed = Signal(bool)
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(WIDGET_FLAGS)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(400)
        self.setStyleSheet(
            f"""
            QLabel {{ color: {theme.TEXT_SECONDARY}; font-family: "{theme.FONT_FAMILY}"; }}
            #card {{
                background: {theme.SURFACE};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
            }}
            #cardTitle {{ color: {theme.TEXT_PRIMARY}; font-size: 15px; font-weight: 600; }}
            #cardPlan {{ color: {theme.TEXT_MUTED}; font-size: 12px; }}
            #header {{ color: {theme.TEXT_PRIMARY}; font-size: 14px; font-weight: 600; }}
            #footer {{ color: {theme.TEXT_MUTED}; font-size: 11px; }}
            QPushButton {{
                background: {theme.SURFACE};
                color: {theme.TEXT_SECONDARY};
                border: 1px solid {theme.BORDER};
                border-radius: 6px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; border-color: {theme.TEXT_MUTED}; }}
            """
        )
        self._cards: dict[str, ProviderCard] = {}
        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(8)
        self.footer = QLabel("")
        self.footer.setObjectName("footer")

        header = QLabel("Лимиты AI-провайдеров")
        header.setObjectName("header")
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh_requested)
        top = QHBoxLayout()
        top.addWidget(header)
        top.addStretch()
        top.addWidget(self.refresh_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addLayout(self.cards_layout)
        layout.addWidget(self.footer)

    def update_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        for snap in snapshots:
            card = self._cards.get(snap.provider)
            if card is None:
                card = ProviderCard()
                self._cards[snap.provider] = card
                self.cards_layout.addWidget(card)
            card.update_snapshot(snap)
        self.footer.setText(
            f"AIBar v{__version__} · Обновлено {datetime.now().strftime('%H:%M:%S')}"
        )
        self.adjustSize()

    def clear_cards(self) -> None:
        for card in self._cards.values():
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    # Keep the panel open while the mouse is over it
    def enterEvent(self, event) -> None:
        self.hover_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_changed.emit(False)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        # Translucent frameless window: paint the rounded surface manually.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor(255, 255, 255, 26))
        painter.setBrush(QColor(theme.PAGE))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)
        painter.end()


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
    """The floating always-on-top column of gauges."""

    geometry_changed = Signal()
    refresh_requested = Signal()
    settings_requested = Signal()
    stats_requested = Signal()
    help_requested = Signal()
    hide_requested = Signal()
    quit_requested = Signal()
    mode_changed = Signal(str)  # "full" | "mini"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(WIDGET_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(45, 60)
        self.resize(120, 260)

        self._drag_offset: QPoint | None = None
        self._tiles: dict[str, GaugeTile] = {}
        self._context_menu: QMenu | None = None
        self._snapshots: list[ProviderSnapshot] = []
        self._mode = "full"  # full | mini
        self._mini_threshold = 70.0
        # (timestamp, {provider: summed window percents}) for the last 15 min
        self._activity: list[tuple[float, dict[str, float]]] = []
        self._last_solo: str | None = None

        self.tiles_layout = QVBoxLayout()
        self.tiles_layout.setSpacing(6)

        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent; width: 14px; height: 14px;")

        self.update_badge = UpdateBadge(self)
        self.vpn_badge = VpnBadge(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 2)
        layout.addWidget(self.update_badge)
        layout.addWidget(self.vpn_badge)
        layout.addLayout(self.tiles_layout, stretch=1)
        layout.addWidget(grip, alignment=Qt.AlignBottom | Qt.AlignRight)

        self.panel = HoverPanel()
        self.panel.hover_changed.connect(self._on_panel_hover)
        self.panel.refresh_requested.connect(self.refresh_requested)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(350)
        self._hide_timer.timeout.connect(self.panel.hide)

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
                self.tiles_layout.addWidget(tile, stretch=1)
            tile.update_snapshot(snap)
        self._snapshots = snapshots
        self.vpn_badge.setVisible(any(s.paused for s in snapshots))
        self._record_activity(snapshots)
        self._apply_visibility()
        # the hover panel always shows every provider, regardless of mode
        self.panel.update_snapshots(snapshots)

    def clear_tiles(self) -> None:
        for tile in self._tiles.values():
            self.tiles_layout.removeWidget(tile)
            tile.deleteLater()
        self._tiles.clear()
        self._activity.clear()
        self._last_solo = None
        self.panel.clear_cards()

    def set_update_available(self, version: str) -> None:
        self.update_badge.show_version(version)

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

    # ---- drag to move ---------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._reposition_panel()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
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
        stats = QAction("Статистика", menu)
        stats.triggered.connect(self.stats_requested)
        settings = QAction("Настройки…", menu)
        settings.triggered.connect(self.settings_requested)
        help_action = QAction("Справка", menu)
        help_action.triggered.connect(self.help_requested)
        hide = QAction("Скрыть виджет (остаётся в трее)", menu)
        hide.triggered.connect(self.hide_requested)
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(refresh)
        menu.addAction(mini)
        menu.addAction(stats)
        menu.addAction(settings)
        menu.addAction(help_action)
        menu.addAction(hide)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._context_menu = menu  # keep alive: popup() is non-blocking
        menu.popup(event.globalPos())

    # ---- hover panel ----------------------------------------------------
    def enterEvent(self, event) -> None:
        self._hide_timer.stop()
        self._reposition_panel()
        self.panel.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hide_timer.start()
        super().leaveEvent(event)

    def _on_panel_hover(self, inside: bool) -> None:
        if inside:
            self._hide_timer.stop()
        else:
            self._hide_timer.start()

    def _reposition_panel(self) -> None:
        self.panel.adjustSize()
        screen = self.screen().availableGeometry()
        geo = self.frameGeometry()
        x = geo.left() - self.panel.width() - 8
        if x < screen.left() + 8:  # no room on the left — open to the right
            x = geo.right() + 8
        y = min(geo.top(), screen.bottom() - self.panel.height() - 8)
        y = max(y, screen.top() + 8)
        self.panel.move(x, y)

    # ---- geometry persistence -------------------------------------------
    def moveEvent(self, event) -> None:
        self._geometry_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._geometry_timer.start()
        self.update_badge.relabel_for_width()
        super().resizeEvent(event)

    def hideEvent(self, event) -> None:
        self.panel.hide()
        super().hideEvent(event)
