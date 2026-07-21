"""Claude usage provider.

Reads the Claude Code OAuth token from ~/.claude/.credentials.json and polls
the same endpoint CodexBar uses: GET https://api.anthropic.com/api/oauth/usage.
The token is refreshed by Claude Code itself; we never write the file.
"""

import json
from pathlib import Path

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    parse_iso8601,
    subscription_renewal,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claude-code/2.1.0"

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# (json key, human label) in display order; first two feed the tray gauge
WINDOW_KEYS = [
    ("five_hour", "Сессия (5ч)"),
    ("seven_day", "Неделя"),
    ("seven_day_opus", "Opus (нед.)"),
    ("seven_day_sonnet", "Sonnet (нед.)"),
]


def _load_credentials() -> dict:
    data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    oauth = data.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise RuntimeError("В .credentials.json нет accessToken — выполните вход в claude")
    return oauth


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    snap = ProviderSnapshot(provider="Claude")
    try:
        oauth = _load_credentials()
    except FileNotFoundError:
        snap.error = "Файл ~/.claude/.credentials.json не найден"
        return snap
    except (RuntimeError, json.JSONDecodeError) as exc:
        snap.error = str(exc)
        return snap

    # Do not fail on a locally expired expiresAt: the file may be stale while
    # the token still works — let the API decide (it returns 401 if it's dead).
    snap.plan = (oauth.get("subscriptionType") or "").capitalize()

    try:
        resp = requests.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {oauth['accessToken']}",
                "Accept": "application/json",
                "anthropic-beta": BETA_HEADER,
                "User-Agent": USER_AGENT,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code != 200:
        snap.http_status = resp.status_code
    if resp.status_code == 401:
        # Claude Code refreshes the token lazily — on the first real API call,
        # not at startup, so just opening a session is not enough.
        snap.error = (
            "Токен истёк — запустите claude и отправьте любой запрос, "
            "токен обновится сам"
        )
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        return snap

    data = resp.json()
    for key, label in WINDOW_KEYS:
        window = data.get(key)
        if not isinstance(window, dict) or window.get("utilization") is None:
            continue
        snap.windows.append(
            RateWindow(
                label=label,
                used_percent=float(window["utilization"]),
                resets_at=parse_iso8601(window.get("resets_at")),
            )
        )

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("monthly_limit"):
        used = extra.get("used_credits") or 0
        snap.extra["Доп. кредиты"] = (
            f"${used / 100:.2f} / ${extra['monthly_limit'] / 100:.2f}"
        )

    if not snap.windows:
        snap.error = "API не вернул ни одного окна лимитов"

    _apply_profile(snap, headers=resp.request.headers)

    # The API exposes no renewal date (subscription_created_at is not the
    # billing anchor after plan changes), so it comes from settings.
    renewal = subscription_renewal(cfg, "claude")
    if renewal:
        snap.extra["Продление"] = renewal
    return snap


def _apply_profile(snap: ProviderSnapshot, headers) -> None:
    """Exact plan tier (e.g. Max 5x) and subscription status from the profile."""
    try:
        resp = requests.get(PROFILE_URL, headers=dict(headers), timeout=15)
        if resp.status_code != 200:
            return
        org = resp.json().get("organization") or {}
    except (requests.RequestException, ValueError):
        return

    tier = org.get("rate_limit_tier") or ""  # e.g. default_claude_max_5x
    if "max_" in tier:
        snap.plan = "Max " + tier.rsplit("_", 1)[-1]
    if org.get("subscription_status") and org["subscription_status"] != "active":
        snap.extra["Статус подписки"] = org["subscription_status"]
