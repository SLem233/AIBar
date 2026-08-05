"""Ресайз виджета за любую сторону и любой угол.

Раньше размер менялся уголком QSizeGrip в правом нижнем углу — у прижатого к
правому краю виджета этот уголок оказывается за краем экрана. Теперь рамка
ловит курсор с четырёх сторон и в четырёх углах, а центр остаётся зоной
перетаскивания и клика.
"""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from aibar.ui.widget import RESIZE_MARGIN, DesktopWidget


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


def new_widget(size=(120, 260), pos=(400, 300)) -> DesktopWidget:
    widget = DesktopWidget()
    _ALIVE.append(widget)
    widget.resize(*size)
    widget.move(*pos)
    return widget


# ---- попадание по рамке ---------------------------------------------------
def test_hit_test_finds_every_side_and_corner(app):
    widget = new_widget()
    w, h = widget.width(), widget.height()
    assert widget._hit_test(QPoint(1, 1)) == "tl"
    assert widget._hit_test(QPoint(w - 1, 1)) == "tr"
    assert widget._hit_test(QPoint(1, h - 1)) == "bl"
    assert widget._hit_test(QPoint(w - 1, h - 1)) == "br"
    assert widget._hit_test(QPoint(1, h // 2)) == "l"
    assert widget._hit_test(QPoint(w - 1, h // 2)) == "r"
    assert widget._hit_test(QPoint(w // 2, 1)) == "t"
    assert widget._hit_test(QPoint(w // 2, h - 1)) == "b"


def test_centre_is_free_for_dragging(app):
    widget = new_widget()
    assert widget._hit_test(QPoint(widget.width() // 2, widget.height() // 2)) == ""
    # сразу за полосой захвата рамка уже не ловит
    assert widget._hit_test(QPoint(RESIZE_MARGIN + 1, widget.height() // 2)) == ""


def test_cursor_matches_the_edge(app):
    widget = new_widget()
    assert widget._cursor_for_edge("tl") == Qt.SizeFDiagCursor
    assert widget._cursor_for_edge("br") == Qt.SizeFDiagCursor
    assert widget._cursor_for_edge("tr") == Qt.SizeBDiagCursor
    assert widget._cursor_for_edge("bl") == Qt.SizeBDiagCursor
    assert widget._cursor_for_edge("l") == Qt.SizeHorCursor
    assert widget._cursor_for_edge("t") == Qt.SizeVerCursor
    assert widget._cursor_for_edge("") == Qt.ArrowCursor


# ---- собственно изменение размера ------------------------------------------
def start_resize(widget, edge):
    widget._resize_edge = edge
    widget._resize_start_geo = widget.geometry()
    widget._resize_start_global = QPoint(0, 0)


def test_right_edge_grows_the_frame_and_keeps_the_corner(app):
    widget = new_widget()
    left, top = widget.x(), widget.y()
    start_resize(widget, "r")
    widget._do_resize(QPoint(40, 0))
    assert widget.width() == 160
    assert (widget.x(), widget.y()) == (left, top)


def test_left_edge_moves_the_frame_instead_of_the_far_side(app):
    widget = new_widget()
    right = widget.geometry().right()
    start_resize(widget, "l")
    widget._do_resize(QPoint(-30, 0))
    assert widget.x() == 370
    assert widget.width() == 150
    assert widget.geometry().right() == right


def test_corner_resizes_both_axes(app):
    widget = new_widget()
    start_resize(widget, "br")
    widget._do_resize(QPoint(30, 40))
    assert (widget.width(), widget.height()) == (150, 300)


def test_minimum_size_is_respected(app):
    widget = new_widget()
    start_resize(widget, "r")
    widget._do_resize(QPoint(-1000, 0))
    assert widget.width() == widget.minimumWidth()


def test_frame_is_not_dragged_off_the_screen(app):
    widget = new_widget()
    screen = widget.screen().availableGeometry()
    widget.move(screen.left(), screen.top())
    start_resize(widget, "t")
    widget._do_resize(QPoint(0, -500))  # тянем верх вверх, за пределы экрана
    assert widget.geometry().top() >= screen.top()


# ---- события мыши ----------------------------------------------------------
def press(widget, local):
    point = QPointF(local)
    return QMouseEvent(
        QMouseEvent.MouseButtonPress,
        point,
        QPointF(widget.x() + local.x(), widget.y() + local.y()),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )


def test_press_on_the_edge_starts_a_resize_not_a_drag(app):
    widget = new_widget()
    widget.mousePressEvent(press(widget, QPoint(widget.width() - 1, widget.height() // 2)))
    assert widget._resize_edge == "r"
    assert widget._drag_offset is None
    widget.mouseReleaseEvent(press(widget, QPoint(widget.width() - 1, widget.height() // 2)))
    assert widget._resize_edge == ""


def test_press_in_the_centre_starts_a_drag(app):
    widget = new_widget()
    widget.mousePressEvent(press(widget, QPoint(widget.width() // 2, widget.height() // 2)))
    assert widget._resize_edge == ""
    assert widget._drag_offset is not None
