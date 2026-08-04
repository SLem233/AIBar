"""Claude usage provider — works with Claude Desktop GUI only (no CLI needed).

Reads the OAuth token from ~/.claude/.credentials.json and polls
GET https://api.anthropic.com/api/oauth/usage. Unlike earlier versions, this
module **auto-refreshes an expired access token** using the stored refresh
token and writes the rotated token back — so the widget shows limits even when
the user only uses the Claude Desktop GUI and never opens the CLI.

The refresh logic mirrors the battle-tested ``airoute/scripts/get-limits.py``
reference: refresh tokens rotate (each refresh annulls the previous one), so
the new token is written back *before* the usage call. Race-aware: if a
parallel session already refreshed, we read the fresh token from disk.
"""

import json
import time

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    parse_iso8601,
    subscription_renewal,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claude-cli/2.1.0 (external, cli)"
# Public OAuth client_id Claude Code uses at login.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TIMEOUT = 30

# (json key, human label) in display order; first two feed the tray gauge
WINDOW_KEYS = [
    ("five_hour", "Сессия (5ч)"),
    ("seven_day", "Неделя"),
    ("seven_day_opus", "Opus (нед.)"),
    ("seven_day_sonnet", "Sonnet (нед.)"),
]


# ---- token helpers --------------------------------------------------------
def _cred_path():
    from pathlib import Path

    return Path.home() / ".claude" / ".credentials.json"


def _load_oauth():
    cred = _cred_path()
    data = json.loads(cred.read_text(encoding="utf-8"))
    # Return (path, oauth) so callers can write back the rotated token.
    return cred, data.get("claudeAiOauth") or {}


def _write_back(cred, oauth):
    """Atomically save the updated claudeAiOauth, preserving other top-level keys."""
    full = json.loads(cred.read_text(encoding="utf-8"))
    full["claudeAiOauth"] = oauth
    tmp = cred.with_name(cred.name + ".tmp")
    tmp.write_text(json.dumps(full, indent=2), encoding="utf-8")
    tmp.replace(cred)


def _token_valid(oauth: dict) -> bool:
    """Access token present and not expiring within the next 60 s."""
    exp = oauth.get("expiresAt")
    return bool(oauth.get("accessToken")) and isinstance(exp, (int, float)) and int(time.time() * 1000) < exp - 60_000


def _do_refresh(cred, oauth: dict) -> str:
    """One POST refresh + immediate write-back of the rotated token."""
    rt = oauth.get("refreshToken")
    if not rt:
        raise RuntimeError("нет refreshToken — выполните вход: claude")
    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": CLIENT_ID,
        },
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"refresh HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    now_ms = int(time.time() * 1000)
    oauth["accessToken"] = body["access_token"]
    if body.get("refresh_token"):
        oauth["refreshToken"] = body["refresh_token"]
    if body.get("expires_in"):
        oauth["expiresAt"] = now_ms + int(body["expires_in"]) * 1000
    if body.get("refresh_token_expires_in"):
        oauth["refreshTokenExpiresAt"] = now_ms + int(body["refresh_token_expires_in"]) * 1000
    _write_back(cred, oauth)
    return oauth["accessToken"]


def _refresh(cred, oauth: dict) -> str:
    """Race-aware refresh for multiple parallel sessions on a shared token file.

    If this refresh fails with invalid_grant, a parallel session likely already
    rotated the token — re-read the file and use whatever is there.
    """
    used_rt = oauth.get("refreshToken")
    try:
        return _do_refresh(cred, oauth)
    except RuntimeError as exc:
        if "invalid_grant" not in str(exc):
            raise
        # Parallel session may have refreshed; re-read.
        _, fresh = _load_oauth()
        if _token_valid(fresh):
            oauth.update(fresh)
            return fresh["accessToken"]
        if fresh.get("refreshToken") and fresh.get("refreshToken") != used_rt:
            try:
                token = _do_refresh(cred, fresh)
                oauth.update(fresh)
                return token
            except RuntimeError:
                pass
        raise RuntimeError(
            "refresh-токен истёк/отозван — выполните вход: запустите claude "
            "или Claude Desktop и залогиньтесь"
        ) from None


def _usage(token: str) -> dict:
    resp = requests.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": USER_AGENT,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise requests.HTTPError(response=resp)
    return resp.json()


# ---- provider -------------------------------------------------------------
def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    snap = ProviderSnapshot(provider="Claude")
    try:
        cred, oauth = _load_oauth()
    except FileNotFoundError:
        snap.error = "Файл ~/.claude/.credentials.json не найден"
        return snap
    except (RuntimeError, json.JSONDecodeError) as exc:
        snap.error = str(exc)
        return snap

    token = oauth.get("accessToken")
    if not token:
        snap.error = "В .credentials.json нет accessToken — войдите в Claude"
        return snap

    snap.plan = (oauth.get("subscriptionType") or "").capitalize()

    # Proactively refresh if the access token has expired (the GUI doesn't
    # always keep it fresh on its own — this is what enables GUI-only use).
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)) and int(time.time() * 1000) >= exp - 60_000:
        try:
            token = _refresh(cred, oauth)
        except (RuntimeError, requests.RequestException) as exc:
            snap.error = str(exc)
            return snap

    try:
        data = _usage(token)
    except requests.HTTPError as exc:
        if exc.response.status_code not in (401, 403):
            snap.http_status = exc.response.status_code
            snap.error = f"HTTP {exc.response.status_code}"
            return snap
        # Token rejected despite local validity — one refresh + retry.
        try:
            token = _refresh(cred, oauth)
            data = _usage(token)
        except (RuntimeError, requests.RequestException, requests.HTTPError) as e:
            snap.error = (
                f"{e} — обновите вход: запустите claude или Claude Desktop "
                "и залогиньтесь"
            )
            return snap
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

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

    # The API exposes no renewal date; it comes from settings.
    renewal = subscription_renewal(cfg, "claude")
    if renewal:
        snap.extra["Продление"] = renewal
    return snap
