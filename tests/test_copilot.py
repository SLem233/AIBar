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
