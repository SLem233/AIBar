"""OpenAI API spend provider (platform.openai.com, not ChatGPT plan).

Polls the organization Costs API: GET /v1/organization/costs. Requires an
Admin API key (platform.openai.com -> Settings -> Organization -> Admin keys);
regular project keys get 401. Prepaid credit balance has no public API, so the
card shows month-to-date spend, and — when a monthly budget is set in
settings — a percent ring against that budget.
"""

from datetime import datetime, timezone

import requests

from .base import ProviderSnapshot, RateWindow, looks_like_api_key

COSTS_URL = "https://api.openai.com/v1/organization/costs"


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, next_month


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

    spent = 0.0
    currency = "usd"
    params = {"start_time": int(month_start.timestamp()), "bucket_width": "1d", "limit": 31}
    for _ in range(4):  # follow pagination defensively
        try:
            resp = requests.get(
                COSTS_URL,
                params=params,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            snap.error = f"Сетевая ошибка: {exc}"
            return snap

        if resp.status_code == 401:
            snap.error = "401 — нужен именно Admin-ключ (sk-admin…), не обычный ключ проекта"
            return snap
        if resp.status_code != 200:
            snap.error = f"HTTP {resp.status_code}"
            return snap

        body = resp.json()
        for bucket in body.get("data") or []:
            for result in bucket.get("results") or []:
                amount = result.get("amount") or {}
                spent += float(amount.get("value") or 0)
                currency = amount.get("currency") or currency
        if not body.get("has_more"):
            break
        params["page"] = body.get("next_page")

    symbol = "$" if currency.lower() == "usd" else f" {currency.upper()}"
    snap.extra["Расход за месяц"] = f"${spent:.2f}" if symbol == "$" else f"{spent:.2f}{symbol}"

    budget = float(cfg.get("openai_budget_usd") or 0)
    if budget > 0:
        percent = min(100.0, spent / budget * 100)
        snap.windows.append(RateWindow("Бюджет (месяц)", percent, resets_at=month_end))
        snap.extra["Бюджет"] = f"${budget:.2f}"
    return snap
