"""The installed version must be visible in the dashboard and hover panel."""

import pytest
from PySide6.QtWidgets import QApplication

from aibar import __version__
from aibar.providers.base import ProviderSnapshot, RateWindow
from aibar.ui.dashboard import DashboardWindow
from aibar.ui.widget import DesktopWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def snap():
    return ProviderSnapshot(provider="Claude", windows=[RateWindow("Сессия (5ч)", 10.0)])


def test_dashboard_footer_shows_version(app):
    dashboard = DashboardWindow()
    dashboard.update_snapshots([snap()])
    assert f"v{__version__}" in dashboard.footer.text()


def test_hover_panel_footer_shows_version(app):
    widget = DesktopWidget()
    widget.update_snapshots([snap()])
    assert f"v{__version__}" in widget.panel.footer.text()
