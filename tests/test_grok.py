"""Grok provider: monthly billing window via /v1/billing REST (cli-chat-proxy).

Research notes (kept here so the rationale travels with the tests):
- The Grok CLI TUI's "Weekly limit: 43% Next reset: August 10" is fetched
  by an in-TUI JSON-RPC ``auth/check_subscription`` call over the WebSocket
  relay ``wss://code.grok.com/ws/code-agent`` — rejected externally with
  ``3000 Unauthorized``. It is **not** reachable from a pull script.
- There are **no** ``x-ratelimit-*`` headers on chat completions (verified by
  ``strings`` on ``~/.grok/bin/grok.exe``). Probing chat for them returned 0% —
  that path has been removed.
- ``GET https://cli-chat-proxy.grok.com/v1/billing`` succeeds (when the relay
  is up) and returns ``config.{monthlyLimit,used,billingPeriodStart,
  billingPeriodEnd,...}`` (cents / ISO-8601). The percent shown by the gauge is
  ``used / monthlyLimit * 100``; the reset countdown is ``billingPeriodEnd``.
  Source of truth for the /v1/billing payload shape: live probe of the user's
  account + opencodex ``src/providers/quota.ts`` implementation.
"""

import json

from aibar.providers import grok
from aibar.providers.base import ProviderSnapshot


class FakeResp:
    """Minimal requests.Response for these tests."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        # requests exposes .content as bytes; _fetch_billing guards on it.
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)


def _write_grok_auth(tmp_path, *, expired=False, refresh_token="rt-grk"):
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


def _billing_payload(*, used=1974, limit=20000, period_start="2026-08-01T00:00:00+00:00",
                     period_end="2026-09-01T00:00:00+00:00", on_demand_cap=0):
    return {
        "config": {
            "monthlyLimit": {"val": limit},
            "used": {"val": used},
            "onDemandCap": {"val": on_demand_cap},
            "billingPeriodStart": period_start,
            "billingPeriodEnd": period_end,
            "history": [],
        }
    }


# ---- _apply_billing ------------------------------------------------------
def test_apply_billing_computes_monthly_percent():
    snap = ProviderSnapshot(provider="Grok")
    config = {
        "monthlyLimit": {"val": 20000},
        "used": {"val": 1974},
        "billingPeriodStart": "2026-08-01T00:00:00+00:00",
        "billingPeriodEnd": "2026-09-01T00:00:00+00:00",
    }
    grok._apply_billing(snap, config)
    assert len(snap.windows) == 1
    # 1974 / 20000 = 9.87%
    assert snap.windows[0].used_percent == 9.87
    assert snap.windows[0].label == "Мес. (списание)"
    assert snap.extra["Списание"] == "$19.74 / $200.00"


def test_apply_billing_weekly_period_label():
    snap = ProviderSnapshot(provider="Grok")
    config = {
        "monthlyLimit": {"val": 10000},
        "used": {"val": 4300},
        "billingPeriodStart": "2026-08-04T00:00:00+00:00",
        "billingPeriodEnd": "2026-08-11T00:00:00+00:00",  # 7 days → weekly
    }
    grok._apply_billing(snap, config)
    assert snap.windows[0].used_percent == 43.0
    assert snap.windows[0].label == "Неделя (списание)"


def test_apply_billing_missing_fields_sets_nothing():
    snap = ProviderSnapshot(provider="Grok")
    grok._apply_billing(snap, {"monthlyLimit": {"val": 0}})
    assert not snap.windows


# ---- fetch ---------------------------------------------------------------
def test_grok_oauth_fetch_valid_token(monkeypatch, tmp_path):
    """Valid token → /v1/billing succeeds → window from config."""
    path = _write_grok_auth(tmp_path, expired=False)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)

    def fake_get(url, headers=None, timeout=30):
        return FakeResp(200, payload=_billing_payload(used=1974, limit=20000))

    monkeypatch.setattr(grok.requests, "get", fake_get)
    snap = grok.fetch({})
    assert snap.error is None
    assert len(snap.windows) == 1
    assert snap.windows[0].used_percent == 9.87
    assert snap.extra["Списание"] == "$19.74 / $200.00"
    # rate-limit probe was removed entirely — requests.post must never be hit
    # (we don't even patch it; if the code calls it, the test errors out).


def test_grok_oauth_refreshes_expired_token(monkeypatch, tmp_path):
    """Expired token → OIDC refresh, write-back, then /v1/billing."""
    path = _write_grok_auth(tmp_path, expired=True)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)

    def fake_get(url, headers=None, timeout=30):
        if "openid-configuration" in url:
            return FakeResp(200, payload={"token_endpoint": "https://auth.x.ai/oauth/token"})
        return FakeResp(200, payload=_billing_payload(used=500, limit=5000))

    def fake_post(url, data=None, headers=None, json=None, timeout=30):
        return FakeResp(200, payload={
            "access_token": "access-fresh",
            "refresh_token": "rt-new",
            "expires_in": 3600,
        })

    monkeypatch.setattr(grok.requests, "get", fake_get)
    monkeypatch.setattr(grok.requests, "post", fake_post)
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == 10.0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["https://auth.x.ai::abc"]["key"] == "access-fresh"
    assert saved["https://auth.x.ai::abc"]["refresh_token"] == "rt-new"


def test_grok_billing_401_triggers_refresh_then_retry(monkeypatch, tmp_path):
    """401 on /v1/billing → refresh → 200 on retry."""
    path = _write_grok_auth(tmp_path, expired=False)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)

    calls = {"count": 0, "refreshes": 0}

    def fake_get(url, headers=None, timeout=30):
        calls["count"] += 1
        if calls["count"] == 1:
            return FakeResp(401, payload={"error": "expired"})
        return FakeResp(200, payload=_billing_payload(used=1000, limit=5000))

    def fake_post(url, data=None, headers=None, json=None, timeout=30):
        calls["refreshes"] += 1
        return FakeResp(200, payload={
            "access_token": "access-refreshed",
            "refresh_token": "rt-new2",
            "expires_in": 3600,
        })

    monkeypatch.setattr(grok.requests, "get", fake_get)
    monkeypatch.setattr(grok.requests, "post", fake_post)
    snap = grok.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == 20.0
    assert calls["refreshes"] == 1


def test_grok_billing_server_down_surfaces_hint(monkeypatch, tmp_path):
    """521 from the relay (Cloudflare origin-down) → error carries the CLI hint."""
    path = _write_grok_auth(tmp_path, expired=False)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)

    def fake_get(url, headers=None, timeout=30):
        return FakeResp(521, payload=None, text="origin down")

    monkeypatch.setattr(grok.requests, "get", fake_get)
    snap = grok.fetch({})
    assert snap.error
    # The hint about reading the subscription window inside the TUI must
    # appear — that's the actionable takeaway for the user.
    assert "grok" in snap.error.lower()
    assert "TUI" in snap.error or "/usage" in snap.error


def test_grok_no_credentials_no_apikey(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({})
    assert snap.error is not None
    assert "grok CLI" in snap.error or "ключ" in snap.error


def test_grok_apikey_fallback_with_budget(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({"grok_api_key": "xai-1234567890abcdef", "grok_budget_usd": 50})
    assert snap.error is None
    assert snap.extra["Бюджет"] == "$ 0 / $ 50"


def test_grok_apikey_without_budget(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"  # does not exist
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    snap = grok.fetch({"grok_api_key": "xai-1234567890abcdef"})
    assert snap.error is not None


def test_grok_billing_headers_include_xai_grok_cli(monkeypatch, tmp_path):
    """The relay rejects x-xai-token-auth='true' (401) but accepts 'xai-grok-cli'."""
    path = _write_grok_auth(tmp_path, expired=False)
    monkeypatch.setattr(grok, "_auth_path", lambda: path)
    sent_headers = {}

    def fake_get(url, headers=None, timeout=30):
        sent_headers.update(headers or {})
        return FakeResp(200, payload=_billing_payload(used=0, limit=10000))

    monkeypatch.setattr(grok.requests, "get", fake_get)
    grok.fetch({})
    assert sent_headers.get("x-xai-token-auth") == "xai-grok-cli"
    assert "x-grok-client-version" in sent_headers
    assert sent_headers["x-grok-client-version"] == grok.CLI_VERSION
