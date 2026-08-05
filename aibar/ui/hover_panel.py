"""Панель с полной разбивкой по провайдерам — всплывает при наведении на виджет.

Живёт отдельным безрамочным окном рядом с виджетом: показывает те же карточки,
что и дашборд, но по всем провайдерам сразу, независимо от режима виджета.
Позицию панели и её жизненный цикл ведёт сам виджет (см. widget.py).
"""

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, theme
from ..providers.base import ProviderSnapshot
from .dashboard import ProviderCard

# Безрамочное окно поверх всех, без кнопки на панели задач.
WIDGET_FLAGS = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool


class HoverPanel(QWidget):
    """Extended info shown while the mouse is over the widget (or the panel)."""

    hover_changed = Signal(bool)
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(WIDGET_FLAGS)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(400)
        self.setStyleSheet(
            f"""
            QLabel {{ color: {theme.TEXT_SECONDARY}; font-family: "{theme.FONT_FAMILY}"; }}
            #card {{
                background: {theme.SURFACE};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
            }}
            #cardTitle {{ color: {theme.TEXT_PRIMARY}; font-size: 15px; font-weight: 600; }}
            #cardPlan {{ color: {theme.TEXT_MUTED}; font-size: 12px; }}
            #header {{ color: {theme.TEXT_PRIMARY}; font-size: 14px; font-weight: 600; }}
            #footer {{ color: {theme.TEXT_MUTED}; font-size: 11px; }}
            QPushButton {{
                background: {theme.SURFACE};
                color: {theme.TEXT_SECONDARY};
                border: 1px solid {theme.BORDER};
                border-radius: 6px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; border-color: {theme.TEXT_MUTED}; }}
            """
        )
        self._cards: dict[str, ProviderCard] = {}
        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(8)
        self.footer = QLabel("")
        self.footer.setObjectName("footer")

        header = QLabel("Лимиты AI-провайдеров")
        header.setObjectName("header")
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh_requested)
        top = QHBoxLayout()
        top.addWidget(header)
        top.addStretch()
        top.addWidget(self.refresh_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addLayout(self.cards_layout)
        layout.addWidget(self.footer)

    def update_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        for snap in snapshots:
            card = self._cards.get(snap.provider)
            if card is None:
                card = ProviderCard()
                self._cards[snap.provider] = card
                self.cards_layout.addWidget(card)
            card.update_snapshot(snap)
        self.footer.setText(
            f"AIBar v{__version__} · Обновлено {datetime.now().strftime('%H:%M:%S')}"
        )
        self.adjustSize()

    def clear_cards(self) -> None:
        for card in self._cards.values():
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    # Keep the panel open while the mouse is over it
    def enterEvent(self, event) -> None:
        self.hover_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_changed.emit(False)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        # Translucent frameless window: paint the rounded surface manually.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor(255, 255, 255, 26))
        painter.setBrush(QColor(theme.PAGE))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)
        painter.end()
