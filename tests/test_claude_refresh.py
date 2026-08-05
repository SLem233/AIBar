"""Claude без запуска CLI: авто-обновление OAuth-токена по refresh-токену.

Раньше модуль на протухшем токене сдавался и просил запустить `claude`. Теперь
он сам меняет токен через POST /v1/oauth/token и записывает ротированный токен
обратно в ~/.claude/.credentials.json — виджет показывает лимиты и тем, кто
пользуется только Claude Desktop.

Отдельно проверяем то, чего в форке не было: 403 остаётся 403 (иначе ломается
определение гео-блока), кривой ответ refresh не роняет опрос, а выключенная
настройка `claude_auto_refresh` возвращает прежнее поведение и не трогает файл.
"""

import json

import pytest

from aibar.providers import claude


class FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


USAGE_BODY = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-07-30T21:00:00Z"},
    "seven_day": {"utilization": 18.0, "resets_at": "2026-08-05T12:00:00Z"},
}

FRESH_TOKENS = {
    "access_token": "access-fresh",
    "refresh_token": "rt-new",
    "expires_in": 3600,
    "refresh_token_expires_in": 2592000,
}


def _write_creds(tmp_path, *, expired=True, refresh_token="rt-old"):
    """Файл учётных данных с (по желанию) протухшим access-токеном."""
    cred = tmp_path / ".credentials.json"
    oauth = {
        "accessToken": "access-stale",
        "refreshToken": refresh_token,
        "expiresAt": 0 if expired else 9_999_999_999_999,
    }
    cred.write_text(
        json.dumps({"claudeAiOauth": oauth, "other": "keep-me"}), encoding="utf-8"
    )
    return cred


@pytest.fixture
def creds(monkeypatch, tmp_path):
    """Подменяем путь к файлу и запрещаем реальные запросы наружу."""

    def _make(expired=True, refresh_token="rt-old"):
        cred = _write_creds(tmp_path, expired=expired, refresh_token=refresh_token)
        monkeypatch.setattr(claude, "_cred_path", lambda: cred)
        return cred

    return _make


def _oauth(cred):
    return json.loads(cred.read_text(encoding="utf-8"))["claudeAiOauth"]


# ---- обновление токена ----------------------------------------------------
def test_expired_token_is_refreshed_and_written_back(monkeypatch, creds):
    cred = creds(expired=True)
    refresh_calls = []

    def fake_post(url, json=None, headers=None, timeout=30):
        refresh_calls.append(json)
        return FakeResp(200, payload=FRESH_TOKENS)

    def fake_get(url, headers=None, timeout=30):
        assert "access-fresh" in headers["Authorization"]
        return FakeResp(200, payload=USAGE_BODY)

    monkeypatch.setattr(claude.requests, "post", fake_post)
    monkeypatch.setattr(claude.requests, "get", fake_get)

    snap = claude.fetch({})
    assert snap.error is None
    assert [w.used_percent for w in snap.windows] == pytest.approx([42.0, 18.0])
    assert len(refresh_calls) == 1
    assert refresh_calls[0]["grant_type"] == "refresh_token"
    assert refresh_calls[0]["refresh_token"] == "rt-old"

    saved = _oauth(cred)
    assert saved["accessToken"] == "access-fresh"
    assert saved["refreshToken"] == "rt-new"  # токен ротируется — храним новый
    assert saved["expiresAt"] > 0
    # остальные ключи файла не потеряны
    assert json.loads(cred.read_text(encoding="utf-8"))["other"] == "keep-me"


def test_valid_token_is_used_as_is(monkeypatch, creds):
    creds(expired=False)
    posts = []
    monkeypatch.setattr(
        claude.requests, "post", lambda *a, **kw: posts.append(1) or FakeResp(200, {})
    )
    monkeypatch.setattr(
        claude.requests, "get", lambda *a, **kw: FakeResp(200, payload=USAGE_BODY)
    )
    snap = claude.fetch({})
    assert snap.error is None
    assert posts == []


def test_401_triggers_one_refresh_and_retry(monkeypatch, creds):
    """Файл говорит «токен живой», а API отвечает 401 — обновляемся и повторяем."""
    creds(expired=False)
    monkeypatch.setattr(
        claude.requests, "post", lambda *a, **kw: FakeResp(200, payload=FRESH_TOKENS)
    )
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=30):
        if url != claude.USAGE_URL:  # запрос профиля здесь не считаем
            return FakeResp(200, payload={})
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(401)
        return FakeResp(200, payload=USAGE_BODY)

    monkeypatch.setattr(claude.requests, "get", fake_get)
    snap = claude.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(42.0)
    assert calls["n"] == 2  # ровно одна повторная попытка


def test_dead_refresh_token_asks_to_log_in(monkeypatch, creds):
    creds(expired=True)
    monkeypatch.setattr(
        claude.requests,
        "post",
        lambda *a, **kw: FakeResp(400, payload=None, text='{"error":"invalid_grant"}'),
    )
    snap = claude.fetch({})
    assert snap.error and "залогиньтесь" in snap.error
    assert not snap.windows


def test_parallel_session_refresh_is_picked_up(monkeypatch, creds, tmp_path):
    """invalid_grant значит «кто-то обновил раньше нас» — перечитываем файл."""
    cred = creds(expired=True)

    def fake_post(url, json=None, headers=None, timeout=30):
        # Пока мы ходили за токеном, соседняя сессия записала свежий.
        cred.write_text(
            __import__("json").dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "access-from-cli",
                        "refreshToken": "rt-cli",
                        "expiresAt": 9_999_999_999_999,
                    }
                }
            ),
            encoding="utf-8",
        )
        return FakeResp(400, text='{"error":"invalid_grant"}')

    def fake_get(url, headers=None, timeout=30):
        assert "access-from-cli" in headers["Authorization"]
        return FakeResp(200, payload=USAGE_BODY)

    monkeypatch.setattr(claude.requests, "post", fake_post)
    monkeypatch.setattr(claude.requests, "get", fake_get)
    snap = claude.fetch({})
    assert snap.error is None
    assert snap.windows


def test_malformed_refresh_response_does_not_crash(monkeypatch, creds):
    """200 без access_token — понятная ошибка, а не KeyError в потоке опроса."""
    cred = creds(expired=True)
    monkeypatch.setattr(
        claude.requests, "post", lambda *a, **kw: FakeResp(200, payload={"ok": True})
    )
    snap = claude.fetch({})
    assert snap.error and "access_token" in snap.error
    # битый ответ не должен затирать файл
    assert _oauth(cred)["accessToken"] == "access-stale"


def test_refresh_error_does_not_leak_response_body(monkeypatch, creds):
    """Тело ответа токен-эндпоинта в UI не показываем."""
    creds(expired=True)
    monkeypatch.setattr(
        claude.requests,
        "post",
        lambda *a, **kw: FakeResp(500, text="secret-ish body with rt-old inside"),
    )
    snap = claude.fetch({})
    assert snap.error and "rt-old" not in snap.error
    assert "500" in snap.error


# ---- выключатель ----------------------------------------------------------
def test_auto_refresh_off_keeps_the_old_behaviour(monkeypatch, creds):
    cred = creds(expired=True)
    posts = []
    monkeypatch.setattr(
        claude.requests, "post", lambda *a, **kw: posts.append(1) or FakeResp(200, {})
    )
    monkeypatch.setattr(claude.requests, "get", lambda *a, **kw: FakeResp(401))
    snap = claude.fetch({"claude_auto_refresh": False})
    assert posts == []  # ни одного обращения к токен-эндпоинту
    assert snap.error and "запустите claude" in snap.error
    assert _oauth(cred)["accessToken"] == "access-stale"  # файл не тронут


# ---- гео-блок -------------------------------------------------------------
def test_403_stays_403_and_does_not_refresh(monkeypatch, creds):
    """403 — это гео-блок; poll_all различает его по http_status."""
    creds(expired=False)
    posts = []
    monkeypatch.setattr(
        claude.requests, "post", lambda *a, **kw: posts.append(1) or FakeResp(200, {})
    )
    monkeypatch.setattr(claude.requests, "get", lambda *a, **kw: FakeResp(403))
    snap = claude.fetch({})
    assert snap.http_status == 403
    assert snap.error == "HTTP 403"
    assert posts == []


def test_missing_credentials_file(monkeypatch, tmp_path):
    monkeypatch.setattr(claude, "_cred_path", lambda: tmp_path / ".credentials.json")
    snap = claude.fetch({})
    assert snap.error and "не найден" in snap.error
