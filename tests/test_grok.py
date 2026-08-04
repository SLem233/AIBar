"""Grok provider: weekly rate limits via chat x-ratelimit-* headers."""

import json

import pytest

from aibar.providers import grok


class FakeResp:
    """Fake requests.Response with configurable headers + JSON."""

    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)


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


def _rate_limit_headers(used_pct, *, limit=5_000_000):
    """Build x-ratelimit-* headers reflecting a given used percentage."""
    remaining = int(limit * (1 - used_pct / 100))
    return {
        "x-ratelimit-limit-tokens": str(limit),
        "x-ratelimit-remaining-tokens": str(remaining),
        "x-ratelimit-limit-requests": "120",
        "x-ratelimit-remaining-requests": str(int(120 * (1 - used_pct / 100))),
    }


def test_apply_rate_limits_computes_percent():
    """(limit - remaining) / limit → percent."""
    snap = grok.ProviderSnapshot(provider="Grok") if hasattr(grok, "ProviderSnapshot") else None
    from aibar.providers.base import ProviderSnapshot

    snap = ProviderSnapshot(provider="Grok")
    limits = {"limit_tokens": 5_000_000, "remaining_tokens": 2_850_000,
              "limit_requests": 120, "remaining_requests": 68}
    grok._apply_rate_limits(snap, limits)
    assert len(snap.windows) == 2
    # 5000000 - 2850000 = 2150000 → 43%
    assert snap.windows[0].used_percent == pytest.approx(43.0)
    assert snap.windows[0].label == "Неделя (токены)"
    # 120 - 68 = 52 → ~43.3%
    assert snap.windows[1].used_percent == pytest.approx(43.33, abs=0.1)


def test_apply_rate_limits_no_headers():
    """No headers → error set, no windows."""
    from aibar.providers.base import ProviderSnapshot

    snap = ProviderSnapshot(provider="Grok")
    grok._apply_rate_limits(snap, {})
    assert snap.error is not None
    assert len(snap.windows) == 0


def test_grok_oauth_fetch_valid_token(monkeypatch, tmp_path):
    """Valid token → chat probe → rate-limit headers parsed."""
    path = _write_grok_auth(tmp_path, expired=False)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)

    def fake_post(url, json=None, headers=None, timeout=30):
        return FakeResp(200, payload={}, headers=_rate_limit_headers(43))

    def fake_get(url, headers=None, timeout=30):
        return FakeResp(200, payload={"config": {"used": {"val": 1974}, "monthlyLimit": {"val": 20000}}})

    monkeypatch.setattr(grok.requests, "post", fake_post)
    monkeypatch.setattr(grok.requests, "get", fake_get)
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(43.0)
    assert snap.extra["Расход/мес."] == "$19.74 / $200.00"


def test_grok_oauth_refreshes_expired_token(monkeypatch, tmp_path):
    """Expired token → OIDC refresh, write-back, then chat probe."""
    path = _write_grok_auth(tmp_path, expired=True)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)

    def fake_get(url, headers=None, timeout=30):
        if "openid-configuration" in url:
            return FakeResp(200, payload={"token_endpoint": "https://auth.x.ai/oauth/token"})
        # billing call
        return FakeResp(200, payload={"config": {}})

    def fake_post(url, data=None, headers=None, json=None, timeout=30):
        # Distinguish refresh (form data) from chat (json body)
        if json is not None:
            return FakeResp(200, payload={}, headers=_rate_limit_headers(10))
        return FakeResp(200, payload={
            "access_token": "access-fresh",
            "refresh_token": "rt-new",
            "expires_in": 3600,
        })

    monkeypatch.setattr(grok.requests, "get", fake_get)
    monkeypatch.setattr(grok.requests, "post", fake_post)
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(10.0)
    # Token written back.
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["https://auth.x.ai::abc"]["key"] == "access-fresh"


def test_grok_no_credentials_no_apikey(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({})
    assert snap.error is not None
    assert "grok CLI" in snap.error or "ключ" in snap.error


def test_grok_apikey_fallback_with_budget(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({"grok_api_key": "xai-123", "grok_budget_usd": 50})
    assert snap.error is None
    assert snap.extra["Бюджет"] == "$ 0 / $ 50"


def test_grok_apikey_without_budget(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({"grok_api_key": "xai-123"})
    assert snap.error is not None


def test_grok_chat_probe_requires_version_header(monkeypatch, tmp_path):
    """The chat endpoint rejects old client versions (426); our headers include
    x-grok-client-version so the probe succeeds."""
    path = _write_grok_auth(tmp_path, expired=False)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    sent_headers = {}

    def fake_post(url, json=None, headers=None, timeout=30):
        sent_headers.update(headers or {})
        return FakeResp(200, payload={}, headers=_rate_limit_headers(0))

    monkeypatch.setattr(grok.requests, "post", fake_post)
    monkeypatch.setattr(grok.requests, "get",
                        lambda *a, **kw: FakeResp(200, payload={"config": {}}))
    grok.fetch({})
    assert "x-grok-client-version" in sent_headers
    assert sent_headers["x-grok-client-version"] == grok.CLI_VERSION
