"""Claude usage provider.

Reads the OAuth token from ~/.claude/.credentials.json and polls the same
endpoint CodexBar uses: GET https://api.anthropic.com/api/oauth/usage.

Протухший access-токен модуль меняет сам: POST /v1/oauth/token по
refresh-токену из того же файла, ротированный токен пишется обратно. Благодаря
этому лимиты видны и тем, кто пользуется только Claude Desktop и не запускает
CLI. Обмен можно выключить настройкой `claude_auto_refresh` — тогда модуль
только читает файл, как раньше.

Refresh-токены Anthropic одноразовые: успешный обмен аннулирует предыдущий,
поэтому новый токен сохраняется сразу, а на invalid_grant файл перечитывается —
соседняя сессия (CLI, второй экземпляр AIBar) могла обновить его первой.
"""

import json
import time
from pathlib import Path

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
USER_AGENT = "claude-code/2.1.0"
# Публичный client_id, с которым Claude Code проходит вход.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TIMEOUT = 30
# Обновляем токен заранее: за минуту до истечения он уже считается протухшим.
EXPIRY_MARGIN_MS = 60_000

RELOGIN_HINT = "обновите вход: запустите claude или Claude Desktop и залогиньтесь"

# (json key, human label) in display order; first two feed the tray gauge
WINDOW_KEYS = [
    ("five_hour", "Сессия (5ч)"),
    ("seven_day", "Неделя"),
    ("seven_day_opus", "Opus (нед.)"),
    ("seven_day_sonnet", "Sonnet (нед.)"),
]


# ---- файл с токеном -------------------------------------------------------
def _cred_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _load_oauth() -> tuple[Path, dict]:
    """(путь, блок claudeAiOauth) — путь нужен, чтобы записать новый токен."""
    cred = _cred_path()
    data = json.loads(cred.read_text(encoding="utf-8"))
    return cred, data.get("claudeAiOauth") or {}


def _write_back(cred: Path, oauth: dict) -> None:
    """Сохранить блок claudeAiOauth, не тронув остальные ключи файла.

    Пишем во временный файл рядом и подменяем одним движением: файл читают
    и другие клиенты, оборванная запись оставила бы их без входа.
    """
    full = json.loads(cred.read_text(encoding="utf-8"))
    full["claudeAiOauth"] = oauth
    tmp = cred.with_name(cred.name + ".aibar.tmp")
    tmp.write_text(json.dumps(full, indent=2), encoding="utf-8")
    tmp.replace(cred)


def _token_expired(oauth: dict) -> bool:
    exp = oauth.get("expiresAt")
    if not isinstance(exp, (int, float)):
        return False  # без отметки времени верим токену — решит API
    return int(time.time() * 1000) >= exp - EXPIRY_MARGIN_MS


def _token_valid(oauth: dict) -> bool:
    return bool(oauth.get("accessToken")) and not _token_expired(oauth)


def _do_refresh(cred: Path, oauth: dict) -> str:
    """Один обмен refresh-токена с немедленной записью результата."""
    rt = oauth.get("refreshToken")
    if not rt:
        raise RuntimeError(f"нет refreshToken — {RELOGIN_HINT}")
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
        # Тело ответа в текст ошибки не тащим: оно уходит в UI. Наружу — только
        # код и признак «токен отозван», по нему решаем, что делать дальше.
        marker = " (invalid_grant)" if "invalid_grant" in (resp.text or "") else ""
        raise RuntimeError(f"обновление токена: HTTP {resp.status_code}{marker}")
    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError("обновление токена: невалидный JSON в ответе") from None
    if not body.get("access_token"):
        raise RuntimeError("обновление токена: в ответе нет access_token")

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


def _refresh(cred: Path, oauth: dict) -> str:
    """Обмен с оглядкой на соседние сессии, работающие с тем же файлом."""
    used_rt = oauth.get("refreshToken")
    try:
        return _do_refresh(cred, oauth)
    except RuntimeError as exc:
        if "invalid_grant" not in str(exc):
            raise
        # Токен уже кем-то ротирован — берём то, что лежит в файле сейчас.
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
        raise RuntimeError(f"refresh-токен истёк или отозван — {RELOGIN_HINT}") from None


# ---- запросы --------------------------------------------------------------
def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": USER_AGENT,
    }


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    snap = ProviderSnapshot(provider="Claude")
    try:
        cred, oauth = _load_oauth()
    except FileNotFoundError:
        snap.error = "Файл ~/.claude/.credentials.json не найден"
        return snap
    except (OSError, json.JSONDecodeError) as exc:
        snap.error = str(exc)
        return snap

    token = oauth.get("accessToken")
    if not token:
        snap.error = "В .credentials.json нет accessToken — выполните вход в claude"
        return snap

    snap.plan = (oauth.get("subscriptionType") or "").capitalize()
    auto_refresh = bool((cfg or {}).get("claude_auto_refresh", True))

    # Claude Desktop не обновляет токен сам — делаем это за него.
    if auto_refresh and _token_expired(oauth):
        try:
            token = _refresh(cred, oauth)
        except (RuntimeError, OSError, requests.RequestException) as exc:
            snap.error = str(exc)
            return snap

    headers = _headers(token)
    try:
        resp = requests.get(USAGE_URL, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code == 401 and auto_refresh:
        # Файл считал токен живым, а API — нет: один обмен и одна повторная
        # попытка. 403 сюда не попадает — это гео-блок, его разбирает poll_all.
        try:
            token = _refresh(cred, oauth)
            resp = requests.get(USAGE_URL, headers=_headers(token), timeout=TIMEOUT)
            headers = _headers(token)
        except (RuntimeError, OSError, requests.RequestException) as exc:
            snap.error = str(exc)
            return snap

    if resp.status_code != 200:
        snap.http_status = resp.status_code
    if resp.status_code == 401:
        # Claude Code обновляет токен лениво — на первом реальном запросе.
        snap.error = (
            "Токен истёк — запустите claude и отправьте любой запрос, "
            "токен обновится сам"
        )
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        return snap

    try:
        data = resp.json()
    except ValueError:
        snap.error = "Невалидный JSON в ответе Anthropic"
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

    _apply_profile(snap, headers)

    # The API exposes no renewal date (subscription_created_at is not the
    # billing anchor after plan changes), so it comes from settings.
    renewal = subscription_renewal(cfg, "claude")
    if renewal:
        snap.extra["Продление"] = renewal
    return snap


def _apply_profile(snap: ProviderSnapshot, headers: dict) -> None:
    """Exact plan tier (e.g. Max 5x) and subscription status from the profile."""
    try:
        resp = requests.get(PROFILE_URL, headers=dict(headers), timeout=15)
        if resp.status_code != 200:
            return
        org = resp.json().get("organization") or {}
    except (requests.RequestException, ValueError, AttributeError):
        return

    tier = org.get("rate_limit_tier") or ""  # e.g. default_claude_max_5x
    if "max_" in tier:
        snap.plan = "Max " + tier.rsplit("_", 1)[-1]
    if org.get("subscription_status") and org["subscription_status"] != "active":
        snap.extra["Статус подписки"] = org["subscription_status"]
