"""Widget and dashboard indication for the paused (no VPN) state."""

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.dashboard import ProviderCard
from aibar.ui.widget import DesktopWidget, GaugeTile


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def paused_snap(name="Claude"):
    return ProviderSnapshot(
        provider=name, windows=[RateWindow("Сессия (5ч)", 33.0)], paused=True
    )


def normal_snap(name="Claude"):
    return ProviderSnapshot(provider=name, windows=[RateWindow("Сессия (5ч)", 33.0)])


def test_tile_caption_marks_paused(app):
    tile = GaugeTile("Claude")
    tile.update_snapshot(paused_snap())
    assert "⏸" in tile.caption.text()
    tile.update_snapshot(normal_snap())
    assert "⏸" not in tile.caption.text()


def test_widget_shows_vpn_badge_when_any_provider_paused(app):
    widget = DesktopWidget()
    widget.update_snapshots([paused_snap(), normal_snap("Codex")])
    assert not widget.vpn_badge.isHidden()
    widget.update_snapshots([normal_snap(), normal_snap("Codex")])
    assert widget.vpn_badge.isHidden()


def test_paused_snapshots_do_not_record_activity(app):
    widget = DesktopWidget()
    widget._record_activity([paused_snap()], now=1000.0)
    assert widget._activity[-1][1] == {}


def test_card_shows_paused_notice_and_keeps_stale_windows(app):
    card = ProviderCard()
    card.update_snapshot(paused_snap())
    labels = [
        card.rows.itemAt(i).widget().text()
        for i in range(card.rows.count())
        if isinstance(card.rows.itemAt(i).widget(), QLabel)
    ]
    assert any("VPN" in text for text in labels)  # the notice row
    assert any("33%" in text for text in labels)  # stale data still visible
