"""ToolRegistry — collects ToolSpecs and dispatches calls.

Users register tools programmatically::

    from faro_research import ToolRegistry
    from faro_research.tools.builtin.tushare import tushare_tools
    from my_app import my_custom_tool

    reg = ToolRegistry()
    reg.register_many(tushare_tools())
    reg.register(my_custom_tool)

    agent = Agent(provider=..., tools=reg)

Plugin discovery via setuptools entry_points is on the v0.2 roadmap.
"""

from __future__ import annotations

import json
import time
from typing import Iterable

from faro_research.tools.types import ToolResult, ToolSpec


class ToolRegistry:
    """A mutable collection of ToolSpecs keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def register_many(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def execute(self, name: str, arguments: dict, *, call_id: str = "") -> ToolResult:
        """Run a tool, capturing latency + errors. Never raises."""
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                tool_call_id=call_id,
                name=name,
                output={"error": f"unknown tool: {name}"},
                latency_ms=0.0,
                error=f"unknown tool: {name}",
            )
        t0 = time.perf_counter()
        try:
            output = spec.fn(**arguments)
            if not isinstance(output, dict):
                output = {"value": output}
            err = None
        except Exception as e:
            output = {"error": f"{type(e).__name__}: {e}"}
            err = output["error"]
        latency_ms = (time.perf_counter() - t0) * 1000
        return ToolResult(
            tool_call_id=call_id, name=name, output=output,
            latency_ms=latency_ms, error=err,
        )


def truncate_result(result: dict, limit_chars: int) -> str:
    """Serialize tool output to JSON, truncating to `limit_chars` to keep the
    LLM context lean. Adds an explicit `[truncated]` marker."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit_chars:
        return text
    return text[:limit_chars] + f"\n...[truncated {len(text) - limit_chars} chars]"
