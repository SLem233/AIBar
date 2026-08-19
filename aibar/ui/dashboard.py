"""Popup dashboard window: one card per provider with gauge and window rows."""

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, theme
from ..geoblock import PAUSED_MESSAGE
from ..providers.base import ProviderSnapshot
from .gauge import RadialGauge

CHIP = '<span style="color:{color}; font-size:13px;">●</span>'

# Плотность карточки. Провайдеров уже одиннадцать, и высота окна упирается в
# экран, поэтому кольцо и поля ужаты до предела, за которым кольцо перестаёт
# читаться: три дорожки по 6 px плюс проценты в центре.
GAUGE_SIZE = 72

# Отступ от края экрана, который окно оставляет себе сверху и снизу.
SCREEN_MARGIN = 16
# Ниже этого списка карточек прокручивать уже нечего — окно просто маленькое.
MIN_CARDS_HEIGHT = 120

# Дата продления / конца подписки — в подвале карточки, у нижнего края рамки.
# Остальные extra (кредиты, расход) остаются со строками окон.
RENEWAL_KEYS = frozenset({"Продление", "Подписка до"})


def cards_viewport_height(content: int, chrome: int, screen: int) -> int:
    """Высота видимой части списка карточек.

    Столько, сколько просит содержимое, но не выше остатка экрана после шапки,
    подвала и полей. Экран меньше минимума (или неизвестен) — отдаём минимум:
    лучше окно с прокруткой, чем окно нулевой высоты.
    """
    ceiling = screen - 2 * SCREEN_MARGIN - chrome
    return max(MIN_CARDS_HEIGHT, min(content, ceiling))


class ProviderCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")

        self.gauge = RadialGauge(size=GAUGE_SIZE)
        self.title = QLabel()
        self.title.setObjectName("cardTitle")
        self.plan = QLabel()
        self.plan.setObjectName("cardPlan")

        self.rows = QGridLayout()
        self.footer_rows = QGridLayout()
        for grid in (self.rows, self.footer_rows):
            grid.setContentsMargins(0, 2, 0, 0)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(1)
            grid.setColumnStretch(0, 1)  # label column absorbs width changes

        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch()
        head.addWidget(self.plan)

        right = QVBoxLayout()
        right.setSpacing(0)
        right.addLayout(head)
        right.addLayout(self.rows)
        right.addStretch()
        right.addLayout(self.footer_rows)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        layout.addWidget(self.gauge, alignment=Qt.AlignTop)
        layout.addLayout(right, stretch=1)

    def _clear_grid(self, grid: QGridLayout) -> None:
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _clear_rows(self) -> None:
        self._clear_grid(self.rows)
        self._clear_grid(self.footer_rows)

    def _add_row(
        self,
        col0: str,
        col1: str = "",
        col2: str = "",
        tooltip: str = "",
        *,
        grid: QGridLayout | None = None,
    ) -> None:
        target = self.rows if grid is None else grid
        row = target.rowCount()
        for col, text in enumerate((col0, col1, col2)):
            if not text:
                continue
            label = QLabel(text)
            label.setTextFormat(Qt.RichText)
            if tooltip and col == 2:
                label.setToolTip(tooltip)
            align = Qt.AlignLeft if col == 0 else Qt.AlignRight
            target.addWidget(label, row, col, alignment=align | Qt.AlignVCenter)

    def update_snapshot(self, snap: ProviderSnapshot) -> None:
        self.title.setText(snap.provider)
        self.plan.setText(snap.plan)
        self._clear_rows()
        self.gauge.set_percents([w.used_percent for w in snap.windows])

        if snap.paused:
            notice = QLabel(f"⏸ {PAUSED_MESSAGE}")
            notice.setWordWrap(True)
            notice.setStyleSheet(f"color: {theme.WARNING};")
            self.rows.addWidget(notice, 0, 0, 1, 3)
            # fall through: stale windows below stay visible

        if snap.error:
            error = QLabel(f"⚠ {snap.error}")
            error.setWordWrap(True)
            error.setStyleSheet(f"color: {theme.WARNING};")
            self.rows.addWidget(error, 0, 0, 1, 3)
            if not snap.stale:
                return
            # fall through: последние удачные значения ниже остаются на виду

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
                grid=self.footer_rows if key in RENEWAL_KEYS else self.rows,
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
            QLabel {{
                color: {theme.TEXT_SECONDARY};
                font-family: "{theme.FONT_FAMILY}";
                font-size: 12px;
            }}
            #card {{
                background: {theme.SURFACE};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
            }}
            #cardTitle {{ color: {theme.TEXT_PRIMARY}; font-size: 14px; font-weight: 600; }}
            #cardPlan {{ color: {theme.TEXT_MUTED}; font-size: 11px; }}
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
            #cards {{ background: transparent; }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {theme.TEXT_MUTED};
                border-radius: 4px;
                min-height: 32px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {theme.TEXT_SECONDARY}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            """
        )

        self.header = QLabel("Лимиты AI-провайдеров")
        self.header.setObjectName("header")
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self.refresh_requested)

        top = QHBoxLayout()
        top.addWidget(self.header)
        top.addStretch()
        top.addWidget(self.refresh_btn)

        # Карточки живут в прокручиваемой области: их высота ограничена экраном,
        # а шапка с кнопкой «Обновить» и подвал остаются на виду.
        cards_host = QWidget()
        cards_host.setObjectName("cards")
        self.cards_layout = QVBoxLayout(cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(6)
        self._cards: dict[str, ProviderCard] = {}

        self.scroll = QScrollArea()
        self.scroll.setWidget(cards_host)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self._cards_host = cards_host

        self.footer = QLabel("")
        self.footer.setObjectName("footer")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addWidget(self.scroll)
        layout.addWidget(self.footer)

    def _chrome_height(self) -> int:
        """Высота всего, кроме списка карточек: поля, шапка, подвал."""
        margins = self.layout().contentsMargins()
        head = max(self.header.sizeHint().height(), self.refresh_btn.sizeHint().height())
        return (
            margins.top()
            + margins.bottom()
            + 2 * self.layout().spacing()
            + head
            + self.footer.sizeHint().height()
        )

    def _screen_height(self) -> int:
        screen = self.screen()
        return screen.availableGeometry().height() if screen else 0

    def fit_to_screen(self) -> None:
        """Подогнать окно: список карточек не выше того, что даёт экран."""
        self._cards_host.adjustSize()  # sizeHint по свежим карточкам, а не по прошлым
        height = cards_viewport_height(
            self._cards_host.sizeHint().height(), self._chrome_height(), self._screen_height()
        )
        self.scroll.setFixedHeight(height)
        self.adjustSize()

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
        self.fit_to_screen()

    def clear_cards(self) -> None:
        for card in self._cards.values():
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def show_at(self, x: int, y: int) -> None:
        self.fit_to_screen()
        screen = self.screen().availableGeometry()
        x = min(max(x - self.width() // 2, screen.left() + 8), screen.right() - self.width() - 8)
        y = min(y, screen.bottom() - self.height() - 8)
        if y < screen.top() + 8:
            y = screen.top() + 8
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
