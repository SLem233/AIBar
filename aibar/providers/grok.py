"""Grok (xAI) usage provider — subscription OAuth or API key.

Two paths, tried in order:

1. **OAuth (subscription login)** — reads the Grok CLI token from
   ``~/.grok/auth.json`` (created by the Grok CLI at login) and polls the same
   billing endpoint the CLI uses:
   ``GET https://cli-chat-proxy.grok.com/v1/billing``
   The token is auto-refreshed with the stored refresh_token (OIDC via
   ``https://auth.x.ai``), so no CLI session is needed after the first login.

2. **API key (console.x.ai)** — falls back to a user-provided API key from
   settings (``grok_api_key``) or ``XAI_API_KEY`` env var. The public
   ``api.x.ai`` has no free quota endpoint, so this path can only report the
   configured monthly budget (best-effort) — it is primarily a fallback.

Subscription OAuth is the recommended, cheaper path (no per-token billing),
matching how the user prefers to use Grok.
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

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
OIDC_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
# Public Grok CLI OAuth client_id (from opencodex/xAI).
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
TIMEOUT = 30
AUTH_KEY_PREFIX = "https://auth.x.ai::"


# ---- helpers --------------------------------------------------------------
def _auth_path():
    from pathlib import Path

    return Path.home() / ".grok" / "auth.json"


def _load_grok_token():
    """Return the OAuth entry from ~/.grok/auth.json (or None if missing).

    The file maps ``"https://auth.x.ai::<id>"`` → {key, refresh_token, expires_at, ...}.
    """
    path = _auth_path()
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    for key, entry in data.items():
        if isinstance(key, str) and key.startswith(AUTH_KEY_PREFIX) and isinstance(entry, dict):
            return path, entry
    return None, None


def _token_expired(entry: dict) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return False
    dt = parse_iso8601(exp)
    if dt is None:
        return False
    # Refresh 2 min early.
    from datetime import datetime, timezone, timedelta

    return datetime.now(timezone.utc) >= dt - timedelta(seconds=120)


def _discover_token_endpoint() -> str:
    resp = requests.get(OIDC_DISCOVERY_URL, timeout=TIMEOUT)
    resp.raise_for_status()
    return (resp.json().get("token_endpoint")
            or "https://auth.x.ai/oauth/token")


def _refresh_grok_token(path, entry: dict) -> str:
    """Refresh the access token via OIDC and write it back to ~/.grok/auth.json."""
    rt = entry.get("refresh_token")
    if not rt:
        raise RuntimeError("нет refresh_token в ~/.grok/auth.json — войдите через grok CLI")
    token_endpoint = _discover_token_endpoint()
    resp = requests.post(
        token_endpoint,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": rt,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        detail = resp.text[:200]
        if "invalid_grant" in detail:
            raise RuntimeError(
                "Grok refresh-токен истёк/отозван — обновите вход: запустите grok CLI"
            )
        raise RuntimeError(f"Grok refresh HTTP {resp.status_code}: {detail}")
    body = resp.json()
    entry["key"] = body["access_token"]
    if body.get("refresh_token"):
        entry["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        from datetime import datetime, timezone, timedelta

        entry["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
        ).isoformat()
    if body.get("id_token"):
        entry["id_token"] = body["id_token"]
    # Write back atomically, preserving other entries.
    full = json.loads(path.read_text(encoding="utf-8"))
    for k in full:
        if k.startswith(AUTH_KEY_PREFIX):
            full[k] = entry
            break
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(full, indent=2), encoding="utf-8")
    tmp.replace(path)
    return body["access_token"]


def _billing(token: str) -> dict:
    resp = requests.get(
        BILLING_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise requests.HTTPError(response=resp)
    return resp.json()


def _api_key(cfg: dict | None) -> str | None:
    import os

    cfg = cfg or {}
    return (
        (cfg.get("grok_api_key") or "").strip()
        or os.environ.get("XAI_API_KEY", "").strip()
        or None
    )


# ---- provider -------------------------------------------------------------
def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Grok")

    # Path 1: subscription OAuth from ~/.grok/auth.json.
    path, entry = _load_grok_token()
    if entry and entry.get("key"):
        token = entry["key"]
        if _token_expired(entry):
            try:
                token = _refresh_grok_token(path, entry)
            except (RuntimeError, requests.RequestException) as exc:
                snap.error = f"{exc}"
                return snap
        try:
            data = _billing(token)
        except requests.HTTPError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                # Try one refresh + retry.
                try:
                    token = _refresh_grok_token(path, entry)
                    data = _billing(token)
                except (RuntimeError, requests.RequestException, requests.HTTPError) as e:
                    snap.error = f"{e} — обновите вход: запустите grok CLI"
                    return snap
            else:
                snap.error = f"HTTP {code}"
                return snap
        except requests.RequestException as exc:
            snap.error = f"Сетевая ошибка: {exc}"
            return snap
        return _parse_billing(snap, data, cfg)

    # Path 2: API key fallback (console.x.ai).
    key = _api_key(cfg)
    if not key:
        snap.error = (
            "Нет OAuth (~/.grok/auth.json) и нет API-ключа — войдите через "
            "grok CLI или укажите ключ XAI в настройках"
        )
        return snap
    # The public api.x.ai has no usage/quota endpoint for API keys, so we can
    # only report the user-configured monthly budget.
    budget = cfg.get("grok_budget_usd") or 0
    if budget > 0:
        snap.windows.append(RateWindow("Бюджет (мес.)", 0.0))
        snap.extra["Бюджет"] = f"$ 0 / $ {budget}"
        renewal = subscription_renewal(cfg, "grok")
        if renewal:
            snap.extra["Продление"] = renewal
        return snap
    snap.error = (
        "API-ключ задан, но публичный api.x.ai не отдаёт лимиты по ключу — "
        "войдите через grok CLI для лимитов подписки"
    )
    return snap


def _parse_billing(snap: ProviderSnapshot, data: dict, cfg: dict) -> ProviderSnapshot:
    """Parse the cli-chat-proxy.grok.com/v1/billing response."""
    config = data.get("config") or data
    # Monthly usage: used.val / monthlyLimit.val (values are in US cents).
    limit = (config.get("monthlyLimit") or {}).get("val")
    used = (config.get("used") or {}).get("val")
    reset = config.get("billingPeriodEnd") or data.get("billingPeriodEnd")
    if limit and limit > 0 and used is not None:
        pct = max(0.0, min(100.0, float(used) / float(limit) * 100))
        snap.windows.append(
            RateWindow(
                "Подписка (мес.)",
                pct,
                resets_at=parse_iso8601(reset),
            )
        )
        snap.extra["Расход"] = f"${used / 100:.2f} / ${limit / 100:.2f}"
    if not snap.windows:
        snap.error = "Grok billing не вернул данных о лимите"
    plan = (config.get("plan") or config.get("tier") or "").capitalize()
    if plan:
        snap.plan = plan
    renewal = subscription_renewal(cfg, "grok")
    if renewal:
        snap.extra["Продление"] = renewal
    return snap
