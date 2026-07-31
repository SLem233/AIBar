"""Плашки виджета: доступное обновление и пауза из-за выключенного VPN."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel

from .. import theme
from ..geoblock import PAUSED_MESSAGE
from ..update import RELEASES_URL

PILL_STYLE = f"""
    background: rgba(250, 178, 25, 38);
    color: {theme.WARNING};
    border: 1px solid {theme.WARNING};
    border-radius: 7px;
    padding: 2px 6px;
    font-family: "{theme.FONT_FAMILY}";
    font-size: 10px;
    font-weight: 600;
"""


class UpdateBadge(QLabel):
    """Clickable pill shown when a newer release is published."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(PILL_STYLE)
        self.hide()

    def show_version(self, version: str) -> None:
        self._version = version
        self._relabel()
        self.show()

    def _relabel(self) -> None:
        wide = self.parentWidget() is None or self.parentWidget().width() >= 90
        self.setText("↓ Update available" if wide else "↓ Update")
        self.setToolTip(
            f"Доступна версия {getattr(self, '_version', '')} — нажмите, чтобы открыть страницу загрузки"
        )

    def relabel_for_width(self) -> None:
        if self.isVisible():
            self._relabel()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            QDesktopServices.openUrl(QUrl(RELEASES_URL))
        super().mousePressEvent(event)


class VpnBadge(QLabel):
    """Pill shown while polling is paused because the VPN is off."""

    def __init__(self, parent=None):
        super().__init__("⏸ нет VPN", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(PAUSED_MESSAGE)
        self.setStyleSheet(PILL_STYLE)
        self.hide()
