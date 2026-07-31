"""Сворачивание виджета за край экрана.

Правила: сворачивается только виджет, прижатый к краю (есть заметный разрыв —
остаётся висеть); после сворачивания видна полоска в несколько миллиметров;
полоска одинакова в полном и в мини-режиме, при любом размере кадра.
"""

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication

from aibar.ui.edge_collapse import (
    HOVER_PAD,
    SNAP_TOLERANCE,
    STRIP_MAX_PX,
    STRIP_MIN_PX,
    collapsed_pos,
    docked_edge,
    flush_pos,
    strip_px,
    tab_hit_area,
)
from aibar.ui.widget import DesktopWidget

SCREEN = QRect(0, 0, 1920, 1080)  # right() = 1919, bottom() = 1079


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


_ALIVE: list[DesktopWidget] = []


@pytest.fixture(autouse=True)
def close_widgets(app):
    """Окна виджета сносим явно, пока жив QApplication (иначе Qt падает на GC)."""
    yield
    for widget in _ALIVE:
        widget.collapse.set_enabled(False)
        widget.panel.hide()
        widget.hide()
        widget.panel.deleteLater()
        widget.deleteLater()
    _ALIVE.clear()
    app.processEvents()


def new_widget() -> DesktopWidget:
    widget = DesktopWidget()
    _ALIVE.append(widget)
    return widget


# ---- геометрия (чистые функции) ----------------------------------------


def test_strip_is_a_few_millimetres_at_96_dpi():
    # 3–4 мм при 96 dpi — это 11–16 px
    assert 11 <= strip_px(96.0) <= 16


def test_strip_is_clamped_and_survives_bogus_dpi():
    assert strip_px(0.0) == strip_px(96.0)  # offscreen/битый экран → как 96 dpi
    assert strip_px(600.0) == STRIP_MAX_PX
    assert strip_px(10.0) == STRIP_MIN_PX


def test_docked_edge_detects_touching_edge():
    frame = QRect(1800, 100, 120, 260)  # right() == 1919 == экран
    assert docked_edge(frame, SCREEN) == "right"
    assert docked_edge(QRect(0, 100, 120, 260), SCREEN) == "left"
    assert docked_edge(QRect(400, 0, 120, 260), SCREEN) == "top"
    assert docked_edge(QRect(400, 820, 120, 260), SCREEN) == "bottom"


def test_docked_edge_allows_a_hairline_gap():
    frame = QRect(1920 - 120 - SNAP_TOLERANCE, 100, 120, 260)
    assert docked_edge(frame, SCREEN) == "right"


def test_docked_edge_ignores_a_visible_gap():
    assert docked_edge(QRect(1920 - 120 - 40, 100, 120, 260), SCREEN) is None
    assert docked_edge(QRect(800, 400, 120, 260), SCREEN) is None


def test_docked_edge_picks_the_closest_edge():
    # угол: до верха 0, до левого края 2 — сворачиваем вверх
    assert docked_edge(QRect(2, 0, 120, 260), SCREEN) == "top"


def test_flush_pos_pushes_the_frame_against_the_edge():
    frame = QRect(1900, 100, 120, 260)  # уехал за правый край
    pos = flush_pos(frame, SCREEN, "right")
    assert pos == QPoint(1920 - 120, 100)  # вторая ось не тронута


@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_collapsed_pos_leaves_only_the_strip(edge):
    frame = QRect(400, 300, 120, 260)
    strip = 13
    pos = collapsed_pos(frame, SCREEN, edge, strip)
    moved = QRect(pos, frame.size())
    visible = moved.intersected(SCREEN)
    seen = visible.width() if edge in ("left", "right") else visible.height()
    assert seen == strip


def test_tab_hit_area_covers_the_strip_with_a_margin():
    strip = 13
    frame = QRect(collapsed_pos(QRect(400, 300, 120, 260), SCREEN, "right", strip),
                  QRect(0, 0, 120, 260).size())
    area = tab_hit_area(frame, SCREEN)
    assert area.contains(QPoint(1915, 400))  # на полоске
    assert area.contains(QPoint(1919 - strip - HOVER_PAD + 2, 400))  # в запасе
    assert not area.contains(QPoint(1700, 400))  # далеко от края


# ---- поведение виджета --------------------------------------------------


def make_widget(app, edge_gap=0, size=(120, 260)):
    """Виджет с правым краем в `edge_gap` px от края экрана."""
    widget = new_widget()
    widget.resize(*size)
    screen = widget.screen().availableGeometry()
    widget.move(
        screen.right() - widget.width() + 1 - edge_gap,
        screen.top() + 40,
    )
    widget.collapse.set_enabled(True)
    return widget, screen


def test_docked_widget_collapses_to_a_strip(app):
    widget, screen = make_widget(app)
    assert widget.collapse.collapse(animated=False) is True
    assert widget.collapse.is_collapsed
    visible = widget.frameGeometry().intersected(screen)
    assert visible.width() == widget.collapse.strip()


def test_widget_with_a_gap_keeps_hanging(app):
    widget, _ = make_widget(app, edge_gap=40)
    before = widget.frameGeometry()
    assert widget.collapse.collapse(animated=False) is False
    assert not widget.collapse.is_collapsed
    assert widget.frameGeometry() == before


def test_collapse_docks_flush_first_then_expands_back_to_the_edge(app):
    widget, screen = make_widget(app, edge_gap=SNAP_TOLERANCE - 2)
    widget.collapse.collapse(animated=False)
    widget.collapse.expand(animated=False)
    assert not widget.collapse.is_collapsed
    assert widget.frameGeometry().right() == screen.right()


def test_strip_survives_a_mode_switch_and_a_resize(app):
    """Полоска одна и та же в полном и в мини-режиме, при любом размере."""
    widget = new_widget()
    widget.show()  # событие resize доходит только до показанного окна
    app.processEvents()
    widget.resize(120, 260)
    screen = widget.screen().availableGeometry()
    widget.move(screen.left(), screen.top() + 40)  # прижат к левому краю
    widget.collapse.set_enabled(True)
    widget.collapse.collapse(animated=False)
    strip = widget.collapse.strip()
    assert widget.frameGeometry().intersected(screen).width() == strip

    widget.set_mode("mini")
    widget.resize(90, 120)  # мини-режим ужали до другого размера кадра
    assert widget.collapse.is_collapsed
    assert widget.frameGeometry().intersected(screen).width() == strip


def test_collapsed_geometry_is_not_persisted(app):
    widget, _ = make_widget(app)
    widget._geometry_timer.stop()
    widget.collapse.collapse(animated=False)
    assert not widget._geometry_timer.isActive()


def test_turning_the_option_off_brings_the_widget_back(app):
    widget, screen = make_widget(app)
    widget.collapse.collapse(animated=False)
    widget.set_autohide(False)
    assert not widget.collapse.is_collapsed
    assert widget.frameGeometry().right() == screen.right()


def test_disabled_option_never_collapses(app):
    widget, _ = make_widget(app)
    widget.set_autohide(False)
    before = widget.frameGeometry()
    widget.collapse._tick()
    assert widget.frameGeometry() == before


def enter_event():
    point = QPointF(5.0, 5.0)
    return QEnterEvent(point, point, QPointF(100.0, 100.0))


def test_hover_panel_stays_hidden_while_collapsed(app):
    widget, _ = make_widget(app)
    widget.collapse.collapse(animated=False)
    widget.enterEvent(enter_event())
    assert widget.panel.isHidden()


def test_hover_panel_still_opens_when_not_collapsed(app):
    widget, _ = make_widget(app)
    widget.enterEvent(enter_event())
    assert not widget.panel.isHidden()
    widget.panel.hide()


def test_grabbing_the_strip_expands_before_the_drag(app):
    widget, screen = make_widget(app)
    widget.collapse.collapse(animated=False)
    widget._begin_interaction()
    assert not widget.collapse.is_collapsed
    assert widget.frameGeometry().right() == screen.right()
