"""User settings stored in %APPDATA%/AIBar/config.json."""

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "AIBar"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "refresh_seconds": 300,
    "providers": ["Claude", "Codex"],
    "widget_enabled": True,
    "widget_geometry": None,  # [x, y, w, h]
    # ряд вместо колонки: виджет разворачивается сам у верхнего и нижнего края
    "widget_horizontal": False,
    "widget_mode": "full",  # full | mini (mini: only providers near the limit)
    # сворачивать виджет за край экрана, когда он к этому краю прижат
    "widget_autohide": False,
    "mini_threshold": 70,  # percent that qualifies a provider for mini mode
    "zai_api_key": "",
    "zai_region": "global",  # global | bigmodel-cn
    "opencode_cookie": "",
    "opencode_workspace": "",
    "openai_admin_key": "",
    "openai_budget_usd": 0,  # 0 = ring off, show spend only
    # Balance anchor: the dashboard balance on a given date; the app subtracts
    # Costs-API spend since that date (OpenAI exposes no balance endpoint)
    "openai_balance_usd": 0,
    "openai_balance_date": "",
    "tavily_api_key": "",
    "kimi_api_key": "",
    # Токен GitHub для Copilot: пусто = берём из GITHUB_TOKEN/GH_TOKEN или gh CLI
    "copilot_token": "",
    # Менять протухший токен самим (нужно тем, кто не запускает CLI).
    # Выключено — файл входа только читается: ~/.claude/.credentials.json,
    # ~/.grok/auth.json.
    "claude_auto_refresh": True,
    "grok_auto_refresh": True,
    # Renewal date (dd.mm.yyyy) + cycle for providers whose APIs expose no
    # billing anchor; empty date = don't show. Past dates roll forward.
    "claude_renewal_date": "",
    "claude_renewal_period": "month",  # month | quarter | year
    "cursor_renewal_date": "",
    "cursor_renewal_period": "year",
    "zai_renewal_date": "",
    "zai_renewal_period": "month",
    "tavily_renewal_date": "",
    "tavily_renewal_period": "month",
    "kimi_renewal_date": "",
    "kimi_renewal_period": "month",
    "copilot_renewal_date": "",
    "copilot_renewal_period": "month",
    "grok_renewal_date": "",
    "grok_renewal_period": "month",
}


def _migrate(data: dict) -> dict:
    """Convert legacy <p>_billing_day (day of month) to <p>_renewal_date."""
    from datetime import date, timedelta

    for prefix in ("claude", "zai", "tavily"):
        day = data.pop(f"{prefix}_billing_day", 0) or 0
        if 1 <= int(day) <= 31 and not data.get(f"{prefix}_renewal_date"):
            candidate = date.today()
            while candidate.day != int(day):  # next occurrence of that day
                candidate += timedelta(days=1)
            data[f"{prefix}_renewal_date"] = candidate.strftime("%d.%m.%Y")

    # One-time: users tracking Codex with an opencodex account pool get the
    # pool provider enabled next to Codex (their extra accounts would be
    # invisible otherwise). The marker keeps this from re-adding the provider
    # after the user deliberately removes it in settings.
    if "codex_pool_seen" not in data:
        data["codex_pool_seen"] = True
        providers = data.get("providers") or (["Claude", "Codex"] if "providers" not in data else [])
        if (
            "Codex" in providers
            and "Codex Пул" not in providers
            and (Path.home() / ".opencodex" / "codex-accounts.json").exists()
        ):
            providers = list(providers)
            providers.insert(providers.index("Codex") + 1, "Codex Пул")
            data["providers"] = providers
    return data


def load() -> dict:
    try:
        # utf-8-sig: tolerate a BOM if the file was edited by external tools
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)
    return {**DEFAULTS, **_migrate(data)}


def save(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
