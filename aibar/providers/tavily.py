"""Tavily usage provider.

Polls GET https://api.tavily.com/usage with the regular tvly-… API key.
Shows the account plan quota (credits per month) and, if the key has its own
limit, the per-key usage. Key sources: settings or TAVILY_API_KEY env var.
"""

import os

import requests

from .base import ProviderSnapshot, RateWindow, looks_like_api_key

USAGE_URL = "https://api.tavily.com/usage"


def _percent(used, limit) -> float | None:
    try:
        used, limit = float(used), float(limit)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    return max(0.0, min(100.0, used / limit * 100))


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Tavily")
    key = (cfg.get("tavily_api_key") or "").strip() or os.environ.get(
        "TAVILY_API_KEY", ""
    ).strip()
    if not key:
        snap.error = "Укажите API-ключ Tavily в настройках (или TAVILY_API_KEY)"
        return snap
    if not looks_like_api_key(key):
        snap.error = "В поле ключа вставлен не ключ — вставьте tvly-… заново"
        return snap

    try:
        resp = requests.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code in (401, 403):
        snap.error = "Неверный API-ключ Tavily"
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        return snap

    data = resp.json()
    account = data.get("account") or {}
    key_info = data.get("key") or {}

    snap.plan = str(account.get("current_plan") or account.get("plan") or "").capitalize()

    plan_used = account.get("plan_usage", account.get("usage"))
    plan_limit = account.get("plan_limit", account.get("limit"))
    percent = _percent(plan_used, plan_limit)
    if percent is not None:
        snap.windows.append(RateWindow("Бесплатные (месяц)", percent))
        snap.extra["Кредиты"] = f"{float(plan_used):g} / {float(plan_limit):g}"
        overage = float(plan_used) - float(plan_limit)
        if overage > 0:  # pay-as-you-go beyond the free tier
            snap.extra["Сверх бесплатных"] = f"{overage:g} кредитов (платно)"

    paygo_used, paygo_limit = account.get("paygo_usage"), account.get("paygo_limit")
    if paygo_limit:
        snap.extra["PAYG"] = f"{float(paygo_used or 0):g} / {float(paygo_limit):g}"

    key_percent = _percent(key_info.get("usage"), key_info.get("limit"))
    if key_percent is not None:
        snap.windows.append(RateWindow("Этот ключ", key_percent))
    elif key_info.get("usage") is not None and not snap.extra:
        snap.extra["Использовано ключом"] = f"{key_info['usage']:g}"

    if not snap.windows and not snap.extra:
        snap.error = "API не вернул данных об использовании"
    return snap
