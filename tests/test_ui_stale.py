"""Индикация несвежих данных: ошибка опроса не гасит кольцо.

Симптом, из-за которого это заведено: при 429 от Anthropic снапшот приходил с
пустыми `windows`, кольцо рисовалось прочерком, а в мини-режиме плитка Claude
пропадала из виджета целиком — «горячим» провайдер без окон стать не может.
"""

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.dashboard import ProviderCard
from aibar.ui.widget import DesktopWidget, GaugeTile


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def stale_snap(name="Claude", percent=88.0, error="HTTP 429"):
    return ProviderSnapshot(
        provider=name,
        windows=[RateWindow("Сессия (5ч)", percent)],
        error=error,
        http_status=429,
        stale=True,
    )


def normal_snap(name="Claude", percent=33.0):
    return ProviderSnapshot(
        provider=name, windows=[RateWindow("Сессия (5ч)", percent)]
    )


def card_labels(card):
    return [
        card.rows.itemAt(i).widget().text()
        for i in range(card.rows.count())
        if isinstance(card.rows.itemAt(i).widget(), QLabel)
    ]


def test_tile_keeps_drawing_the_ring_on_a_stale_snapshot(app):
    tile = GaugeTile("Claude")
    tile.update_snapshot(stale_snap())

    assert tile.gauge._percents == [88.0]  # кольцо не погасло в прочерк
    assert "⚠" in tile.caption.text()  # но помечено предупреждением


def test_tile_drops_the_warning_once_the_poll_recovers(app):
    tile = GaugeTile("Claude")
    tile.update_snapshot(stale_snap())
    tile.update_snapshot(normal_snap())

    assert "⚠" not in tile.caption.text()
    assert tile.gauge._percents == [33.0]


def test_stale_provider_stays_visible_in_mini_mode(app):
    """Главный симптом: плитка не должна исчезать из мини-виджета."""
    widget = DesktopWidget()
    widget.set_mode("mini")
    widget.set_mini_threshold(70.0)
    widget.update_snapshots([stale_snap(percent=88.0), normal_snap("Codex", 12.0)])

    assert not widget._tiles["Claude"].isHidden()


def test_card_shows_both_the_error_and_the_stale_numbers(app):
    card = ProviderCard()
    card.update_snapshot(stale_snap(percent=88.0))
    labels = card_labels(card)

    assert any("429" in text for text in labels)  # причина видна
    assert any("88%" in text for text in labels)  # и последние значения тоже


def test_card_without_any_stale_data_shows_the_error_alone(app):
    card = ProviderCard()
    card.update_snapshot(
        ProviderSnapshot(provider="Claude", error="HTTP 429", http_status=429)
    )
    labels = card_labels(card)

    assert any("429" in text for text in labels)
    assert not any("%" in text for text in labels)  # нулей из воздуха нет


def test_stale_snapshots_do_not_count_as_activity(app):
    """Иначе «рост» насчитается на повторе одних и тех же старых чисел."""
    widget = DesktopWidget()
    widget._record_activity([stale_snap()], now=1000.0)

    assert widget._activity[-1][1] == {}
