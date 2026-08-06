"""Удержание окон виджета в верхнем слое.

`WindowStaysOnTopHint` только кладёт окно в верхний слой Windows, но порядок
внутри слоя система не хранит: чужое окно, попавшее туда позже (плеер,
презентация, установщик, оверлей мессенджера, возврат из полноэкранного
приложения), встаёт выше нашего и остаётся там до перезапуска виджета. У
развёрнутого виджета это заметно сразу, а у свёрнутого — нет: перекрытая
полоска в 3–4 мм не бросается в глаза, и развернуть виджет становится нечем.

Событие «нас накрыли» система не шлёт, поэтому порядок возвращаем таймером.
`SetWindowPos` с `HWND_TOPMOST` дешевле, чем `raise_()`: он и поднимает окно
внутри слоя, и возвращает сам слой, если его сняли, при этом не трогает фокус
(`SWP_NOACTIVATE`) и не тащит за собой окно-владельца (`SWP_NOOWNERZORDER`).
Пересоздавать окно через `setWindowFlag`, как обычно советуют, нельзя: Qt на
это прячет и показывает окно заново — виджет мигал бы каждые пару секунд.
"""

import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication

# Реже — накрытый виджет висит заметно долго, чаще — незачем: вызов дешёвый,
# но он всё-таки лезет в системный z-order.
INTERVAL_MS = 1500

_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERZORDER = 0x0200
_SWP_FLAGS = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER


def _load_set_window_pos():
    """user32.SetWindowPos или None — на не-Windows и при отказе загрузки."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        func = ctypes.windll.user32.SetWindowPos
        func.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        func.restype = wintypes.BOOL
        return func
    except (AttributeError, OSError, ValueError):  # pragma: no cover — не Windows
        return None


_set_window_pos = _load_set_window_pos()


def popup_open() -> bool:
    """Открыто меню: наверху сейчас законно чужое окно, своё не выпячиваем."""
    return QApplication.activePopupWidget() is not None


def raise_to_top(widget) -> bool:
    """Вернуть окно наверх верхнего слоя. False — если сделать это не вышло."""
    try:
        hwnd = int(widget.winId())
    except (RuntimeError, ValueError, TypeError):  # окно уже снесено
        return False
    if _set_window_pos is None or not hwnd:
        widget.raise_()  # не Windows: без слоёв, просто наверх
        return False
    return bool(
        _set_window_pos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(_HWND_TOPMOST),
            0,
            0,
            0,
            0,
            _SWP_FLAGS,
        )
    )


class TopmostKeeper(QObject):
    """Держит окна наверху: по таймеру и на каждый показ первого из них.

    Окна перечисляются снизу вверх — последнее оказывается над остальными.
    Таймер живёт по видимости первого окна (виджета): скрытый виджет ничего не
    удерживает.
    """

    def __init__(self, *widgets, interval_ms: int = INTERVAL_MS, raise_fn=raise_to_top):
        super().__init__(widgets[0])
        self._widgets = widgets
        self._raise = raise_fn
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.reassert)
        widgets[0].installEventFilter(self)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    @property
    def interval(self) -> int:
        return self._timer.interval()

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def reassert(self) -> None:
        """Поднять все видимые окна наверх, снизу вверх."""
        if popup_open():
            return
        for widget in self._widgets:
            if widget.isVisible():
                self._raise(widget)

    def eventFilter(self, obj, event):
        # Виджет сносят вместе с детьми, и последние события доходят сюда уже
        # после смерти C++-половины keeper'а: у python-обёртки к этому моменту
        # нет ни атрибутов, ни права звать методы базового класса.
        try:
            watched = self._widgets[0]
        except (AttributeError, RuntimeError):
            return False
        if obj is watched:
            if event.type() == QEvent.Show:
                self.reassert()  # сразу наверх, не дожидаясь первого тика
                self.start()
            elif event.type() == QEvent.Hide:
                self.stop()
        return False  # фильтр только наблюдает, событие идёт дальше
