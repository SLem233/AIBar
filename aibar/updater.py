"""Download the latest upstream release into the user's Downloads folder and
keep a Desktop shortcut pointed at the newest version.

This is the *action* layer (download + apply). The version-comparison badge
(``update.UpdateChecker``) is separate and stays as-is.

Update source is the upstream repo ``SLem233/AIBar`` (per project decision):
releases on a downstream fork are *not* seen by the checker.
"""

import os
import re
import subprocess
import threading
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QThread, Signal

from . import __version__
from .update import LATEST_RELEASE_API, _parse_version

ASSET_NAME = "AIBar.exe"
SHORTCUT_NAME = "AIBar.lnk"


def downloads_dir() -> Path:
    """The user's Downloads folder, with sensible fallbacks."""
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    known = Path(profile) / "Downloads"
    if known.is_dir():
        return known
    fallback = Path.home() / "Downloads"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def desktop_dir() -> Path:
    """The user's Desktop folder, with sensible fallbacks."""
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    desktop = Path(profile) / "Desktop"
    if desktop.is_dir():
        return desktop
    fallback = Path.home() / "Desktop"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def versioned_path(version: str) -> Path:
    """Download target path for a given version, e.g. Downloads/AIBar-v0.6.0.exe."""
    safe = ".".join(str(p) for p in version) if not isinstance(version, str) else version
    return downloads_dir() / f"AIBar-v{safe}.exe"


def prune_old_versions(keep: Path) -> None:
    """Delete older AIBar-v*.exe in Downloads except the one we just downloaded."""
    if not keep.parent.is_dir():
        return
    for old in keep.parent.glob("AIBar-v*.exe"):
        try:
            if old.resolve() != keep.resolve():
                old.unlink()
        except OSError:
            pass


def create_shortcut(target: Path) -> Path:
    """Create (or overwrite) the Desktop shortcut ``AIBar.lnk`` -> ``target``.

    Uses the Windows Script Host COM via PowerShell (available on any
    Win10+), so no extra Python dependency is required. Returns the shortcut
    path.
    """
    lnk = desktop_dir() / SHORTCUT_NAME
    target_str = str(target).replace("'", "''")
    lnk_str = str(lnk).replace("'", "''")
    cmd = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk_str}'); "
        f"$s.TargetPath = '{target_str}'; "
        f"$s.WorkingDirectory = '{target_str}'; "
        "$s.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # Best-effort: shortcut is a convenience, not a requirement.
        pass
    return lnk


def fetch_latest_release() -> dict:
    """Return the latest release JSON from GitHub, or raise on failure."""
    resp = requests.get(
        LATEST_RELEASE_API,
        headers={"Accept": "application/vnd.github+json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def find_asset(release: dict) -> tuple[str, str] | None:
    """Return (browser_download_url, asset_name) for the exe asset, or None."""
    for asset in release.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            return asset.get("browser_download_url", ""), asset.get("name", ASSET_NAME)
    return None


class Updater(QObject):
    """Background download of the latest release; emits progress and result."""

    checked = Signal(str)        # latest version string (even if not newer)
    up_to_date = Signal(str)     # current version is the latest
    progress = Signal(int)       # 0..100 percent of the download
    downloaded = Signal(str, str)  # (version, local path)
    failed = Signal(str)         # human-readable error message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: "_DownloadWorker" | None = None
        self._cancel = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def check_and_download(self) -> None:
        """Query the latest release and download it if newer than current."""
        if self.is_running:
            return
        self._cancel.clear()
        current = _parse_version(__version__)
        self._thread = QThread()
        self._worker = _DownloadWorker(current, self._cancel)
        self._worker.moveToThread(self._thread)
        self._worker.checked.connect(self.checked)
        self._worker.up_to_date.connect(self.up_to_date)
        self._worker.progress.connect(self.progress)
        self._worker.downloaded.connect(self.downloaded)
        self._worker.failed.connect(self.failed)
        self._worker.finished.connect(self._thread.quit)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()


class _DownloadWorker(QObject):
    """Runs in a worker thread: fetch metadata, stream the exe, prune, shortcut."""

    checked = Signal(str)
    up_to_date = Signal(str)
    progress = Signal(int)
    downloaded = Signal(str, str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, current_version, cancel_event):
        super().__init__()
        self._current = current_version
        self._cancel = cancel_event

    def run(self) -> None:
        try:
            release = fetch_latest_release()
            latest_str = release.get("tag_name") or ""
            latest = _parse_version(latest_str)
            version_label = ".".join(str(p) for p in latest) if latest else latest_str
            self.checked.emit(version_label)
            if not latest or self._current is None or latest <= self._current:
                self.up_to_date.emit(__version__)
                return
            asset = find_asset(release)
            if not asset:
                self.failed.emit("В последнем релизе нет файла AIBar.exe")
                return
            url, _name = asset
            dest = versioned_path(version_label)
            self._stream_download(url, dest)
            if self._cancel.is_set():
                # Clean up a partial download on cancel.
                try:
                    dest.unlink()
                except OSError:
                    pass
                self.failed.emit("Загрузка отменена")
                return
            prune_old_versions(dest)
            create_shortcut(dest)
            self.downloaded.emit(version_label, str(dest))
        except requests.RequestException as exc:
            self.failed.emit(f"Сетевая ошибка: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface any unexpected failure
            self.failed.emit(f"Не удалось проверить обновления: {exc}")
        finally:
            self.finished.emit()

    def _stream_download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            tmp = dest.with_suffix(".exe.part")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if self._cancel.is_set():
                        break
                    if chunk:
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            self.progress.emit(min(100, int(done * 100 / total)))
            if not self._cancel.is_set():
                tmp.replace(dest)
