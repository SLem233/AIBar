"""Popup dashboard window: one card per provider with gauge and window rows."""

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..providers.base import ProviderSnapshot
from .gauge import RadialGauge

CHIP = '<span style="color:{color}; font-size:14px;">●</span>'


class ProviderCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")

        self.gauge = RadialGauge(size=96)
        self.title = QLabel()
        self.title.setObjectName("cardTitle")
        self.plan = QLabel()
        self.plan.setObjectName("cardPlan")

        self.rows = QGridLayout()
        self.rows.setContentsMargins(0, 6, 0, 0)
        self.rows.setHorizontalSpacing(10)
        self.rows.setVerticalSpacing(4)
        self.rows.setColumnStretch(0, 1)  # label column absorbs width changes

        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.plan)

        right = QVBoxLayout()
        right.addLayout(head)
        right.addLayout(self.rows)
        right.addStretch()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(14)
        layout.addWidget(self.gauge, alignment=Qt.AlignTop)
        layout.addLayout(right, stretch=1)

    def _clear_rows(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_row(self, col0: str, col1: str = "", col2: str = "", tooltip: str = "") -> None:
        row = self.rows.rowCount()
        for col, text in enumerate((col0, col1, col2)):
            if not text:
                continue
            label = QLabel(text)
            label.setTextFormat(Qt.RichText)
            if tooltip and col == 2:
                label.setToolTip(tooltip)
            align = Qt.AlignLeft if col == 0 else Qt.AlignRight
            self.rows.addWidget(label, row, col, alignment=align | Qt.AlignVCenter)

    def update_snapshot(self, snap: ProviderSnapshot) -> None:
        self.title.setText(snap.provider)
        self.plan.setText(snap.plan)
        self._clear_rows()
        self.gauge.set_percents([w.used_percent for w in snap.windows])

        if snap.error:
            error = QLabel(f"⚠ {snap.error}")
            error.setWordWrap(True)
            error.setStyleSheet(f"color: {theme.WARNING};")
            self.rows.addWidget(error, 0, 0, 1, 3)
            return

        for i, window in enumerate(snap.windows):
            chip = CHIP.format(color=theme.RING_COLORS[i]) if i < 3 else "·"
            countdown = window.reset_countdown()
            self._add_row(
                f'{chip} <span style="color:{theme.TEXT_SECONDARY};">{window.label}</span>',
                f'<b style="color:{theme.TEXT_PRIMARY};">{window.used_percent:.0f}%</b>',
                f'<span style="color:{theme.TEXT_MUTED};">↺ {countdown}</span>'
                if countdown
                else "",
                tooltip=f"Сброс лимита через {countdown}" if countdown else "",
            )
        for key, value in snap.extra.items():
            self._add_row(
                f'<span style="color:{theme.TEXT_MUTED};">{key}</span>',
                f'<span style="color:{theme.TEXT_SECONDARY};">{value}</span>',
            )


class DashboardWindow(QWidget):
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setFixedWidth(400)
        self.setStyleSheet(
            f"""
            DashboardWindow {{
                background: {theme.PAGE};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
            }}
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

        header = QLabel("Лимиты AI-провайдеров")
        header.setObjectName("header")
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh_requested)

        top = QHBoxLayout()
        top.addWidget(header)
        top.addStretch()
        top.addWidget(self.refresh_btn)

        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(8)
        self._cards: dict[str, ProviderCard] = {}

        self.footer = QLabel("")
        self.footer.setObjectName("footer")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(10)
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
            f"Обновлено {datetime.now().strftime('%H:%M:%S')}"
        )
        self.adjustSize()

    def clear_cards(self) -> None:
        for card in self._cards.values():
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def show_at(self, x: int, y: int) -> None:
        self.adjustSize()
        screen = self.screen().availableGeometry()
        x = min(max(x - self.width() // 2, screen.left() + 8), screen.right() - self.width() - 8)
        y = min(y, screen.bottom() - self.height() - 8)
        if y < screen.top() + 8:
            y = screen.top() + 8
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
