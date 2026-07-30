"""Auto-hide (v0.6.0 model).

The widget slides behind the nearest screen edge, leaving ~15% of its frame as
a "bookmark" tab; hovering it slides it back and docks it flush to the edge.
The widget does NOT change size or color — only its on-screen position.
"""

import pytest
from PySide6.QtCore import QAbstractAnimation, QRect
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
        def availableGeometry(self):
            return rect
    widget.screen = lambda: FakeScreen()


SCREEN = QRect(0, 0, 1920, 1080)


def test_hide_on_idle_setter_armed_when_cursor_off(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    # Park the widget where the offscreen cursor (0,0) is clearly outside it.
    widget.move(1000, 1000)
    widget.show()
    assert widget._hide_on_idle is False
    widget.set_hide_on_idle(True)
    assert widget._hide_on_idle is True
    assert widget._idle_hide_timer.isActive()  # cursor off -> armed
    widget.set_hide_on_idle(False)
    assert not widget._idle_hide_timer.isActive()


def test_slide_off_refuses_while_cursor_inside(app):
    """Hide must not fire while the cursor is genuinely over the widget."""
    from PySide6.QtGui import QCursor
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.move(100, 100)
    widget.show()
    # Put the cursor in the middle of the widget.
    QCursor.setPos(160, 230)
    widget._hide_on_idle = True
    widget._slide_off()
    assert widget._hidden is False  # refused because cursor is inside
    # Restore the cursor so it doesn't leak into subsequent tests.
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


def test_nearest_edge_by_distance_mid_screen(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    # closer to the right edge than any other
    widget.move(SCREEN.right() - widget.width() - 10, SCREEN.top() + 500)
    assert widget._nearest_edge_by_distance() == "right"
