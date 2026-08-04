"""Kimi (Moonshot) coding-plan usage provider.

Polls ``GET https://api.kimi.com/coding/v1/usages`` with a coding-plan API
key (bearer). The endpoint shape mirrors what opencodex parses in
``src/providers/quota.ts:parseKimiQuotaPayload`` — a JSON envelope with the
following possible layouts:

  {
    "usage":     {"limit": N, "used": N, "remaining": N, "resetTime": ...},
    "totalQuota":{"limit": N, "used": N, "remaining": N},
    "limits": [
       {"name": "...weekly...", "window": {"duration": 7, "timeUnit": "day", ...},
        "detail": {"limit": N, "used": N, "resetTime": ...}},
       ...
    ]
  }

The same fields appear under nested ``data`` when the outer object is only an
envelope (``unwrapKimiQuotaPayload`` rule). Each row exposes ``limit/used``
(cents or tokens — opencodex does not care, we report percent), and a
optional ``resetTime`` (ISO or epoch). Some payloads expose only
``utilization``/``percent``/``usedPercent`` directly when the limit/used
arithmetic is absent.

Window classification:
  - 5-hour session: ``window.timeUnit="MINUTE" duration=300`` or
    ``"HOUR" duration=5``, or label matching ``5h/5 hour``.
  - weekly: ``timeUnit="DAY" duration=7`` or ``"HOUR" duration=168``,
    or label matching ``weekly/7d/7 day``.
  - The top-level ``usage`` (when present) is treated as the weekly window;
    ``totalQuota`` becomes a custom "Total credits" sub-window.

Key sources: ``kimi_api_key`` in settings, or the ``KIMI_API_KEY`` env var.
"""

import os
import re

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    looks_like_api_key,
    parse_iso8601,
    parse_unix,
    subscription_renewal,
)

KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/v1"
USAGE_URL = f"{KIMI_CODE_BASE_URL}/usages"
TIMEOUT = 30

# Max numeric value treated as a Unix epoch in seconds (vs ms). Mirrors
# opencodex's `>10_000_000_000` heuristic from normalizeResetAt.
_UNIX_MS_THRESHOLD = 10_000_000_000


def _api_key(cfg: dict | None) -> str | None:
    cfg = cfg or {}
    return (
        (cfg.get("kimi_api_key") or "").strip()
        or os.environ.get("KIMI_API_KEY", "").strip()
        or None
    )


# ---- response parsing ----------------------------------------------------
def _to_number(value) -> float | None:
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


def _normalize_reset_at(value) -> float | None:
    """ISO string, epoch seconds, or epoch-ms → epoch-ms (mirrors opencodex)."""
    if isinstance(value, (int, float)):
        return float(value) if value > _UNIX_MS_THRESHOLD else value * 1000
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            n = float(s)
            return n if n > _UNIX_MS_THRESHOLD else n * 1000
        dt = parse_iso8601(s)
        if dt is not None:
            return dt.timestamp() * 1000
    return None


def _quota_reset_at(row: dict, fallback: dict | None = None) -> float | None:
    """Reset timestamp from a row, with optional fallback (e.g. the parent window)."""
    for key in ("resetTime", "resetAt", "reset_time", "reset_at"):
        if row.get(key) is not None:
            r = _normalize_reset_at(row[key])
            if r is not None:
                return r
    if fallback:
        for key in ("resetTime", "resetAt", "reset_time", "reset_at"):
            if fallback.get(key) is not None:
                r = _normalize_reset_at(fallback[key])
                if r is not None:
                    return r
    return None


def _parse_quota_row(row: dict, fallback: dict | None = None):
    """Return (percent, reset_ms) or None from a {limit,used,remaining,...} row."""
    if not isinstance(row, dict):
        return None
    limit = _to_number(row.get("limit"))
    reset = _quota_reset_at(row, fallback)
    if limit and limit > 0:
        used = _to_number(row.get("used"))
        if used is None:
            remaining = _to_number(row.get("remaining"))
            if remaining is not None:
                used = limit - remaining
        if used is not None:
            pct = _clamp_percent(used / limit * 100)
            if pct is not None:
                return pct, reset
    # Direct utilization fallback (when limit/used arithmetic is absent).
    for key in ("utilization", "percent", "usedPercent", "used_percent"):
        pct = _clamp_percent(row.get(key))
        if pct is not None:
            return pct, reset
    return None


def _label_text(*rows: dict) -> str:
    bits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("name", "title", "scope"):
            v = row.get(key)
            if isinstance(v, str):
                bits.append(v)
    return " ".join(bits).lower()


def _is_five_hour(item: dict, detail: dict, window: dict) -> bool:
    duration = _to_number(window.get("duration") or item.get("duration") or detail.get("duration"))
    unit = str(window.get("timeUnit") or item.get("timeUnit") or detail.get("timeUnit") or "").upper()
    if "MINUTE" in unit and duration == 300:
        return True
    if "HOUR" in unit and duration == 5:
        return True
    return bool(re.search(r"(^|\b)5[\s-]*(?:h|hour)", _label_text(item, detail, window)))


def _is_weekly(item: dict, detail: dict, window: dict) -> bool:
    duration = _to_number(window.get("duration") or item.get("duration") or detail.get("duration"))
    unit = str(window.get("timeUnit") or item.get("timeUnit") or detail.get("timeUnit") or "").upper()
    if "DAY" in unit and duration == 7:
        return True
    if "HOUR" in unit and duration == 168:
        return True
    return bool(re.search(r"weekly|7\s*(?:d|day)", _label_text(item, detail, window)))


def _unwrap_envelope(body):
    """If outer is just an envelope with `data` holding the real payload, use it."""
    if not isinstance(body, dict):
        return None
    nested = body.get("data")
    if not isinstance(nested, dict):
        return body
    usable = lambda v: v is not None
    outer_has = usable(body.get("usage")) or usable(body.get("limits")) or usable(body.get("totalQuota"))
    nested_has = usable(nested.get("usage")) or usable(nested.get("limits")) or usable(nested.get("totalQuota"))
    return nested if (not outer_has and nested_has) else body


def _parse_payload(body) -> dict:
    """Return {five_hour: (pct, reset_ms)|None, weekly: ..., total: ...}."""
    body = _unwrap_envelope(body)
    out = {"five_hour": None, "weekly": None, "total": None}
    if not isinstance(body, dict):
        return out
    out["weekly"] = _parse_quota_row(body.get("usage") or {})
    total = _parse_quota_row(body.get("totalQuota") or {})
    if total is not None:
        out["total"] = total
    limits = body.get("limits")
    if isinstance(limits, list):
        for raw in limits:
            item = raw if isinstance(raw, dict) else None
            if not item:
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


# ---- provider ------------------------------------------------------------
def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Kimi")
    key = _api_key(cfg)
    if not key:
        snap.error = "Укажите coding-plan API-ключ Kimi в настройках (или KIMI_API_KEY)"
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

    if resp.status_code in (401, 403):
        snap.error = "Неверный API-ключ Kimi"
        snap.http_status = resp.status_code
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        snap.http_status = resp.status_code
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
    if parsed["five_hour"]:
        pct, reset = parsed["five_hour"]
        snap.windows.append(RateWindow("Сессия (5ч)", pct, resets_at=parse_unix(_ms_to_seconds(reset))))
    if parsed["weekly"]:
        pct, reset = parsed["weekly"]
        snap.windows.append(RateWindow("Неделя", pct, resets_at=parse_unix(_ms_to_seconds(reset))))
    if parsed["total"]:
        pct, reset = parsed["total"]
        snap.windows.append(RateWindow("Кредиты (всего)", pct, resets_at=parse_unix(_ms_to_seconds(reset))))

    renewal = subscription_renewal(cfg, "kimi")
    if renewal:
        snap.extra["Продление"] = renewal
    if not snap.windows:
        snap.error = "Kimi API не вернул окон лимитов"
    return snap


def _ms_to_seconds(ms):
    """Convert epoch-ms (from _normalize_reset_at) back to seconds for parse_unix."""
    if ms is None:
        return None
    return ms / 1000
