"""Codex multi-account provider (opencodex account pool).

Tracks every OpenAI Codex account managed by opencodex
(https://github.com/lidge-jun/opencodex). The main account lives in
~/.codex/auth.json and is covered by the plain "Codex" provider; added pool
accounts live in ~/.opencodex/codex-accounts.json with last-known quotas in
codex-quota-cache.json.

Each pool account is polled against the same GET /backend-api/wham/usage
endpoint opencodex itself uses, strictly read-only: tokens are refreshed by
opencodex, never by us — refreshing here would rotate the refresh token and
deauthorize the opencodex service holding it. When a live poll is impossible
(no/expired token, network error), the cached quota from opencodex serves as
the fallback so the rings never go dark while opencodex keeps its store fresh.
"""

import time
from pathlib import Path

import requests

from .base import ProviderSnapshot, RateWindow, parse_unix
from .codex import USAGE_URL, _window_label

OCX_HOME = Path.home() / ".opencodex"

# Skip the live poll when the token is this close to expiry (opencodex will
# rotate it on its own schedule; a 401 wastes a request).
TOKEN_EXPIRY_MARGIN_SECONDS = 120


def _load_json(path: Path) -> dict:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _email_prefix(email: str) -> str:
    return (email or "?").split("@", 1)[0]


def _pool_entries(ocx_config: dict, credentials: dict) -> list[dict]:
    """Pool accounts as [{id, email, plan}]: opencodex config.json's
    codexAccounts wins; keys of codex-accounts.json are the fallback."""
    entries = []
    for account in ocx_config.get("codexAccounts") or []:
        if isinstance(account, dict) and account.get("id"):
            entries.append(
                {
                    "id": str(account["id"]),
                    "email": str(account.get("email") or ""),
                    "plan": str(account.get("plan") or ""),
                }
            )
    if not entries:
        entries = [
            {"id": str(account_id), "email": "", "plan": ""}
            for account_id in credentials
        ]
    return entries


def _live_token(credential: dict) -> tuple[str, str] | None:
    """(access_token, chatgpt_account_id) while the token is still valid."""
    token = credential.get("accessToken") or ""
    account_id = credential.get("chatgptAccountId") or ""
    expires_at = credential.get("expiresAt")
    if not token or not account_id:
        return None
    if isinstance(expires_at, (int, float)):
        if expires_at / 1000 - time.time() < TOKEN_EXPIRY_MARGIN_SECONDS:
            return None
    return token, account_id


def _live_windows(token: str, account_id: str) -> tuple[list[RateWindow], str, str] | None:
    """Poll wham/usage for one pool account: (windows, plan, email) or None."""
    try:
        resp = requests.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "ChatGPT-Account-Id": account_id,
                "Accept": "application/json",
                "User-Agent": "AIBar",
            },
            timeout=30,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    windows = []
    rate_limit = data.get("rate_limit") or {}
    for key, fallback in (("primary_window", "Сессия"), ("secondary_window", "Неделя")):
        window = rate_limit.get(key)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        windows.append(
            RateWindow(
                label=_window_label(window, fallback),
                used_percent=float(window["used_percent"]),
                resets_at=parse_unix(window.get("reset_at")),
            )
        )
    plan = str(data.get("plan_type") or "")
    email = str(data.get("email") or "")
    return windows, plan, email


def _cache_windows(quota: dict) -> list[RateWindow]:
    """Windows from opencodex's codex-quota-cache.json entry."""
    windows = []
    if isinstance(quota.get("weeklyPercent"), (int, float)):
        windows.append(
            RateWindow(
                label="Неделя",
                used_percent=float(quota["weeklyPercent"]),
                resets_at=parse_unix(quota.get("weeklyResetAt")),
            )
        )
    if isinstance(quota.get("shortPercent"), (int, float)):
        hours = (quota.get("shortWindowSeconds") or 18000) // 3600 or 5
        windows.append(
            RateWindow(
                label=f"Сессия ({hours}ч)",
                used_percent=float(quota["shortPercent"]),
                resets_at=parse_unix(quota.get("shortResetAt")),
            )
        )
    return windows


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    snap = ProviderSnapshot(provider="Codex Пул")

    have_store = any(
        (OCX_HOME / name).exists()
        for name in ("config.json", "codex-accounts.json")
    )
    if not have_store:
        snap.error = "opencodex не найден (~/.opencodex) — пул аккаунтов пуст"
        return snap

    ocx_config = _load_json(OCX_HOME / "config.json")
    credentials = _load_json(OCX_HOME / "codex-accounts.json")
    quota_cache = _load_json(OCX_HOME / "codex-quota-cache.json").get("quotas") or {}

    entries = _pool_entries(ocx_config, credentials)
    if not entries:
        snap.error = "Пул Codex пуст — добавьте аккаунты в opencodex"
        return snap

    active_id = str(ocx_config.get("activeCodexAccountId") or "")
    failed: list[str] = []
    plans: list[str] = []

    for entry in entries:
        account_id = entry["id"]
        email = entry["email"]
        windows: list[RateWindow] | None = None

        credential = credentials.get(account_id) or {}
        if isinstance(credential, dict):
            live = _live_token(credential.get("credential") or {})
            if live is not None:
                polled = _live_windows(*live)
                if polled is not None:
                    windows, plan, api_email = polled
                    email = api_email or email
                    entry["plan"] = plan or entry["plan"]
                    entry["email"] = email

        if windows is None:
            cached = _cache_windows(quota_cache.get(account_id) or {})
            if cached:
                windows = cached

        if windows is None:
            failed.append(email or account_id)
            continue

        prefix = _email_prefix(email or account_id)
        for window in windows:
            window.label = f"{window.label} · {prefix}"
        snap.windows.extend(windows)
        if entry["plan"] and entry["plan"] not in plans:
            plans.append(entry["plan"])
        if account_id == active_id:
            snap.extra["Выбран"] = email or account_id

    if plans:
        snap.plan = " + ".join(part.replace("_", " ").capitalize() for part in plans)
        if len(entries) > 1:
            snap.plan += f" ×{len(entries)}"

    if not snap.windows:
        missing = ", ".join(failed) or "нет данных"
        snap.error = f"Квоты пула недоступны ({missing})"
    return snap
