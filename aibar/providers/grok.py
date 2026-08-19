"""Grok (xAI) usage provider — окно расхода подписки.

Читается `GET https://cli-chat-proxy.grok.com/v1/billing`. Ответ разбирается в
одной из двух схем:

* **денежная**: `monthlyLimit` и `used` в центах за расчётный период. Это то,
  что ручка отдаёт сейчас.
* **unified billing**: `creditUsagePercent` — процент израсходованного пула — и
  `currentPeriod` с типом окна и границами. Это и есть «Weekly limit: N%» из
  TUI.

Схему различаем по наличию `creditUsagePercent`, а не по `isUnifiedBillingUser`:
процент — это то, что рисуется, и решать должно наличие самих данных.

Оговорка, стоившая разведки 17.08.2026. Образец unified-ответа снят не с нашего
запроса, а с лога самого CLI (`~/.grok/logs/unified.jsonl`, строка «billing:
fetched credits config»). По HTTP этих полей не отдают: на аккаунте с активной
подпиской SuperGrok `/v1/billing` возвращает денежную схему с нулевым лимитом и
нулевой историей. Перебраны `/v1` и `/v2` на cli-chat-proxy, `api.x.ai/v1`,
`grok.com/rest`, полтора десятка путей, POST вместо GET, заголовок и путь от
лица команды — везде 404, живые только `/v1/billing` и `/v1/user`. Версия
клиента в заголовках роли не играет: ответы с `grok-cli/0.2.118` и
`grok-cli/1.0.4` побайтово совпадают.

Отсюда рабочая версия: unified-данные CLI получает по WebSocket-реле
(`auth/check_subscription`), которое чужому bearer отвечает «3000
Unauthorized», — в логе строка идёт без URL, а конверт ответа отличается от
HTTP-шного. Поэтому разбор unified оставлен как есть: схема настоящая, снята с
живого ответа xAI, и включится сама, если ручку однажды откроют. Но пока карточка
на таком аккаунте честно показывает ошибку, а не выдуманный процент.

Чат-эндпоинт не пробуем: заголовков `x-ratelimit-*` у него нет, проба всегда
возвращала 0%.

Токен берётся из `~/.grok/auth.json` (вход через grok CLI) и обновляется по
OIDC, поэтому запускать CLI ради свежего токена не нужно. Обмен выключается
настройкой `grok_auto_refresh`: refresh-токен одноразовый, и его ротация
разлогинивает параллельную сессию CLI.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .base import (
    ProviderSnapshot,
    RateWindow,
    parse_iso8601,
    subscription_renewal,
)

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
OIDC_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
FALLBACK_TOKEN_ENDPOINT = "https://auth.x.ai/oauth/token"
# Публичный client_id grok CLI.
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
# Версия клиента, с которой реле принимает bearer (как у зашитого бинаря CLI).
CLI_VERSION = "0.2.118"
TIMEOUT = 30
AUTH_KEY_PREFIX = "https://auth.x.ai::"
EXPIRY_MARGIN_S = 120

LOGIN_HINT = "войдите через grok CLI"
TUI_HINT = "остаток покажет `grok` командой /usage"

# Тип окна из currentPeriod. Список неполный намеренно: неизвестный тип уводим
# в определение по длине периода, а не в «Подписка».
PERIOD_LABELS = {
    "USAGE_PERIOD_TYPE_DAILY": "Сутки",
    "USAGE_PERIOD_TYPE_WEEKLY": "Неделя",
    "USAGE_PERIOD_TYPE_MONTHLY": "Месяц",
}


# ---- файл с токеном -------------------------------------------------------
def _auth_path() -> Path:
    return Path.home() / ".grok" / "auth.json"


def _load_token() -> tuple[Path | None, dict | None]:
    """(путь, запись входа) из ~/.grok/auth.json или (None, None)."""
    path = _auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    for key, entry in data.items():
        if isinstance(key, str) and key.startswith(AUTH_KEY_PREFIX) and isinstance(entry, dict):
            return path, entry
    return None, None


def _token_expired(entry: dict) -> bool:
    expires_at = parse_iso8601(entry.get("expires_at"))
    if expires_at is None:
        return False  # без отметки времени верим токену — решит сервер
    return datetime.now(timezone.utc) >= expires_at - timedelta(seconds=EXPIRY_MARGIN_S)


def _write_back(path: Path, entry: dict) -> None:
    """Сохранить запись входа, не тронув остальные записи файла."""
    full = json.loads(path.read_text(encoding="utf-8"))
    for key in full:
        if isinstance(key, str) and key.startswith(AUTH_KEY_PREFIX):
            full[key] = entry
            break
    tmp = path.with_name(path.name + ".aibar.tmp")
    tmp.write_text(json.dumps(full, indent=2), encoding="utf-8")
    tmp.replace(path)


def _token_endpoint() -> str:
    try:
        resp = requests.get(OIDC_DISCOVERY_URL, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("token_endpoint") or FALLBACK_TOKEN_ENDPOINT
    except (requests.RequestException, ValueError):
        pass
    return FALLBACK_TOKEN_ENDPOINT


def _refresh(path: Path, entry: dict) -> str:
    """Обменять refresh-токен по OIDC и записать новый в файл."""
    refresh_token = entry.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(f"в ~/.grok/auth.json нет refresh_token — {LOGIN_HINT}")
    resp = requests.post(
        _token_endpoint(),
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        # Тело ответа в UI не показываем — только код и причину отказа.
        if "invalid_grant" in (resp.text or ""):
            raise RuntimeError(f"refresh-токен Grok истёк или отозван — {LOGIN_HINT}")
        raise RuntimeError(f"обновление токена Grok: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError("обновление токена Grok: невалидный JSON в ответе") from None
    if not body.get("access_token"):
        raise RuntimeError("обновление токена Grok: в ответе нет access_token")

    entry["key"] = body["access_token"]
    if body.get("refresh_token"):
        entry["refresh_token"] = body["refresh_token"]
    if body.get("expires_in"):
        entry["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
        ).isoformat()
    if body.get("id_token"):
        entry["id_token"] = body["id_token"]
    _write_back(path, entry)
    return entry["key"]


# ---- лог CLI --------------------------------------------------------------
def _credits_log_path() -> Path:
    """unified.jsonl, куда grok CLI пишет «billing: fetched credits config»."""
    return Path.home() / ".grok" / "logs" / "unified.jsonl"


def _latest_credits_from_log(path: Path | None = None) -> dict | None:
    """Последний ctx биллинга из лога CLI. None — лога нет или схемы в нём нет.

    HTTP `/v1/billing` на SuperGrok отдаёт нулевой лимит без процента. Сам CLI
    тот же процент получает по WebSocket и кладёт в этот лог при старте сессии.
    Читаем хвост файла: это не перехват трафика, а то, что CLI уже сохранил.
    """
    path = path or _credits_log_path()
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    chunk = raw[-262144:] if len(raw) > 262144 else raw
    last = None
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        if "fetched credits config" not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("msg") != "billing: fetched credits config":
            continue
        ctx = obj.get("ctx")
        if isinstance(ctx, dict) and isinstance(ctx.get("config"), dict):
            last = ctx
    return last


# ---- биллинг --------------------------------------------------------------
def _headers(token: str) -> dict:
    """Заголовки, с которыми реле принимает запрос (как у самого grok CLI)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"grok-cli/{CLI_VERSION}",
        "x-grok-client-version": CLI_VERSION,
        "x-grok-client-identifier": "grok-cli",
        "x-xai-token-auth": "xai-grok-cli",
    }


def _money(cents: float) -> str:
    return f"${cents / 100:.2f}"


def _period_by_length(start_iso: str | None, end_iso: str | None) -> str:
    """Название окна по длине расчётного периода."""
    start, end = parse_iso8601(start_iso), parse_iso8601(end_iso)
    if start is None or end is None:
        return "Подписка"
    days = (end - start).total_seconds() / 86400
    if 6 <= days <= 8:
        return "Неделя"
    if 27 <= days <= 33:
        return "Месяц"
    return "Подписка"


def _period_label(start_iso: str | None, end_iso: str | None) -> str:
    """То же для денежной схемы: там окно — это списание, а не квота."""
    spend_labels = {"Неделя": "Неделя (списание)", "Месяц": "Мес. (списание)"}
    return spend_labels.get(_period_by_length(start_iso, end_iso), "Подписка")


def _apply_unified(snap: ProviderSnapshot, config: dict) -> bool:
    """Схема unified billing: процент пула. False — если её признаков нет."""
    percent = config.get("creditUsagePercent")
    if percent is None:
        return False
    period = config.get("currentPeriod") or {}
    start = period.get("start") or config.get("billingPeriodStart")
    end = period.get("end") or config.get("billingPeriodEnd")
    snap.windows.append(
        RateWindow(
            PERIOD_LABELS.get(period.get("type") or "") or _period_by_length(start, end),
            max(0.0, min(100.0, float(percent))),
            resets_at=parse_iso8601(end),
        )
    )
    # Строки ниже показываем только когда за ними стоят деньги: карточка и так
    # тесная, а нули не сообщают ничего.
    cap = (config.get("onDemandCap") or {}).get("val") or 0
    if cap:
        used = (config.get("onDemandUsed") or {}).get("val") or 0
        snap.extra["PAYG"] = f"{_money(used)} / {_money(cap)}"
    prepaid = (config.get("prepaidBalance") or {}).get("val") or 0
    if prepaid:
        snap.extra["Предоплата"] = _money(prepaid)
    return True


def _apply_billing(snap: ProviderSnapshot, config: dict) -> None:
    """Блок config из /v1/billing → окно расхода. Суммы приходят в центах."""
    if _apply_unified(snap, config):
        return
    limit = (config.get("monthlyLimit") or {}).get("val")
    used = (config.get("used") or {}).get("val")
    if not limit or used is None:
        return
    period_end = config.get("billingPeriodEnd")
    snap.windows.append(
        RateWindow(
            _period_label(config.get("billingPeriodStart"), period_end),
            min(100.0, used / limit * 100),
            resets_at=parse_iso8601(period_end),
        )
    )
    snap.extra["Списание"] = f"{_money(used)} / {_money(limit)}"
    on_demand_cap = (config.get("onDemandCap") or {}).get("val")
    if on_demand_cap:
        snap.extra["PAYG"] = _money(on_demand_cap)


# ---- provider -------------------------------------------------------------
def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    cfg = cfg or {}
    snap = ProviderSnapshot(provider="Grok")
    path, entry = _load_token()
    if not entry or not entry.get("key"):
        snap.error = f"Файл ~/.grok/auth.json не найден или пуст — {LOGIN_HINT}"
        return snap

    auto_refresh = bool(cfg.get("grok_auto_refresh", True))
    token = entry["key"]
    if auto_refresh and _token_expired(entry):
        try:
            token = _refresh(path, entry)
        except (RuntimeError, OSError, requests.RequestException) as exc:
            snap.error = str(exc)
            return snap

    try:
        resp = requests.get(BILLING_URL, headers=_headers(token), timeout=TIMEOUT)
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code == 401 and auto_refresh:
        # Токен отвергнут — один обмен и одна повторная попытка. Прочие коды
        # сюда не попадают: 403 разбирает poll_all как гео-блок, а 5xx — это
        # упавшее реле, ротировать из-за него токен незачем.
        try:
            token = _refresh(path, entry)
        except (RuntimeError, OSError, requests.RequestException) as exc:
            snap.error = str(exc)
            return snap
        try:
            resp = requests.get(BILLING_URL, headers=_headers(token), timeout=TIMEOUT)
        except requests.RequestException as exc:
            snap.error = f"Сетевая ошибка: {exc}"
            return snap

    if resp.status_code != 200:
        snap.http_status = resp.status_code
        if resp.status_code == 401:
            snap.error = f"Grok не принял токен — {LOGIN_HINT}"
        elif resp.status_code >= 500:
            snap.error = f"Grok: биллинг не отвечает (HTTP {resp.status_code}), {TUI_HINT}"
        else:
            snap.error = f"HTTP {resp.status_code}"
        return snap

    try:
        body = resp.json() or {}
    except ValueError:
        snap.error = "Невалидный JSON в ответе Grok"
        return snap

    _apply_billing(snap, body.get("config") or {})
    tier = body.get("subscriptionTier")
    if tier:
        snap.plan = str(tier)
    renewal = subscription_renewal(cfg, "grok")
    if renewal:
        snap.extra["Продление"] = renewal
    if not snap.windows:
        # SuperGrok: HTTP без процента, CLI уже положил его в unified.jsonl.
        logged = _latest_credits_from_log()
        if logged:
            _apply_billing(snap, logged.get("config") or {})
            if not snap.plan and logged.get("subscriptionTier"):
                snap.plan = str(logged["subscriptionTier"])
    if not snap.windows:
        # Ни процента, ни лимита — значит формат снова поехал. Не молчим и не
        # рисуем ноль: ноль расхода и отсутствие данных выглядят одинаково.
        snap.error = f"Grok: в ответе биллинга нет ни процента, ни лимита — {TUI_HINT}"
    return snap
