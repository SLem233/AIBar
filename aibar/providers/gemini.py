"""Google (Gemini) quota provider.

Reads Gemini CLI OAuth credentials from ~/.gemini/oauth_creds.json and polls
the Code Assist quota endpoint (cloudcode-pa.googleapis.com), the same flow
CodexBar uses. Shows per-model quota utilization (Pro / Flash, 24h windows).

Google does not rotate OAuth refresh tokens, so an expired access token is
refreshed in memory only — the CLI's stored credentials stay untouched.
AI Studio prepaid balance/spend has no public API and is not shown.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from .base import ProviderSnapshot, RateWindow, decode_jwt_payload, parse_iso8601

CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
LOAD_CA_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
PROJECTS_URL = "https://cloudresourcemanager.googleapis.com/v1/projects"

# The OAuth client id/secret for token refresh are read from the installed
# gemini-cli itself (they live in its oauth2.js); env vars override like in
# CodexBar. They are deliberately not shipped in this repo.
_OAUTH2_JS_CANDIDATES = [
    Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@google/gemini-cli/node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js",
    Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js",
    Path(os.environ.get("ProgramFiles", "")) / "nodejs/node_modules/@google/gemini-cli/node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js",
]


def _client_credentials() -> tuple[str, str]:
    env_id = os.environ.get("GEMINI_OAUTH_CLIENT_ID", "").strip()
    env_secret = os.environ.get("GEMINI_OAUTH_CLIENT_SECRET", "").strip()
    if env_id and env_secret:
        return env_id, env_secret
    for path in _OAUTH2_JS_CANDIDATES:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        cid = re.search(r"OAUTH_CLIENT_ID\s*=\s*['\"]([^'\"]+)", text)
        secret = re.search(r"OAUTH_CLIENT_SECRET\s*=\s*['\"]([^'\"]+)", text)
        if cid and secret:
            return cid.group(1), secret.group(1)
    raise RuntimeError(
        "Не найден установленный gemini-cli (нужен для обновления токена) — "
        "установите его или задайте GEMINI_OAUTH_CLIENT_ID/SECRET"
    )


def _access_token(creds: dict) -> str:
    token = creds.get("access_token") or ""
    expiry_ms = creds.get("expiry_date") or 0
    if token and expiry_ms / 1000 > datetime.now(timezone.utc).timestamp() + 60:
        return token
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Нет refresh_token — выполните вход в gemini CLI")
    client_id, client_secret = _client_credentials()
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError("Не удалось обновить токен — запустите gemini для входа")
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Google не вернул access_token")
    return token


def _model_label(model_id: str) -> str:
    name = model_id.replace("models/", "")
    name = re.sub(r"^gemini-", "", name)
    return name[:1].upper() + name[1:]


def fetch(cfg: dict | None = None) -> ProviderSnapshot:
    snap = ProviderSnapshot(provider="Google")
    try:
        creds = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        snap.error = "Нет ~/.gemini/oauth_creds.json — выполните вход в gemini CLI"
        return snap
    except json.JSONDecodeError as exc:
        snap.error = f"oauth_creds.json повреждён: {exc}"
        return snap

    try:
        token = _access_token(creds)
    except (RuntimeError, requests.RequestException) as exc:
        snap.error = str(exc)
        return snap

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    project_id = None
    plan = ""
    try:
        resp = requests.post(
            LOAD_CA_URL,
            headers=headers,
            json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
            timeout=30,
        )
        if resp.status_code == 200:
            ca = resp.json()
            project_id = ca.get("cloudaicompanionProject")
            tier = ca.get("currentTier") or {}
            plan = tier.get("name") or tier.get("id") or ""
    except requests.RequestException:
        pass

    if not project_id:
        try:
            resp = requests.get(PROJECTS_URL, headers=headers, timeout=30)
            if resp.status_code == 200:
                for project in resp.json().get("projects") or []:
                    pid = project.get("projectId") or ""
                    labels = project.get("labels") or {}
                    if pid.startswith("gen-lang-client") or "generative-language" in labels:
                        project_id = pid
                        break
        except requests.RequestException:
            pass

    try:
        resp = requests.post(
            QUOTA_URL,
            headers=headers,
            json={"project": project_id} if project_id else {},
            timeout=30,
        )
    except requests.RequestException as exc:
        snap.error = f"Сетевая ошибка: {exc}"
        return snap

    if resp.status_code == 401:
        snap.error = "401 — запустите gemini CLI для повторного входа"
        return snap
    if resp.status_code == 429:
        # The endpoint serves only Code Assist-onboarded accounts
        snap.error = (
            "Аккаунт не активирован в Gemini CLI — запустите gemini, "
            "выполните вход, и квоты появятся"
        )
        return snap
    if resp.status_code != 200:
        snap.error = f"HTTP {resp.status_code}"
        return snap

    snap.plan = plan.replace("_", " ").strip()

    # Per model keep the most-consumed bucket (input tokens usually)
    per_model: dict[str, tuple[float, str | None]] = {}
    for bucket in resp.json().get("buckets") or []:
        model_id = bucket.get("modelId")
        fraction = bucket.get("remainingFraction")
        if not model_id or fraction is None:
            continue
        used = max(0.0, min(100.0, (1 - float(fraction)) * 100))
        if model_id not in per_model or used > per_model[model_id][0]:
            per_model[model_id] = (used, bucket.get("resetTime"))

    for model_id, (used, reset) in sorted(
        per_model.items(), key=lambda kv: kv[1][0], reverse=True
    ):
        snap.windows.append(
            RateWindow(_model_label(model_id), used, resets_at=parse_iso8601(reset))
        )

    email = decode_jwt_payload(creds.get("id_token") or "").get("email")
    if email:
        snap.extra["Аккаунт"] = email

    if not snap.windows:
        snap.error = "API не вернул квот по моделям"
    return snap
