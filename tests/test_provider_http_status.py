"""Providers must expose the HTTP status so poll_all can classify 403s."""

import json
from pathlib import Path

from aibar.providers import claude, codex, openai_api


class Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.request = type("R", (), {"headers": {}})()

    def json(self):
        return {}


def test_claude_sets_http_status_on_403(monkeypatch, tmp_path):
    """A non-401/403-but-still-failing status is surfaced as http_status.

    Note: on 401/403 the new claude module attempts a refresh+retry; to test the
    pure http_status path we use a 503 (which is not retried)."""
    cred = tmp_path / ".credentials.json"
    cred.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9_999_999_999_999}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude, "_cred_path", lambda: cred)

    def fake_get(url, headers=None, timeout=30):
        return Resp(503)

    monkeypatch.setattr(claude.requests, "get", fake_get)
    snap = claude.fetch({})
    assert snap.http_status == 503
    assert snap.error == "HTTP 503"


def test_codex_sets_http_status_on_403(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}), encoding="utf-8")
    monkeypatch.setattr(codex, "AUTH_PATH", auth)
    monkeypatch.setattr(codex.requests, "get", lambda *a, **kw: Resp(403))
    snap = codex.fetch({})
    assert snap.http_status == 403


def test_openai_sets_http_status_on_403(monkeypatch):
    monkeypatch.setattr(openai_api.requests, "get", lambda *a, **kw: Resp(403))
    snap = openai_api.fetch({"openai_admin_key": "sk-admin-test1234"})
    assert snap.http_status == 403
    assert snap.error == "HTTP 403"
