"""Grok (xAI) usage provider — subscription billing window.

Sources researched for the Grok weekly limit ("Weekly limit: Next reset: …")
shown by the Grok Build CLI ``/usage`` slash command inside the TUI:

  1. **No ``x-ratelimit-*`` headers on chat completions.** ``strings`` over
     ``~/.grok/bin/grok.exe`` confirms the headers emitted by
     ``cli-chat-proxy.grok.com`` are ``x-grok-*``/``x-xai-*`` only — there is no
     *weekly-token* quota exposed through chat. Probing chat for rate-limit
     headers returns ``0%`` because the headers are absent; that is what the
     previous version of this provider did, and why it was wrong.

  2. **The 43%/reset-10-Aug figures come from ``auth/check_subscription``** —
     a JSON-RPC method sent over the WebSocket relay ``wss://code.grok.com/
     ws/code-agent`` inside the TUI process (source: ``serialize check_subscription
     params`` / ``x.ai/auth/check_subscription`` strings in the binary). This
     relay rejects our bearer token with ``3000 Unauthorized`` externally — let
     alone that ``cli-chat-proxy.grok.com`` is intermittently down (521/Cloudflare),
     so we cannot reach ``check_subscription`` reliably from a pull script.

  3. **REST ``GET https://cli-chat-proxy.grok.com/v1/billing``** with the
     Grok-CLI OAuth bearer succeeds (200) and returns::

         {
           "config": {
             "monthlyLimit":  {"val": 20000},        # cents
             "used":          {"val": 1974},
             "onDemandCap":   {"val": 0},
             "billingPeriodStart": "2026-08-01T00:00:00+00:00",
             "billingPeriodEnd":   "2026-09-01T00:00:00+00:00",
             "history": [...]
           }
         }

For the user whose CLI reports ``Monthly limit: 10%`` ($19.74 of $200
prepaid monthly credit), this REST call returns exactly that — and the
``billingPeriodEnd`` drives the gauge's reset countdown. The *separate*
``Weekly limit: 43%`` rolling-chat-tokens window lives behind the
relay-lock gated by the TUI process; we surface it as a hint in the error
string when billing is unavailable and otherwise let the gauge reflect the
 billed monthly spend (the only authority we can poll headlessly).

OAuth token from ``~/.grok/auth.json`` is auto-refreshed via OIDC
(``auth.x.ai``), so no CLI session is needed after first login. API-key
fallback (console.x.ai) only supports a manual monthly budget field.
"""

import json
from datetime import datetime, timezone, timedelta

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    looks_like_api_key,
    parse_iso8601,
    subscription_renewal,
)

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
OIDC_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
# Public Grok CLI OAuth client_id.
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
# Minimum CLI version the proxy checked (mirrors the bundled binary's
# x-grok-client-version so the relay accepts the bearer).
CLI_VERSION = "0.2.118"
TIMEOUT = 30
AUTH_KEY_PREFIX = "https://auth.x.ai::"

# Friendly hint when the relay is down or rejects our token. Kept in Russian
# because the project is Russian-language UI (vas status push).
RELAY_HINT = (
    "Подписка-квоту (недельные окна) видно только внутри grok TUI — "
    "откройте `grok` и нажмите /usage."
)


# ---- helpers --------------------------------------------------------------
def _auth_path():
    from pathlib import Path

    return Path.home() / ".grok" / "auth.json"


def _load_grok_token():
    """Return (path, entry) from ~/.grok/auth.json, or (None, None)."""
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
    return datetime.now(timezone.utc) >= dt - timedelta(seconds=120)


def _discover_token_endpoint() -> str:
    resp = requests.get(OIDC_DISCOVERY_URL, timeout=TIMEOUT)
    resp.raise_for_status()
    return (resp.json().get("token_endpoint") or "https://auth.x.ai/oauth/token")


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


def _billing_headers(token: str) -> dict:
    """Headers the relay accepts for the billing endpoint.

    The ``x-grok-*`` family mirrors what the bundled CLI sends; ``x-xai-token-auth``
    must be ``xai-grok-cli`` (the relay rejects ``true`` with 401, observed live).
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"grok-cli/{CLI_VERSION}",
        "x-grok-client-version": CLI_VERSION,
        "x-grok-client-identifier": "grok-cli",
        "x-xai-token-auth": "xai-grok-cli",
    }


def _fetch_billing(token: str) -> dict | None:
    """Best-effort fetch of /v1/billing. Returns the ``config`` sub-dict, or None."""
    try:
        resp = requests.get(
            BILLING_URL,
            headers=_billing_headers(token),
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content:
        return None
    try:
        return resp.json().get("config") or None
    except (ValueError, AttributeError):
        return None


def _api_key(cfg: dict | None) -> str | None:
    import os

    cfg = cfg or {}
    return (
        (cfg.get("grok_api_key") or "").strip()
        or os.environ.get("XAI_API_KEY", "").strip()
        or None
    )


def _window_span_start_end(start_iso: str | None, end_iso: str | None) -> str:
    """Human-readable label for the window based on its length: "Неделя" or "Мес."."""
    if not start_iso or not end_iso:
        return "Подписка"
    start = parse_iso8601(start_iso)
    end = parse_iso8601(end_iso)
    if start is None or end is None:
        return "Подписка"
    days = (end - start).total_seconds() / 86400
    if 6 <= days <= 8:
        return "Неделя (списание)"
    if 27 <= days <= 33:
        return "Мес. (списание)"
    return "Подписка"


def _apply_billing(snap: ProviderSnapshot, config: dict) -> None:
    """Turn the /v1/billing config block into the primary RateWindow."""
    limit_raw = (config.get("monthlyLimit") or {}).get("val")
    used_raw = (config.get("used") or {}).get("val")
    period_end = config.get("billingPeriodEnd")
    if limit_raw is None or used_raw is None or not limit_raw:
        return
    pct = min(100.0, used_raw / limit_raw * 100)
    label = _window_span_start_end(config.get("billingPeriodStart"), period_end)
    resets_at = parse_iso8601(period_end) if period_end else None
    snap.windows.append(RateWindow(label, pct, resets_at=resets_at))
    # Spent/limit as an extra line, in dollars (config values are cents).
    snap.extra["Списание"] = f"${used_raw / 100:.2f} / ${limit_raw / 100:.2f}"
    on_demand_cap = (config.get("onDemandCap") or {}).get("val")
    if on_demand_cap:
        # prepaid pay-as-you-go pool; show remaining.
        snap.extra[" PAYG"] = f"${on_demand_cap / 100:.2f}"


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
        config = _fetch_billing(token)
        if config is None:
            # One refresh + retry on auth failure (proxy may have rotated
            # the token between us and the relay).
            try:
                token = _refresh_grok_token(path, entry)
                config = _fetch_billing(token)
            except (RuntimeError, requests.RequestException) as exc:
                # Don't hide the relay-down message under the refresh error.
                snap.error = f"{exc}"
                if "runtime" not in snap.error.lower():
                    snap.error += " — " + RELAY_HINT
                return snap
        if config is None:
            # All probes failed: either the relay is down (521) or the token
            # is rejected. Surface the hint so the user knows how to read
            # their actual weekly window.
            snap.error = (
                "Grok: /v1/billing не отвечает. " + RELAY_HINT
            )
            return snap
        _apply_billing(snap, config)
        renewal = subscription_renewal(cfg, "grok")
        if renewal:
            snap.extra["Продление"] = renewal
        if not snap.windows:
            snap.error = "Grok: биллинг вернул пустые данные"
        return snap

    # Path 2: API key fallback (console.x.ai).
    key = _api_key(cfg)
    if not key:
        snap.error = (
            "Нет OAuth (~/.grok/auth.json) и нет API-ключа — войдите через "
            "grok CLI или укажите ключ XAI в настройках"
        )
        return snap
    if not looks_like_api_key(key):
        snap.error = "В поле ключа вставлен не ключ — вставьте XAI_API_KEY заново"
        return snap
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
