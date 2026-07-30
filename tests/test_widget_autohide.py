"""Auto-hide (polling-based model).

The widget slides behind the nearest screen edge, leaving ~15% of its frame as
a "bookmark" tab; bringing the cursor over that tab slides it back and docks it
flush to the edge. The widget does NOT change size or color — only its on-screen
position. A polling timer checks the real cursor position (enter/leave events
on a translucent frameless window are unreliable) and drives the transitions.
"""

import time

import pytest
from PySide6.QtCore import QAbstractAnimation, QRect
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.widget import DesktopWidget, HIDE_VISIBLE_FRACTION


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def snap(name="Claude"):
    return ProviderSnapshot(provider=name, windows=[RateWindow("Сессия (5ч)", 42.0)])


def _stub_screen(widget, rect):
    class FakeScreen:
        def geometry(self):
            return rect

        def availableGeometry(self):
            return rect
    widget.screen = lambda: FakeScreen()


SCREEN = QRect(0, 0, 1920, 1080)


def test_hide_on_idle_setter_starts_poller(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(1000, 1000)
    widget.show()
    assert widget._hide_on_idle is False
    widget.set_hide_on_idle(True)
    assert widget._hide_on_idle is True
    assert widget._idle_check_timer.isActive()  # poller armed
    widget.set_hide_on_idle(False)
    assert not widget._idle_check_timer.isActive()


def test_slide_off_refuses_while_cursor_inside(app):
    """Hide must not fire while the cursor is genuinely over the widget."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(100, 100)
    widget.show()
    # Put the cursor in the middle of the widget.
    QCursor.setPos(160, 230)
    widget._hide_on_idle = True
    widget._slide_off()
    assert widget._hidden is False  # refused because cursor is inside
    QCursor.setPos(0, 0)


def test_slide_off_top_leaves_15pct_tab(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget._hide_on_idle = True
    widget.move(100, 50)  # nearest edge = top
    h = widget.height()
    widget._slide_off()
    assert widget._hidden is True
    assert widget._hidden_anchor == "top"
    # The animation target leaves only ~15% of the height poking out.
    expected_visible = max(6, int(h * HIDE_VISIBLE_FRACTION))
    target = widget._slide_anim.endValue()
    assert target.y() == SCREEN.top() - (h - expected_visible)
    # An animation was started (smooth slide, not instant teleport).
    assert widget._slide_anim is not None
    assert widget._slide_anim.state() == QAbstractAnimation.Running


def test_slide_off_does_not_change_size(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget._hide_on_idle = True
    w_before, h_before = widget.width(), widget.height()
    widget.move(100, 50)
    widget._slide_off()
    assert (widget.width(), widget.height()) == (w_before, h_before)


def test_slide_off_left_edge(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget._hide_on_idle = True
    widget.move(10, 500)  # nearest edge = left
    w = widget.width()
    widget._slide_off()
    assert widget._hidden_anchor == "left"
    expected_visible = max(6, int(w * HIDE_VISIBLE_FRACTION))
    target = widget._slide_anim.endValue()
    assert target.x() == SCREEN.left() - (w - expected_visible)


def test_slide_in_docks_flush_to_anchor_edge(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget._hide_on_idle = True
    widget.move(100, 50)
    widget._slide_off()
    assert widget._hidden is True
    widget._slide_in()
    assert widget._hidden is False
    # The slide-back target is flush against the top edge.
    target = widget._slide_anim.endValue()
    assert target.y() == SCREEN.top()


def test_slide_off_noop_when_disabled(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget._hide_on_idle = False
    widget.move(100, 50)
    before = widget.pos()
    widget._slide_off()
    assert widget._hidden is False
    assert widget.pos() == before


def test_idle_check_hides_when_cursor_off_after_delay(app):
    """The poller fires _slide_off once the cursor is off and the delay elapsed."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(100, 50)
    widget.show()
    QCursor.setPos(0, 0)  # cursor clearly outside the widget
    widget.set_hide_on_idle(True)
    # Pretend the delay already elapsed -> poller should hide.
    widget._last_active = time.time() - 10
    widget._on_idle_check()
    assert widget._hidden is True


def test_idle_check_keeps_visible_when_cursor_inside(app):
    """The poller must not hide while the cursor is over the widget."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(100, 100)
    widget.show()
    QCursor.setPos(160, 230)  # cursor inside the widget
    widget.set_hide_on_idle(True)
    widget._last_active = time.time() - 10  # delay well past
    widget._on_idle_check()
    assert widget._hidden is False
    QCursor.setPos(0, 0)


def test_idle_check_slides_in_when_cursor_returns(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(100, 50)
    widget.show()
    widget.set_hide_on_idle(True)
    widget._slide_off()
    assert widget._hidden is True
    QCursor.setPos(widget.geometry().center().x(), widget.geometry().center().y())
    widget._on_idle_check()
    assert widget._hidden is False
    QCursor.setPos(0, 0)


def test_tab_hit_area_around_top_edge(app):
    """While hidden behind the top edge, the cursor just off the screen edge
    (over the tab + hover pad) must count as 'inside' to trigger slide-in."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(100, 50)
    widget.show()
    widget._hide_on_idle = True
    widget._slide_off()
    assert widget._hidden_anchor == "top"
    # Cursor slightly above the screen edge, aligned with the tab horizontally.
    geo = widget.frameGeometry()
    QCursor.setPos(geo.center().x(), SCREEN.top() - 5)
    assert widget._cursor_inside() is True
    # Cursor clearly far away -> not inside.
    QCursor.setPos(geo.center().x(), SCREEN.top() - 200)
    assert widget._cursor_inside() is False
    QCursor.setPos(0, 0)


def test_slide_anim_cleared_after_stop(app):
    """_slide_anim must be None after the hide animation stops (finished or
    interrupted), so a fresh hide cycle can start and moveEvent resumes
    persistence. Relying on DeleteWhenStopped alone leaves a dangling ref."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(100, 50)
    widget.show()
    widget._hide_on_idle = True
    widget._slide_off()
    anim = widget._slide_anim
    assert anim is not None
    # Stopping (simulates natural finish or interruption) clears the ref via
    # the stateChanged signal.
    anim.stop()
    app.processEvents()
    assert widget._slide_anim is None


def test_nearest_edge_by_distance_mid_screen(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    # closer to the right edge than any other
    widget.move(SCREEN.right() - widget.width() - 10, SCREEN.top() + 500)
    assert widget._nearest_edge_by_distance() == "right"
