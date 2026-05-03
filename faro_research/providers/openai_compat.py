"""OpenAI-compatible chat completions provider.

Works for: OpenAI, DeepSeek, Moonshot, Volcengine Ark, Together AI, Groq,
Ollama (with `/v1` path), and most other open-source servers that ship an
OpenAI-shaped endpoint.

Notes on quirks handled here:
  - DeepSeek reasoning models put the final answer in `content` and
    chain-of-thought in `reasoning_content`; the API requires the latter to
    be echoed back unchanged on the next turn or the request 400s.
  - Some providers reject `response_format` — we don't use it here.
  - `tool_choice="auto"` is omitted when no tools are provided to avoid
    400s on stricter servers.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from faro_research.providers.base import ChatResponse, Message, Provider
from faro_research.tools.types import ToolCall, ToolSpec


class OpenAICompatibleProvider(Provider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str,
                 *, timeout: float = 180.0) -> None:
        if not api_key:
            raise ValueError("OpenAICompatibleProvider: api_key is empty")
        self.base_url = base_url.rstrip("/")
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
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [self._serialize_message(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [self._serialize_tool(t) for t in tools]
            body["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }
        r = httpx.post(url, json=body, headers=headers, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(
                f"openai_compat {r.status_code} from {self.base_url}: {r.text[:300]}"
            )
        data = r.json()
        return self._parse(data)

    # ── serialization ────────────────────────────────────────────────────

    @staticmethod
    def _serialize_tool(spec: ToolSpec) -> dict:
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }

    @staticmethod
    def _serialize_message(m: Message) -> dict:
        if m.role == "system" or m.role == "user":
            return {"role": m.role, "content": m.content or ""}

        if m.role == "assistant":
            out: dict[str, Any] = {
                "role": "assistant",
                "content": m.content or "",
            }
            if m.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in m.tool_calls
                ]
            # Echo back DeepSeek reasoning_content if present
            if "reasoning_content" in m.extra:
                out["reasoning_content"] = m.extra["reasoning_content"]
            return out

        if m.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": m.tool_call_id or "",
                "content": m.content or "",
            }

        raise ValueError(f"unknown role: {m.role}")

    # ── parse ────────────────────────────────────────────────────────────

    @staticmethod
    def _parse(data: dict) -> ChatResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"openai_compat: malformed response: {data!r}") from e
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason") or "stop"

        # Tool calls
        raw_tcs = msg.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for tc in raw_tcs:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
            tool_calls.append(ToolCall(
                id=tc.get("id") or "",
                name=fn.get("name") or "",
                arguments=args,
            ))

        content = msg.get("content")
        # Reasoning models may return only reasoning_content when finish=stop
        if (not content) and msg.get("reasoning_content") and not tool_calls:
            content = msg["reasoning_content"]

        extra: dict[str, Any] = {}
        if msg.get("reasoning_content"):
            extra["reasoning_content"] = msg["reasoning_content"]

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,
            extra=extra,
        )
