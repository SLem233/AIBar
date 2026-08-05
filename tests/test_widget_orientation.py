"""Ориентация виджета по краю экрана.

У левого и правого края виджет — колонка колец, у верхнего и нижнего — ряд.
Ориентация меняется, как только кадр коснулся края (в том числе прямо во время
перетаскивания), и вместе с ней переворачивается сам кадр: ширина и высота
меняются местами вокруг центра, чтобы кольца сохранили размер, а виджет не
прыгал углом. Восстановление сохранённой ориентации при запуске кадр не трогает
— он уже сохранён перевёрнутым.
"""

import pytest
from PySide6.QtWidgets import QApplication

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.widget import DesktopWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


_ALIVE: list[DesktopWidget] = []


@pytest.fixture(autouse=True)
def close_widgets(app):
    yield
    for widget in _ALIVE:
        widget.collapse.set_enabled(False)
        widget.panel.hide()
        widget.hide()
        widget.panel.deleteLater()
        widget.deleteLater()
    _ALIVE.clear()
    app.processEvents()


def new_widget(size=(120, 260)) -> DesktopWidget:
    widget = DesktopWidget()
    _ALIVE.append(widget)
    widget.resize(*size)
    return widget


def with_tiles(widget, *names):
    widget.update_snapshots(
        [
            ProviderSnapshot(provider=name, windows=[RateWindow("Сессия", 10.0)])
            for name in names
        ]
    )
    return widget


def dock(widget, edge):
    """Поставить кадр вплотную к краю экрана."""
    screen = widget.screen().availableGeometry()
    if edge == "top":
        widget.move(screen.left() + 300, screen.top())
    elif edge == "bottom":
        widget.move(screen.left() + 300, screen.bottom() - widget.height() + 1)
    elif edge == "left":
        widget.move(screen.left(), screen.top() + 100)
    else:
        widget.move(screen.right() - widget.width() + 1, screen.top() + 100)
    return screen


# ---- ориентация по краю ----------------------------------------------------
def test_top_edge_turns_the_widget_into_a_row(app):
    widget = new_widget()
    dock(widget, "top")
    assert widget.sync_edge_layout() == "top"
    assert widget.is_horizontal


def test_bottom_edge_also_means_a_row(app):
    widget = new_widget()
    dock(widget, "bottom")
    widget.sync_edge_layout()
    assert widget.is_horizontal


@pytest.mark.parametrize("edge", ["left", "right"])
def test_side_edges_keep_the_column(app, edge):
    widget = new_widget()
    widget.set_orientation(True)  # был рядом
    dock(widget, edge)
    widget.sync_edge_layout()
    assert not widget.is_horizontal


def test_a_widget_in_the_middle_keeps_its_orientation(app):
    widget = new_widget()
    screen = widget.screen().availableGeometry()
    widget.move(screen.center())
    before = widget.is_horizontal
    assert widget.sync_edge_layout() is None
    assert widget.is_horizontal == before


def test_docking_pushes_the_frame_flush_to_the_edge(app):
    widget = new_widget()
    screen = widget.screen().availableGeometry()
    widget.move(screen.right() - widget.width() + 1 - 5, screen.top() + 100)  # почти у края
    widget.sync_edge_layout()
    assert widget.frameGeometry().right() == screen.right()


# ---- переворот кадра -------------------------------------------------------
def test_orientation_change_swaps_width_and_height(app):
    widget = new_widget(size=(120, 260))
    widget.set_orientation(True, transpose=True)
    assert (widget.width(), widget.height()) == (260, 120)


def test_transpose_keeps_the_centre_in_place(app):
    widget = new_widget(size=(120, 260))
    widget.move(500, 300)
    before = widget.geometry().center()
    widget.set_orientation(True, transpose=True)
    after = widget.geometry().center()
    assert abs(after.x() - before.x()) <= 1
    assert abs(after.y() - before.y()) <= 1


def test_transposed_frame_stays_on_screen(app):
    widget = new_widget(size=(120, 400))
    screen = widget.screen().availableGeometry()
    widget.move(screen.left() + 10, screen.top() + 20)
    widget.set_orientation(True, transpose=True)
    assert widget.geometry().left() >= screen.left()
    assert widget.geometry().right() <= screen.right()


def test_restoring_a_saved_orientation_does_not_transpose(app):
    """При запуске кадр уже сохранён перевёрнутым — второй раз переворачивать нельзя."""
    widget = new_widget(size=(260, 120))
    widget.set_orientation(True)  # без transpose
    assert (widget.width(), widget.height()) == (260, 120)
    assert widget.is_horizontal


def test_switching_back_and_forth_returns_the_original_size(app):
    widget = new_widget(size=(120, 260))
    widget.set_orientation(True, transpose=True)
    widget.set_orientation(False, transpose=True)
    assert (widget.width(), widget.height()) == (120, 260)


# ---- плитки ----------------------------------------------------------------
def test_tiles_move_into_the_row_layout(app):
    widget = with_tiles(new_widget(), "Claude", "Codex")
    assert widget.tiles_vbox.count() == 2
    widget.set_orientation(True, transpose=True)
    assert widget.tiles_hbox.count() == 2
    assert widget.tiles_vbox.count() == 0


def test_new_tiles_land_in_the_active_layout(app):
    widget = new_widget()
    widget.set_orientation(True, transpose=True)
    with_tiles(widget, "Claude", "Codex")
    assert widget.tiles_hbox.count() == 2


def test_repeated_orientation_call_is_a_no_op(app):
    widget = with_tiles(new_widget(), "Claude")
    widget.set_orientation(True, transpose=True)
    size = (widget.width(), widget.height())
    widget.set_orientation(True, transpose=True)
    assert (widget.width(), widget.height()) == size
    assert widget.tiles_hbox.count() == 1
