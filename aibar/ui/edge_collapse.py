"""Плавное сворачивание виджета за край экрана.

Работает только с виджетом, прижатым к краю: если между кадром и краём есть
заметный разрыв, виджет остаётся висеть на месте. Свёрнутый виджет уезжает за
край, оставляя полоску в 3–4 мм — по ней видно, где он спрятался, и по ней же
он возвращается при наведении курсора. Полоска задана в миллиметрах, а не в
процентах кадра, поэтому она одинакова и у полного виджета, и у мини-режима.

Курсор отслеживается таймером, а не enter/leave-событиями: у полупрозрачного
безрамочного окна на краю экрана эти события приходят ненадёжно.
"""

import time

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

# Зазор (px), который ещё считается «прижат»: попасть мышью пиксель в пиксель
# нельзя, а разрыв больше этого уже виден глазом.
SNAP_TOLERANCE = 8
# Видимая полоска свёрнутого виджета.
STRIP_MM = 3.5
STRIP_MIN_PX = 8
STRIP_MAX_PX = 24
# Запас вокруг полоски, чтобы курсор попадал в неё без прицеливания.
HOVER_PAD = 24
# Пауза без курсора перед сворачиванием (с) и длительность анимации (мс).
IDLE_DELAY = 0.4
ANIM_MS = 500
# Как часто проверяем курсор; свёрнутый виджет опрашиваем чаще, чтобы
# разворачивался сразу при наведении.
POLL_MS = 300
POLL_MS_COLLAPSED = 120
FALLBACK_DPI = 96.0

# Порядок перебора: при равном зазоре колонка гаугов уезжает вбок, а не вверх.
EDGES = ("right", "left", "top", "bottom")


def strip_px(dpi: float) -> int:
    """Ширина видимой полоски в px экрана для заданного DPI."""
    if not dpi or dpi <= 0:
        dpi = FALLBACK_DPI
    px = round(STRIP_MM / 25.4 * dpi)
    return int(min(max(px, STRIP_MIN_PX), STRIP_MAX_PX))


def edge_gaps(frame: QRect, screen: QRect) -> dict[str, int]:
    """Расстояние от кадра до каждого края экрана (отрицательное — заехал)."""
    return {
        "right": screen.right() - frame.right(),
        "left": frame.left() - screen.left(),
        "top": frame.top() - screen.top(),
        "bottom": screen.bottom() - frame.bottom(),
    }


def docked_edge(
    frame: QRect, screen: QRect, tolerance: int = SNAP_TOLERANCE
) -> str | None:
    """Край, к которому прижат кадр, или None — если виджет висит с разрывом."""
    gaps = edge_gaps(frame, screen)
    docked = [edge for edge in EDGES if gaps[edge] <= tolerance]
    if not docked:
        return None
    return min(docked, key=lambda edge: gaps[edge])


def flush_pos(frame: QRect, screen: QRect, edge: str) -> QPoint:
    """Позиция кадра вплотную к краю; вдоль края положение не меняется."""
    x, y = frame.left(), frame.top()
    if edge == "left":
        x = screen.left()
    elif edge == "right":
        x = screen.right() - frame.width() + 1
    elif edge == "top":
        y = screen.top()
    else:  # bottom
        y = screen.bottom() - frame.height() + 1
    return QPoint(x, y)


def collapsed_pos(frame: QRect, screen: QRect, edge: str, strip: int) -> QPoint:
    """Позиция, при которой на экране остаётся полоска в `strip` px."""
    x, y = frame.left(), frame.top()
    if edge == "left":
        x = screen.left() - frame.width() + strip
    elif edge == "right":
        x = screen.right() - strip + 1
    elif edge == "top":
        y = screen.top() - frame.height() + strip
    else:  # bottom
        y = screen.bottom() - strip + 1
    return QPoint(x, y)


def tab_hit_area(frame: QRect, screen: QRect, pad: int = HOVER_PAD) -> QRect:
    """Область, попадание курсора в которую разворачивает виджет."""
    visible = frame.intersected(screen)
    if visible.isEmpty():
        visible = frame
    return visible.adjusted(-pad, -pad, pad, pad)


class EdgeCollapse(QObject):
    """Следит за курсором и прячет прижатый к краю виджет за этот край."""

    collapsed = Signal()  # уехал за край
    expanded = Signal()  # вернулся и стоит вплотную к краю

    def __init__(self, widget, keep_open=None):
        super().__init__(widget)
        self._widget = widget
        # Необязательная проверка «пользователь ещё занят виджетом»
        # (например, курсор на ховер-панели) — сворачивание откладывается.
        self._keep_open = keep_open
        self._enabled = False
        self._collapsed = False
        self._edge: str | None = None
        self._docked_pos: QPoint | None = None
        self._last_active = time.monotonic()
        self._anim: QPropertyAnimation | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)

    # ---- состояние ------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed

    @property
    def busy(self) -> bool:
        """Кадр не на своём «жилом» месте: свёрнут или едет."""
        return self._collapsed or self._anim is not None

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if self._enabled:
            self.notify_activity()
            self._timer.start()
        else:
            self._timer.stop()
            self.expand(animated=False)  # опция выключена — виджет виден целиком

    def notify_activity(self) -> None:
        """Отметить, что пользователь работает с виджетом (отложить сворачивание)."""
        self._last_active = time.monotonic()

    def strip(self) -> int:
        screen = self._widget.screen()
        return strip_px(screen.physicalDotsPerInch() if screen else 0.0)

    # ---- переходы -------------------------------------------------------
    def collapse(self, animated: bool = True) -> bool:
        """Убрать виджет за край. False — если он не прижат к краю."""
        if self._collapsed:
            return False
        screen = self._screen_rect()
        edge = docked_edge(self._widget.frameGeometry(), screen)
        if edge is None:
            return False  # висит с разрывом — не трогаем
        # Сначала вплотную к краю: это и есть «жилая» позиция, куда вернёмся.
        self._widget.move(flush_pos(self._widget.frameGeometry(), screen, edge))
        self._docked_pos = self._widget.pos()
        self._edge = edge
        self._collapsed = True
        self.collapsed.emit()
        target = collapsed_pos(
            self._widget.frameGeometry(), screen, edge, self.strip()
        )
        self._move_to(target, animated)
        return True

    def expand(self, animated: bool = True) -> None:
        """Вернуть виджет на место у края."""
        if not self._collapsed:
            return
        self._collapsed = False
        self.notify_activity()
        target = self._docked_pos
        if target is None:
            self._stop_anim()
            self.expanded.emit()
            return
        self._move_to(target, animated, on_finish=self.expanded.emit)

    def refresh(self) -> None:
        """Пересчитать позицию после смены режима или размера кадра."""
        if not self._collapsed or self._edge is None:
            return
        screen = self._screen_rect()
        frame = self._widget.frameGeometry()
        self._docked_pos = flush_pos(frame, screen, self._edge)
        self._stop_anim()
        self._widget.move(collapsed_pos(frame, screen, self._edge, self.strip()))

    # ---- слежение за курсором -------------------------------------------
    def _tick(self) -> None:
        if not self._enabled or not self._widget.isVisible():
            return
        if QApplication.activePopupWidget() is not None:  # открыто меню
            self.notify_activity()
            return
        if self._cursor_inside() or (self._keep_open and self._keep_open()):
            self.notify_activity()
            if self._collapsed:
                self.expand()
        elif not self._collapsed and time.monotonic() - self._last_active >= IDLE_DELAY:
            self.collapse()
        want = POLL_MS_COLLAPSED if self._collapsed else POLL_MS
        if self._timer.interval() != want:
            self._timer.setInterval(want)

    def _cursor_inside(self) -> bool:
        frame = self._widget.frameGeometry()
        if frame.isEmpty():
            return False
        if self._collapsed:
            return tab_hit_area(frame, self._screen_rect()).contains(QCursor.pos())
        return frame.contains(QCursor.pos())

    # ---- движение -------------------------------------------------------
    def _screen_rect(self) -> QRect:
        screen = self._widget.screen()
        return screen.availableGeometry() if screen else QRect()

    def _move_to(self, target: QPoint, animated: bool, on_finish=None) -> None:
        self._stop_anim()
        if not animated or ANIM_MS <= 0:
            self._widget.move(target)
            if on_finish is not None:
                on_finish()
            return
        anim = QPropertyAnimation(self._widget, b"pos", self)
        anim.setDuration(ANIM_MS)
        anim.setStartValue(self._widget.pos())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        if on_finish is not None:
            anim.finished.connect(on_finish)
        # Ссылку снимаем по остановке (не только по finished): иначе после
        # DeleteWhenStopped в Python останется указатель на удалённый объект.
        anim.finished.connect(self._forget_anim)
        self._anim = anim
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _forget_anim(self) -> None:
        self._anim = None

    def _stop_anim(self) -> None:
        anim, self._anim = self._anim, None
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:  # C++-объект уже удалён
                pass
