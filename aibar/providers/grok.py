"""Grok (xAI) usage provider — subscription rate limits via chat headers.

The Grok CLI ``/usage`` command shows WEEKLY rate limits as percentages. Those
limits are returned in ``x-ratelimit-*`` response headers on chat completions
requests — there is no dedicated usage endpoint. So this provider makes one
minimal chat request (1 token) to ``cli-chat-proxy.grok.com`` and reads the
headers:

  x-ratelimit-limit-tokens      — weekly token quota
  x-ratelimit-remaining-tokens  — tokens remaining in the window
  x-ratelimit-limit-requests    — request quota (per-window)
  x-ratelimit-remaining-requests

The token quota is the one ``/usage`` reports as a percentage. We also fetch
``/v1/billing`` for the monthly spend/budget (best-effort, shown as extra).

OAuth token from ``~/.grok/auth.json`` (created by the Grok CLI at login) is
auto-refreshed via OIDC (``auth.x.ai``), so no CLI session is needed after the
first login. API-key fallback (console.x.ai) only supports the monthly budget.
"""

import json

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    parse_iso8601,
    subscription_renewal,
)

CHAT_URL = "https://cli-chat-proxy.grok.com/v1/chat/completions"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
OIDC_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
# Public Grok CLI OAuth client_id.
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
# Minimum CLI version the proxy accepts.
CLI_VERSION = "0.1.202"
TIMEOUT = 30
AUTH_KEY_PREFIX = "https://auth.x.ai::"


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


def _chat_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"grok-cli/{CLI_VERSION}",
        "x-grok-client-version": CLI_VERSION,
        "x-grok-client-identifier": "grok-cli",
        "x-xai-token-auth": "xai-grok-cli",
    }


def _probe_rate_limits(token: str) -> dict:
    """Minimal chat request (1 token) to capture x-ratelimit-* headers.

    Returns a dict with keys: limit_tokens, remaining_tokens, limit_requests,
    remaining_requests (ints or None).
    """
    payload = {
        "model": "grok-4",
        "messages": [{"role": "user", "content": "1"}],
        "max_tokens": 1,
        "stream": False,
    }
    resp = requests.post(
        CHAT_URL,
        json=payload,
        headers=_chat_headers(token),
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise requests.HTTPError(response=resp)
    h = resp.headers
    out = {}
    for field, header in (
        ("limit_tokens", "x-ratelimit-limit-tokens"),
        ("remaining_tokens", "x-ratelimit-remaining-tokens"),
        ("limit_requests", "x-ratelimit-limit-requests"),
        ("remaining_requests", "x-ratelimit-remaining-requests"),
    ):
        val = h.get(header)
        if val is not None:
            try:
                out[field] = int(val)
            except (TypeError, ValueError):
                pass
    return out


def _fetch_billing(token: str) -> dict | None:
    """Best-effort monthly spend/budget from /v1/billing (may 404 or fail)."""
    try:
        resp = requests.get(
            BILLING_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


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
            limits = _probe_rate_limits(token)
        except requests.HTTPError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                # Try one refresh + retry.
                try:
                    token = _refresh_grok_token(path, entry)
                    limits = _probe_rate_limits(token)
                except (RuntimeError, requests.RequestException, requests.HTTPError) as e:
                    snap.error = f"{e} — обновите вход: запустите grok CLI"
                    return snap
            else:
                snap.error = f"HTTP {code}"
                return snap
        except requests.RequestException as exc:
            snap.error = f"Сетевая ошибка: {exc}"
            return snap
        _apply_rate_limits(snap, limits)
        # Best-effort monthly spend.
        billing = _fetch_billing(token)
        if billing:
            config = billing.get("config") or {}
            used = (config.get("used") or {}).get("val")
            limit = (config.get("monthlyLimit") or {}).get("val")
            if used is not None and limit:
                snap.extra["Расход/мес."] = f"${used / 100:.2f} / ${limit / 100:.2f}"
        renewal = subscription_renewal(cfg, "grok")
        if renewal:
            snap.extra["Продление"] = renewal
        return snap

    # Path 2: API key fallback (console.x.ai).
    key = _api_key(cfg)
    if not key:
        snap.error = (
            "Нет OAuth (~/.grok/auth.json) и нет API-ключа — войдите через "
            "grok CLI или укажите ключ XAI в настройках"
        )
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


def _apply_rate_limits(snap: ProviderSnapshot, limits: dict) -> None:
    """Convert x-ratelimit-* header values into a RateWindow (weekly %)."""
    limit = limits.get("limit_tokens")
    remaining = limits.get("remaining_tokens")
    if limit and remaining is not None:
        used = max(0, limit - remaining)
        pct = min(100.0, used / limit * 100) if limit > 0 else 0.0
        snap.windows.append(RateWindow("Неделя (токены)", pct))
    # Request quota as a secondary window if present.
    rlim = limits.get("limit_requests")
    rrem = limits.get("remaining_requests")
    if rlim and rrem is not None:
        rused = max(0, rlim - rrem)
        rpct = min(100.0, rused / rlim * 100) if rlim > 0 else 0.0
        snap.windows.append(RateWindow("Окно (запросы)", rpct))
    if not snap.windows:
        snap.error = "Grok не вернул заголовков x-ratelimit-*"
