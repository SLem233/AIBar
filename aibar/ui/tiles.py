"""Плитка виджета: кольцо провайдера, его имя и время до ближайшего сброса.

Подписи борются за место с кольцом. По ширине они не исчезают, а укорачиваются
многоточием (полное имя остаётся в подсказке); по высоте уступают место кольцу
по очереди — сначала уходит отсчёт, потом имя.
"""

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .. import theme
from ..providers.base import ProviderSnapshot, RateWindow
from .gauge import RadialGauge

# Подпись не прячем, а укорачиваем многоточием; прячем, когда не влезает и оно.
MIN_TEXT_W = 24
# Кольцо ниже этого нечитаемо — тогда строки текста уступают ему место.
MIN_GAUGE_H = 34

__all__ = ["GaugeTile", "soonest_countdown", "MIN_TEXT_W", "MIN_GAUGE_H"]


def soonest_countdown(windows: list[RateWindow], now: datetime | None = None) -> str:
    """Время до ближайшего сброса среди окон провайдера («2д 3ч», «43м»)."""
    soonest = min(
        (w for w in windows if w.resets_at), key=lambda w: w.resets_at, default=None
    )
    return soonest.reset_countdown(now) if soonest is not None else ""


class GaugeTile(QWidget):
    """One provider's gauge with a caption underneath."""

    LABEL_SPACING = 2

    def __init__(self, provider: str, parent=None):
        super().__init__(parent)
        self._caption_text = provider
        self._countdown_text = ""
        self.gauge = RadialGauge(scalable=True)
        self.caption = QLabel(provider)
        self.caption.setAlignment(Qt.AlignHCenter)
        self.caption.setStyleSheet(
            f'color: {theme.TEXT_SECONDARY}; font-family: "{theme.FONT_FAMILY}"; font-size: 11px;'
        )
        # Время до ближайшего сброса — отдельной строкой под именем провайдера.
        self.countdown = QLabel("")
        self.countdown.setAlignment(Qt.AlignHCenter)
        # Тот же цвет, что у имени провайдера: приглушённый TEXT_MUTED на
        # полупрозрачном кадре читался плохо. Размер шрифта держит иерархию.
        self.countdown.setStyleSheet(
            f'color: {theme.TEXT_SECONDARY}; font-family: "{theme.FONT_FAMILY}"; font-size: 10px;'
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(self.LABEL_SPACING)
        layout.addWidget(self.gauge, stretch=1)
        layout.addWidget(self.caption)
        layout.addWidget(self.countdown)
        self._apply_labels()

    def update_snapshot(self, snap: ProviderSnapshot) -> None:
        self.gauge.set_percents([w.used_percent for w in snap.windows])
        suffix = " ⏸" if snap.paused else (" ⚠" if snap.error else "")
        self._caption_text = f"{snap.provider}{suffix}"
        self._countdown_text = soonest_countdown(snap.windows)
        self._apply_labels()

    def _line_height(self, label: QLabel) -> int:
        return label.sizeHint().height()

    def _lines_that_fit(self) -> int:
        """Сколько строк текста поместится под кольцом (0, 1 или 2).

        Кольцо главнее: сначала уходит отсчёт, потом имя провайдера.
        """
        caption_h = self._line_height(self.caption) + self.LABEL_SPACING
        countdown_h = self._line_height(self.countdown) + self.LABEL_SPACING
        if self.height() - caption_h - countdown_h >= MIN_GAUGE_H:
            return 2
        if self.height() - caption_h >= MIN_GAUGE_H:
            return 1
        return 0

    def _apply_labels(self) -> None:
        """Уместить подписи в плитку: узкие — укоротить, низкой — убрать."""
        width = self.width()
        lines = self._lines_that_fit()
        fits_text = width >= MIN_TEXT_W

        show_caption = fits_text and lines >= 1
        self.caption.setVisible(show_caption)
        if show_caption:
            self.caption.setText(
                self.caption.fontMetrics().elidedText(
                    self._caption_text, Qt.ElideRight, width
                )
            )

        show_countdown = fits_text and lines >= 2 and bool(self._countdown_text)
        self.countdown.setVisible(show_countdown)
        if show_countdown:
            self.countdown.setText(
                self.countdown.fontMetrics().elidedText(
                    self._countdown_text, Qt.ElideRight, width
                )
            )

        # Имя целиком — в подсказке, когда на плитке оно урезано или скрыто.
        shortened = not show_caption or self.caption.text() != self._caption_text
        self.setToolTip(self._caption_text if shortened else "")

    def resizeEvent(self, event) -> None:
        self._apply_labels()
        super().resizeEvent(event)
