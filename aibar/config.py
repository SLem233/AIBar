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
    "zai_api_key": "",
    "zai_region": "global",  # global | bigmodel-cn
    "opencode_cookie": "",
    "opencode_workspace": "",
}


def load() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)
    return {**DEFAULTS, **data}


def save(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
