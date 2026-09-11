from . import (
    claude,
    codex,
    codex_pool,
    copilot,
    cursor,
    gemini,
    grok,
    kimi,
    opencode,
    openai_api,
    tavily,
    zai,
)
from .base import ProviderSnapshot, RateWindow

# Registry of available providers: name -> fetch(cfg) callable.
# Order defines display order in the dashboard and widget.
PROVIDERS = {
    "Claude": claude.fetch,
    "Codex": codex.fetch,
    "Codex Пул": codex_pool.fetch,
    "Cursor": cursor.fetch,
    "Z.ai": zai.fetch,
    "Kimi": kimi.fetch,
    "Grok": grok.fetch,
    "Copilot": copilot.fetch,
    "OpenCode": opencode.fetch,
    "Google": gemini.fetch,
    "OpenAI API": openai_api.fetch,
    "Tavily": tavily.fetch,
}

# Short hints shown in the settings dialog
PROVIDER_HINTS = {
    "Claude": "токен Claude Code (~/.claude)",
    "Codex": "токен codex CLI (~/.codex)",
    "Codex Пул": "пул аккаунтов opencodex (~/.opencodex)",
    "Cursor": "сессия приложения Cursor",
    "Z.ai": "API-ключ coding-плана (zcode)",
    "Kimi": "API-ключ coding-плана (api.kimi.com)",
    "Grok": "токен grok CLI (~/.grok/auth.json)",
    "Copilot": "токен GitHub (gh auth login)",
    "OpenCode": "cookie с opencode.ai",
    "Google": "квоты Gemini (вход gemini CLI, ~/.gemini)",
    "OpenAI API": "Admin-ключ: расход и остаток",
    "Tavily": "API-ключ tvly-…",
}

__all__ = ["PROVIDERS", "PROVIDER_HINTS", "ProviderSnapshot", "RateWindow"]
