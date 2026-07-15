"""Cursor usage provider.

Reads the Cursor app access token from its local VS Code-style state DB
(%APPDATA%/Cursor/User/globalStorage/state.vscdb, key cursorAuth/accessToken),
builds the WorkosCursorSessionToken cookie and polls
GET https://cursor.com/api/usage-summary — the same flow CodexBar uses.
"""

import os
import sqlite3
from pathlib import Path

import requests

from .base import ProviderSnapshot, RateWindow, decode_jwt_payload, parse_iso8601

USAGE_SUMMARY_URL = "https://cursor.com/api/usage-summary"

DB_PATH = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    / "Cursor/User/globalStorage/state.vscdb"
)


def _access_token() -> str:
    # immutable=1: read without locking the DB while Cursor is running
    uri = f"file:{DB_PATH.as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken' LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise RuntimeError("В Cursor нет сохранённого токена — войдите в аккаунт в приложении")
    token = row[0]
    if isinstance(token, bytes):
        token = token.decode("utf-8", errors="replace")
    return token.strip().strip('"')


def _cookie_header(token: str) -> str:
    sub = decode_jwt_payload(token).get("sub") or ""
    user_id = sub.split("|")[-1]
    if not user_id:
        raise RuntimeError("Не удалось извлечь ID пользователя из токена Cursor")
    return f"WorkosCursorSessionToken={user_id}%3A%3A{token}"


def _usd(cents: int | float | None) -> str:
    return f"${(cents or 0) / 100:.2f}"


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    snap = ProviderSnapshot(provider="Cursor")
    if not DB_PATH.exists():
        snap.error = "Cursor не установлен (нет state.vscdb)"
        return snap
    try:
        cookie = _cookie_header(_access_token())
    except (RuntimeError, sqlite3.Error) as exc:
        snap.error = str(exc)
        return snap

    try:
        resp = requests.get(
            USAGE_SUMMARY_URL,
            headers={"Accept": "application/json", "Cookie": cookie},
            timeout=30,
        )
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code in (401, 403):
        snap.error = "Сессия Cursor истекла — войдите в аккаунт в приложении"
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        return snap

    data = resp.json()
    snap.plan = (data.get("membershipType") or "").replace("_", " ").title()
    resets_at = parse_iso8601(data.get("billingCycleEnd"))

    individual = data.get("individualUsage") or {}
    plan = individual.get("plan") or {}
    overall = individual.get("overall") or {}
    pooled = (data.get("teamUsage") or {}).get("pooled") or {}

    percent = plan.get("totalPercentUsed")
    used, limit = plan.get("used"), plan.get("limit")
    if percent is None and limit:
        percent = (used or 0) / limit * 100
    if percent is None and overall.get("limit"):
        used, limit = overall.get("used"), overall.get("limit")
        percent = (used or 0) / limit * 100
    if percent is None and pooled.get("limit"):
        used, limit = pooled.get("used"), pooled.get("limit")
        percent = (used or 0) / limit * 100

    if percent is not None:
        snap.windows.append(
            RateWindow("Тариф (месяц)", float(percent), resets_at=resets_at)
        )
    for key, label in (("autoPercentUsed", "Auto"), ("apiPercentUsed", "API")):
        value = plan.get(key)
        if value is not None:
            snap.windows.append(RateWindow(label, float(value), resets_at=resets_at))

    if limit:
        snap.extra["Включено в тариф"] = f"{_usd(used)} / {_usd(limit)}"
    on_demand = individual.get("onDemand") or {}
    if on_demand.get("enabled") and (on_demand.get("used") or on_demand.get("limit")):
        limit_text = _usd(on_demand["limit"]) if on_demand.get("limit") else "∞"
        snap.extra["On-demand"] = f"{_usd(on_demand.get('used'))} / {limit_text}"

    if not snap.windows:
        snap.error = "API не вернул данных об использовании"
    return snap
