"""Copilot provider: api.github.com/copilot_internal/user parsing.

Live probe (chelaxian account) returned:
    {
      "login": "chelaxian",
      "access_type_sku": "free_limited_copilot",
      "copilot_plan": "individual",
      "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
      "quota_snapshots": {
        "chat":                {"entitlement": 200,  "remaining": 200,  "unlimited": false, ...},
        "completions":         {"entitlement": 2000, "remaining": 2000, "unlimited": false, ...},
        "premium_interactions":{"entitlement": 0,    "remaining": 0,    "unlimited": false, ...}
      }
    }

Premium is dropped (entitlement=0), chat and completions render as windows,
reset countdown pulls from quota_reset_date_utc.
"""

import json
from datetime import timezone

import pytest

from aibar.providers import copilot
from aibar.providers.base import ProviderSnapshot


class FakeResp:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content if content else (
            json.dumps(payload).encode() if payload is not None else b""
        )

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ---- _snapshot_window ----------------------------------------------------
def test_snapshot_window_computes_percent():
    from datetime import datetime

    reset = datetime(2026, 9, 1, tzinfo=timezone.utc)
    win = copilot._snapshot_window("chat", {"entitlement": 200, "remaining": 50}, reset)
    assert win.label == "Chat"
    assert win.used_percent == pytest.approx(75.0)  # (200-50)/200
    assert win.resets_at == reset


def test_snapshot_window_zero_entitlement_returns_none():
    """entitlement=0 (quota disabled) → no window, not 0%."""
    win = copilot._snapshot_window("premium_interactions", {"entitlement": 0, "remaining": 0}, None)
    assert win is None


def test_snapshot_window_unlimited_pins_zero():
    win = copilot._snapshot_window("chat", {"unlimited": True}, None)
    assert win is not None
    assert win.label == "Chat ∞"
    assert win.used_percent == 0.0


def test_snapshot_window_clamps_to_100():
    """remaining < 0 (overage) shouldn't blow past 100%."""
    win = copilot._snapshot_window("chat", {"entitlement": 200, "remaining": -50}, None)
    assert win.used_percent == 100.0


# ---- fetch ---------------------------------------------------------------
def test_fetch_no_token_anywhere(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: None)
    snap = copilot.fetch({"copilot_token": ""})
    assert snap.error and "gh auth login" in snap.error


def test_fetch_invalid_token_string(monkeypatch):
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: None)
    snap = copilot.fetch({"copilot_token": "with\nnewline"})
    assert snap.error and "не токен" in snap.error


def test_fetch_401_unauthorized(monkeypatch):
    def fake_get(url, headers=None, timeout=30):
        return FakeResp(401)

    monkeypatch.setattr(copilot.requests, "get", fake_get)
    snap = copilot.fetch({"copilot_token": "ghp_0123456789abcdef0123456789abcdef"})
    assert snap.error and "авторизован" in snap.error
    assert snap.http_status == 401


def test_fetch_404_no_copilot_access(monkeypatch):
    def fake_get(url, headers=None, timeout=30):
        return FakeResp(404)

    monkeypatch.setattr(copilot.requests, "get", fake_get)
    snap = copilot.fetch({"copilot_token": "ghp_0123456789abcdef0123456789abcdef"})
    assert snap.error and "не подключён" in snap.error


def test_fetch_free_limited_copilot(monkeypatch):
    """Full free-tier payload: chat + completions windows, premium dropped."""
    payload = {
        "login": "octocat",
        "access_type_sku": "free_limited_copilot",
        "copilot_plan": "individual",
        "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
        "quota_snapshots": {
            "chat": {"entitlement": 200, "remaining": 50, "unlimited": False},
            "completions": {"entitlement": 2000, "remaining": 1500, "unlimited": False},
            "premium_interactions": {"entitlement": 0, "remaining": 0, "unlimited": False},
        },
    }

    def fake_get(url, headers=None, timeout=30):
        return FakeResp(200, payload=payload)

    monkeypatch.setattr(copilot.requests, "get", fake_get)
    snap = copilot.fetch({"copilot_token": "ghp_0123456789abcdef0123456789abcdef"})
    assert snap.error is None
    assert snap.plan == "Free"
    assert snap.extra["Аккаунт"] == "octocat"
    assert snap.extra["Сброс"] == "01.09.2026"
    labels = [w.label for w in snap.windows]
    assert labels == ["Chat", "Completions"]  # premium dropped, no ∞
    chat = snap.windows[0]
    assert chat.used_percent == pytest.approx(75.0)  # (200-50)/200
    completions = snap.windows[1]
    assert completions.used_percent == pytest.approx(25.0)  # (2000-1500)/2000
    assert chat.resets_at.year == 2026 and chat.resets_at.month == 9


def test_fetch_pro_plan_unlimited_chat(monkeypatch):
    """Pro plans may set chat.unlimited=true → pinned 0% with ∞ label."""
    payload = {
        "login": "pro_user",
        "access_type_sku": "copilot_pro",
        "copilot_plan": "individual",
        "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
        "quota_snapshots": {
            "chat": {"unlimited": True},
            "completions": {"entitlement": 1000, "remaining": 800, "unlimited": False},
            "premium_interactions": {"entitlement": 500, "remaining": 100, "unlimited": False},
        },
    }

    def fake_get(url, headers=None, timeout=30):
        return FakeResp(200, payload=payload)

    monkeypatch.setattr(copilot.requests, "get", fake_get)
    snap = copilot.fetch({"copilot_token": "ghp_0123456789abcdef0123456789abcdef"})
    assert snap.plan == "Pro"
    labels = [w.label for w in snap.windows]
    assert labels == ["Chat ∞", "Completions", "Premium"]
    assert snap.windows[0].used_percent == 0.0  # unlimited pinned
    assert snap.windows[2].used_percent == pytest.approx(80.0)  # premium 400/500


def test_token_from_env_takes_precedence_over_gh(monkeypatch):
    """GITHUB_TOKEN env var beats gh CLI subprocess."""
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-1234567890")
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: "gh-cli-token")
    assert copilot._api_token({}) == "env-token-1234567890"


def test_token_from_settings_beats_both_env_and_gh(monkeypatch):
    """Explicit copilot_token in settings is highest priority."""
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-1234567890")
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: "gh-cli-token")
    assert copilot._api_token({"copilot_token": "explicit-ghp-token"}) == "explicit-ghp-token"


# ---- _token_from_gh_cli security & UX ------------------------------------
def test_gh_cli_no_subprocess_when_which_returns_none(monkeypatch):
    """If shutil.which('gh') returns None, no subprocess is launched.

    This is the security core: never trust a relative 'gh' that CreateProcess
    would resolve from the exe directory or CWD on Windows.
    """
    monkeypatch.setattr(copilot.shutil, "which", lambda name: None)
    launched = []
    monkeypatch.setattr(
        copilot.subprocess, "run",
        lambda *a, **kw: launched.append(a) or None,
    )
    # Reset cache so the function re-evaluates.
    monkeypatch.setattr(copilot, "_gh_cli_resolved", False)
    monkeypatch.setattr(copilot, "_gh_cli_token_cache", None)
    result = copilot._token_from_gh_cli()
    assert result is None
    assert launched == []  # never called subprocess


def test_gh_cli_launches_by_absolute_path(monkeypatch):
    """subprocess.run argv[0] is the absolute path returned by shutil.which,
    never the bare 'gh' string. This blocks planted gh.exe hijacking."""
    abs_path = "/usr/bin/gh" if copilot.sys.platform != "win32" else r"C:\Program Files\GitHub CLI\gh.exe"
    monkeypatch.setattr(copilot.shutil, "which", lambda name: abs_path)

    captured = {}

    class FakeResult:
        returncode = 0
        stdout = "gho_token_1234567890abcdef\n"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return FakeResult()

    monkeypatch.setattr(copilot.subprocess, "run", fake_run)
    monkeypatch.setattr(copilot, "_gh_cli_resolved", False)
    monkeypatch.setattr(copilot, "_gh_cli_token_cache", None)

    result = copilot._token_from_gh_cli()
    assert result == "gho_token_1234567890abcdef"
    assert captured["argv"][0] == abs_path  # absolute, not "gh"


def test_gh_cli_passes_create_no_window_on_windows(monkeypatch):
    """On Windows, creationflags includes CREATE_NO_WINDOW (no console flash)."""
    monkeypatch.setattr(copilot.sys, "platform", "win32")
    monkeypatch.setattr(copilot.shutil, "which", lambda name: r"C:\gh.exe")
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        captured.update(kw)
        return FakeResult()

    monkeypatch.setattr(copilot.subprocess, "run", fake_run)
    monkeypatch.setattr(copilot, "_gh_cli_resolved", False)
    monkeypatch.setattr(copilot, "_gh_cli_token_cache", None)
    copilot._token_from_gh_cli()
    flags = captured.get("creationflags", 0)
    # CREATE_NO_WINDOW = 0x08000000
    assert flags & 0x08000000 == 0x08000000


def test_gh_cli_token_is_cached(monkeypatch):
    """Second call does NOT spawn another subprocess (5-min poll cycle)."""
    monkeypatch.setattr(copilot.shutil, "which", lambda name: "/usr/bin/gh")
    call_count = {"n": 0}

    class FakeResult:
        returncode = 0
        stdout = "gho_cached_token_abcdef\n"
        stderr = ""

    def fake_run(argv, **kw):
        call_count["n"] += 1
        return FakeResult()

    monkeypatch.setattr(copilot.subprocess, "run", fake_run)
    monkeypatch.setattr(copilot, "_gh_cli_resolved", False)
    monkeypatch.setattr(copilot, "_gh_cli_token_cache", None)

    assert copilot._token_from_gh_cli() == "gho_cached_token_abcdef"
    assert copilot._token_from_gh_cli() == "gho_cached_token_abcdef"
    assert copilot._token_from_gh_cli() == "gho_cached_token_abcdef"
    assert call_count["n"] == 1  # cached, only one subprocess launch


def test_gh_cli_timeout_returns_none(monkeypatch):
    """subprocess.TimeoutExpired → None (no exception escape)."""
    monkeypatch.setattr(copilot.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(argv, **kw):
        raise copilot.subprocess.TimeoutExpired(cmd=argv, timeout=8)

    monkeypatch.setattr(copilot.subprocess, "run", fake_run)
    monkeypatch.setattr(copilot, "_gh_cli_resolved", False)
    monkeypatch.setattr(copilot, "_gh_cli_token_cache", None)
    assert copilot._token_from_gh_cli() is None
