"""Kimi provider: parses api.kimi.com/coding/v1/usages envelope into windows.

Test cases mirror opencodex parseKimiQuotaPayload, covering:
  - flat {usage: {limit,used}} payload
  - nested envelope {data: {usage: {...}, limits: [...]}}
  - 5h vs weekly classification from window {timeUnit, duration}
  - direct `utilization` fallback when limit/used is absent
  - 401 → 'Неверный API-ключ Kimi'
"""

import json
from datetime import timezone

import pytest

from aibar.providers import kimi
from aibar.providers.base import ProviderSnapshot


class FakeResp:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content if content else (
            json.dumps(payload).encode() if payload is not None else b""
        )

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ---- _parse_payload unit tests -------------------------------------------
def test_parse_flat_usage_row():
    """Top-level `usage` becomes the weekly window; reset from resetTime."""
    body = {
        "usage": {"limit": 100, "used": 43, "resetTime": 1785000000},
    }
    out = kimi._parse_payload(body)
    assert out["weekly"] == (43.0, 1785000000 * 1000)
    assert out["five_hour"] is None
    assert out["total"] is None


def test_parse_unwraps_nested_envelope():
    """{data: {usage: {...}, limits: [...]}} unwraps to the inner payload."""
    body = {
        "usage": None,
        "data": {
            "usage": {"limit": 50, "used": 12, "resetTime": 1785000000},
            "totalQuota": {"limit": 1000, "used": 500},
        },
    }
    out = kimi._parse_payload(body)
    assert out["weekly"] == (24.0, 1785000000 * 1000)
    assert out["total"] == (50.0, None)


def test_parse_limits_array_classifies_windows():
    """limits[] with window {timeUnit, duration} → 5h vs weekly detection."""
    body = {
        "limits": [
            {
                "name": "session usage",
                "window": {"timeUnit": "MINUTE", "duration": 300, "resetTime": 1785000000},
                "detail": {"limit": 200, "used": 100},
            },
            {
                "name": "weekly usage",
                "window": {"timeUnit": "DAY", "duration": 7, "resetTime": 1785500000},
                "detail": {"limit": 1000, "used": 430},
            },
        ],
    }
    out = kimi._parse_payload(body)
    assert out["five_hour"] == (50.0, 1785000000 * 1000)
    assert out["weekly"] == (43.0, 1785500000 * 1000)


def test_parse_utilization_fallback():
    """When limit/used is absent, fall back to utilization/percent."""
    body = {"usage": {"utilization": 73.5, "resetTime": "2026-09-01T00:00:00Z"}}
    out = kimi._parse_payload(body)
    # 2026-09-01T00:00:00Z in epoch-ms (verified with Python).
    import datetime as _dt
    expected_ms = _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000
    assert out["weekly"] == (73.5, expected_ms)


def test_parse_empty_body_returns_none():
    out = kimi._parse_payload({"usage": None})
    assert out["weekly"] is None and out["five_hour"] is None and out["total"] is None


def test_classify_via_label_when_window_missing():
    """No timeUnit → fall back to label regex (5h/weekly keywords)."""
    body = {
        "limits": [
            {"name": "5-hour rolling window", "detail": {"limit": 100, "used": 30}},
            {"name": "weekly quota", "detail": {"limit": 500, "used": 250}},
        ],
    }
    out = kimi._parse_payload(body)
    assert out["five_hour"] == (30.0, None)
    assert out["weekly"] == (50.0, None)


# ---- fetch ---------------------------------------------------------------
def test_fetch_no_api_key(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    snap = kimi.fetch({"kimi_api_key": ""})
    assert snap.error and "Kimi" in snap.error
    assert not snap.windows


def test_fetch_invalid_key_string():
    """looks_like_api_key rejects keys with whitespace (33-126 printable ASCII only)."""
    snap = kimi.fetch({"kimi_api_key": "key with spaces"})
    assert snap.error == "В поле ключа вставлен не ключ — вставьте ключ Kimi заново"
    assert not snap.windows


def test_fetch_401_invalid_key(monkeypatch):
    def fake_get(url, headers=None, timeout=30):
        return FakeResp(401)

    monkeypatch.setattr(kimi.requests, "get", fake_get)
    snap = kimi.fetch({"kimi_api_key": "sk-kimi-1234567890abcdef"})
    assert snap.error == "Неверный API-ключ Kimi"
    assert snap.http_status == 401


def test_fetch_success_with_two_windows(monkeypatch):
    payload = {
        "usage": {"limit": 1000, "used": 430, "resetTime": 1785500000},
        "limits": [
            {
                "name": "session",
                "window": {"timeUnit": "HOUR", "duration": 5, "resetTime": 1785000000},
                "detail": {"limit": 100, "used": 60},
            },
        ],
        "totalQuota": {"limit": 5000, "used": 2500},
    }

    def fake_get(url, headers=None, timeout=30):
        return FakeResp(200, payload=payload)

    monkeypatch.setattr(kimi.requests, "get", fake_get)
    snap = kimi.fetch({"kimi_api_key": "sk-kimi-1234567890abcdef"})
    assert snap.error is None
    # windows: 5h session → weekly → total credits
    labels = [w.label for w in snap.windows]
    assert "Сессия (5ч)" in labels
    assert "Неделя" in labels
    assert "Кредиты (всего)" in labels
    weekly = next(w for w in snap.windows if w.label == "Неделя")
    assert weekly.used_percent == pytest.approx(43.0)
    session = next(w for w in snap.windows if w.label == "Сессия (5ч)")
    assert session.used_percent == pytest.approx(60.0)
    # Reset countdown drives the gauge label.
    assert weekly.resets_at is not None
    assert weekly.resets_at.tzinfo == timezone.utc


def test_fetch_empty_response(monkeypatch):
    def fake_get(url, headers=None, timeout=30):
        return FakeResp(200, content=b"")

    monkeypatch.setattr(kimi.requests, "get", fake_get)
    snap = kimi.fetch({"kimi_api_key": "sk-kimi-1234567890abcdef"})
    assert snap.error and "Пустой ответ" in snap.error
