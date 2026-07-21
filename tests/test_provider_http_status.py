"""Providers must expose the HTTP status so poll_all can classify 403s."""

import json

from aibar.providers import claude, codex, openai_api


class Resp:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {}


def test_claude_sets_http_status_on_403(monkeypatch):
    monkeypatch.setattr(claude, "_load_credentials", lambda: {"accessToken": "tok"})
    monkeypatch.setattr(claude.requests, "get", lambda *a, **kw: Resp(403))
    snap = claude.fetch({})
    assert snap.http_status == 403
    assert snap.error == "HTTP 403"


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
