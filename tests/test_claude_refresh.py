"""Claude auto-refresh: the widget must show limits without running the CLI.

The new claude.py proactively refreshes an expired access token using the
stored refresh token and writes it back, so Claude Desktop GUI-only use works.
"""

import json

import pytest

from aibar.providers import claude


class FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.request = type("R", (), {"headers": {}})()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


USAGE_BODY = {
    "five_hour": {"utilization": 42.0, "resets_at": "2026-07-30T21:00:00Z"},
    "seven_day": {"utilization": 18.0, "resets_at": "2026-08-05T12:00:00Z"},
}


def _write_creds(tmp_path, *, expired=True, refresh_token="rt-old"):
    """Write a credentials file with an (optionally expired) access token."""
    cred = tmp_path / ".credentials.json"
    oauth = {
        "accessToken": "access-stale",
        "refreshToken": refresh_token,
    }
    if expired:
        oauth["expiresAt"] = 0  # far in the past
    else:
        oauth["expiresAt"] = 9_999_999_999_999  # far future
    cred.write_text(
        json.dumps({"claudeAiOauth": oauth, "other": "keep-me"}),
        encoding="utf-8",
    )
    return cred


def test_claude_refreshes_expired_token_then_fetches(monkeypatch, tmp_path):
    """Expired access token is refreshed, written back, then usage is fetched."""
    cred = _write_creds(tmp_path, expired=True)
    monkeypatch.setattr(claude, "_cred_path", lambda: cred)

    refresh_calls = []

    def fake_post(url, json=None, headers=None, timeout=30):
        refresh_calls.append(json)
        return FakeResp(
            200,
            payload={
                "access_token": "access-fresh",
                "refresh_token": "rt-new",
                "expires_in": 3600,
                "refresh_token_expires_in": 2592000,
            },
        )

    def fake_get(url, headers=None, timeout=30):
        # The usage call must use the freshly-refreshed token.
        assert "access-fresh" in headers["Authorization"]
        return FakeResp(200, payload=USAGE_BODY)

    monkeypatch.setattr(claude.requests, "post", fake_post)
    monkeypatch.setattr(claude.requests, "get", fake_get)

    snap = claude.fetch({})
    assert snap.error is None
    assert len(snap.windows) == 2
    assert snap.windows[0].used_percent == pytest.approx(42.0)
    assert snap.windows[1].used_percent == pytest.approx(18.0)
    # Refresh was attempted exactly once.
    assert len(refresh_calls) == 1
    assert refresh_calls[0]["grant_type"] == "refresh_token"

    # The rotated token was written back to disk (access + refresh).
    saved = json.loads(cred.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert saved["accessToken"] == "access-fresh"
    assert saved["refreshToken"] == "rt-new"
    assert saved["expiresAt"] > 0
    # Other top-level keys are preserved.
    assert json.loads(cred.read_text(encoding="utf-8"))["other"] == "keep-me"


def test_claude_skips_refresh_when_token_valid(monkeypatch, tmp_path):
    """A still-valid access token is used directly; no refresh call."""
    cred = _write_creds(tmp_path, expired=False)
    monkeypatch.setattr(claude, "_cred_path", lambda: cred)
    monkeypatch.setattr(
        claude.requests, "get",
        lambda *a, **kw: FakeResp(200, payload=USAGE_BODY),
    )
    post_calls = []
    monkeypatch.setattr(
        claude.requests, "post",
        lambda *a, **kw: post_calls.append(1) or FakeResp(200, payload={}),
    )
    snap = claude.fetch({})
    assert snap.error is None
    assert len(post_calls) == 0  # no refresh


def test_claude_retries_on_401_after_refresh(monkeypatch, tmp_path):
    """Usage returns 401 → one refresh+retry, then succeeds."""
    cred = _write_creds(tmp_path, expired=False)
    monkeypatch.setattr(claude, "_cred_path", lambda: cred)

    def fake_post(url, json=None, headers=None, timeout=30):
        return FakeResp(
            200,
            payload={
                "access_token": "access-fresh",
                "refresh_token": "rt-new",
                "expires_in": 3600,
            },
        )

    call_count = {"n": 0}

    def fake_get(url, headers=None, timeout=30):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResp(401)
        return FakeResp(200, payload=USAGE_BODY)

    monkeypatch.setattr(claude.requests, "post", fake_post)
    monkeypatch.setattr(claude.requests, "get", fake_get)
    snap = claude.fetch({})
    assert snap.error is None
    assert snap.windows[0].used_percent == pytest.approx(42.0)
    assert call_count["n"] == 2  # retried once


def test_claude_missing_credentials_file(monkeypatch, tmp_path):
    """Missing .credentials.json → clear error, no crash."""
    cred = tmp_path / ".credentials.json"
    monkeypatch.setattr(claude, "_cred_path", lambda: cred)
    snap = claude.fetch({})
    assert snap.error is not None
    assert "не найден" in snap.error
