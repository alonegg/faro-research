"""Provider factory + exports."""

from __future__ import annotations

from faro_research.config import settings
from faro_research.providers.anthropic import AnthropicProvider
from faro_research.providers.base import ChatResponse, Message, Provider, Role
from faro_research.providers.openai_compat import OpenAICompatibleProvider


def make_provider(name: str | None = None) -> Provider:
    """Build a Provider from settings (or override `name`).

    Recognised names: 'openai_compat', 'anthropic'.
    """
    p = (name or settings.provider).strip().lower()
    if p in ("openai_compat", "openai", "deepseek", "moonshot", "ark", "ollama"):
        return OpenAICompatibleProvider(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            timeout=settings.llm_timeout_sec,
        )
    if p in ("anthropic", "claude"):
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout=settings.llm_timeout_sec,
        )
    raise ValueError(f"unknown provider: {p!r} (try 'openai_compat' or 'anthropic')")


__all__ = [
    "make_provider", "Provider", "Message", "ChatResponse", "Role",
    "OpenAICompatibleProvider", "AnthropicProvider",
]
