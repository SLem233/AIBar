"""Z.ai (GLM coding plan, zcode) usage provider.

Polls GET {host}/api/monitor/usage/quota/limit with a Bearer API key —
the key from the Z.ai coding-plan dashboard (also used by the zcode CLI).
Key sources: settings (config.json) or the Z_AI_API_KEY / ZAI_API_KEY env vars.
"""

import os
from datetime import datetime, timezone

import requests

from .base import ProviderSnapshot, RateWindow, parse_unix

HOSTS = {
    "global": "https://api.z.ai",
    "bigmodel-cn": "https://open.bigmodel.cn",
}
QUOTA_PATH = "/api/monitor/usage/quota/limit"


def _api_key(cfg: dict) -> str | None:
    return (
        (cfg.get("zai_api_key") or "").strip()
        or os.environ.get("Z_AI_API_KEY", "").strip()
        or os.environ.get("ZAI_API_KEY", "").strip()
        or None
    )


def _window_label(resets_at, index: int) -> str:
    if resets_at is not None:
        hours = (resets_at - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours <= 26:
            return "Сессия"
        if hours <= 8 * 24:
            return "Неделя"
        return "Месяц"
    return f"Лимит {index + 1}"


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Z.ai")
    key = _api_key(cfg)
    if not key:
        snap.error = "Укажите API-ключ Z.ai в настройках (или Z_AI_API_KEY)"
        return snap

    host = HOSTS.get(cfg.get("zai_region", "global"), HOSTS["global"])
    try:
        resp = requests.get(
            host + QUOTA_PATH,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code in (401, 403):
        snap.error = "Неверный API-ключ Z.ai"
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        return snap
    if not resp.content:
        snap.error = "Пустой ответ — проверьте регион (Global / BigModel CN) и ключ"
        return snap

    body = resp.json()
    if not (body.get("success") and body.get("code") == 200):
        snap.error = body.get("msg") or f"Z.ai API вернул код {body.get('code')}"
        return snap

    data = body.get("data") or {}
    snap.plan = data.get("planName") or data.get("plan") or ""

    entries = []
    for raw in data.get("limits") or []:
        percentage = raw.get("percentage")
        if percentage is None:
            # derive from usage/remaining when the API omits the percent
            limit_value = raw.get("usage")
            remaining = raw.get("remaining")
            if limit_value and remaining is not None:
                percentage = max(0, min(100, (limit_value - remaining) / limit_value * 100))
            else:
                continue
        entries.append((float(percentage), parse_unix((raw.get("nextResetTime") or 0) / 1000 or None)))

    entries.sort(key=lambda e: e[1] or datetime.max.replace(tzinfo=timezone.utc))
    for i, (percentage, resets_at) in enumerate(entries):
        snap.windows.append(
            RateWindow(_window_label(resets_at, i), percentage, resets_at=resets_at)
        )

    if not snap.windows:
        snap.error = "API не вернул ни одного лимита"
    return snap
