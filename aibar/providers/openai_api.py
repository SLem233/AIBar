"""OpenAI API spend provider (platform.openai.com, not ChatGPT plan).

Polls the organization Costs API: GET /v1/organization/costs. Requires an
Admin API key (platform.openai.com -> Settings -> Organization -> Admin keys);
regular project keys get 401.

The prepaid credit balance has no public API (the dashboard endpoint accepts
only browser session keys), so the card derives it from a user-set anchor:
"balance $X on date D" minus everything the Costs API reports since D.
"""

from datetime import datetime, timezone

import requests

from .base import ProviderSnapshot, RateWindow, looks_like_api_key, parse_user_date

COSTS_URL = "https://api.openai.com/v1/organization/costs"


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, next_month


def _fetch_costs(key: str, since: datetime) -> list[tuple[int, float]]:
    """Daily cost buckets [(bucket_start_unix, usd)] since the given moment."""
    buckets: list[tuple[int, float]] = []
    params = {"start_time": int(since.timestamp()), "bucket_width": "1d", "limit": 180}
    for _ in range(8):  # follow pagination defensively
        resp = requests.get(
            COSTS_URL,
            params=params,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise PermissionError(resp.status_code)
        body = resp.json()
        for bucket in body.get("data") or []:
            total = sum(
                float((r.get("amount") or {}).get("value") or 0)
                for r in bucket.get("results") or []
            )
            buckets.append((int(bucket.get("start_time") or 0), total))
        if not body.get("has_more"):
            break
        params["page"] = body.get("next_page")
    return buckets


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="OpenAI API")
    key = (cfg.get("openai_admin_key") or "").strip()
    if not key:
        snap.error = "Укажите Admin-ключ OpenAI в настройках (Settings → Admin keys)"
        return snap
    if not looks_like_api_key(key):
        snap.error = "В поле ключа вставлен не ключ — вставьте sk-admin-… заново"
        return snap

    now = datetime.now(timezone.utc)
    month_start, month_end = _month_bounds(now)
    anchor_date = parse_user_date(cfg.get("openai_balance_date") or "")
    anchor_usd = float(cfg.get("openai_balance_usd") or 0)

    since = min(month_start, anchor_date) if anchor_date else month_start
    try:
        buckets = _fetch_costs(key, since)
    except PermissionError as exc:
        code = exc.args[0]
        if code == 401:
            snap.error = "401 — нужен именно Admin-ключ (sk-admin…), не обычный ключ проекта"
        else:
            snap.error = f"HTTP {code}"
        return snap
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    month_spent = sum(usd for ts, usd in buckets if ts >= month_start.timestamp())
    snap.extra["Расход за месяц"] = f"${month_spent:.2f}"

    if anchor_date and anchor_usd > 0:
        spent_since_anchor = sum(
            usd for ts, usd in buckets if ts >= anchor_date.timestamp()
        )
        balance = anchor_usd - spent_since_anchor
        snap.extra["Остаток на счету"] = f"≈ ${balance:.2f}"
        if balance < 0:
            snap.extra["Остаток на счету"] += " (обновите якорь в настройках)"

    budget = float(cfg.get("openai_budget_usd") or 0)
    if budget > 0:
        percent = min(100.0, month_spent / budget * 100)
        snap.windows.append(RateWindow("Бюджет (месяц)", percent, resets_at=month_end))
        snap.extra["Бюджет"] = f"${budget:.2f}"
    return snap
