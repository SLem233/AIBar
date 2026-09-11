"""Codex Пул: аккаунты opencodex — живой опрос wham/usage + кэш-фолбэк.

Формат файлов opencodex (~/.opencodex):
  codex-accounts.json     {id: {credential: {accessToken, chatgptAccountId,
                                             expiresAt(ms)}}}
  codex-quota-cache.json  {quotas: {id: {weeklyPercent, weeklyResetAt(s),
                                         shortPercent, shortResetAt(s),
                                         shortWindowSeconds}}}
  config.json             {codexAccounts: [{id, email, plan}],
                          activeCodexAccountId}

Токены читаются, но не обновляются: ротация refresh-токена отнимет доступ у
самого opencodex. Просроченный access-токен → живой опрос пропускается.
"""

import json
import time

import pytest

from aibar.providers import codex_pool


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _write(tmp_path, name, payload):
    (tmp_path / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def ocx_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(codex_pool, "OCX_HOME", home / ".opencodex")
    return home / ".opencodex"


def _accounts_file(expires_in_s=3600):
    return {
        "chatgpt-1": {
            "credential": {
                "accessToken": "tok1",
                "chatgptAccountId": "acct-1",
                "expiresAt": (time.time() + expires_in_s) * 1000,
            }
        }
    }


LIVE_USAGE = {
    "plan_type": "plus",
    "email": "chelaxian@gmail.com",
    "rate_limit": {
        "primary_window": {
            "used_percent": 6,
            "limit_window_seconds": 604800,
            "reset_at": 1789659049,
        }
    },
}


def test_no_opencodex(ocx_home):
    snap = codex_pool.fetch()
    assert snap.error and "opencodex" in snap.error
    assert not snap.windows


def test_empty_pool(ocx_home):
    ocx_home.mkdir(parents=True)
    _write(ocx_home, "codex-accounts.json", {})
    snap = codex_pool.fetch()
    assert snap.error and "Пул Codex пуст" in snap.error


def test_live_poll_labels_window_with_email(ocx_home, monkeypatch):
    ocx_home.mkdir(parents=True)
    _write(ocx_home, "codex-accounts.json", _accounts_file())
    monkeypatch.setattr(
        codex_pool.requests, "get", lambda *a, **kw: FakeResp(payload=LIVE_USAGE)
    )
    snap = codex_pool.fetch()
    assert snap.error is None
    assert [(w.label, w.used_percent) for w in snap.windows] == [
        ("Неделя · chelaxian", 6.0)
    ]
    assert snap.plan.startswith("Plus")


def test_expired_token_falls_back_to_cache(ocx_home, monkeypatch):
    ocx_home.mkdir(parents=True)
    _write(ocx_home, "codex-accounts.json", _accounts_file(expires_in_s=-10))
    _write(
        ocx_home,
        "codex-quota-cache.json",
        {
            "quotas": {
                "chatgpt-1": {
                    "weeklyPercent": 5,
                    "weeklyResetAt": 1789659049,
                }
            }
        },
    )
    called = []
    monkeypatch.setattr(
        codex_pool.requests, "get", lambda *a, **kw: called.append(1)
    )
    snap = codex_pool.fetch()
    assert not called  # протухший токен — в сеть не идём
    assert snap.error is None
    assert [(w.label, w.used_percent) for w in snap.windows] == [
        ("Неделя · chatgpt-1", 5.0)
    ]
    assert snap.windows[0].resets_at is not None


def test_network_error_falls_back_to_cache(ocx_home, monkeypatch):
    ocx_home.mkdir(parents=True)
    _write(ocx_home, "codex-accounts.json", _accounts_file())
    _write(
        ocx_home,
        "codex-quota-cache.json",
        {
            "quotas": {
                "chatgpt-1": {
                    "weeklyPercent": 5,
                    "weeklyResetAt": 1789659049,
                    "shortPercent": 10,
                    "shortResetAt": 1789074103,
                    "shortWindowSeconds": 18000,
                }
            }
        },
    )

    def boom(*a, **kw):
        raise codex_pool.requests.RequestException("down")

    monkeypatch.setattr(codex_pool.requests, "get", boom)
    snap = codex_pool.fetch()
    assert snap.error is None
    labels = [w.label for w in snap.windows]
    assert labels == ["Неделя · chatgpt-1", "Сессия (5ч) · chatgpt-1"]


def test_two_accounts_interleave_windows(ocx_home, monkeypatch):
    ocx_home.mkdir(parents=True)
    _write(
        ocx_home,
        "codex-accounts.json",
        {
            "chatgpt-1": _accounts_file()["chatgpt-1"],
            "chatgpt-2": {
                "credential": {
                    "accessToken": "tok2",
                    "chatgptAccountId": "acct-2",
                    "expiresAt": (time.time() + 3600) * 1000,
                }
            },
        },
    )
    _write(
        ocx_home,
        "config.json",
        {
            "codexAccounts": [
                {"id": "chatgpt-1", "email": "a@gmail.com", "plan": "plus"},
                {"id": "chatgpt-2", "email": "b@gmail.com", "plan": "pro"},
            ],
            "activeCodexAccountId": "chatgpt-2",
        },
    )

    def fake_get(url, headers=None, **kw):
        token = headers["Authorization"].split()[-1]
        if token == "tok1":
            return FakeResp(
                payload={
                    "plan_type": "plus",
                    "email": "a@gmail.com",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 0,
                            "limit_window_seconds": 18000,
                            "reset_at": 1789074771,
                        },
                        "secondary_window": {
                            "used_percent": 100,
                            "limit_window_seconds": 604800,
                            "reset_at": 1789455490,
                        },
                    },
                }
            )
        return FakeResp(
            payload={
                "plan_type": "pro",
                "email": "b@gmail.com",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 4,
                        "limit_window_seconds": 604800,
                        "reset_at": 1789659049,
                    }
                },
            }
        )

    monkeypatch.setattr(codex_pool.requests, "get", fake_get)
    snap = codex_pool.fetch()
    assert snap.error is None
    assert [(w.label, w.used_percent) for w in snap.windows] == [
        ("Сессия (5ч) · a", 0.0),
        ("Неделя · a", 100.0),
        ("Неделя · b", 4.0),
    ]
    # планы обоих аккаунтов и метка выбранного
    assert "Plus" in snap.plan and "Pro" in snap.plan and "×2" in snap.plan
    assert snap.extra["Выбран"] == "b@gmail.com"


def test_all_failed_sets_error(ocx_home, monkeypatch):
    ocx_home.mkdir(parents=True)
    _write(ocx_home, "codex-accounts.json", _accounts_file(expires_in_s=-10))
    monkeypatch.setattr(
        codex_pool.requests, "get", lambda *a, **kw: FakeResp(status_code=401)
    )
    snap = codex_pool.fetch()
    assert snap.error and "недоступны" in snap.error
    assert not snap.windows


def test_bad_json_body_is_not_fatal(ocx_home, monkeypatch):
    ocx_home.mkdir(parents=True)
    _write(ocx_home, "codex-accounts.json", _accounts_file())
    monkeypatch.setattr(
        codex_pool.requests, "get", lambda *a, **kw: FakeResp(payload=None)
    )
    snap = codex_pool.fetch()
    assert snap.error and "недоступны" in snap.error


# ---- однократное включение провайдера при найденном opencodex --------------


def _fake_home(monkeypatch, tmp_path, with_ocx: bool):
    home = tmp_path / "cfg-home"
    home.mkdir()
    if with_ocx:
        (home / ".opencodex").mkdir()
        (home / ".opencodex" / "codex-accounts.json").write_text("{}", encoding="utf-8")
    from aibar import config

    monkeypatch.setattr(config.Path, "home", lambda: home)
    return config


def test_migration_enables_pool_next_to_codex(monkeypatch, tmp_path):
    config = _fake_home(monkeypatch, tmp_path, with_ocx=True)
    data = {"providers": ["Claude", "Codex"]}
    migrated = config._migrate(data)
    assert migrated["providers"] == ["Claude", "Codex", "Codex Пул"]


def test_migration_respects_manual_removal(monkeypatch, tmp_path):
    config = _fake_home(monkeypatch, tmp_path, with_ocx=True)
    # уже «видели» opencodex, пользователь пул убрал — не возвращаем
    data = {"providers": ["Claude", "Codex"], "codex_pool_seen": True}
    migrated = config._migrate(data)
    assert migrated["providers"] == ["Claude", "Codex"]

    # и до этого: отметка ставится при первом прогоне
    data = {"providers": ["Claude", "Codex"]}
    config._migrate(data)
    assert data["codex_pool_seen"] is True


def test_migration_skips_without_opencodex(monkeypatch, tmp_path):
    config = _fake_home(monkeypatch, tmp_path, with_ocx=False)
    data = {"providers": ["Claude", "Codex"]}
    migrated = config._migrate(data)
    assert migrated["providers"] == ["Claude", "Codex"]
    assert "codex_pool_seen" not in migrated or migrated["codex_pool_seen"]
