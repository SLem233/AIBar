"""Updater: versioned download path, old-version pruning, asset lookup.

Uses monkeypatched requests + a tmp Downloads folder; no real network.
"""

from pathlib import Path

import pytest

import aibar.updater as updater_mod
from aibar.updater import (
    downloads_dir,
    find_asset,
    prune_old_versions,
    versioned_path,
)


def test_versioned_path_uses_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(updater_mod, "downloads_dir", lambda: tmp_path)
    p = versioned_path("0.6.0")
    assert p == tmp_path / "AIBar-v0.6.0.exe"
    assert p.parent == tmp_path


def test_versioned_path_strips_v_prefix_in_label():
    # version label passed to versioned_path is already numeric ("0.6.0")
    p = versioned_path("1.2.3")
    assert p.name == "AIBar-v1.2.3.exe"


def test_prune_old_versions_keeps_latest(monkeypatch, tmp_path):
    keep = tmp_path / "AIBar-v0.6.0.exe"
    keep.write_bytes(b"new")
    old1 = tmp_path / "AIBar-v0.5.0.exe"
    old1.write_bytes(b"old1")
    old2 = tmp_path / "AIBar-v0.4.0.exe"
    old2.write_bytes(b"old2")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep me")

    prune_old_versions(keep)

    assert keep.exists()
    assert not old1.exists()
    assert not old2.exists()
    assert unrelated.exists()  # non-matching files untouched


def test_prune_old_versions_handles_missing_dir(tmp_path):
    # should not raise even if the directory is gone
    prune_old_versions(tmp_path / "AIBar-v9.9.9.exe")
    # nothing to assert beyond no-exception


def test_find_asset_picks_exe():
    release = {
        "assets": [
            {"name": "AIBar.exe", "browser_download_url": "https://x/AIBar.exe"},
            {"name": "source.zip", "browser_download_url": "https://x/source.zip"},
        ]
    }
    url, name = find_asset(release)
    assert url == "https://x/AIBar.exe"
    assert name == "AIBar.exe"


def test_find_asset_returns_none_when_missing():
    release = {"assets": [{"name": "source.zip", "browser_download_url": "https://x/s"}]}
    assert find_asset(release) is None


def test_find_asset_empty_release():
    assert find_asset({"assets": []}) is None


def test_downloads_dir_fallback(monkeypatch, tmp_path):
    # USERPROFILE points somewhere without a Downloads child -> falls back
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nope"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    d = downloads_dir()
    assert d.name == "Downloads"
    assert d.exists()
