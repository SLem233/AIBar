"""Auto-hide: the widget slides off-screen after idle and back on hover."""

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.widget import DesktopWidget, HIDE_SLIVER


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def snap(name="Claude"):
    return ProviderSnapshot(provider=name, windows=[RateWindow("Сессия (5ч)", 42.0)])


def _stub_screen(widget, rect):
    class FakeScreen:
        def availableGeometry(self):
            return rect
    widget.screen = lambda: FakeScreen()


SCREEN = QRect(0, 0, 1920, 1080)


def test_hide_on_idle_reads_from_setter(app):
    widget = DesktopWidget()
    assert widget._hide_on_idle is False
    widget.set_hide_on_idle(True)
    assert widget._hide_on_idle is True
    # enabling arms the idle timer
    assert widget._idle_hide_timer.isActive()
    widget.set_hide_on_idle(False)
    assert widget._hide_on_idle is False
    assert not widget._idle_hide_timer.isActive()


def test_slide_off_top_leaves_sliver(app):
    widget = DesktopWidget()
    widget._hide_on_idle = True
    widget.resize(120, 120)
    widget.move(SCREEN.left() + 100, SCREEN.top() + 50)  # nearest edge = top
    _stub_screen(widget, SCREEN)
    widget._slide_off()
    assert widget._hidden is True
    # the widget's top should now be just above the screen, leaving HIDE_SLIVER
    assert widget.pos().y() == SCREEN.top() + 50 - (120 - HIDE_SLIVER)


def test_slide_off_records_pre_hide_position(app):
    widget = DesktopWidget()
    widget._hide_on_idle = True
    widget.resize(120, 120)
    pos_before = SCREEN.left() + 100, SCREEN.top() + 50
    widget.move(*pos_before)
    _stub_screen(widget, SCREEN)
    widget._slide_off()
    assert widget._pre_hide_pos is not None
    assert (widget._pre_hide_pos.x(), widget._pre_hide_pos.y()) == pos_before


def test_slide_in_restores_position(app):
    widget = DesktopWidget()
    widget._hide_on_idle = True
    widget.resize(120, 120)
    pos_before = SCREEN.left() + 100, SCREEN.top() + 50
    widget.move(*pos_before)
    _stub_screen(widget, SCREEN)
    widget._slide_off()
    assert widget._hidden is True
    widget._slide_in()
    assert widget._hidden is False
    assert (widget.pos().x(), widget.pos().y()) == pos_before


def test_slide_off_noop_when_disabled(app):
    widget = DesktopWidget()
    widget._hide_on_idle = False
    widget.resize(120, 120)
    widget.move(SCREEN.left() + 100, SCREEN.top() + 50)
    _stub_screen(widget, SCREEN)
    widget._slide_off()
    assert widget._hidden is False  # nothing happened


def test_slide_off_left_edge(app):
    widget = DesktopWidget()
    widget.resize(120, 120)
    widget.move(SCREEN.left() + 10, SCREEN.top() + 500)  # nearest edge = left
    _stub_screen(widget, SCREEN)
    widget._hide_on_idle = True
    widget._slide_off()
    assert widget._hidden is True
    assert widget.pos().x() == SCREEN.left() + 10 - (120 - HIDE_SLIVER)
