"""Kimi (Moonshot) coding-plan usage provider.

Читает GET https://api.kimi.com/coding/v1/usages с ключом coding-плана.
Форма ответа у эндпоинта не одна: то плоский `usage`, то тот же объект внутри
конверта `data`, то массив `limits` с описанием окна. Разбор повторяет логику
opencodex (`parseKimiQuotaPayload`), поэтому терпит все три варианта.

Окна определяются по спецификации (`window.timeUnit` + `duration`), а когда её
нет — по названию: «5h/5 hour» — сессия, «weekly/7d» — неделя. Верхнеуровневый
`usage` считается недельным окном, `totalQuota` — общим остатком кредитов.

Ключ берётся из настроек (`kimi_api_key`) или из переменной KIMI_API_KEY.
"""

import os
import re
from datetime import datetime

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    looks_like_api_key,
    parse_iso8601,
    parse_unix,
    subscription_renewal,
)

USAGE_URL = "https://api.kimi.com/coding/v1/usages"
TIMEOUT = 30

# Больше этого числа значение считается миллисекундами, а не секундами
# (эвристика normalizeResetAt из opencodex).
_UNIX_MS_THRESHOLD = 10_000_000_000

_RESET_KEYS = ("resetTime", "resetAt", "reset_time", "reset_at")


def _api_key(cfg: dict | None) -> str | None:
    cfg = cfg or {}
    return (
        (cfg.get("kimi_api_key") or "").strip()
        or os.environ.get("KIMI_API_KEY", "").strip()
        or None
    )


# ---- разбор ответа --------------------------------------------------------
def _to_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _clamp_percent(value) -> float | None:
    n = _to_number(value)
    return None if n is None else max(0.0, min(100.0, n))


def _normalize_reset(value) -> datetime | None:
    """ISO-строка, epoch-секунды или epoch-миллисекунды → datetime."""
    number = _to_number(value)
    if number is not None:
        seconds = number / 1000 if number > _UNIX_MS_THRESHOLD else number
        return parse_unix(seconds)
    if isinstance(value, str):
        return parse_iso8601(value.strip())
    return None


def _row_reset(row: dict, fallback: dict | None) -> datetime | None:
    """Отметка сброса из строки лимита, иначе из описания окна."""
    for source in (row, fallback or {}):
        for key in _RESET_KEYS:
            if source.get(key) is not None:
                reset = _normalize_reset(source[key])
                if reset is not None:
                    return reset
    return None


def _parse_quota_row(row: dict, fallback: dict | None = None):
    """(процент, время сброса) из строки {limit, used, remaining, …} или None."""
    if not isinstance(row, dict):
        return None
    reset = _row_reset(row, fallback)
    limit = _to_number(row.get("limit"))
    if limit and limit > 0:
        used = _to_number(row.get("used"))
        if used is None:
            remaining = _to_number(row.get("remaining"))
            if remaining is not None:
                used = limit - remaining
        if used is not None:
            percent = _clamp_percent(used / limit * 100)
            if percent is not None:
                return percent, reset
    # Готовый процент — когда арифметики limit/used в ответе нет.
    for key in ("utilization", "percent", "usedPercent", "used_percent"):
        percent = _clamp_percent(row.get(key))
        if percent is not None:
            return percent, reset
    return None


def _label_text(*rows: dict) -> str:
    bits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("name", "title", "scope"):
            value = row.get(key)
            if isinstance(value, str):
                bits.append(value)
    return " ".join(bits).lower()


def _window_spec(item: dict, detail: dict, window: dict) -> tuple[float | None, str]:
    duration = _to_number(
        window.get("duration") or item.get("duration") or detail.get("duration")
    )
    unit = str(
        window.get("timeUnit") or item.get("timeUnit") or detail.get("timeUnit") or ""
    ).upper()
    return duration, unit


def _is_five_hour(item: dict, detail: dict, window: dict) -> bool:
    duration, unit = _window_spec(item, detail, window)
    if "MINUTE" in unit and duration == 300:
        return True
    if "HOUR" in unit and duration == 5:
        return True
    return bool(re.search(r"(^|\b)5[\s-]*(?:h|hour)", _label_text(item, detail, window)))


def _is_weekly(item: dict, detail: dict, window: dict) -> bool:
    duration, unit = _window_spec(item, detail, window)
    if "DAY" in unit and duration == 7:
        return True
    if "HOUR" in unit and duration == 168:
        return True
    return bool(re.search(r"weekly|7\s*(?:d|day)", _label_text(item, detail, window)))


def _unwrap_envelope(body):
    """Если снаружи только конверт, а данные в `data` — работаем с `data`."""
    if not isinstance(body, dict):
        return None
    nested = body.get("data")
    if not isinstance(nested, dict):
        return body
    keys = ("usage", "limits", "totalQuota")
    outer_has = any(body.get(k) is not None for k in keys)
    nested_has = any(nested.get(k) is not None for k in keys)
    return nested if (not outer_has and nested_has) else body


def _parse_payload(body) -> dict:
    """{five_hour: (процент, сброс)|None, weekly: …, total: …}."""
    body = _unwrap_envelope(body)
    out = {"five_hour": None, "weekly": None, "total": None}
    if not isinstance(body, dict):
        return out
    out["weekly"] = _parse_quota_row(body.get("usage") or {})
    out["total"] = _parse_quota_row(body.get("totalQuota") or {})
    limits = body.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else item
            window = item.get("window") if isinstance(item.get("window"), dict) else {}
            if out["five_hour"] is None and _is_five_hour(item, detail, window):
                out["five_hour"] = _parse_quota_row(detail, window)
            if out["weekly"] is None and _is_weekly(item, detail, window):
                out["weekly"] = _parse_quota_row(detail, window)
            if out["five_hour"] and out["weekly"]:
                break
    return out


# ---- provider -------------------------------------------------------------
def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Kimi")
    key = _api_key(cfg)
    if not key:
        snap.error = "Укажите API-ключ coding-плана Kimi в настройках (или KIMI_API_KEY)"
        return snap
    if not looks_like_api_key(key):
        snap.error = "В поле ключа вставлен не ключ — вставьте ключ Kimi заново"
        return snap

    try:
        resp = requests.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code != 200:
        snap.http_status = resp.status_code
        snap.error = (
            "Неверный API-ключ Kimi"
            if resp.status_code == 401
            else f"HTTP {resp.status_code}"
        )
        return snap
    if not resp.content:
        snap.error = "Пустой ответ от api.kimi.com"
        return snap
    try:
        body = resp.json()
    except ValueError:
        snap.error = "Невалидный JSON в ответе Kimi"
        return snap

    parsed = _parse_payload(body)
    for key_name, label in (
        ("five_hour", "Сессия (5ч)"),
        ("weekly", "Неделя"),
        ("total", "Кредиты (всего)"),
    ):
        if parsed[key_name]:
            percent, reset = parsed[key_name]
            snap.windows.append(RateWindow(label, percent, resets_at=reset))

    renewal = subscription_renewal(cfg, "kimi")
    if renewal:
        snap.extra["Продление"] = renewal
    if not snap.windows:
        snap.error = "Kimi не вернул окон лимитов"
    return snap
