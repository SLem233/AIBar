"""Плотность дашборда: карточка провайдера не должна расти вхолостую.

Цифры — не украшение: с одиннадцатью провайдерами окно упирается в высоту
экрана, и каждый десяток пикселей на карточку стоит сотни на окне. Границы
взяты с запасом к фактическим значениям, чтобы тест ловил разбухание вёрстки,
а не дрожание шрифтовых метрик.
"""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QApplication

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.dashboard import (
    MIN_CARDS_HEIGHT,
    SCREEN_MARGIN,
    DashboardWindow,
    ProviderCard,
    cards_viewport_height,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


def window(label: str, percent: float, hours: float | None = None) -> RateWindow:
    resets = NOW + timedelta(hours=hours) if hours else None
    return RateWindow(label, percent, resets_at=resets)


def snapshot(name: str, windows: int = 2, extras: int = 1) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider=name,
        plan="Max 5x",
        windows=[window(f"Окно {i}", 30 + i, 5) for i in range(windows)],
        extra={f"Строка {i}": "01.01.2027" for i in range(extras)},
    )


def test_typical_card_is_not_taller_than_the_gauge_needs(app):
    """Обычная карточка (2 окна + строка) — не выше сотни пикселей."""
    card = ProviderCard()
    card.update_snapshot(snapshot("Claude"))
    assert card.sizeHint().height() <= 100


def test_card_grows_only_with_extra_rows(app):
    """Лишние строки добавляют высоту скупо: не больше 20 px на строку."""
    small = ProviderCard()
    small.update_snapshot(snapshot("A", windows=1, extras=0))
    big = ProviderCard()
    big.update_snapshot(snapshot("B", windows=3, extras=3))
    per_row = (big.sizeHint().height() - small.sizeHint().height()) / 5
    assert per_row <= 20


def test_short_list_does_not_get_a_scrollbar():
    """Содержимое влезает в экран — область карточек ровно по содержимому."""
    assert cards_viewport_height(content=600, chrome=80, screen=1080) == 600


def test_long_list_is_capped_by_the_screen():
    """Содержимое выше экрана — область режется, остаток уходит в прокрутку."""
    assert cards_viewport_height(content=1400, chrome=80, screen=1080) == 1080 - 2 * SCREEN_MARGIN - 80


def test_tiny_or_unknown_screen_still_leaves_a_usable_window():
    """Экран не определился (0) — окно не схлопывается в ноль."""
    assert cards_viewport_height(content=1400, chrome=80, screen=0) == MIN_CARDS_HEIGHT


def test_eleven_providers_fit_the_screen(app):
    """Полный набор провайдеров умещается в экран, на котором открыт дашборд."""
    win = DashboardWindow()
    win.update_snapshots([snapshot(f"P{i}") for i in range(11)])
    assert win.height() <= win.screen().availableGeometry().height() - SCREEN_MARGIN


def test_window_shrinks_back_when_providers_are_few(app):
    """Двум провайдерам достаётся ровно их высота, а не потолок экрана."""
    win = DashboardWindow()
    win.update_snapshots([snapshot(f"P{i}") for i in range(2)])
    tall = win.height()
    assert tall < win.screen().availableGeometry().height() // 2
