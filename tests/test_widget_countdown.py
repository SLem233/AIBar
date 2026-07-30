"""Per-tile countdown: soonest window reset formatted as two units (h:m / d:h)."""

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QApplication

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.widget import GaugeTile, _widget_countdown


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def at(delta):
    return NOW + delta


def test_no_resets_returns_empty():
    assert _widget_countdown([RateWindow("Сессия", 10.0)], now=NOW) == ""


def test_empty_windows_returns_empty():
    assert _widget_countdown([], now=NOW) == ""


def test_short_window_uses_hours_minutes():
    # exactly 1h23m -> "1ч 23м"
    win = RateWindow("Сессия (5ч)", 42.0, resets_at=at(timedelta(seconds=4980)))
    assert _widget_countdown([win], now=NOW) == "1ч 23м"


def test_weekly_window_uses_days_hours():
    # exactly 2d5h -> "2д 5ч"
    win = RateWindow("Неделя", 10.0, resets_at=at(timedelta(days=2, hours=5)))
    assert _widget_countdown([win], now=NOW) == "2д 5ч"


def test_picks_soonest_of_multiple_windows():
    soon = RateWindow("Сессия (5ч)", 42.0, resets_at=at(timedelta(minutes=30)))
    later = RateWindow("Неделя", 10.0, resets_at=at(timedelta(days=2)))
    assert _widget_countdown([later, soon], now=NOW) == "30м"


def test_under_one_hour_uses_minutes():
    win = RateWindow("Сессия", 42.0, resets_at=at(timedelta(minutes=5)))
    assert _widget_countdown([win], now=NOW) == "5м"


def test_expired_returns_zero():
    win = RateWindow("Сессия", 42.0, resets_at=at(timedelta(seconds=-10)))
    assert _widget_countdown([win], now=NOW) == "0м"


def test_tile_shows_countdown_below_name(app):
    """The production path (update_snapshot) reads the real clock; use offsets
    large enough that sub-second drift can't change the formatted units."""
    tile = GaugeTile("Codex")
    # 2d 5h 0m 30s rounds stably to "2д 5ч" even with small clock drift.
    weekly = RateWindow(
        "Неделя", 10.0, resets_at=datetime.now(timezone.utc) + timedelta(days=2, hours=5, seconds=30)
    )
    tile.update_snapshot(ProviderSnapshot(provider="Codex", windows=[weekly]))
    assert tile.caption.text() == "Codex"  # name stays clean
    assert tile.countdown_label.text() == "2д 5ч"
    assert not tile.countdown_label.isHidden()


def test_tile_hides_countdown_when_no_reset(app):
    tile = GaugeTile("Tavily")
    tile.update_snapshot(
        ProviderSnapshot(provider="Tavily", windows=[RateWindow("План", 5.0)])
    )
    assert tile.countdown_label.text() == ""
    assert tile.countdown_label.isHidden()
