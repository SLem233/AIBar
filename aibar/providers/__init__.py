from . import claude, codex, cursor, opencode, zai
from .base import ProviderSnapshot, RateWindow

# Registry of available providers: name -> fetch(cfg) callable.
# Order defines display order in the dashboard and widget.
PROVIDERS = {
    "Claude": claude.fetch,
    "Codex": codex.fetch,
    "Cursor": cursor.fetch,
    "Z.ai": zai.fetch,
    "OpenCode": opencode.fetch,
}

# Short hints shown in the settings dialog
PROVIDER_HINTS = {
    "Claude": "токен Claude Code (~/.claude)",
    "Codex": "токен codex CLI (~/.codex)",
    "Cursor": "сессия приложения Cursor",
    "Z.ai": "API-ключ coding-плана (zcode)",
    "OpenCode": "cookie с opencode.ai",
}

__all__ = ["PROVIDERS", "PROVIDER_HINTS", "ProviderSnapshot", "RateWindow"]
