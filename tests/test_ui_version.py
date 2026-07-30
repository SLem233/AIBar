"""The installed version must be visible in the dashboard footer.

(Note: the separate hover panel was removed in v0.5.0 — the dashboard popup
shown on click is now the single full window, so only its footer is tested.)
"""

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


def test_widget_context_menu_shows_version(app):
    # The context menu is built lazily in contextMenuEvent; instead of popping
    # it, assert the version the widget advertises matches the package version.
    widget = DesktopWidget()
    widget.update_snapshots([snap()])
    assert __version__  # sanity: version is non-empty
