"""GitHub Copilot usage provider (личный план).

Публичный REST API GitHub отдаёт расход Copilot только для организаций, поэтому
читаем тот же внутренний эндпоинт, что и расширение Copilot Chat в VS Code:
GET https://api.github.com/copilot_internal/user. Эндпоинт недокументирован —
он может измениться без предупреждения, и тогда провайдер честно покажет ошибку.

Ответ (живая проба):
    {
      "login": "octocat",
      "access_type_sku": "free_limited_copilot",
      "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
      "quota_snapshots": {
        "chat":                 {entitlement, remaining, unlimited, …},
        "completions":          {…},
        "premium_interactions": {…}
      }
    }
Процент считаем как (entitlement − remaining) / entitlement.

Токен ищется по порядку: `copilot_token` из настроек → GITHUB_TOKEN/GH_TOKEN →
`gh auth token` (у gh CLI он лежит в хранилище учётных данных ОС, файла нет).
Сам gh запускается только по абсолютному пути из PATH: на Windows поиск
исполняемого файла начинается с папки запущенной программы, и подложенный рядом
с AIBar.exe gh.exe иначе получил бы наш токен.
"""

import os
import shutil
import subprocess

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    format_date,
    looks_like_api_key,
    parse_iso8601,
    subscription_renewal,
)

USAGE_URL = "https://api.github.com/copilot_internal/user"
API_VERSION = "2025-04-01"
EDITOR_VERSION = "vscode/1.95.0"
EDITOR_PLUGIN_VERSION = "copilot-chat/0.26.7"
USER_AGENT = "GitHubCopilotChat/0.26.7"
TIMEOUT = 30
GH_TIMEOUT = 8
# Запуск gh не должен мигать окном консоли: AIBar — оконное приложение.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SNAPSHOT_LABELS = {
    "chat": "Chat",
    "completions": "Completions",
    "premium_interactions": "Premium",
}

PLAN_SKUS = {
    "free_limited_copilot": "Free",
    "copilot_business": "Business",
    "copilot_enterprise": "Enterprise",
    "copilot_pro": "Pro",
    "copilot_plus": "Plus",
}


# ---- токен ----------------------------------------------------------------
def _token_from_gh_cli() -> str | None:
    """`gh auth token`, если gh установлен и вход выполнен."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        done = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or "").strip() or None


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
        "editor-version": EDITOR_VERSION,
        "editor-plugin-version": EDITOR_PLUGIN_VERSION,
        "user-agent": USER_AGENT,
        "x-github-api-version": API_VERSION,
    }


# ---- окна квот ------------------------------------------------------------
def _snapshot_window(name: str, snapshot: dict, resets_at) -> RateWindow | None:
    """Одна квота → окно; None, если квота не выдана (entitlement = 0)."""
    if not isinstance(snapshot, dict):
        return None
    label = SNAPSHOT_LABELS.get(name, name)
    if snapshot.get("unlimited"):
        # Безлимит: кольцо держим на нуле — тратить не из чего.
        return RateWindow(f"{label} ∞", 0.0, resets_at=resets_at)
    try:
        entitlement = float(snapshot.get("entitlement") or 0)
        remaining = float(snapshot.get("remaining") or 0)
    except (TypeError, ValueError):
        return None
    if entitlement <= 0:
        return None
    percent = max(0.0, min(100.0, (entitlement - remaining) / entitlement * 100))
    return RateWindow(label, percent, resets_at=resets_at)


# ---- provider -------------------------------------------------------------
def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Copilot")
    token = _api_token(cfg)
    if not token:
        snap.error = (
            "Нет токена GitHub — выполните gh auth login или укажите токен "
            "в настройках"
        )
        return snap
    if not looks_like_api_key(token):
        snap.error = "В поле токена вставлен не токен — вставьте токен GitHub заново"
        return snap

    try:
        resp = requests.get(USAGE_URL, headers=_headers(token), timeout=TIMEOUT)
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code != 200:
        snap.http_status = resp.status_code
        if resp.status_code == 401:
            snap.error = "Токен GitHub не авторизован (нужен gh auth login)"
        elif resp.status_code == 404:
            # Аккаунты без доступа к Copilot отвечают 404 на внутренний эндпоинт.
            snap.error = "Copilot не подключён к аккаунту GitHub"
        else:
            snap.error = f"HTTP {resp.status_code}"
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
    if body.get("login"):
        snap.extra["Аккаунт"] = body["login"]

    reset = parse_iso8601(body.get("quota_reset_date_utc") or body.get("quota_reset_date"))
    snapshots = body.get("quota_snapshots") or {}
    for key in ("chat", "completions", "premium_interactions"):
        window = _snapshot_window(key, snapshots.get(key) or {}, reset)
        if window is not None:
            snap.windows.append(window)
    if reset:
        snap.extra["Сброс"] = format_date(reset)

    renewal = subscription_renewal(cfg, "copilot")
    if renewal:
        snap.extra["Продление"] = renewal
    if not snap.windows:
        snap.error = "GitHub не вернул окон квот Copilot"
    return snap
