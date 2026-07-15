"""OpenCode (opencode.ai Zen subscription) usage provider.

opencode.ai has no public usage API; like CodexBar, we call the site's
SolidStart server functions with the browser session cookie. The user pastes
the Cookie header (the `auth` / `__Host-auth` cookie from opencode.ai) in
settings; the workspace ID (wrk_…) is discovered automatically or set there.
"""

import json
import re
import uuid

import requests

from .base import ProviderSnapshot, RateWindow, parse_unix

BASE_URL = "https://opencode.ai"
SERVER_URL = BASE_URL + "/_server"
# SolidStart server-function IDs, pinned from CodexBar (may change on site redeploys)
WORKSPACES_FN = "def39973159c7f0483d8793a822b8dbb10d067e12c65455fcb4608459ba0234f"
SUBSCRIPTION_FN = "7abeebee372f304e050aaaf92be863f4a86490e382f8c79db68fd94040d691b4"

ALLOWED_COOKIES = ("auth", "__Host-auth")


def _cookie_header(raw: str) -> str | None:
    pairs = []
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name in ALLOWED_COOKIES and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs) or None


def _server_get(fn_id: str, args: list | None, cookie: str, referer: str) -> str:
    params = {"id": fn_id}
    if args:
        params["args"] = json.dumps(args)
    resp = requests.get(
        SERVER_URL,
        params=params,
        headers={
            "Cookie": cookie,
            "X-Server-Id": fn_id,
            "X-Server-Instance": f"server-fn:{uuid.uuid4()}",
            "Origin": BASE_URL,
            "Referer": referer,
            "Accept": "text/javascript, application/json;q=0.9, */*;q=0.8",
            "User-Agent": "AIBar",
        },
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise PermissionError("Сессия opencode.ai недействительна — обновите cookie в настройках")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.text


def _find_usage(obj) -> dict | None:
    """Recursively look for rollingUsage/weeklyUsage blocks in a JSON payload."""
    if isinstance(obj, dict):
        if "rollingUsage" in obj or "weeklyUsage" in obj:
            return obj
        for value in obj.values():
            found = _find_usage(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_usage(value)
            if found:
                return found
    return None


def _percent_and_reset(block: dict) -> tuple[float | None, int | None]:
    percent = block.get("usagePercent") or block.get("usage_percent")
    reset = block.get("resetInSec") or block.get("reset_in_sec")
    return (
        float(percent) if percent is not None else None,
        int(reset) if reset is not None else None,
    )


def _extract(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    return float(match.group(1)) if match else None


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="OpenCode")
    cookie = _cookie_header(cfg.get("opencode_cookie") or "")
    if not cookie:
        snap.error = "Вставьте cookie `auth` с opencode.ai в настройках"
        return snap

    try:
        workspace = (cfg.get("opencode_workspace") or "").strip()
        if not workspace:
            text = _server_get(WORKSPACES_FN, None, cookie, BASE_URL)
            match = re.search(r"wrk_[A-Za-z0-9]+", text)
            if not match:
                snap.error = "Не удалось определить workspace — укажите wrk_… в настройках"
                return snap
            workspace = match.group(0)

        referer = f"{BASE_URL}/workspace/{workspace}/billing"
        text = _server_get(SUBSCRIPTION_FN, [workspace], cookie, referer)
    except (PermissionError, RuntimeError) as exc:
        snap.error = str(exc)
        return snap
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    import time

    now = time.time()
    rolling = weekly = None
    try:
        payload = json.loads(text)
        usage = _find_usage(payload)
        if usage:
            rolling = _percent_and_reset(usage.get("rollingUsage") or {})
            weekly = _percent_and_reset(usage.get("weeklyUsage") or {})
    except json.JSONDecodeError:
        pass

    if not rolling or rolling[0] is None:
        rolling = (
            _extract(r"rollingUsage[^}]*?usagePercent\s*:\s*([0-9.]+)", text),
            _extract(r"rollingUsage[^}]*?resetInSec\s*:\s*([0-9]+)", text),
        )
        weekly = (
            _extract(r"weeklyUsage[^}]*?usagePercent\s*:\s*([0-9.]+)", text),
            _extract(r"weeklyUsage[^}]*?resetInSec\s*:\s*([0-9]+)", text),
        )

    for (percent, reset), label in ((rolling, "Сессия"), (weekly, "Неделя")):
        if percent is not None:
            snap.windows.append(
                RateWindow(
                    label,
                    percent,
                    resets_at=parse_unix(now + reset) if reset else None,
                )
            )

    if not snap.windows:
        snap.error = "Не удалось разобрать данные подписки opencode.ai"
    return snap
