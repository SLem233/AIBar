"""GitHub Copilot usage provider (reverse-engineered, individual plan).

Polls ``GET https://api.github.com/copilot_internal/user`` with the user's
GitHub OAuth token (the same one ``gh`` CLI stores). This is an **internal**
GitHub endpoint used by VS Code Copilot Chat itself; the public GitHub REST
API does not expose Copilot usage for individual users (only
org/enterprise seat metrics).

Response shape (live probe):
    {
      "login": "octocat",
      "access_type_sku": "free_limited_copilot" | "copilot_business" | …,
      "copilot_plan": "individual" | "business" | "enterprise",
      "quota_reset_date": "2026-09-01",
      "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
      "quota_snapshots": {
        "chat":                {entitlement, remaining, percent_remaining, unlimited, …},
        "completions":         {entitlement, remaining, percent_remaining, unlimited, …},
        "premium_interactions":{entitlement, remaining, percent_remaining, unlimited, …}
      },
      "endpoints": {"api": "https://api.individual.githubcopilot.com", …}
    }

We compute ``used_percent = (entitlement − remaining) / entitlement × 100``
per snapshot and surface them as three windows (chat, completions, premium).
Reset countdown uses ``quota_reset_date_utc``.

Token resolution order:
  1. ``copilot_token`` in settings (explicit ``ghp_…``/``gho_…`` token).
  2. ``GITHUB_TOKEN`` / ``GH_TOKEN`` env vars.
  3. ``gh auth token`` subprocess (when the official GitHub CLI is installed
     and the user is logged in — token is stored in OS keyring there).

This endpoint lives behind GitHub's standard OAuth flow; ``read:user`` scope
(which ``gh`` requests by default) is sufficient. No Copilot subscription
auth or device flow is needed for the usage read.
"""

import os
import subprocess

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    looks_like_api_key,
    parse_iso8601,
    subscription_renewal,
)

USAGE_URL = "https://api.github.com/copilot_internal/user"
# Mirror the headers VS Code Copilot Chat sends (api-config.ts in copilot-api).
API_VERSION = "2025-04-01"
EDITOR_PLUGIN_VERSION = "copilot-chat/0.26.7"
USER_AGENT = "GitHubCopilotChat/0.26.7"
TIMEOUT = 30

# Per-snapshot display labels (kept short for the gauge).
SNAPSHOT_LABELS = {
    "chat": "Chat",
    "completions": "Completions",
    "premium_interactions": "Premium",
}

# SKU → friendly plan string for the snapshot header.
PLAN_SKUS = {
    "free_limited_copilot": "Free",
    "copilot_business": "Business",
    "copilot_enterprise": "Enterprise",
    "copilot_pro": "Pro",
    "copilot_plus": "Plus",
}


def _token_from_gh_cli() -> str | None:
    """Best-effort: `gh auth token` (token is in the OS keyring, not a file)."""
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    tok = (out.stdout or "").strip()
    return tok or None


def _api_token(cfg: dict | None) -> str | None:
    cfg = cfg or {}
    return (
        (cfg.get("copilot_token") or "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
        or os.environ.get("GH_TOKEN", "").strip()
        or _token_from_gh_cli()
        or None
    )


def _headers(token: str) -> dict:
    return {
        "authorization": f"token {token}",
        "accept": "application/json",
        "content-type": "application/json",
        "editor-version": "vscode/1.95.0",
        "editor-plugin-version": EDITOR_PLUGIN_VERSION,
        "user-agent": USER_AGENT,
        "x-github-api-version": API_VERSION,
    }


def _snapshot_window(name: str, snap: dict, resets_at) -> RateWindow | None:
    """One quota snapshot → RateWindow; None if entitlement=0 (quota disabled)."""
    if not isinstance(snap, dict):
        return None
    unlimited = bool(snap.get("unlimited"))
    entitlement = snap.get("entitlement")
    remaining = snap.get("remaining")
    label = SNAPSHOT_LABELS.get(name, name)
    if unlimited:
        # Unlimited snapshot: gauge pinned to 0% (nothing consumed off an
        # infinite allowance). Useful for Pro plans where chat is unlimited.
        return RateWindow(f"{label} ∞", 0.0, resets_at=resets_at)
    try:
        ent = float(entitlement or 0)
        rem = float(remaining or 0)
    except (TypeError, ValueError):
        return None
    if ent <= 0:
        return None
    pct = max(0.0, min(100.0, (ent - rem) / ent * 100))
    return RateWindow(label, pct, resets_at=resets_at)


# ---- provider ------------------------------------------------------------
def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Copilot")
    token = _api_token(cfg)
    if not token:
        snap.error = (
            "Нет токена GitHub — установите gh CLI (gh auth login) или "
            "укажите copilot_token в настройках"
        )
        return snap
    if not looks_like_api_key(token):
        snap.error = "В поле токена вставлен не токен — вставьте GitHub-токен заново"
        return snap

    try:
        resp = requests.get(USAGE_URL, headers=_headers(token), timeout=TIMEOUT)
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code in (401, 403):
        snap.error = "Токен GitHub не авторизован для Copilot (нужен gh auth login)"
        snap.http_status = resp.status_code
        return snap
    if resp.status_code == 404:
        # Free accounts without Copilot access sometimes 404 on _internal.
        snap.error = "Copilot не подключён к аккаунту GitHub"
        snap.http_status = 404
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        snap.http_status = resp.status_code
        return snap
    if not resp.content:
        snap.error = "Пустой ответ от api.github.com"
        return snap

    try:
        body = resp.json()
    except ValueError:
        snap.error = "Невалидный JSON в ответе GitHub"
        return snap

    sku = body.get("access_type_sku") or ""
    snap.plan = PLAN_SKUS.get(sku) or body.get("copilot_plan") or sku
    login = body.get("login")
    if login:
        snap.extra["Аккаунт"] = login

    reset_dt = parse_iso8601(body.get("quota_reset_date_utc") or body.get("quota_reset_date"))
    snapshots = body.get("quota_snapshots") or {}
    # Order: chat (most relevant) → completions → premium.
    for key in ("chat", "completions", "premium_interactions"):
        win = _snapshot_window(key, snapshots.get(key) or {}, reset_dt)
        if win is not None:
            snap.windows.append(win)
    if reset_dt:
        snap.extra["Сброс"] = reset_dt.astimezone().strftime("%d.%m.%Y")

    renewal = subscription_renewal(cfg, "copilot")
    if renewal:
        snap.extra["Продление"] = renewal
    if not snap.windows:
        snap.error = "GitHub не вернул окон квот Copilot"
    return snap
