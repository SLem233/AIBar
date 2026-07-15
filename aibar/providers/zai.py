"""Z.ai (GLM coding plan, zcode) usage provider.

Polls GET {host}/api/monitor/usage/quota/limit with a Bearer API key —
the key from the Z.ai coding-plan dashboard (also used by the zcode CLI).
Key sources: settings (config.json) or the Z_AI_API_KEY / ZAI_API_KEY env vars.
"""

import os

import requests

from .base import ProviderSnapshot, RateWindow, looks_like_api_key, parse_unix

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


# unit codes from the z.ai API (via CodexBar): 1=days, 3=hours, 5=minutes, 6=weeks
UNIT_MINUTES = {1: 24 * 60, 3: 60, 5: 1, 6: 7 * 24 * 60}


def _window_minutes(raw: dict) -> int | None:
    number = raw.get("number") or 0
    factor = UNIT_MINUTES.get(raw.get("unit"))
    return number * factor if number > 0 and factor else None


def _used_percent(raw: dict) -> float | None:
    """Prefer used/limit over the API's rounded `percentage` field."""
    limit = raw.get("usage")  # yes: `usage` is the quota total in this API
    if limit and limit > 0:
        used = None
        if raw.get("remaining") is not None:
            used = limit - raw["remaining"]
            if raw.get("currentValue") is not None:
                used = max(used, raw["currentValue"])
        elif raw.get("currentValue") is not None:
            used = raw["currentValue"]
        if used is not None:
            return max(0.0, min(100.0, used / limit * 100))
    percentage = raw.get("percentage")
    return float(percentage) if percentage is not None else None


def _token_label(minutes: int | None) -> str:
    if minutes:
        if minutes <= 26 * 60:
            return f"Сессия ({minutes // 60}ч)"
        if minutes <= 9 * 24 * 60:
            return "Неделя"
        return "Месяц"
    return "Токены"


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Z.ai")
    key = _api_key(cfg)
    if not key:
        snap.error = "Укажите API-ключ Z.ai в настройках (или Z_AI_API_KEY)"
        return snap
    if not looks_like_api_key(key):
        snap.error = "В поле ключа вставлен не ключ — вставьте ключ Z.ai заново"
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

    token_limits = []
    tools_limit = None  # TIME_LIMIT: monthly Web Search / Reader / Zread quota
    for raw in data.get("limits") or []:
        percent = _used_percent(raw)
        if percent is None:
            continue
        resets_at = parse_unix((raw.get("nextResetTime") or 0) / 1000 or None)
        if raw.get("type") == "TOKENS_LIMIT":
            token_limits.append((_window_minutes(raw), percent, resets_at))
        else:
            tools_limit = (percent, resets_at)

    # Shortest token window first (session — shown in the gauge center),
    # then the longer one; the tools quota goes last, never primary.
    token_limits.sort(key=lambda e: e[0] if e[0] is not None else 10**9)
    for minutes, percent, resets_at in token_limits:
        snap.windows.append(
            RateWindow(_token_label(minutes), percent, resets_at=resets_at)
        )
    if tools_limit is not None:
        snap.windows.append(
            RateWindow("Инструменты (мес.)", tools_limit[0], resets_at=tools_limit[1])
        )

    if not snap.windows:
        snap.error = "API не вернул ни одного лимита"
    return snap
