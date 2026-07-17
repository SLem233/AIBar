"""Update check against the GitHub releases of this project."""

import re
import threading

import requests
from PySide6.QtCore import QObject, Signal

from . import __version__

LATEST_RELEASE_API = "https://api.github.com/repos/SLem233/AIBar/releases/latest"
RELEASES_URL = "https://github.com/SLem233/AIBar/releases/latest"


def _parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


class UpdateChecker(QObject):
    """Polls the latest GitHub release; emits when it is newer than this build."""

    update_available = Signal(str)  # latest version, e.g. "0.3.0"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False

    def check(self) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self) -> None:
        try:
            resp = requests.get(
                LATEST_RELEASE_API,
                headers={"Accept": "application/vnd.github+json"},
                timeout=20,
            )
            if resp.status_code != 200:
                return
            latest = _parse_version(resp.json().get("tag_name") or "")
            current = _parse_version(__version__)
            if latest and current and latest > current:
                self.update_available.emit(".".join(map(str, latest)))
        except (requests.RequestException, ValueError):
            pass  # silent: no network is not an error worth surfacing
        finally:
            self._busy = False
