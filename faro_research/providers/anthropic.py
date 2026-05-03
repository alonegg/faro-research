"""Anthropic Messages API provider.

Translates the agent's normalised Message/ToolCall types to Anthropic's
content-block format::

    {role: "assistant", content: [
        {type: "text", text: "..."},
        {type: "tool_use", id: "...", name: "...", input: {...}},
    ]}
    {role: "user", content: [
        {type: "tool_result", tool_use_id: "...", content: "..."}
    ]}

Spec: https://docs.anthropic.com/en/api/messages
"""

from __future__ import annotations

from typing import Any

import httpx

from faro_research.providers.base import ChatResponse, Message, Provider
from faro_research.tools.types import ToolCall, ToolSpec

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, *, timeout: float = 180.0) -> None:
        if not api_key:
            raise ValueError("AnthropicProvider: api_key is empty")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        # Extract system messages → Anthropic puts them in a top-level field
        system_text = "\n\n".join(
            (m.content or "") for m in messages if m.role == "system"
        ).strip() or None

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": self._serialize_messages([m for m in messages if m.role != "system"]),
        }
        if system_text:
            body["system"] = system_text
        if tools:
            body["tools"] = [self._serialize_tool(t) for t in tools]

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        r = httpx.post(_API_URL, json=body, headers=headers, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"anthropic {r.status_code}: {r.text[:300]}")
        return self._parse(r.json())

    # ── serialization ────────────────────────────────────────────────────

    @staticmethod
    def _serialize_tool(spec: ToolSpec) -> dict:
        return {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }

    @staticmethod
    def _serialize_messages(messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        # Anthropic groups consecutive tool_results into one user message.
        pending_tool_results: list[dict] = []

        def flush_tool_results():
            if pending_tool_results:
                out.append({"role": "user", "content": list(pending_tool_results)})
                pending_tool_results.clear()

        for m in messages:
            if m.role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content or "",
                })
                continue
            flush_tool_results()
            if m.role == "user":
                out.append({"role": "user", "content": m.content or ""})
            elif m.role == "assistant":
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls or []:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                out.append({"role": "assistant", "content": blocks})
        flush_tool_results()
        return out

    # ── parse ────────────────────────────────────────────────────────────

    @staticmethod
    def _parse(data: dict) -> ChatResponse:
        blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for blk in blocks:
            btype = blk.get("type")
            if btype == "text":
                text_parts.append(blk.get("text") or "")
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=blk.get("id") or "",
                    name=blk.get("name") or "",
                    arguments=dict(blk.get("input") or {}),
                ))
        finish = data.get("stop_reason") or "stop"
        # normalise stop reasons to OpenAI-ish values
        if finish == "tool_use":
            finish = "tool_calls"
        elif finish == "end_turn":
            finish = "stop"
        return ChatResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=finish,
            extra={},
        )
