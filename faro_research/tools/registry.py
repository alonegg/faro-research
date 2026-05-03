"""ToolRegistry — collects ToolSpecs and dispatches calls.

Users register tools programmatically::

    from faro_research import ToolRegistry
    from faro_research.tools.builtin.tushare import tushare_tools
    from my_app import my_custom_tool

    reg = ToolRegistry()
    reg.register_many(tushare_tools())
    reg.register(my_custom_tool)

    agent = Agent(provider=..., tools=reg)

v0.2 features:
  - per-tool cache TTL (in-memory, keyed by name + frozen-args)
  - per-tool timeout via thread executor
  - parallel execution of concurrency-safe tools in one turn
  - formatter callbacks (raw dict → markdown for the LLM)

Plugin discovery via setuptools entry_points is on the v0.3 roadmap.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from faro_research.tools.types import ToolCall, ToolResult, ToolSpec


def _freeze_args(args: dict) -> tuple:
    """Stable hashable key from kwargs for the cache."""
    def _f(v):
        if isinstance(v, dict):
            return tuple(sorted((k, _f(x)) for k, x in v.items()))
        if isinstance(v, list):
            return tuple(_f(x) for x in v)
        return v
    return tuple(sorted((k, _f(v)) for k, v in args.items()))


@dataclass
class _CacheEntry:
    output: dict
    expires_at: float


class ToolRegistry:
    """A mutable collection of ToolSpecs keyed by name."""

    def __init__(self, *, max_workers: int = 5) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._cache: dict[tuple, _CacheEntry] = {}
        self._max_workers = max_workers

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

    # ── execution ────────────────────────────────────────────────────────

    def execute(self, name: str, arguments: dict, *, call_id: str = "") -> ToolResult:
        """Run a single tool. Honours cache_ttl + timeout + formatter."""
        spec = self._tools.get(name)
        if spec is None:
            err = f"unknown tool: {name}"
            return ToolResult(
                tool_call_id=call_id, name=name,
                output={"error": err}, latency_ms=0.0, error=err,
            )

        # Cache hit?
        cache_key: tuple | None = None
        if spec.cache_ttl_sec and spec.cache_ttl_sec > 0:
            cache_key = (name, _freeze_args(arguments))
            now = time.time()
            entry = self._cache.get(cache_key)
            if entry and entry.expires_at > now:
                fmt = self._format(spec, entry.output, arguments)
                return ToolResult(
                    tool_call_id=call_id, name=name, output=entry.output,
                    latency_ms=0.0, error=None, formatted=fmt, cached=True,
                )

        # Run with timeout
        t0 = time.perf_counter()
        err: str | None = None
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(self._invoke, spec, arguments)
            try:
                output = future.result(timeout=spec.timeout_sec)
            except FutureTimeout:
                err = f"timeout after {spec.timeout_sec:g}s"
                output = {"error": err}
                future.cancel()
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                output = {"error": err}
        latency_ms = (time.perf_counter() - t0) * 1000

        # Persist to cache only on success
        if cache_key is not None and err is None:
            self._cache[cache_key] = _CacheEntry(
                output=output,
                expires_at=time.time() + spec.cache_ttl_sec,
            )

        fmt = self._format(spec, output, arguments) if err is None else None
        return ToolResult(
            tool_call_id=call_id, name=name, output=output,
            latency_ms=latency_ms, error=err, formatted=fmt,
        )

    def execute_many(self, calls: Sequence[ToolCall]) -> list[ToolResult]:
        """Run a batch of tool calls. Concurrency-safe ones run in parallel
        via a thread pool; non-safe ones run after them, sequentially.

        Result order is preserved (matches input order).
        """
        if not calls:
            return []
        safe_idx = [i for i, c in enumerate(calls)
                    if (s := self._tools.get(c.name)) and s.concurrency_safe]
        unsafe_idx = [i for i in range(len(calls)) if i not in set(safe_idx)]

        results: list[ToolResult | None] = [None] * len(calls)

        # Parallel batch
        if safe_idx:
            workers = min(self._max_workers, len(safe_idx))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(self.execute, calls[i].name, calls[i].arguments,
                              call_id=calls[i].id): i
                    for i in safe_idx
                }
                for fut in futures:
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                    except Exception as e:
                        results[i] = ToolResult(
                            tool_call_id=calls[i].id, name=calls[i].name,
                            output={"error": f"{type(e).__name__}: {e}"},
                            latency_ms=0.0, error=f"{type(e).__name__}: {e}",
                        )

        # Sequential batch
        for i in unsafe_idx:
            results[i] = self.execute(
                calls[i].name, calls[i].arguments, call_id=calls[i].id,
            )

        return [r for r in results if r is not None]

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _invoke(spec: ToolSpec, arguments: dict) -> dict:
        out = spec.fn(**arguments)
        if not isinstance(out, dict):
            return {"value": out}
        return out

    @staticmethod
    def _format(spec: ToolSpec, output: dict, arguments: dict) -> str | None:
        if spec.formatter is None:
            return None
        try:
            text = spec.formatter(output, arguments)
            return text if isinstance(text, str) else str(text)
        except Exception as e:
            # Formatter must never crash the tool result; fall back to raw JSON
            return f"[formatter failed: {type(e).__name__}: {e}]"


def render_for_llm(result: ToolResult, limit_chars: int) -> str:
    """Pick the best string representation of a tool result for the LLM,
    falling back from formatted markdown → raw JSON → truncation marker."""
    if result.formatted is not None:
        text = result.formatted
    else:
        text = json.dumps(result.output, ensure_ascii=False, default=str)
    if len(text) <= limit_chars:
        return text
    return text[:limit_chars] + f"\n...[truncated {len(text) - limit_chars} chars]"


def truncate_result(result: dict, limit_chars: int) -> str:
    """Backward-compat helper. New code should use render_for_llm()."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit_chars:
        return text
    return text[:limit_chars] + f"\n...[truncated {len(text) - limit_chars} chars]"
