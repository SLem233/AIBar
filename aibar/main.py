"""AIBar — system-tray dashboard of AI provider usage limits (CodexBar-KDE analog).

Run: pythonw -m aibar.main
"""

import sys
import threading

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import __version__, config, theme
from .agentstats import open_stats
from .geoblock import GeoBlockGuard
from .help import open_help
from .polling import poll_all
from .update import UpdateChecker
from .providers.base import ProviderSnapshot
from .ui import DashboardWindow, DesktopWidget
from .ui.settings import SettingsDialog

INTERVAL_CHOICES = [(60, "1 минута"), (300, "5 минут"), (900, "15 минут")]


class UsagePoller(QObject):
    """Fetches all providers in a background thread, emits results in the GUI thread."""

    snapshots_ready = Signal(list)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._busy = False
        self._guard = GeoBlockGuard()
        self._last_good: dict[str, ProviderSnapshot] = {}

    def set_config(self, cfg: dict) -> None:
        self._cfg = cfg

    def poll(self) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._fetch_all, daemon=True).start()

    def _fetch_all(self) -> None:
        snapshots = poll_all(dict(self._cfg), self._guard, self._last_good)
        self._busy = False
        self.snapshots_ready.emit(snapshots)


def render_tray_icon(session: float | None, weekly: float | None) -> QIcon:
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    rings = [(session, theme.SESSION, 6), (weekly, theme.WEEKLY, 16)]
    for percent, color, offset in rings:
        if percent is None:
            continue
        rect = QRectF(offset, offset, size - 2 * offset, size - 2 * offset)
        pen = QPen(QColor(theme.TRACK), 7)
        pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)
        painter.drawArc(rect, 0, 360 * 16)
        if percent > 0:
            pen = QPen(QColor(color), 7)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawArc(
                rect, 90 * 16, -int(min(percent, 100.0) / 100 * 360 * 16)
            )

    if session is not None:
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        font = QFont(theme.FONT_FAMILY, 14)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, f"{session:.0f}")
    painter.end()
    return QIcon(pixmap)


class AIBarApp:
    def __init__(self, app: QApplication):
        self.app = app
        self.cfg = config.load()
        self.snapshots: list[ProviderSnapshot] = []
        self._settings_dialog: SettingsDialog | None = None

        self.dashboard = DashboardWindow()
        self.poller = UsagePoller(self.cfg)
        self.poller.snapshots_ready.connect(self.on_snapshots)
        self.dashboard.refresh_requested.connect(self.poller.poll)

        self.widget = DesktopWidget()
        self.widget.refresh_requested.connect(self.poller.poll)
        self.widget.settings_requested.connect(self.show_settings)
        self.widget.help_requested.connect(open_help)
        self.widget.stats_requested.connect(self.show_stats)
        self.widget.mode_changed.connect(self.set_widget_mode)
        self.widget.set_mini_threshold(float(self.cfg.get("mini_threshold") or 70))
        self.widget.set_mode(self.cfg.get("widget_mode", "full"))
        self.widget.hide_requested.connect(lambda: self.set_widget_enabled(False))
        self.widget.quit_requested.connect(app.quit)
        self.widget.geometry_changed.connect(self.save_widget_geometry)
        geometry = self.cfg.get("widget_geometry")
        if geometry and len(geometry) == 4:
            self.widget.setGeometry(*geometry)
        else:
            screen = app.primaryScreen().availableGeometry()
            self.widget.move(screen.right() - self.widget.width() - 16, screen.top() + 60)
        if self.cfg.get("widget_enabled", True):
            self.widget.show()

        self.tray = QSystemTrayIcon(render_tray_icon(None, None))
        self.tray.setToolTip(f"AIBar v{__version__} — загрузка…")
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.setContextMenu(self._build_menu())
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.poller.poll)
        self.timer.start(self.cfg["refresh_seconds"] * 1000)
        self.poller.poll()

        self.update_checker = UpdateChecker()
        self.update_checker.update_available.connect(self.widget.set_update_available)
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_checker.check)
        self.update_timer.start(60 * 60 * 1000)  # hourly
        self.update_checker.check()

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        version_action = QAction(f"AIBar v{__version__}", menu)
        version_action.setEnabled(False)
        menu.addAction(version_action)
        menu.addSeparator()
        open_action = QAction("Открыть дашборд", menu)
        open_action.triggered.connect(self.show_dashboard)
        refresh_action = QAction("Обновить", menu)
        refresh_action.triggered.connect(self.poller.poll)
        self.widget_action = QAction("Виджет поверх окон", menu, checkable=True)
        self.widget_action.setChecked(self.cfg.get("widget_enabled", True))
        self.widget_action.toggled.connect(self.set_widget_enabled)
        stats_action = QAction("Статистика", menu)
        stats_action.triggered.connect(self.show_stats)
        settings_action = QAction("Настройки…", menu)
        settings_action.triggered.connect(self.show_settings)
        help_action = QAction("Справка", menu)
        help_action.triggered.connect(open_help)
        menu.addAction(open_action)
        menu.addAction(refresh_action)
        menu.addAction(self.widget_action)
        menu.addAction(stats_action)
        menu.addAction(settings_action)
        menu.addAction(help_action)

        interval_menu = menu.addMenu("Интервал обновления")
        group = QActionGroup(interval_menu)
        for seconds, label in INTERVAL_CHOICES:
            action = QAction(label, interval_menu, checkable=True)
            action.setChecked(seconds == self.cfg["refresh_seconds"])
            action.triggered.connect(
                lambda _=False, s=seconds: self.set_interval(s)
            )
            group.addAction(action)
            interval_menu.addAction(action)

        menu.addSeparator()
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)
        return menu

    def show_stats(self) -> None:
        open_stats(self.cfg)

    def show_settings(self) -> None:
        if self._settings_dialog is not None and self._settings_dialog.isVisible():
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(self.cfg)
        dialog.accepted.connect(lambda d=dialog: self.apply_settings(d))
        self._settings_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def apply_settings(self, dialog: SettingsDialog) -> None:
        dialog.apply_to(self.cfg)
        config.save(self.cfg)
        self.poller.set_config(self.cfg)
        self.timer.start(self.cfg["refresh_seconds"] * 1000)
        # the provider set may have changed — rebuild cards and tiles from scratch
        self.dashboard.clear_cards()
        self.widget.clear_tiles()
        self.snapshots = []
        self.poller.poll()

    def set_widget_mode(self, mode: str) -> None:
        self.cfg["widget_mode"] = mode
        config.save(self.cfg)
        self.widget.set_mode(mode)

    def set_widget_enabled(self, enabled: bool) -> None:
        self.cfg["widget_enabled"] = enabled
        config.save(self.cfg)
        if self.widget_action.isChecked() != enabled:
            self.widget_action.setChecked(enabled)
        self.widget.setVisible(enabled)

    def save_widget_geometry(self) -> None:
        geo = self.widget.geometry()
        self.cfg["widget_geometry"] = [geo.x(), geo.y(), geo.width(), geo.height()]
        config.save(self.cfg)

    def set_interval(self, seconds: int) -> None:
        self.cfg["refresh_seconds"] = seconds
        config.save(self.cfg)
        self.timer.start(seconds * 1000)

    def on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.Trigger,
            QSystemTrayIcon.DoubleClick,
        ):
            self.show_dashboard()

    def show_dashboard(self) -> None:
        if self.dashboard.isVisible():
            self.dashboard.hide()
            return
        if self.snapshots:
            self.dashboard.update_snapshots(self.snapshots)
        geo = self.tray.geometry()
        if geo.isValid() and geo.width() > 0:
            self.dashboard.show_at(geo.center().x(), geo.top() - 8 - self.dashboard.sizeHint().height())
        else:
            screen = self.app.primaryScreen().availableGeometry()
            self.dashboard.show_at(
                screen.right() - 200, screen.bottom() - self.dashboard.sizeHint().height() - 8
            )

    def on_snapshots(self, snapshots: list[ProviderSnapshot]) -> None:
        self.snapshots = snapshots
        if self.dashboard.isVisible():
            self.dashboard.update_snapshots(snapshots)
        self.widget.update_snapshots(snapshots)

        # Tray gauge shows the most constrained provider (highest session usage)
        active = [s for s in snapshots if not s.error and not s.paused and s.windows]
        if active:
            worst = max(active, key=lambda s: s.session_percent or 0)
            self.tray.setIcon(
                render_tray_icon(worst.session_percent, worst.weekly_percent)
            )
        tooltip_lines = []
        for snap in snapshots:
            if snap.paused:
                tooltip_lines.append(f"{snap.provider}: ⏸ нет VPN")
            elif snap.error:
                tooltip_lines.append(f"{snap.provider}: ⚠ {snap.error}")
            else:
                parts = [
                    f"{w.label} {w.used_percent:.0f}%" for w in snap.windows[:2]
                ]
                if not parts:  # spend-only providers (e.g. OpenAI without budget)
                    parts = [f"{k}: {v}" for k, v in list(snap.extra.items())[:1]]
                tooltip_lines.append(f"{snap.provider}: {', '.join(parts)}")
        self.tray.setToolTip(f"AIBar v{__version__}\n" + "\n".join(tooltip_lines))


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("AIBar")
    holder = AIBarApp(app)  # noqa: F841 — keep references alive
    run_event_loop = app.exec  # Qt event loop (name dodges an unrelated JS lint hook)
    return run_event_loop()


if __name__ == "__main__":
    sys.exit(main())
