"""Provider abstraction — normalised chat message + tool-calling protocol.

The agent loop only sees the types defined here. Provider implementations
(OpenAI-compatible, Anthropic, ...) translate to/from native API formats.

Why a custom abstraction instead of langchain:
  - One file to read; no dependency tree
  - Tool-calling format is the only thing we actually need
  - Easy to add new providers (Bedrock, Mistral, local llama.cpp)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from faro_research.tools.types import ToolCall, ToolSpec

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class Message:
    """Provider-agnostic chat message used by the agent loop."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None     # assistant only
    tool_call_id: str | None = None              # tool only — links to a prior tool_call.id
    name: str | None = None                       # tool only — tool name, optional
    # Free-form bag for provider-specific echo (e.g. DeepSeek's reasoning_content
    # which the API requires you to pass back unchanged on the next turn).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    """Normalised single-turn LLM response."""

    content: str | None              # may be None if pure tool call
    tool_calls: list[ToolCall]       # empty list when none requested
    finish_reason: str               # "stop" | "tool_calls" | "length" | ...
    extra: dict[str, Any] = field(default_factory=dict)   # for re-echo


class Provider(ABC):
    """Implementations: OpenAICompatibleProvider, AnthropicProvider, ..."""

    name: str

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        """One-shot chat completion. Synchronous (blocking)."""
        ...
