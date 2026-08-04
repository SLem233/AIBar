from . import claude, codex, copilot, cursor, gemini, grok, kimi, opencode, openai_api, tavily, zai
from .base import ProviderSnapshot, RateWindow

# Registry of available providers: name -> fetch(cfg) callable.
# Order defines display order in the dashboard and widget.
PROVIDERS = {
    "Claude": claude.fetch,
    "Codex": codex.fetch,
    "Cursor": cursor.fetch,
    "Z.ai": zai.fetch,
    "Grok": grok.fetch,
    "Kimi": kimi.fetch,
    "Copilot": copilot.fetch,
    "OpenCode": opencode.fetch,
    "Google": gemini.fetch,
    "OpenAI API": openai_api.fetch,
    "Tavily": tavily.fetch,
}

# Short hints shown in the settings dialog
PROVIDER_HINTS = {
    "Claude": "токен Claude Code/Desktop (~/.claude, авто-refresh)",
    "Codex": "токен codex CLI (~/.codex)",
    "Cursor": "сессия приложения Cursor",
    "Z.ai": "API-ключ coding-плана (zcode)",
    "Grok": "OAuth grok CLI (~/.grok): списание подписки (месячный prepaid)",
    "Kimi": "coding-plan API-ключ (api.kimi.com)",
    "Copilot": "токен GitHub (gh auth login или ghp_…)",
    "OpenCode": "cookie с opencode.ai",
    "Google": "квоты Gemini (вход gemini CLI, ~/.gemini)",
    "OpenAI API": "Admin-ключ: расход и остаток",
    "Tavily": "API-ключ tvly-…",
}

__all__ = ["PROVIDERS", "PROVIDER_HINTS", "ProviderSnapshot", "RateWindow"]
