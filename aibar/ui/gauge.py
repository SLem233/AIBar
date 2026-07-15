"""Radial multi-ring gauge widget (QPainter), CodexBar-style."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import theme

START_ANGLE = 90 * 16  # 12 o'clock, Qt angles are in 1/16 degree, CCW positive


class RadialGauge(QWidget):
    """Concentric rings, one per rate window; center shows the first ring's %.

    Fixed-size by default (cards); pass scalable=True to let layouts resize it
    (desktop widget) — geometry then derives from the current widget size.
    """

    def __init__(self, size: int = 96, scalable: bool = False, parent=None):
        super().__init__(parent)
        self._percents: list[float] = []
        if scalable:
            self.setMinimumSize(48, 48)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        else:
            self.setFixedSize(size, size)

    def set_percents(self, percents: list[float]) -> None:
        self._percents = [max(0.0, min(100.0, p)) for p in percents[:3]]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        side = min(self.width(), self.height())
        x0 = (self.width() - side) / 2
        y0 = (self.height() - side) / 2
        ring_width = max(4.0, side * 0.085)
        gap = max(2.0, side * 0.03)

        inset = ring_width / 2
        if not self._percents:
            # placeholder for spend-only providers: bare track ring, muted dash
            rect = QRectF(x0 + inset, y0 + inset, side - 2 * inset, side - 2 * inset)
            pen = QPen(QColor(theme.TRACK), ring_width)
            pen.setCapStyle(Qt.FlatCap)
            painter.setPen(pen)
            painter.drawArc(rect, 0, 360 * 16)
            painter.setPen(QColor(theme.TEXT_MUTED))
            font = QFont(theme.FONT_FAMILY, max(7, int(side * 0.16)))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(x0, y0, side, side), Qt.AlignCenter, "—")
            painter.end()
            return

        for i, percent in enumerate(self._percents):
            offset = inset + i * (ring_width + gap)
            rect = QRectF(
                x0 + offset, y0 + offset, side - 2 * offset, side - 2 * offset
            )
            if rect.width() <= ring_width:
                break

            track_pen = QPen(QColor(theme.TRACK), ring_width)
            track_pen.setCapStyle(Qt.FlatCap)
            painter.setPen(track_pen)
            painter.drawArc(rect, 0, 360 * 16)

            if percent > 0:
                pen = QPen(QColor(theme.RING_COLORS[i]), ring_width)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                painter.drawArc(rect, START_ANGLE, -int(percent / 100 * 360 * 16))

        if self._percents:
            painter.setPen(QColor(theme.TEXT_PRIMARY))
            font = QFont(theme.FONT_FAMILY, max(7, int(side * 0.16)))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                QRectF(x0, y0, side, side),
                Qt.AlignCenter,
                f"{self._percents[0]:.0f}%",
            )
        painter.end()
