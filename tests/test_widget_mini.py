"""Мини-режим виджета: у лимита + последний, кто изменился."""

from datetime import datetime, timezone

import pytest
from PySide6.QtWidgets import QApplication

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.widget import DesktopWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def snap(name: str, *percents: float) -> ProviderSnapshot:
    windows = [
        RateWindow(f"Окно {i}", p, resets_at=datetime.now(timezone.utc))
        for i, p in enumerate(percents)
    ]
    return ProviderSnapshot(provider=name, windows=windows)


def visible(widget: DesktopWidget) -> set[str]:
    return {name for name, tile in widget._tiles.items() if not tile.isHidden()}


def seed(widget: DesktopWidget, *snaps: ProviderSnapshot) -> None:
    """Плитки без истории: update_snapshots пишет time.time() и снесёт метки."""
    widget.update_snapshots(list(snaps))
    widget._activity.clear()
    widget._last_solo = None


def poll(widget: DesktopWidget, now: float, *snaps: ProviderSnapshot) -> None:
    widget._snapshots = list(snaps)
    for s in snaps:
        widget._tiles[s.provider].update_snapshot(s)
    widget._record_activity(list(snaps), now=now)
    widget._apply_visibility()


def test_mini_shows_hot_and_the_last_who_grew(app):
    """Claude на 100% недели остаётся, рядом — Grok, который только что вырос."""
    widget = DesktopWidget()
    widget.set_mode("mini")
    widget.set_mini_threshold(70.0)
    claude = snap("Claude", 0.0, 100.0)
    grok = snap("Grok", 0.0)
    codex = snap("Codex", 12.0)
    seed(widget, claude, grok, codex)
    poll(widget, 1000.0, claude, grok, codex)
    poll(widget, 1060.0, claude, snap("Grok", 7.0), codex)
    assert visible(widget) == {"Claude", "Grok"}


def test_mini_without_anyone_hot_shows_only_the_last_growth(app):
    """Никто не у порога — только тот, чьи лимиты росли."""
    widget = DesktopWidget()
    widget.set_mode("mini")
    widget.set_mini_threshold(70.0)
    seed(widget, snap("Claude", 0.0), snap("Grok", 0.0), snap("Codex", 8.0))
    poll(widget, 1000.0, snap("Claude", 0.0), snap("Grok", 0.0), snap("Codex", 8.0))
    poll(widget, 1060.0, snap("Claude", 0.0), snap("Grok", 7.0), snap("Codex", 8.0))
    assert visible(widget) == {"Grok"}


def test_mini_keeps_last_growth_after_the_window_goes_quiet(app):
    """Затишье: дельта пустая, остаётся тот, кто менялся раньше, плюс горячий."""
    widget = DesktopWidget()
    widget.set_mode("mini")
    widget.set_mini_threshold(70.0)
    claude = snap("Claude", 0.0, 100.0)
    seed(widget, claude, snap("Grok", 0.0))
    poll(widget, 1000.0, claude, snap("Grok", 0.0))
    poll(widget, 1060.0, claude, snap("Grok", 7.0))
    poll(widget, 1120.0, claude, snap("Grok", 7.0))
    assert visible(widget) == {"Claude", "Grok"}
