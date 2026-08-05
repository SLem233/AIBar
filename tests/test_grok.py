"""Grok (xAI): окно списания подписки через cli-chat-proxy.grok.com/v1/billing.

Почему именно биллинг, а не «Weekly limit» из grok CLI: недельное окно TUI
берёт из JSON-RPC `auth/check_subscription` по WebSocket-реле, которое снаружи
отвечает «3000 Unauthorized». Заголовков `x-ratelimit-*` у чат-эндпоинта нет.
Единственное, что читается снаружи, — расход за расчётный период.

Сверх форка проверяем: 403 остаётся 403 (гео-блок), при недоступном реле токен
не ротируется впустую, кривой ответ OIDC не роняет опрос и не затирает файл,
а выключатель `grok_auto_refresh` запрещает трогать ~/.grok/auth.json.
"""

import json

import pytest

from aibar.providers import grok
from aibar.providers.base import ProviderSnapshot

AUTH_KEY = "https://auth.x.ai::abc"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth/token"


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)


def billing(used=1974, limit=20000, start="2026-08-01T00:00:00+00:00",
            end="2026-09-01T00:00:00+00:00", on_demand_cap=0):
    return {
        "config": {
            "monthlyLimit": {"val": limit},
            "used": {"val": used},
            "onDemandCap": {"val": on_demand_cap},
            "billingPeriodStart": start,
            "billingPeriodEnd": end,
            "history": [],
        }
    }


FRESH_TOKENS = {
    "access_token": "access-fresh",
    "refresh_token": "rt-new",
    "expires_in": 3600,
}


@pytest.fixture
def auth(monkeypatch, tmp_path):
    """Файл ~/.grok/auth.json с (по желанию) протухшим токеном."""

    def _make(expired=False, exists=True):
        path = tmp_path / "auth.json"
        if exists:
            entry = {
                "key": "access-grk",
                "refresh_token": "rt-grk",
                "expires_at": "2020-01-01T00:00:00Z" if expired else "2099-01-01T00:00:00Z",
            }
            path.write_text(
                json.dumps({AUTH_KEY: entry, "другой::ключ": {"keep": "me"}}),
                encoding="utf-8",
            )
        monkeypatch.setattr(grok, "_auth_path", lambda: path)
        return path

    return _make


def saved_entry(path):
    return json.loads(path.read_text(encoding="utf-8"))[AUTH_KEY]


def oidc_get(response):
    """requests.get: сначала OIDC-дискавери, потом биллинг."""

    def fake_get(url, headers=None, timeout=30):
        if "openid-configuration" in url:
            return FakeResp(200, payload={"token_endpoint": TOKEN_ENDPOINT})
        return response

    return fake_get


# ---- расчёт окна ----------------------------------------------------------
def test_monthly_period_gives_percent_and_spend():
    snap = ProviderSnapshot(provider="Grok")
    grok._apply_billing(snap, billing()["config"])
    assert snap.windows[0].used_percent == pytest.approx(9.87)
    assert snap.windows[0].label == "Мес. (списание)"
    assert snap.extra["Списание"] == "$19.74 / $200.00"
    assert snap.windows[0].resets_at.month == 9


def test_weekly_period_is_labelled_weekly():
    snap = ProviderSnapshot(provider="Grok")
    config = billing(
        used=4300, limit=10000,
        start="2026-08-04T00:00:00+00:00", end="2026-08-11T00:00:00+00:00",
    )["config"]
    grok._apply_billing(snap, config)
    assert snap.windows[0].used_percent == pytest.approx(43.0)
    assert snap.windows[0].label == "Неделя (списание)"


def test_zero_limit_gives_no_window():
    snap = ProviderSnapshot(provider="Grok")
    grok._apply_billing(snap, {"monthlyLimit": {"val": 0}, "used": {"val": 10}})
    assert not snap.windows


def test_prepaid_pool_is_shown_separately():
    snap = ProviderSnapshot(provider="Grok")
    grok._apply_billing(snap, billing(on_demand_cap=5000)["config"])
    assert snap.extra["PAYG"] == "$50.00"


# ---- опрос ----------------------------------------------------------------
def test_valid_token_is_used_as_is(monkeypatch, auth):
    auth(expired=False)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(200, payload=billing())))
    posts = []
    monkeypatch.setattr(
        grok.requests, "post", lambda *a, **kw: posts.append(1) or FakeResp(200, {})
    )
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(9.87)
    assert posts == []


def test_expired_token_is_refreshed_and_written_back(monkeypatch, auth):
    path = auth(expired=True)
    monkeypatch.setattr(
        grok.requests, "get", oidc_get(FakeResp(200, payload=billing(used=500, limit=5000)))
    )
    monkeypatch.setattr(
        grok.requests, "post", lambda *a, **kw: FakeResp(200, payload=FRESH_TOKENS)
    )
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(10.0)
    entry = saved_entry(path)
    assert entry["key"] == "access-fresh"
    assert entry["refresh_token"] == "rt-new"
    # чужие записи файла не тронуты
    assert json.loads(path.read_text(encoding="utf-8"))["другой::ключ"] == {"keep": "me"}


def test_401_triggers_one_refresh_and_retry(monkeypatch, auth):
    auth(expired=False)
    calls = {"billing": 0, "refresh": 0}

    def fake_get(url, headers=None, timeout=30):
        if "openid-configuration" in url:
            return FakeResp(200, payload={"token_endpoint": TOKEN_ENDPOINT})
        calls["billing"] += 1
        if calls["billing"] == 1:
            return FakeResp(401, payload={"error": "expired"})
        return FakeResp(200, payload=billing(used=1000, limit=5000))

    def fake_post(url, **kwargs):
        calls["refresh"] += 1
        return FakeResp(200, payload=FRESH_TOKENS)

    monkeypatch.setattr(grok.requests, "get", fake_get)
    monkeypatch.setattr(grok.requests, "post", fake_post)
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(20.0)
    assert calls["refresh"] == 1


def test_401_after_refresh_asks_to_log_in(monkeypatch, auth):
    auth(expired=False)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(401)))
    monkeypatch.setattr(
        grok.requests, "post", lambda *a, **kw: FakeResp(200, payload=FRESH_TOKENS)
    )
    snap = grok.fetch({})
    assert snap.error and "grok" in snap.error.lower()
    assert snap.http_status == 401


def test_relay_down_does_not_rotate_the_token(monkeypatch, auth):
    """521 — это упавшее реле, а не протухший токен: менять токен незачем."""
    path = auth(expired=False)
    monkeypatch.setattr(
        grok.requests, "get", oidc_get(FakeResp(521, text="origin down"))
    )
    posts = []
    monkeypatch.setattr(
        grok.requests, "post", lambda *a, **kw: posts.append(1) or FakeResp(200, {})
    )
    snap = grok.fetch({})
    assert snap.error and ("/usage" in snap.error or "TUI" in snap.error)
    assert snap.http_status == 521
    assert posts == []
    assert saved_entry(path)["key"] == "access-grk"


def test_403_stays_403_for_the_geoblock_check(monkeypatch, auth):
    auth(expired=False)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(403)))
    posts = []
    monkeypatch.setattr(
        grok.requests, "post", lambda *a, **kw: posts.append(1) or FakeResp(200, {})
    )
    snap = grok.fetch({})
    assert snap.http_status == 403
    assert snap.error == "HTTP 403"
    assert posts == []


def test_missing_auth_file_asks_to_log_in(monkeypatch, auth):
    auth(exists=False)
    snap = grok.fetch({})
    assert snap.error and "grok" in snap.error.lower()
    assert not snap.windows


def test_headers_impersonate_the_cli(monkeypatch, auth):
    """Реле отвечает 401 на x-xai-token-auth='true' — нужен 'xai-grok-cli'."""
    auth(expired=False)
    seen = {}

    def fake_get(url, headers=None, timeout=30):
        seen.update(headers or {})
        return FakeResp(200, payload=billing(used=0, limit=10000))

    monkeypatch.setattr(grok.requests, "get", fake_get)
    grok.fetch({})
    assert seen["x-xai-token-auth"] == "xai-grok-cli"
    assert seen["x-grok-client-version"] == grok.CLI_VERSION


def test_malformed_refresh_response_does_not_crash(monkeypatch, auth):
    path = auth(expired=True)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(200, payload=billing())))
    monkeypatch.setattr(
        grok.requests, "post", lambda *a, **kw: FakeResp(200, payload={"ok": True})
    )
    snap = grok.fetch({})
    assert snap.error and "access_token" in snap.error
    assert saved_entry(path)["key"] == "access-grk"  # файл не затёрт


def test_refresh_error_does_not_leak_response_body(monkeypatch, auth):
    auth(expired=True)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(200, payload=billing())))
    monkeypatch.setattr(
        grok.requests,
        "post",
        lambda *a, **kw: FakeResp(500, text="тело с rt-grk внутри"),
    )
    snap = grok.fetch({})
    assert snap.error and "rt-grk" not in snap.error
    assert "500" in snap.error


def test_dead_refresh_token_asks_to_log_in(monkeypatch, auth):
    auth(expired=True)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(200, payload=billing())))
    monkeypatch.setattr(
        grok.requests,
        "post",
        lambda *a, **kw: FakeResp(400, text='{"error":"invalid_grant"}'),
    )
    snap = grok.fetch({})
    assert snap.error and "grok" in snap.error.lower()


def test_auto_refresh_off_never_touches_the_file(monkeypatch, auth):
    path = auth(expired=True)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(200, payload=billing())))
    posts = []
    monkeypatch.setattr(
        grok.requests, "post", lambda *a, **kw: posts.append(1) or FakeResp(200, {})
    )
    snap = grok.fetch({"grok_auto_refresh": False})
    assert posts == []
    assert saved_entry(path)["key"] == "access-grk"
    # с протухшим токеном биллинг всё равно пробуем — решает сервер
    assert snap.error is None or "grok" in snap.error.lower()


def test_renewal_date_from_settings_is_shown(monkeypatch, auth):
    auth(expired=False)
    monkeypatch.setattr(grok.requests, "get", oidc_get(FakeResp(200, payload=billing())))
    snap = grok.fetch({"grok_renewal_date": "01.01.2030", "grok_renewal_period": "month"})
    assert snap.extra["Продление"] == "01.01.2030"
