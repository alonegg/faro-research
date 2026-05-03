"""Agent loop — provider-agnostic, tool-aware, streaming.

`stream_agent` is the canonical entry point: it yields events as they happen
(turn_start, tool_call, tool_result, final, error). `run_agent` is a thin
blocking wrapper that drains the stream and returns an `AgentTrace`.

Both work with any conversation history, so multi-turn sessions just pass in
a longer `messages` list. The server route loads history from `SessionStore`
and prepends it before calling.

Loop invariants:
  - Hard cap MAX_TOOL_TURNS prevents infinite loops
  - Tool result text is truncated to TOOL_RESULT_MAX_CHARS
  - Provider errors interrupt the loop and yield a single error event
"""

from __future__ import annotations

import textwrap
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from faro_research.config import settings
from faro_research.providers.base import Message, Provider
from faro_research.tools.registry import ToolRegistry, truncate_result

DEFAULT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a research assistant for Chinese A-share equities. All data must
    come from the provided tools — never fabricate numbers.

    Working rules:
    1. For any question about a specific stock, FIRST call resolve_ticker to
       get the ts_code. Don't guess from memory. If the resolver returns
       multiple candidates, pick the most plausible (prefer main-board /
       larger market cap); if truly ambiguous, ask the user.
    2. Use the smallest set of tools that answers the question; do not call
       every tool by reflex. Tool calls in the same turn run sequentially.
    3. Units: revenue / profit / assets / liabilities are in CNY (元);
       market caps (total_mv, circ_mv) are in 10k CNY (万元); margins,
       ratios, and YoY growth fields are already percentages — do not
       multiply by 100 again.
    4. PE distinction: `pe` is the static P/E (last fiscal year's earnings);
       `pe_ttm` is the trailing twelve months. Default to pe_ttm unless the
       user explicitly asks for static.
    5. The daily quote tool returns UNADJUSTED prices — fine for short-term
       trend / volume questions, not suitable for long-window return
       comparisons.
    6. When you have all the data you need, write the final answer in concise
       Chinese markdown: bold the key numbers, use small tables, end with
       the data date(s) (from trade_date / end_date / ann_date fields).
    7. If a tool returns no data for a field, say "未披露" — never guess.

    Tools registered in this session may include user-supplied plugins that
    expose additional context (e.g. portfolio holdings, research notes).
    Read each tool's description and use it when relevant.
""")


@dataclass
class AgentTrace:
    """Final outcome of one agent run, suitable for persistence / display."""

    final_answer: str
    turns: int
    tool_calls: list[dict] = field(default_factory=list)
    latency_total_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "final_answer": self.final_answer,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "latency_total_ms": self.latency_total_ms,
            "error": self.error,
        }


class Agent:
    """Wraps a Provider + ToolRegistry + system prompt.

    Use `stream(messages)` for SSE / live UI; `run(messages)` for batch.
    """

    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_turns: int | None = None,
        tool_result_max_chars: int | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_turns = max_turns or settings.max_tool_turns
        self.tool_result_max_chars = (
            tool_result_max_chars or settings.tool_result_max_chars
        )

    # ── streaming ───────────────────────────────────────────────────────

    def stream(self, messages: list[Message]) -> Iterator[dict]:
        """Yield event dicts. See module docstring for schema.

        `messages` should NOT include the system prompt — it's injected here.
        Pass full conversation history (user + previous assistant turns + any
        previous tool messages) to enable multi-turn context.
        """
        full_messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            *messages,
        ]
        tool_log: list[dict] = []
        t_start = time.perf_counter()
        tools_specs = self.tools.specs() or None

        for turn in range(self.max_turns):
            yield {"type": "turn_start", "turn": turn + 1}
            try:
                resp = self.provider.chat(full_messages, tools=tools_specs)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                yield {"type": "error", "message": msg}
                return

            if not resp.tool_calls:
                content = resp.content or "(model returned empty content)"
                yield {
                    "type": "final",
                    "answer": content,
                    "turns": turn + 1,
                    "tool_calls": tool_log,
                    "latency_total_ms": (time.perf_counter() - t_start) * 1000,
                }
                return

            # Echo assistant message back so the next turn has context
            full_messages.append(Message(
                role="assistant",
                content=resp.content,
                tool_calls=resp.tool_calls,
                extra=dict(resp.extra),
            ))

            for tc in resp.tool_calls:
                yield {
                    "type": "tool_call",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "args": tc.arguments,
                }
                result = self.tools.execute(tc.name, tc.arguments, call_id=tc.id)
                truncated = truncate_result(result.output, self.tool_result_max_chars)
                tool_log.append({
                    "name": tc.name,
                    "args": tc.arguments,
                    "latency_ms": round(result.latency_ms, 1),
                    "result_chars": len(truncated),
                    "error": result.error,
                })
                yield {
                    "type": "tool_result",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "latency_ms": round(result.latency_ms, 1),
                    "result_chars": len(truncated),
                    "error": result.error,
                }
                full_messages.append(Message(
                    role="tool",
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=truncated,
                ))

            if resp.finish_reason == "stop":
                # Edge case: model said stop but emitted tool_calls;
                # loop again to digest the results
                continue

        # Hit cap — request a wrap-up without tools
        full_messages.append(Message(
            role="user",
            content="Tool-call limit reached. Give your best final answer with no further tools.",
        ))
        try:
            resp = self.provider.chat(full_messages, tools=None)
            content = resp.content or "(no final answer produced)"
        except Exception as e:
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
            return
        yield {
            "type": "final",
            "answer": content,
            "turns": self.max_turns,
            "tool_calls": tool_log,
            "latency_total_ms": (time.perf_counter() - t_start) * 1000,
        }

    # ── blocking ────────────────────────────────────────────────────────

    def run(self, messages: list[Message]) -> AgentTrace:
        """Drain the stream into an AgentTrace."""
        final_answer = ""
        turns = 0
        tool_calls: list[dict] = []
        latency = 0.0
        error: str | None = None
        for ev in self.stream(messages):
            if ev["type"] == "final":
                final_answer = ev["answer"]
                turns = ev["turns"]
                tool_calls = ev["tool_calls"]
                latency = ev["latency_total_ms"]
            elif ev["type"] == "error":
                error = ev["message"]
        return AgentTrace(
            final_answer=final_answer or (f"agent failed: {error}" if error else ""),
            turns=turns,
            tool_calls=tool_calls,
            latency_total_ms=latency,
            error=error,
        )


__all__ = ["Agent", "AgentTrace", "DEFAULT_SYSTEM_PROMPT", "Message"]
