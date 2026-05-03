"""Tool primitives — provider-agnostic.

A `ToolSpec` is the unit users register. Providers (OpenAI / Anthropic / ...)
translate it to their own native format internally. The `fn` is a regular
Python callable; arguments come from the LLM as keyword args (already JSON-parsed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# A formatter takes (raw_output_dict, original_arguments_dict) → markdown string.
# Used to compress raw API JSON into compact LLM-readable tables BEFORE the
# result is sent back to the model. Optional; if None, raw JSON is sent.
ToolFormatter = Callable[[dict[str, Any], dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool.

    Required:
      name:        Unique identifier the LLM uses to call.
      description: Multi-paragraph rich description for the system prompt
                   (When to Use / When NOT to Use / Usage Notes). Be specific
                   about inputs, outputs, and limits — bad descriptions cause
                   bad tool choices.
      parameters:  JSON schema dict (OpenAI / Anthropic compatible subset).
                   Top level should be `{"type":"object", "properties":{...},
                   "required":[...]}`.
      fn:          Pure Python callable returning a dict. Errors inside `fn`
                   should return `{"error": "..."}` for graceful degradation;
                   uncaught exceptions are caught by the registry and recorded.

    Optional:
      compact_description: 1-line version (~120 chars) used in token-optimised
                   system prompts. Defaults to a truncation of `description`.
      formatter:   (raw_output, args) → markdown string. Compresses raw API
                   JSON into compact tables. Saves 5-10x tokens AND gives the
                   LLM a cleaner mental model. None = ship raw JSON.
      cache_ttl_sec: Memoize results by (name, args) for this many seconds.
                   None = never cache. Use 60 for snapshots, 86400 for closed
                   periods (annual reports).
      timeout_sec: Hard cap on `fn` execution. On timeout, the tool returns
                   `{"error": "timeout after Xs"}` and the agent moves on.
      concurrency_safe: If True, multiple tool calls in one turn may run in
                   parallel via a thread pool. Set False for tools that mutate
                   state (file writes, DB writes).
    """

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., dict[str, Any]]
    compact_description: str | None = None
    formatter: ToolFormatter | None = None
    cache_ttl_sec: float | None = None
    timeout_sec: float = 15.0
    concurrency_safe: bool = True

    def short(self) -> str:
        """Return the compact description, falling back to a truncation of
        the rich one. Used by token-optimised prompts."""
        if self.compact_description:
            return self.compact_description
        first_line = self.description.strip().split("\n", 1)[0]
        return first_line[:160]


@dataclass
class ToolCall:
    """Normalised tool-call request from any provider."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """Outcome of executing a tool.

    `output` is the dict returned by `fn` (or `{"error": "..."}` on failure).
    `formatted` is the markdown string from `spec.formatter` if set; agent
    sends this to the LLM instead of `json.dumps(output)`."""

    tool_call_id: str
    name: str
    output: dict[str, Any]
    latency_ms: float
    error: str | None = None
    formatted: str | None = None
    cached: bool = False
