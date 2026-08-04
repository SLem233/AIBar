"""Grok provider: OAuth billing parsing and fallback paths."""

import json

import pytest

from aibar.providers import grok


class FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)


BILLING_BODY = {
    "config": {
        "monthlyLimit": {"val": 10000},  # $100.00 in cents
        "used": {"val": 2500},           # $25.00 in cents
        "billingPeriodEnd": "2026-07-31T00:00:00Z",
    }
}


def _write_grok_auth(tmp_path, *, expired=True, refresh_token="rt-grk"):
    path = tmp_path / "auth.json"
    entry = {
        "key": "access-grk",
        "refresh_token": refresh_token,
    }
    if expired:
        entry["expires_at"] = "2020-01-01T00:00:00Z"
    else:
        entry["expires_at"] = "2099-01-01T00:00:00Z"
    path.write_text(
        json.dumps({"https://auth.x.ai::abc": entry}),
        encoding="utf-8",
    )
    return path


def test_grok_parse_billing_computes_monthly_percent():
    """used/limit in cents → percent, with $ X / $ Y extra."""
    from aibar.providers.base import ProviderSnapshot

    snap = ProviderSnapshot(provider="Grok")
    result = grok._parse_billing(snap, BILLING_BODY, {})
    assert result.error is None
    assert len(result.windows) == 1
    assert result.windows[0].used_percent == pytest.approx(25.0)  # 2500/10000
    assert result.windows[0].label == "Подписка (мес.)"
    assert result.extra["Расход"] == "$25.00 / $100.00"


def test_grok_oauth_fetch_valid_token(monkeypatch, tmp_path):
    """Valid OAuth token → billing fetched, no refresh."""
    path = _write_grok_auth(tmp_path, expired=False)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    monkeypatch.setattr(grok.requests, "get",
                        lambda *a, **kw: FakeResp(200, payload=BILLING_BODY))
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(25.0)


def test_grok_oauth_refreshes_expired_token(monkeypatch, tmp_path):
    """Expired token → OIDC refresh, write-back, then billing."""
    path = _write_grok_auth(tmp_path, expired=True)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)

    def fake_get(url, headers=None, timeout=30):
        if "openid-configuration" in url:
            return FakeResp(
                200, payload={"token_endpoint": "https://auth.x.ai/oauth/token"}
            )
        return FakeResp(200, payload=BILLING_BODY)

    def fake_post(url, data=None, headers=None, timeout=30):
        return FakeResp(
            200,
            payload={
                "access_token": "access-fresh",
                "refresh_token": "rt-new",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(grok.requests, "get", fake_get)
    monkeypatch.setattr(grok.requests, "post", fake_post)
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(25.0)
    # Token was written back.
    saved = json.loads(path.read_text(encoding="utf-8"))
    entry = saved["https://auth.x.ai::abc"]
    assert entry["key"] == "access-fresh"
    assert entry["refresh_token"] == "rt-new"


def test_grok_no_credentials_no_apikey(monkeypatch, tmp_path):
    """No OAuth file, no API key → clear error."""
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({})
    assert snap.error is not None
    assert "grok CLI" in snap.error or "ключ" in snap.error


def test_grok_apikey_fallback_with_budget(monkeypatch, tmp_path):
    """OAuth missing but API key + budget set → budget window shown."""
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({"grok_api_key": "xai-123", "grok_budget_usd": 50})
    assert snap.error is None
    assert len(snap.windows) == 1
    assert snap.extra["Бюджет"] == "$ 0 / $ 50"


def test_grok_apikey_without_budget(monkeypatch, tmp_path):
    """API key but no budget and no OAuth → error (api.x.ai has no quota)."""
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({"grok_api_key": "xai-123"})
    assert snap.error is not None
