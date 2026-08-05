"""Orientation & dragging (v0.6.0 model).

- Live orientation during drag: as soon as the frame touches a screen edge it
  snaps to that edge's orientation.
- On orientation change the *frame* transposes (w <-> h) around its center, so
  gauges keep their size; only row/column arrangement changes.
- The widget can't be dragged off-screen (position clamped to the screen).
"""

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
    class FakeScreen:
        def geometry(self):
            return rect

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
    _stub_screen(widget, SCREEN)
    widget.update_snapshots([snap(), snap("Codex")])
    assert widget.tiles_vbox.count() == 2
    widget._set_orientation(True)
    assert widget._horizontal is True
    assert widget.tiles_hbox.count() == 2
    assert widget.tiles_vbox.count() == 0
    widget._set_orientation(False)
    assert widget.tiles_vbox.count() == 2


def test_set_orientation_transposes_frame_keeping_center(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.resize(120, 260)
    widget.move(100, 100)
    cx_before, cy_before = widget._center().x(), widget._center().y()
    w_before, h_before = widget.width(), widget.height()
    widget._set_orientation(True)
    # frame dimensions swapped (around the scale-adjusted base)
    assert widget.width() > widget.height()  # now wide
    # center preserved
    assert widget._center().x() == cx_before
    assert widget._center().y() == cy_before
    # toggling back restores dimensions
    widget._set_orientation(False)
    assert (widget.width(), widget.height()) == (w_before, h_before)


def test_orient_to_edge_top_is_horizontal(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget._orient_to_edge("top")
    assert widget._horizontal is True
    widget._orient_to_edge("bottom")
    assert widget._horizontal is True


def test_orient_to_edge_left_right_is_vertical(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget._orient_to_edge("left")
    assert widget._horizontal is False
    widget._orient_to_edge("right")
    assert widget._horizontal is False


def test_nearest_docked_edge_detects_contact(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.resize(120, 120)
    # touching top
    widget.move(100, SCREEN.top())
    assert widget._nearest_docked_edge() == "top"
    # touching right
    widget.move(SCREEN.right() - 120 + 1, 100)
    assert widget._nearest_docked_edge() == "right"
    # mid-screen: nothing docked
    widget.move(500, 400)
    assert widget._nearest_docked_edge() is None


def test_clamp_keeps_frame_inside_screen(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.resize(120, 120)
    # try to push top-left corner off-screen
    clamped = widget._clamp_to_screen(QPoint(-500, -500))
    assert clamped.x() >= SCREEN.left()
    assert clamped.y() >= SCREEN.top()
    # try to push bottom-right corner off-screen
    clamped = widget._clamp_to_screen(QPoint(SCREEN.width(), SCREEN.height()))
    assert clamped.x() <= SCREEN.right() - widget.width() + 1
    assert clamped.y() <= SCREEN.bottom() - widget.height() + 1


def test_dock_to_edge_flush(app):
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.resize(120, 120)
    widget._dock_to_edge("top")
    assert widget.pos().y() == SCREEN.top()
    widget._dock_to_edge("bottom")
    assert widget.pos().y() == SCREEN.bottom() - widget.height() + 1
    widget._dock_to_edge("left")
    assert widget.pos().x() == SCREEN.left()
    widget._dock_to_edge("right")
    assert widget.pos().x() == SCREEN.right() - widget.width() + 1


def test_stale_tiny_geometry_grows_to_readable_size(app):
    """A tiny frame saved from a prior run must grow so each gauge is readable.

    Regression: with 3 providers in a 136px-tall frame, each gauge got ~24px
    and the rings were nearly invisible (Z.ai limits not seen).
    """
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.resize(306, 136)  # stale tiny geometry
    snaps = [
        ProviderSnapshot(provider="Claude", windows=[RateWindow("Сессия", 0.0)]),
        ProviderSnapshot(provider="Codex", windows=[RateWindow("Неделя", 100.0)]),
        ProviderSnapshot(provider="Z.ai", windows=[RateWindow("Сессия", 24.0)]),
    ]
    widget.update_snapshots(snaps)
    # Each tile now has a gauge tall enough to draw readable rings.
    for name, tile in widget._tiles.items():
        assert tile.gauge.height() >= 40, f"{name} gauge too small: {tile.gauge.height()}"
    # Normalization only happens once; a later poll must not resize again.
    h_after = widget.height()
    widget._size_normalized = True
    widget.update_snapshots(snaps)
    assert widget.height() == h_after



def test_orientation_changed_signal_emits_on_flip(app):
    """_set_orientation emits orientation_changed so main.py can persist it."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    received = []
    widget.orientation_changed.connect(lambda h: received.append(h))
    widget._set_orientation(True)
    assert received == [True]
    widget._set_orientation(False)
    assert received == [True, False]
    # No-op flip (same orientation) doesn't re-emit.
    widget._set_orientation(False)
    assert received == [True, False]


def test_set_orientation_silent_does_not_transpose_frame(app):
    """Restore path: orientation applied without transposing the frame,
    because the saved geometry already has the right w/h."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.resize(300, 120)  # wide geometry, like a saved horizontal layout
    w_before, h_before = widget.width(), widget.height()
    received = []
    widget.orientation_changed.connect(lambda h: received.append(h))
    widget.set_orientation_silent(True)
    assert widget._horizontal is True
    # Frame NOT transposed (saved geometry wins).
    assert (widget.width(), widget.height()) == (w_before, h_before)
    # No signal emitted — this is a restore, not a user action.
    assert received == []


def test_set_orientation_silent_swaps_tile_layout(app):
    """Despite no frame transpose, tiles DO move to the right layout."""
    widget = DesktopWidget()
    _stub_screen(widget, SCREEN)
    widget.update_snapshots([snap(), snap("Codex")])
    widget.set_orientation_silent(True)
    assert widget.tiles_hbox.count() == 2
    assert widget.tiles_vbox.count() == 0
    assert widget._horizontal is True
