"""Отсчёт до сброса лимита на плитке виджета.

Раньше время до сброса было видно только в ховер-панели и на дашборде. Теперь
оно стоит отдельной строкой под именем провайдера.

Место под текст даётся до последнего: узкая плитка не прячет подписи, а
укорачивает их многоточием (имени «Claude» хватает 36 px, отсчёту — 31 px, и
прятать их при 60 px было рано). Убираются строки только когда кольцу иначе не
остаётся места по высоте: сначала уходит отсчёт, потом имя.
"""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QApplication, QLayout

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.widget import GaugeTile, soonest_countdown

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def window(label, percent, *, hours=None, days=None, minutes=None):
    resets_at = None
    if (hours, days, minutes) != (None, None, None):
        resets_at = NOW + timedelta(hours=hours or 0, days=days or 0, minutes=minutes or 0)
    return RateWindow(label, percent, resets_at=resets_at)


# ---- выбор окна и формат ---------------------------------------------------
def test_countdown_shows_the_soonest_window():
    windows = [window("Неделя", 10, days=2), window("Сессия", 80, hours=5, minutes=12)]
    assert soonest_countdown(windows, NOW) == "5ч 12м"


def test_days_are_shown_with_hours():
    assert soonest_countdown([window("Неделя", 10, days=2, hours=3)], NOW) == "2д 3ч"


def test_less_than_an_hour_is_minutes_only():
    assert soonest_countdown([window("Сессия", 10, minutes=43)], NOW) == "43м"


def test_windows_without_a_reset_time_give_nothing():
    assert soonest_countdown([window("Кредиты", 10)], NOW) == ""
    assert soonest_countdown([], NOW) == ""


def test_elapsed_window_is_not_negative():
    assert soonest_countdown([window("Сессия", 10, hours=-3)], NOW) == "сейчас"


# ---- плитка ---------------------------------------------------------------
def snapshot(*windows, provider="Claude"):
    return ProviderSnapshot(provider=provider, windows=list(windows))


def make_tile(app, provider="Claude", size=(120, 120)):
    """Показанная плитка: событие resize доходит только до показанного окна.

    Внутри виджета плитку ужимает родительский layout — он выдаёт детям место
    даже меньше их минимума. Отдельному окну это правило мешает, поэтому в
    тесте ограничение снимаем.
    """
    tile = GaugeTile(provider)
    tile.layout().setSizeConstraint(QLayout.SetNoConstraint)
    tile.setMinimumSize(0, 0)
    tile.resize(*size)
    tile.show()
    app.processEvents()
    return tile


def resize_tile(app, tile, width, height):
    tile.resize(width, height)
    app.processEvents()


def test_tile_shows_the_countdown_under_the_name(app):
    tile = make_tile(app)
    session = window("Сессия", 40, hours=1, minutes=22)
    tile.update_snapshot(snapshot(session))
    assert tile.countdown.text() == soonest_countdown([session])
    assert not tile.countdown.isHidden()
    assert tile.countdown.y() > tile.caption.y()
    tile.deleteLater()


def test_tile_hides_the_line_when_there_is_nothing_to_count(app):
    tile = make_tile(app, provider="Tavily")
    tile.update_snapshot(snapshot(window("Кредиты", 40), provider="Tavily"))
    assert tile.countdown.text() == ""
    assert tile.countdown.isHidden()
    tile.deleteLater()


def test_narrow_tile_keeps_both_lines(app):
    """60 px хватает обеим строкам — раньше на этой ширине они пропадали."""
    tile = make_tile(app)
    tile.update_snapshot(snapshot(window("Сессия", 40, hours=2)))
    resize_tile(app, tile, 60, 120)
    assert not tile.caption.isHidden()
    assert not tile.countdown.isHidden()
    tile.deleteLater()


def test_very_narrow_tile_shortens_the_name(app):
    """Имя не исчезает, а укорачивается; целиком оно уходит в подсказку."""
    tile = make_tile(app, provider="OpenAI API")
    tile.update_snapshot(snapshot(window("Сессия", 40, hours=2), provider="OpenAI API"))
    resize_tile(app, tile, 34, 120)
    assert not tile.caption.isHidden()
    assert tile.caption.text() != "OpenAI API"
    assert "…" in tile.caption.text()
    assert tile.toolTip() == "OpenAI API"
    tile.deleteLater()


def test_full_name_needs_no_tooltip(app):
    tile = make_tile(app)
    tile.update_snapshot(snapshot(window("Сессия", 40, hours=2)))
    assert tile.caption.text() == "Claude"
    assert tile.toolTip() == ""
    tile.deleteLater()


def test_hair_thin_tile_drops_the_text_entirely(app):
    tile = make_tile(app)
    tile.update_snapshot(snapshot(window("Сессия", 40, hours=2)))
    resize_tile(app, tile, 18, 120)  # не влезает даже многоточие
    assert tile.caption.isHidden()
    assert tile.countdown.isHidden()
    tile.deleteLater()


def test_short_tile_drops_the_countdown_but_keeps_the_name(app):
    """В мини-режиме кадр низкий: кольцо важнее отсчёта."""
    tile = make_tile(app)
    tile.update_snapshot(snapshot(window("Сессия", 40, hours=2)))
    resize_tile(app, tile, 120, 50)
    assert not tile.caption.isHidden()
    assert tile.countdown.isHidden()
    tile.deleteLater()


def test_flat_tile_keeps_only_the_ring(app):
    tile = make_tile(app)
    tile.update_snapshot(snapshot(window("Сессия", 40, hours=2)))
    resize_tile(app, tile, 120, 34)
    assert tile.caption.isHidden()
    assert tile.countdown.isHidden()
    tile.deleteLater()


def test_countdown_returns_when_the_tile_grows_back(app):
    tile = make_tile(app, size=(120, 50))
    tile.update_snapshot(snapshot(window("Сессия", 40, hours=2)))
    assert tile.countdown.isHidden()
    resize_tile(app, tile, 120, 140)
    assert not tile.countdown.isHidden()
    tile.deleteLater()
