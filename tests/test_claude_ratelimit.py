"""Claude: 429 от эндпоинта лимитов и лишние запросы к /profile.

Anthropic ограничивает частоту обращений к /api/oauth/usage и отдаёт 429 с
заголовком Retry-After. Раньше модуль этого не замечал: продолжал долбить
эндпоинт каждый цикл (при `refresh_seconds = 60` — 120 запросов в час, потому
что на каждый опрос шло два запроса, usage и profile) и отдавал наверх пустой
снапшот, из-за чего кольцо в виджете гасло.

Проверяем: 429 не выдаётся за ошибку входа, Retry-After удерживает следующий
опрос, а тариф из /profile берётся из кэша и не удваивает трафик.
"""

import json
import time

import pytest

from aibar.providers import claude


class FakeResp:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


USAGE_BODY = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-07-30T21:00:00Z"},
    "seven_day": {"utilization": 18.0, "resets_at": "2026-08-05T12:00:00Z"},
}

PROFILE_BODY = {
    "organization": {"rate_limit_tier": "default_claude_max_5x"},
}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Свежие учётные данные и обнулённое состояние бэкоффа/кэша на каждый тест."""
    cred = tmp_path / ".credentials.json"
    cred.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-live",
                    "refreshToken": "rt",
                    "expiresAt": 9_999_999_999_999,
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude, "_cred_path", lambda: cred)
    monkeypatch.setattr(claude, "_retry_until", 0.0)
    monkeypatch.setattr(claude, "_profile_cache", None)
    return cred


class GetSpy:
    """Считает запросы по адресам; отдаёт заранее заданные ответы."""

    def __init__(self, usage, profile=None):
        self.usage = usage
        self.profile = profile or FakeResp(200, payload=PROFILE_BODY)
        self.urls = []

    def __call__(self, url, headers=None, timeout=30):
        self.urls.append(url)
        return self.profile if url == claude.PROFILE_URL else self.usage

    @property
    def usage_calls(self):
        return self.urls.count(claude.USAGE_URL)

    @property
    def profile_calls(self):
        return self.urls.count(claude.PROFILE_URL)


# ---- 429 --------------------------------------------------------------------
def test_rate_limited_usage_is_reported_as_such_not_as_login_problem(monkeypatch):
    spy = GetSpy(FakeResp(429, text="rate_limit_error", headers={"Retry-After": "600"}))
    monkeypatch.setattr(claude.requests, "get", spy)

    snap = claude.fetch({})

    assert snap.http_status == 429
    assert snap.windows == []
    # Формулировка про частоту, а не про протухший токен или повторный вход.
    assert "частот" in snap.error.lower()
    assert claude.RELOGIN_HINT not in (snap.error or "")


def test_retry_after_header_holds_off_the_next_poll(monkeypatch):
    spy = GetSpy(FakeResp(429, headers={"Retry-After": "600"}))
    monkeypatch.setattr(claude.requests, "get", spy)

    claude.fetch({})
    assert spy.usage_calls == 1

    snap = claude.fetch({})  # следующий цикл опроса — внутри окна ожидания
    assert spy.usage_calls == 1  # запрос не ушёл
    assert snap.http_status == 429
    assert snap.error


def test_poll_resumes_after_the_retry_window_passes(monkeypatch):
    spy = GetSpy(FakeResp(429, headers={"Retry-After": "600"}))
    monkeypatch.setattr(claude.requests, "get", spy)
    claude.fetch({})

    monkeypatch.setattr(claude, "_retry_until", time.time() - 1)  # окно истекло
    spy.usage = FakeResp(200, payload=USAGE_BODY)
    snap = claude.fetch({})

    assert spy.usage_calls == 2
    assert snap.error is None
    assert snap.session_percent == 42.0


def test_missing_retry_after_falls_back_to_a_sane_pause(monkeypatch):
    spy = GetSpy(FakeResp(429))  # заголовка нет
    monkeypatch.setattr(claude.requests, "get", spy)

    claude.fetch({})

    assert claude._retry_until > time.time()  # пауза всё равно взята
    claude.fetch({})
    assert spy.usage_calls == 1


def test_retry_after_is_capped_so_a_huge_value_cannot_freeze_polling(monkeypatch):
    spy = GetSpy(FakeResp(429, headers={"Retry-After": "999999"}))
    monkeypatch.setattr(claude.requests, "get", spy)

    claude.fetch({})

    assert claude._retry_until - time.time() <= claude.MAX_RETRY_AFTER + 1


def test_a_429_never_triggers_a_token_refresh(monkeypatch):
    """Ограничение частоты — не повод ротировать одноразовый refresh-токен."""
    posts = []
    monkeypatch.setattr(claude.requests, "get", GetSpy(FakeResp(429)))
    monkeypatch.setattr(
        claude.requests, "post", lambda *a, **kw: posts.append(a) or FakeResp(200)
    )

    claude.fetch({})

    assert posts == []


# ---- кэш профиля ------------------------------------------------------------
def test_profile_is_fetched_once_and_reused_within_the_ttl(monkeypatch):
    spy = GetSpy(FakeResp(200, payload=USAGE_BODY))
    monkeypatch.setattr(claude.requests, "get", spy)

    first = claude.fetch({})
    second = claude.fetch({})

    assert spy.usage_calls == 2  # лимиты — каждый цикл, они и нужны свежими
    assert spy.profile_calls == 1  # тариф меняется редко, хватает одного
    assert first.plan == "Max 5x"
    assert second.plan == "Max 5x"  # из кэша, не потерялся


def test_profile_is_refetched_after_the_ttl_expires(monkeypatch):
    spy = GetSpy(FakeResp(200, payload=USAGE_BODY))
    monkeypatch.setattr(claude.requests, "get", spy)
    claude.fetch({})

    monkeypatch.setattr(
        claude, "_profile_cache", (time.time() - claude.PROFILE_TTL - 1, "Max 5x", {})
    )
    claude.fetch({})

    assert spy.profile_calls == 2


def test_failed_profile_does_not_poison_the_cache(monkeypatch):
    spy = GetSpy(FakeResp(200, payload=USAGE_BODY), profile=FakeResp(500))
    monkeypatch.setattr(claude.requests, "get", spy)

    snap = claude.fetch({})

    assert snap.plan == "Max"  # тариф из файла учётных данных
    assert snap.session_percent == 42.0  # сам опрос лимитов не пострадал
    assert claude._profile_cache is None  # пустышка не закэширована
    claude.fetch({})
    assert spy.profile_calls == 2  # в следующий цикл пробуем снова
