"""Tool primitives — provider-agnostic.

A `ToolSpec` is the unit users register. Providers (OpenAI / Anthropic / ...)
translate it to their own native format internally. The `fn` is a regular
Python callable; arguments come from the LLM as keyword args (already JSON-parsed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool.

    name:        Unique identifier the LLM uses to call.
    description: Tells the LLM when to invoke this tool. Be specific about
                 inputs, outputs, and limits — bad descriptions cause bad
                 tool choices.
    parameters:  JSON schema dict (OpenAI/Anthropic compatible subset).
                 Top level should be `{"type": "object", "properties": {...},
                 "required": [...]}`.
    fn:          Pure Python callable that returns a dict (will be JSON-serialised
                 back to the LLM). Errors should be caught inside `fn` and returned
                 as `{"error": "..."}` for graceful degradation; uncaught
                 exceptions are surfaced to the agent loop and recorded.
    concurrency_safe: If True, the agent may call this tool in parallel with
                 other concurrency-safe tools in the same turn. (Reserved for
                 future use; the v0.1 loop runs tools sequentially.)
    """

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., dict[str, Any]]
    concurrency_safe: bool = True


@dataclass
class ToolCall:
    """Normalised tool-call request from any provider."""

    id: str          # provider-supplied call id (echoed back in tool results)
    name: str
    arguments: dict[str, Any]   # already JSON-parsed


@dataclass
class ToolResult:
    """Outcome of executing a tool. `output` is the dict returned by `fn`,
    or `{"error": "..."}` if the call failed."""

    tool_call_id: str
    name: str
    output: dict[str, Any]
    latency_ms: float
    error: str | None = None
