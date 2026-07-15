"""Codex (ChatGPT) usage provider.

Reads OAuth tokens from ~/.codex/auth.json (maintained by the codex CLI) and
polls GET https://chatgpt.com/backend-api/wham/usage, the same endpoint
CodexBar uses. Tokens are refreshed by codex itself; we never write the file.
"""

import base64
import json
from pathlib import Path

import requests

from .base import ProviderSnapshot, RateWindow, parse_unix

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

AUTH_PATH = Path.home() / ".codex" / "auth.json"


def _account_id(tokens: dict) -> str | None:
    if tokens.get("account_id"):
        return tokens["account_id"]
    # Fall back to the chatgpt_account_id claim inside the id_token JWT
    id_token = tokens.get("id_token")
    if not id_token or id_token.count(".") != 2:
        return None
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    return auth_claims.get("chatgpt_account_id")


def _window_label(window: dict, fallback: str) -> str:
    seconds = window.get("limit_window_seconds") or 0
    if seconds >= 6 * 86400:
        return "Неделя"
    if seconds >= 3600:
        return f"Сессия ({seconds // 3600}ч)"
    return fallback


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    snap = ProviderSnapshot(provider="Codex")
    try:
        auth = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        snap.error = "Файл ~/.codex/auth.json не найден"
        return snap
    except json.JSONDecodeError as exc:
        snap.error = f"auth.json повреждён: {exc}"
        return snap

    tokens = auth.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        snap.error = "В auth.json нет access_token — выполните вход в codex"
        return snap

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "AIBar",
    }
    account_id = _account_id(tokens)
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    try:
        resp = requests.get(USAGE_URL, headers=headers, timeout=30)
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code in (401, 403):
        snap.error = f"{resp.status_code} — запустите codex для повторной авторизации"
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        return snap

    data = resp.json()
    plan = data.get("plan_type") or ""
    snap.plan = plan.replace("_", " ").capitalize()

    rate_limit = data.get("rate_limit") or {}
    for key, fallback in (("primary_window", "Сессия"), ("secondary_window", "Неделя")):
        window = rate_limit.get(key)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        snap.windows.append(
            RateWindow(
                label=_window_label(window, fallback),
                used_percent=float(window["used_percent"]),
                resets_at=parse_unix(window.get("reset_at")),
            )
        )

    credits = data.get("credits") or {}
    if credits.get("unlimited"):
        snap.extra["Кредиты"] = "безлимит"
    elif credits.get("balance") is not None:
        try:
            snap.extra["Кредиты"] = f"{float(credits['balance']):g}"
        except (TypeError, ValueError):
            snap.extra["Кредиты"] = str(credits["balance"])

    if not snap.windows:
        snap.error = "API не вернул ни одного окна лимитов"
    return snap
