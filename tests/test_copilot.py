"""GitHub Copilot: разбор ответа api.github.com/copilot_internal/user.

Публичный REST API квоты Copilot для личного аккаунта не отдаёт, поэтому
читаем тот же внутренний эндпоинт, что и расширение Copilot Chat в VS Code.

Отдельно закреплено то, чего в форке не было: `gh` запускается только по
абсолютному пути (иначе на Windows подхватится gh.exe, подложенный рядом с
AIBar.exe) и без окна консоли.
"""

import json
from datetime import datetime, timezone

import pytest

from aibar.providers import copilot

TOKEN = "ghp_0123456789abcdef0123456789abcdef"
# Настоящая функция: в тестах ниже она подменена заглушкой (см. no_gh_cli).
REAL_GH_TOKEN = copilot._token_from_gh_cli


class FakeResp:
    def __init__(self, status_code=200, payload=None, content=None):
        self.status_code = status_code
        self._payload = payload
        if content is not None:
            self.content = content
        else:
            self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


FREE_PAYLOAD = {
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


@pytest.fixture(autouse=True)
def no_gh_cli(monkeypatch):
    """По умолчанию gh в тестах недоступен — иначе подхватится реальный токен."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: None)


def _get(monkeypatch, resp):
    monkeypatch.setattr(copilot.requests, "get", lambda *a, **kw: resp)


# ---- одно окно квоты ------------------------------------------------------
def test_window_percent_is_used_not_remaining():
    reset = datetime(2026, 9, 1, tzinfo=timezone.utc)
    win = copilot._snapshot_window("chat", {"entitlement": 200, "remaining": 50}, reset)
    assert win.label == "Chat"
    assert win.used_percent == pytest.approx(75.0)
    assert win.resets_at == reset


def test_zero_entitlement_means_no_window():
    """entitlement=0 — квота не выдана; рисовать 0% было бы враньём."""
    assert copilot._snapshot_window("premium_interactions", {"entitlement": 0}, None) is None


def test_unlimited_is_pinned_to_zero():
    win = copilot._snapshot_window("chat", {"unlimited": True}, None)
    assert win.label == "Chat ∞" and win.used_percent == 0.0


def test_overage_is_clamped():
    win = copilot._snapshot_window("chat", {"entitlement": 200, "remaining": -50}, None)
    assert win.used_percent == 100.0


# ---- источник токена ------------------------------------------------------
def test_settings_token_beats_env_and_gh(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-1234567890")
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: "gh-cli-token")
    assert copilot._api_token({"copilot_token": "explicit-token-123"}) == "explicit-token-123"


def test_env_token_beats_gh(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-1234567890")
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: "gh-cli-token")
    assert copilot._api_token({}) == "env-token-1234567890"


def test_gh_cli_is_the_last_resort(monkeypatch):
    monkeypatch.setattr(copilot, "_token_from_gh_cli", lambda: "gh-cli-token")
    assert copilot._api_token({}) == "gh-cli-token"


def test_gh_is_launched_by_absolute_path_only(monkeypatch):
    """Без which() Windows взял бы gh.exe из папки запуска — то есть чужой."""
    monkeypatch.setattr(copilot.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(
        copilot.subprocess, "run", lambda *a, **kw: calls.append(a) or None
    )
    assert REAL_GH_TOKEN() is None
    assert calls == []  # gh не найден — ничего не запускаем


def test_gh_runs_without_a_console_window(monkeypatch):
    gh_path = r"C:\Programs\gh\bin\gh.exe"
    monkeypatch.setattr(copilot.shutil, "which", lambda name: gh_path)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return type("P", (), {"returncode": 0, "stdout": "gho_tokenvalue\n"})()

    monkeypatch.setattr(copilot.subprocess, "run", fake_run)
    assert REAL_GH_TOKEN() == "gho_tokenvalue"
    assert seen["cmd"][0] == gh_path  # абсолютный путь, не имя
    assert seen["kwargs"].get("creationflags", 0) == copilot.NO_WINDOW


def test_gh_failure_is_not_an_error(monkeypatch):
    monkeypatch.setattr(copilot.shutil, "which", lambda name: "gh")
    monkeypatch.setattr(
        copilot.subprocess,
        "run",
        lambda *a, **kw: type("P", (), {"returncode": 1, "stdout": ""})(),
    )
    assert REAL_GH_TOKEN() is None


# ---- fetch ----------------------------------------------------------------
def test_no_token_anywhere_explains_how_to_fix_it():
    snap = copilot.fetch({"copilot_token": ""})
    assert snap.error and "gh auth login" in snap.error


def test_garbage_in_the_token_field_is_rejected():
    snap = copilot.fetch({"copilot_token": "with\nnewline"})
    assert snap.error and "не токен" in snap.error


def test_401_is_reported_as_unauthorized(monkeypatch):
    _get(monkeypatch, FakeResp(401))
    snap = copilot.fetch({"copilot_token": TOKEN})
    assert snap.error and "авторизован" in snap.error
    assert snap.http_status == 401


def test_404_means_copilot_is_not_enabled(monkeypatch):
    _get(monkeypatch, FakeResp(404))
    snap = copilot.fetch({"copilot_token": TOKEN})
    assert snap.error and "не подключён" in snap.error


def test_403_keeps_the_status_for_the_geoblock_check(monkeypatch):
    _get(monkeypatch, FakeResp(403))
    snap = copilot.fetch({"copilot_token": TOKEN})
    assert snap.http_status == 403


def test_free_plan_drops_the_empty_premium_window(monkeypatch):
    _get(monkeypatch, FakeResp(200, payload=FREE_PAYLOAD))
    snap = copilot.fetch({"copilot_token": TOKEN})
    assert snap.error is None
    assert snap.plan == "Free"
    assert snap.extra["Аккаунт"] == "octocat"
    assert snap.extra["Сброс"] == "01.09.2026"
    assert [w.label for w in snap.windows] == ["Chat", "Completions"]
    assert snap.windows[0].used_percent == pytest.approx(75.0)
    assert snap.windows[1].used_percent == pytest.approx(25.0)


def test_pro_plan_shows_unlimited_chat(monkeypatch):
    payload = {
        "login": "pro_user",
        "access_type_sku": "copilot_pro",
        "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
        "quota_snapshots": {
            "chat": {"unlimited": True},
            "completions": {"entitlement": 1000, "remaining": 800},
            "premium_interactions": {"entitlement": 500, "remaining": 100},
        },
    }
    _get(monkeypatch, FakeResp(200, payload=payload))
    snap = copilot.fetch({"copilot_token": TOKEN})
    assert snap.plan == "Pro"
    assert [w.label for w in snap.windows] == ["Chat ∞", "Completions", "Premium"]
    assert snap.windows[0].used_percent == 0.0
    assert snap.windows[2].used_percent == pytest.approx(80.0)


def test_payload_without_quotas_is_reported(monkeypatch):
    _get(monkeypatch, FakeResp(200, payload={"login": "octocat"}))
    snap = copilot.fetch({"copilot_token": TOKEN})
    assert snap.error and "не вернул окон" in snap.error
