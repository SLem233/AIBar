"""Kimi (Moonshot): разбор ответа api.kimi.com/coding/v1/usages.

Форма ответа у coding-плана плавает: то плоский `usage`, то конверт с `data`,
то массив `limits` с описанием окна. Разбор повторяет логику opencodex
(parseKimiQuotaPayload), поэтому проверяем все встречавшиеся варианты.
"""

import json
from datetime import datetime, timezone

import pytest

from aibar.providers import kimi

KEY = "sk-kimi-1234567890abcdef"
RESET_S = 1785000000  # epoch-секунды
RESET_DT = datetime.fromtimestamp(RESET_S, tz=timezone.utc)


class FakeResp:
    def __init__(self, status_code=200, payload=None, content=None):
        self.status_code = status_code
        self._payload = payload
        if content is not None:
            self.content = content
        else:
            self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _get(monkeypatch, resp):
    monkeypatch.setattr(kimi.requests, "get", lambda *a, **kw: resp)


# ---- разбор тела ----------------------------------------------------------
def test_flat_usage_becomes_the_weekly_window():
    out = kimi._parse_payload({"usage": {"limit": 100, "used": 43, "resetTime": RESET_S}})
    assert out["weekly"] == (43.0, RESET_DT)
    assert out["five_hour"] is None and out["total"] is None


def test_nested_data_envelope_is_unwrapped():
    body = {
        "usage": None,
        "data": {
            "usage": {"limit": 50, "used": 12, "resetTime": RESET_S},
            "totalQuota": {"limit": 1000, "used": 500},
        },
    }
    out = kimi._parse_payload(body)
    assert out["weekly"] == (24.0, RESET_DT)
    assert out["total"] == (50.0, None)


def test_limits_array_is_classified_by_window_spec():
    body = {
        "limits": [
            {
                "name": "session usage",
                "window": {"timeUnit": "MINUTE", "duration": 300, "resetTime": RESET_S},
                "detail": {"limit": 200, "used": 100},
            },
            {
                "name": "weekly usage",
                "window": {"timeUnit": "DAY", "duration": 7},
                "detail": {"limit": 1000, "used": 430},
            },
        ]
    }
    out = kimi._parse_payload(body)
    assert out["five_hour"] == (50.0, RESET_DT)
    assert out["weekly"] == (43.0, None)


def test_labels_classify_windows_when_the_spec_is_missing():
    body = {
        "limits": [
            {"name": "5-hour rolling window", "detail": {"limit": 100, "used": 30}},
            {"name": "weekly quota", "detail": {"limit": 500, "used": 250}},
        ]
    }
    out = kimi._parse_payload(body)
    assert out["five_hour"] == (30.0, None)
    assert out["weekly"] == (50.0, None)


def test_percent_from_remaining_when_used_is_absent():
    out = kimi._parse_payload({"usage": {"limit": 200, "remaining": 50}})
    assert out["weekly"] == (75.0, None)


def test_direct_utilization_is_the_fallback():
    body = {"usage": {"utilization": 73.5, "resetTime": "2026-09-01T00:00:00Z"}}
    out = kimi._parse_payload(body)
    assert out["weekly"] == (73.5, datetime(2026, 9, 1, tzinfo=timezone.utc))


def test_reset_accepts_epoch_milliseconds():
    out = kimi._parse_payload({"usage": {"limit": 10, "used": 1, "resetTime": RESET_S * 1000}})
    assert out["weekly"] == (10.0, RESET_DT)


def test_percent_is_clamped_to_a_hundred():
    out = kimi._parse_payload({"usage": {"limit": 100, "used": 250}})
    assert out["weekly"] == (100.0, None)


def test_empty_body_yields_nothing():
    out = kimi._parse_payload({"usage": None})
    assert out == {"five_hour": None, "weekly": None, "total": None}


# ---- fetch ----------------------------------------------------------------
def test_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    snap = kimi.fetch({"kimi_api_key": ""})
    assert snap.error and "Kimi" in snap.error
    assert not snap.windows


def test_key_from_environment_is_used(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", KEY)
    seen = {}

    def fake_get(url, headers=None, timeout=30):
        seen.update(headers)
        return FakeResp(200, payload={"usage": {"limit": 10, "used": 5}})

    monkeypatch.setattr(kimi.requests, "get", fake_get)
    snap = kimi.fetch({})
    assert snap.error is None
    assert seen["Authorization"] == f"Bearer {KEY}"


def test_garbage_in_the_key_field_is_rejected(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    snap = kimi.fetch({"kimi_api_key": "key with spaces"})
    assert snap.error and "не ключ" in snap.error


def test_401_means_a_bad_key(monkeypatch):
    _get(monkeypatch, FakeResp(401))
    snap = kimi.fetch({"kimi_api_key": KEY})
    assert snap.error == "Неверный API-ключ Kimi"
    assert snap.http_status == 401


def test_403_keeps_the_status_for_the_geoblock_check(monkeypatch):
    _get(monkeypatch, FakeResp(403))
    snap = kimi.fetch({"kimi_api_key": KEY})
    assert snap.http_status == 403


def test_full_payload_produces_three_windows(monkeypatch):
    payload = {
        "usage": {"limit": 1000, "used": 430, "resetTime": RESET_S},
        "limits": [
            {
                "name": "session",
                "window": {"timeUnit": "HOUR", "duration": 5, "resetTime": RESET_S},
                "detail": {"limit": 100, "used": 60},
            }
        ],
        "totalQuota": {"limit": 5000, "used": 2500},
    }
    _get(monkeypatch, FakeResp(200, payload=payload))
    snap = kimi.fetch({"kimi_api_key": KEY})
    assert snap.error is None
    assert [w.label for w in snap.windows] == ["Сессия (5ч)", "Неделя", "Кредиты (всего)"]
    assert snap.windows[0].used_percent == pytest.approx(60.0)
    assert snap.windows[1].used_percent == pytest.approx(43.0)
    assert snap.windows[1].resets_at == RESET_DT


def test_empty_response_is_reported(monkeypatch):
    _get(monkeypatch, FakeResp(200, content=b""))
    snap = kimi.fetch({"kimi_api_key": KEY})
    assert snap.error and "Пустой ответ" in snap.error


def test_payload_without_limits_is_reported(monkeypatch):
    _get(monkeypatch, FakeResp(200, payload={"hello": "world"}))
    snap = kimi.fetch({"kimi_api_key": KEY})
    assert snap.error and "не вернул окон" in snap.error
