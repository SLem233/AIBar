"""Opening the bundled help page in the default browser."""

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from .config import CONFIG_DIR


def _bundled_help() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller: unpacked next to the module tree
        return Path(sys._MEIPASS) / "aibar" / "resources" / "help.html"
    return Path(__file__).parent / "resources" / "help.html"


def bundled_icon_path() -> Path:
    """Path to the bundled app icon (works frozen and from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "aibar" / "resources" / "aibar.ico"
    return Path(__file__).parent / "resources" / "aibar.ico"


def open_help() -> None:
    """Copy help.html to a stable location and open it in the browser.

    The onefile exe unpacks to a temp dir that dies with the process, so the
    browser must read from a persistent path.
    """
    source = _bundled_help()
    target = CONFIG_DIR / "help.html"
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError:
        target = source  # fall back to opening in place
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
