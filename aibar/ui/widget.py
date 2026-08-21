"""Always-on-top desktop widget: a row or column of per-provider radial gauges.

Виджет таскается за середину и меняет размер за любую сторону и любой угол
рамки. Ориентация подстраивается под край экрана: у левого и правого края это
колонка колец, у верхнего и нижнего — ряд. Наведение показывает панель с полной
разбивкой по провайдерам, а прижатый к краю виджет умеет сворачиваться туда за
ненадобностью — см. edge_collapse.
"""

import time

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QMenu, QVBoxLayout, QWidget

from .. import __version__
from ..providers.base import ProviderSnapshot
from .badges import UpdateBadge, VpnBadge
from .edge_collapse import EdgeCollapse, docked_edge, flush_pos
from .hover_panel import WIDGET_FLAGS, HoverPanel
from .tiles import GaugeTile, soonest_countdown
from .topmost import TopmostKeeper

# Полоса у края рамки, за которую её тянут; центр остаётся зоной перетаскивания.
RESIZE_MARGIN = 8
# Поля кадра равны полосе захвата: меньше нельзя (полоса уедет на плитки),
# больше — зря съедает ширину, из-за которой подписи пропадают раньше времени.
FRAME_MARGIN = RESIZE_MARGIN

__all__ = [
    "DesktopWidget",
    "GaugeTile",
    "HoverPanel",
    "UpdateBadge",
    "VpnBadge",
    "soonest_countdown",
]


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
    autohide_changed = Signal(bool)  # сворачивать ли виджет у края экрана

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(WIDGET_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(45, 60)
        self.resize(120, 260)
        # Курсор над рамкой должен меняться и без нажатой кнопки.
        self.setMouseTracking(True)

        self._drag_offset: QPoint | None = None
        # Изменение размера за рамку: какие стороны схвачены и с чего начали.
        self._resize_edge = ""  # сочетание из 'l', 'r', 't', 'b'
        self._resize_start_geo: QRect | None = None
        self._resize_start_global: QPoint | None = None
        self._tiles: dict[str, GaugeTile] = {}
        self._context_menu: QMenu | None = None
        self._snapshots: list[ProviderSnapshot] = []
        self._mode = "full"  # full | mini
        self._mini_threshold = 70.0
        self._horizontal = False  # колонка; ряд — у верхнего и нижнего края
        # (timestamp, {provider: summed window percents}) for the last 15 min
        self._activity: list[tuple[float, dict[str, float]]] = []
        self._last_solo: str | None = None

        # Плитки живут в одном из двух layout'ов — активный зависит от края.
        self.tiles_vbox = QVBoxLayout()
        self.tiles_vbox.setSpacing(6)
        self.tiles_hbox = QHBoxLayout()
        self.tiles_hbox.setSpacing(6)

        self.update_badge = UpdateBadge(self)
        self.vpn_badge = VpnBadge(self)

        layout = QVBoxLayout(self)
        # Поля со всех сторон одинаковые: за них рамку и тянут, дети сюда не лезут.
        layout.setContentsMargins(
            FRAME_MARGIN, FRAME_MARGIN, FRAME_MARGIN, FRAME_MARGIN
        )
        layout.addWidget(self.update_badge)
        layout.addWidget(self.vpn_badge)
        layout.addLayout(self.tiles_vbox, stretch=1)

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

        # Сворачивание за край экрана (по умолчанию выключено)
        self.collapse = EdgeCollapse(self, keep_open=self._panel_holds_cursor)
        self.collapse.collapsed.connect(self._on_collapsed)
        self.collapse.expanded.connect(self._on_expanded)

        # Флага «поверх всех» хватает до первого чужого окна в верхнем слое —
        # дальше порядок возвращаем сами, иначе полоску свёрнутого виджета
        # накрывает и развернуть его нечем. Панель поднимается после виджета.
        self.topmost = TopmostKeeper(self, self.panel)
        self.collapse.collapsed.connect(self.topmost.reassert)

    # ---- data -----------------------------------------------------------
    def update_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        for snap in snapshots:
            tile = self._tiles.get(snap.provider)
            if tile is None:
                tile = GaugeTile(snap.provider)
                self._tiles[snap.provider] = tile
                self._tiles_layout().addWidget(tile, stretch=1)
            tile.update_snapshot(snap)
        self._snapshots = snapshots
        self.vpn_badge.setVisible(any(s.paused for s in snapshots))
        self._record_activity(snapshots)
        self._apply_visibility()
        # the hover panel always shows every provider, regardless of mode
        self.panel.update_snapshots(snapshots)

    def clear_tiles(self) -> None:
        for tile in self._tiles.values():
            self.tiles_vbox.removeWidget(tile)
            self.tiles_hbox.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()
        self._activity.clear()
        self._last_solo = None
        self.panel.clear_cards()

    def set_update_available(self, version: str) -> None:
        self.update_badge.show_version(version)

    # ---- ориентация ------------------------------------------------------
    @property
    def is_horizontal(self) -> bool:
        return self._horizontal

    def _tiles_layout(self) -> QVBoxLayout | QHBoxLayout:
        return self.tiles_hbox if self._horizontal else self.tiles_vbox

    def _screen_rect(self) -> QRect:
        screen = self.screen()
        return screen.availableGeometry() if screen else QRect()

    def set_orientation(self, horizontal: bool, *, transpose: bool = False) -> None:
        """Переложить плитки в ряд или в колонку.

        `transpose` переворачивает и сам кадр (ш↔в вокруг центра) — так кольца
        сохраняют размер, а виджет не прыгает углом. При восстановлении
        сохранённой ориентации переворот не нужен: кадр уже сохранён таким.
        """
        if horizontal == self._horizontal:
            return
        old, new = self._tiles_layout(), (
            self.tiles_vbox if self._horizontal else self.tiles_hbox
        )
        for tile in self._tiles.values():
            old.removeWidget(tile)
            new.addWidget(tile, stretch=1)
        main = self.layout()
        for i in range(main.count()):
            item = main.itemAt(i)
            if item is old or item.layout() is old:
                main.removeItem(item)
                main.insertLayout(i, new, stretch=1)
                break
        self._horizontal = horizontal
        if transpose:
            self._transpose_frame()

    def _transpose_frame(self) -> None:
        """Поменять местами ширину и высоту кадра, оставив центр на месте."""
        center = self.geometry().center()
        width, height = self.height(), self.width()
        self.resize(width, height)
        self.move(self._clamp_to_screen(QPoint(center.x() - width // 2, center.y() - height // 2)))

    def _clamp_to_screen(self, pos: QPoint) -> QPoint:
        screen = self._screen_rect()
        if screen.isNull():
            return pos
        x = min(max(pos.x(), screen.left()), screen.right() - self.width() + 1)
        y = min(max(pos.y(), screen.top()), screen.bottom() - self.height() + 1)
        return QPoint(x, y)

    def sync_edge_layout(self, dock: bool = True) -> str | None:
        """Подстроить ориентацию под край, к которому прижат кадр.

        Возвращает край или None, если виджет висит посреди экрана — тогда
        ориентация остаётся прежней. `dock` дожимает кадр вплотную к краю.
        """
        screen = self._screen_rect()
        edge = docked_edge(self.frameGeometry(), screen)
        if edge is None:
            return None
        self.set_orientation(edge in ("top", "bottom"), transpose=True)
        if dock:
            self.move(flush_pos(self.frameGeometry(), screen, edge))
        return edge

    # ---- сворачивание за край -------------------------------------------
    def set_autohide(self, enabled: bool) -> None:
        self.collapse.set_enabled(enabled)

    def _panel_holds_cursor(self) -> bool:
        """Курсор на ховер-панели — виджет ещё нужен, сворачивание откладываем."""
        return self.panel.isVisible() and self.panel.frameGeometry().contains(
            QCursor.pos()
        )

    def _begin_interaction(self) -> None:
        """Клик/перетаскивание: развернуть свёрнутый виджет и отложить сворачивание."""
        self.collapse.expand(animated=False)
        self.collapse.notify_activity()

    def _on_collapsed(self) -> None:
        self._hide_timer.stop()
        self.panel.hide()

    def _on_expanded(self) -> None:
        # вернулись под курсор — показываем панель, как при обычном наведении
        if self.frameGeometry().contains(QCursor.pos()):
            self._reposition_panel()
            self.panel.show()

    # ---- mini mode -------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ("full", "mini") else "full"
        self._apply_visibility()
        self.collapse.refresh()  # размер кадра мог поменяться — полоска та же

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
        delta = self._activity_delta()
        if delta:
            self._last_solo = max(delta, key=delta.get)
        visible = set(hot)
        if self._last_solo in self._tiles:
            visible.add(self._last_solo)
        if not visible:
            # ни горячих, ни роста — одна плитка, чтобы кадр не опустел
            first = next(iter(self._tiles), None)
            if first:
                self._last_solo = first
                visible.add(first)
        return visible

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

    # ---- изменение размера за рамку --------------------------------------
    def _hit_test(self, local: QPoint) -> str:
        """Стороны рамки под точкой: строка из 'l', 'r', 't', 'b' («» — центр)."""
        edge = ""
        if local.y() <= RESIZE_MARGIN:
            edge += "t"
        elif local.y() >= self.height() - 1 - RESIZE_MARGIN:
            edge += "b"
        if local.x() <= RESIZE_MARGIN:
            edge += "l"
        elif local.x() >= self.width() - 1 - RESIZE_MARGIN:
            edge += "r"
        return edge

    @staticmethod
    def _cursor_for_edge(edge: str) -> Qt.CursorShape:
        if edge in ("tl", "br"):
            return Qt.SizeFDiagCursor
        if edge in ("tr", "bl"):
            return Qt.SizeBDiagCursor
        if edge in ("l", "r"):
            return Qt.SizeHorCursor
        if edge in ("t", "b"):
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def _do_resize(self, global_pos: QPoint) -> None:
        """Двигать схваченные стороны за курсором, не выпуская кадр за экран."""
        start, origin = self._resize_start_geo, self._resize_start_global
        edge = self._resize_edge
        if start is None or origin is None or not edge:
            return
        delta = global_pos - origin
        screen = self._screen_rect()
        left, top = start.left(), start.top()
        right, bottom = start.right() + 1, start.bottom() + 1
        if "l" in edge:
            left = min(start.left() + delta.x(), right - self.minimumWidth())
            left = max(left, screen.left())
        if "r" in edge:
            right = max(start.right() + 1 + delta.x(), left + self.minimumWidth())
            right = min(right, screen.right() + 1)
        if "t" in edge:
            top = min(start.top() + delta.y(), bottom - self.minimumHeight())
            top = max(top, screen.top())
        if "b" in edge:
            bottom = max(start.bottom() + 1 + delta.y(), top + self.minimumHeight())
            bottom = min(bottom, screen.bottom() + 1)
        self.setGeometry(left, top, right - left, bottom - top)

    # ---- drag to move ---------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            # схватили за полоску свёрнутого виджета — сначала вернуть его
            self._begin_interaction()
            local = event.position().toPoint()
            edge = self._hit_test(local)
            if edge:
                self._resize_edge = edge
                self._resize_start_geo = self.geometry()
                self._resize_start_global = event.globalPosition().toPoint()
                self._drag_offset = None
            else:
                self._drag_offset = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.LeftButton):
            self.setCursor(self._cursor_for_edge(self._hit_test(event.position().toPoint())))
            super().mouseMoveEvent(event)
            return

        pos = event.globalPosition().toPoint()
        if self._resize_edge:
            self._do_resize(pos)
        elif self._drag_offset is not None:
            self.move(pos - self._drag_offset)
            was_horizontal = self._horizontal
            # у края экрана виджет разворачивается и прижимается прямо на лету
            self.sync_edge_layout()
            if self._horizontal != was_horizontal:
                # кадр перевернулся — заново привязываем курсор к рамке
                self._drag_offset = pos - self.frameGeometry().topLeft()
            self._reposition_panel()
        self.collapse.notify_activity()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        self._resize_edge = ""
        self._resize_start_geo = None
        self._resize_start_global = None
        self.collapse.notify_activity()
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
        autohide = QAction("Сворачивать у края экрана", menu, checkable=True)
        autohide.setChecked(self.collapse.enabled)
        autohide.setToolTip(
            "Прижатый к краю виджет уезжает за край, оставляя тонкую полоску"
        )
        autohide.toggled.connect(self.autohide_changed)
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
        menu.addAction(autohide)
        menu.addAction(stats)
        menu.addAction(settings)
        menu.addAction(help_action)
        menu.addAction(hide)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._context_menu = menu  # keep alive: popup() is non-blocking
        self.collapse.notify_activity()
        menu.popup(event.globalPos())

    # ---- hover panel ----------------------------------------------------
    def enterEvent(self, event) -> None:
        self.collapse.notify_activity()
        # свёрнутый виджет сначала выезжает обратно — панель ждёт до этого
        if not self.collapse.is_collapsed:
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
        # позиции свёрнутого виджета временные — храним «жилую», у края
        if not self.collapse.busy:
            self._geometry_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self._geometry_timer.start()
        self.update_badge.relabel_for_width()
        self.collapse.refresh()  # другой размер кадра — та же полоска у края
        super().resizeEvent(event)

    def hideEvent(self, event) -> None:
        self.panel.hide()
        self.collapse.expand(animated=False)  # прятать/показывать целиком
        super().hideEvent(event)
