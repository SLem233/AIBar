"""Виджет остаётся поверх чужих окон.

Флаг «поверх всех» кладёт окно в верхний слой, но порядок внутри слоя Windows
не держит: чужое окно, поднятое туда позже, остаётся выше нашего навсегда.
Больнее всего свёрнутому виджету — перекрытую полоску в 3–4 мм не видно и
развернуть его нечем. Поэтому порядок возвращаем сами, по таймеру.
"""

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget

from aibar.ui import topmost
from aibar.ui.topmost import TopmostKeeper
from aibar.ui.widget import DesktopWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


_ALIVE: list[QWidget] = []


@pytest.fixture(autouse=True)
def close_widgets(app):
    """Окна сносим явно, пока жив QApplication (иначе Qt падает на GC)."""
    yield
    for widget in _ALIVE:
        widget.hide()
        widget.deleteLater()
    _ALIVE.clear()
    app.processEvents()


class Recorder:
    """Подменяет системный вызов подъёма и запоминает, кого поднимали."""

    def __init__(self):
        self.calls: list[QWidget] = []

    def __call__(self, widget) -> bool:
        self.calls.append(widget)
        return True


def plain_widget(app, *, visible=True) -> QWidget:
    widget = QWidget()
    widget.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
    _ALIVE.append(widget)
    if visible:
        widget.show()
        app.processEvents()
    return widget


def new_desktop_widget() -> DesktopWidget:
    widget = DesktopWidget()
    _ALIVE.append(widget)
    _ALIVE.append(widget.panel)
    return widget


# ---- сам механизм -------------------------------------------------------


def test_widget_is_always_on_top(app):
    widget = new_desktop_widget()
    assert widget.windowFlags() & Qt.WindowStaysOnTopHint


def test_reassert_raises_every_visible_window(app):
    first, second = plain_widget(app), plain_widget(app)
    rec = Recorder()
    keeper = TopmostKeeper(first, second, raise_fn=rec)
    keeper.reassert()
    # порядок снизу вверх: панель поднимается после виджета и остаётся над ним
    assert rec.calls == [first, second]


def test_reassert_skips_hidden_windows(app):
    shown, hidden = plain_widget(app), plain_widget(app, visible=False)
    rec = Recorder()
    TopmostKeeper(shown, hidden, raise_fn=rec).reassert()
    assert rec.calls == [shown]


def test_reassert_waits_out_an_open_menu(app, monkeypatch):
    """Пока открыто меню, наверху законно чужое окно — своё не выпячиваем."""
    widget = plain_widget(app)
    rec = Recorder()
    keeper = TopmostKeeper(widget, raise_fn=rec)
    monkeypatch.setattr(topmost, "popup_open", lambda: True)
    keeper.reassert()
    assert rec.calls == []


def test_keeper_repeats_itself(app):
    """Событий «нас накрыли» нет — порядок возвращается по таймеру."""
    widget = plain_widget(app)
    keeper = TopmostKeeper(widget, interval_ms=50)
    keeper.start()
    assert keeper.active
    assert keeper.interval == 50


# ---- проводка в виджете -------------------------------------------------


def test_keeper_follows_the_widget_visibility(app):
    widget = new_desktop_widget()
    assert not widget.topmost.active  # ещё не показан — нечего держать
    widget.show()
    app.processEvents()
    assert widget.topmost.active
    widget.hide()
    app.processEvents()
    assert not widget.topmost.active


def test_showing_the_widget_raises_it_at_once(app):
    widget = new_desktop_widget()
    rec = Recorder()
    widget.topmost._raise = rec
    widget.show()
    app.processEvents()
    assert widget in rec.calls  # не ждём первого тика таймера


def test_collapsing_raises_the_strip_at_once(app):
    """Полоска появляется сразу поверх всего, а не через такт таймера."""
    widget = new_desktop_widget()
    widget.show()
    app.processEvents()
    screen = widget.screen().availableGeometry()
    widget.move(screen.right() - widget.width() + 1, screen.top() + 40)
    widget.collapse.set_enabled(True)
    rec = Recorder()
    widget.topmost._raise = rec
    assert widget.collapse.collapse(animated=False) is True
    assert widget in rec.calls
    widget.collapse.set_enabled(False)


def test_filter_survives_the_widget_teardown(app):
    """Виджет сносится вместе с keeper'ом, а события до фильтра ещё доходят."""
    widget = DesktopWidget()
    keeper = widget.topmost
    widget.panel.deleteLater()
    widget.deleteLater()
    app.processEvents()
    assert keeper.eventFilter(None, QEvent(QEvent.Show)) is False


def test_raise_to_top_never_throws_on_a_dead_window(app):
    """Окно могло уже уйти — таймер не должен ронять приложение."""
    widget = plain_widget(app, visible=False)
    assert topmost.raise_to_top(widget) in (True, False)
