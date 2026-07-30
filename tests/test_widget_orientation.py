"""Auto-orientation: vertical at the left/right edge, horizontal at top/bottom."""

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.widget import DesktopWidget, EDGE_THRESHOLD


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def snap(name="Claude"):
    return ProviderSnapshot(provider=name, windows=[RateWindow("Сессия (5ч)", 42.0)])


def _stub_screen(widget, rect):
    """Force the widget's available screen geometry for deterministic tests."""
    class FakeScreen:
        def availableGeometry(self):
            return rect
    widget.screen = lambda: FakeScreen()


SCREEN = QRect(0, 0, 1920, 1080)


def test_tiles_added_to_vertical_layout_by_default(app):
    widget = DesktopWidget()
    widget.update_snapshots([snap()])
    assert widget.tiles_vbox.count() == 1
    assert widget.tiles_hbox.count() == 0
    assert widget._horizontal is False


def test_set_orientation_moves_tiles(app):
    widget = DesktopWidget()
    widget.update_snapshots([snap(), snap("Codex")])
    assert widget.tiles_vbox.count() == 2
    widget._set_orientation(True)
    assert widget._horizontal is True
    assert widget.tiles_hbox.count() == 2
    assert widget.tiles_vbox.count() == 0
    # back to vertical
    widget._set_orientation(False)
    assert widget._horizontal is False
    assert widget.tiles_vbox.count() == 2


def test_edge_orientation_top_is_horizontal(app):
    widget = DesktopWidget()
    widget.resize(120, 120)
    widget.move(SCREEN.left() + 100, SCREEN.top() + 2)  # touching top
    _stub_screen(widget, SCREEN)
    assert widget._edge_orientation() is True


def test_edge_orientation_bottom_is_horizontal(app):
    widget = DesktopWidget()
    widget.resize(120, 120)
    widget.move(SCREEN.left() + 100, SCREEN.bottom() - 120 - 2)
    _stub_screen(widget, SCREEN)
    assert widget._edge_orientation() is True


def test_edge_orientation_left_is_vertical(app):
    widget = DesktopWidget()
    widget.resize(120, 120)
    widget.move(SCREEN.left() + 2, SCREEN.top() + 100)
    _stub_screen(widget, SCREEN)
    assert widget._edge_orientation() is False


def test_edge_orientation_right_is_vertical(app):
    widget = DesktopWidget()
    widget.resize(120, 120)
    widget.move(SCREEN.right() - 120 - 2, SCREEN.top() + 100)
    _stub_screen(widget, SCREEN)
    assert widget._edge_orientation() is False


def test_edge_orientation_mid_screen_keeps_current(app):
    widget = DesktopWidget()
    widget.resize(120, 120)
    widget.move(SCREEN.left() + 500, SCREEN.top() + 400)  # far from any edge
    _stub_screen(widget, SCREEN)
    assert widget._edge_orientation() is None


def test_threshold_boundary(app):
    widget = DesktopWidget()
    widget.resize(120, 120)
    # exactly at threshold from the top -> still counts as docked (<=)
    widget.move(SCREEN.left() + 100, SCREEN.top() + EDGE_THRESHOLD)
    _stub_screen(widget, SCREEN)
    assert widget._edge_orientation() is True
