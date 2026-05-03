"""Meta-tool: get_company_data — LLM router consolidating finance sub-tools.

Inspired by dexter's `get_financials` / `get_market_data`. The main agent
sees ONE tool that takes a natural-language query; internally a cheap LLM
plans which sub-tools to call (resolve_ticker / key_ratios / three_statements
/ holder_trades) and orchestrates them in parallel.

Why: keeping the main agent's tool-list short reduces:
  - context (tool schemas in every system prompt)
  - choice paralysis (LLM no longer hesitates between 5 finance tools)
  - turn count (one meta-call replaces 3-4 sequential calls)

This factory is exposed via `meta_tool(provider, sub_registry)`. Pass it a
Provider for the routing LLM and a sub-registry containing the actual tools
(typically the 4 finance Tushare tools). The router uses the same Provider
type as the main agent for consistency, but you can pass a cheaper one.
"""

from __future__ import annotations

import json
import re
import time

from faro_research.providers.base import Message, Provider
from faro_research.tools.registry import ToolRegistry
from faro_research.tools.types import ToolCall, ToolSpec

_ROUTER_SYSTEM = """You are a finance data dispatcher for Chinese A-shares.

Given a natural-language query, decide which of these sub-tools to call,
then return STRICT JSON describing the calls. The downstream system will
execute them in parallel and stitch the results into one answer.

Sub-tools available:
- resolve_ticker(query: str): Map company name / 6-digit symbol → ts_code.
  CALL FIRST whenever the query references a stock by Chinese name or
  6-digit symbol (not a fully-qualified ts_code like "600519.SH").
- get_key_ratios(ts_code: str): Latest valuation snapshot + 4Q financial
  indicators. Use for valuation / profitability / margin questions.
- get_three_statements(ts_code: str, period: "annual"|"quarterly", limit: int):
  Income / balance / cash flow. Use for revenue / profit / asset / cashflow.
- get_holder_trades(ts_code: str, months: int): Insider trades. Use only
  when query mentions 高管 / 减持 / 增持 / insider.

Output STRICT JSON in this shape (no prose, no code fences):
{
  "needs_resolve": true,
  "resolve_query": "宁德时代",
  "calls": [
    {"name": "get_key_ratios", "args": {}},
    {"name": "get_three_statements", "args": {"period": "annual", "limit": 4}}
  ]
}

Rules:
- If the query already includes a fully-qualified ts_code, set needs_resolve=false
  and put the ts_code in calls[].args.ts_code directly.
- Always include get_key_ratios when the query is general ("查一下 X" /
  "X 怎么样" / "看看 X").
- Default period="annual", limit=4 when unspecified.
- DO NOT call holder_trades unless explicitly asked.
- Only output JSON. No markdown, no explanation."""


def _route(provider: Provider, query: str) -> dict:
    """Single LLM call: query → routing JSON. Returns parsed dict or error."""
    msgs = [
        Message(role="system", content=_ROUTER_SYSTEM),
        Message(role="user", content=query),
    ]
    resp = provider.chat(msgs, tools=None, temperature=0.0, max_tokens=512)
    text = (resp.content or "").strip()
    # strip ```json fences if any
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    # try to find a JSON object
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {"_error": f"router returned non-JSON: {text[:200]}"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"_error": f"router JSON parse failed: {e}"}


def _build_meta_fn(provider: Provider, sub_registry: ToolRegistry):
    """Closure factory — captures the routing provider + sub-tools."""

    def fn(query: str, ts_code: str | None = None) -> dict:
        t0 = time.perf_counter()

        # Step 1: route
        plan = _route(provider, query)
        if plan.get("_error"):
            return {"error": plan["_error"]}

        # Step 2: resolve ts_code if needed
        resolved_ts_code = ts_code
        if not resolved_ts_code and plan.get("needs_resolve"):
            rq = plan.get("resolve_query") or query
            r = sub_registry.execute("resolve_ticker", {"query": rq})
            if r.error:
                return {"error": f"resolve failed: {r.error}"}
            matches = (r.output or {}).get("matches") or []
            if not matches:
                return {"error": f"no ts_code match for {rq!r}"}
            resolved_ts_code = matches[0]["ts_code"]
        elif not resolved_ts_code:
            # Try to pull a ts_code from the first call's args
            for call in plan.get("calls") or []:
                args = call.get("args") or {}
                if args.get("ts_code"):
                    resolved_ts_code = args["ts_code"]
                    break

        if not resolved_ts_code:
            return {"error": "could not determine ts_code"}

        # Step 3: build & execute sub-calls in parallel
        sub_calls: list[ToolCall] = []
        for i, call in enumerate(plan.get("calls") or []):
            name = call.get("name") or ""
            if name not in sub_registry:
                continue
            args = dict(call.get("args") or {})
            args.setdefault("ts_code", resolved_ts_code)
            sub_calls.append(ToolCall(id=f"meta-{i}", name=name, arguments=args))

        if not sub_calls:
            return {"error": "router produced no sub-calls"}

        results = sub_registry.execute_many(sub_calls)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Stitch: keep raw output + formatted blocks side by side
        sections = []
        raw_outputs: dict[str, dict] = {}
        for tc, r in zip(sub_calls, results, strict=True):
            sections.append({
                "tool": tc.name,
                "args": tc.arguments,
                "formatted": r.formatted,
                "error": r.error,
                "latency_ms": round(r.latency_ms, 1),
                "cached": r.cached,
            })
            raw_outputs[tc.name] = r.output

        return {
            "ts_code": resolved_ts_code,
            "router_plan": plan,
            "sub_calls": sections,
            "raw": raw_outputs,
            "_total_latency_ms": round(elapsed_ms, 1),
        }

    return fn


def _meta_formatter(out: dict, args: dict) -> str:
    """Stitch the formatted markdown from each sub-tool result."""
    if out.get("error"):
        return f"**get_company_data 失败**: {out['error']}"

    parts: list[str] = []
    ts = out.get("ts_code", "")
    if ts:
        parts.append(f"# {ts} 综合数据\n")

    for sec in out.get("sub_calls") or []:
        if sec.get("error"):
            parts.append(f"**{sec['tool']}** 调用失败: {sec['error']}")
            continue
        if sec.get("formatted"):
            parts.append(sec["formatted"])
            parts.append("")  # spacer between sub-sections

    cached_n = sum(1 for s in (out.get("sub_calls") or []) if s.get("cached"))
    total = out.get("_total_latency_ms", 0)
    parts.append(f"_(meta-tool: {len(out.get('sub_calls') or [])} sub-calls, "
                 f"{cached_n} cached, {total:.0f}ms)_")
    return "\n".join(parts)


def make_meta_tool(provider: Provider, sub_registry: ToolRegistry) -> ToolSpec:
    """Build the get_company_data ToolSpec.

    `provider` is the routing LLM (use the cheapest you have).
    `sub_registry` must contain at minimum: resolve_ticker, get_key_ratios,
    get_three_statements, get_holder_trades.
    """
    fn = _build_meta_fn(provider, sub_registry)
    return ToolSpec(
        name="get_company_data",
        description=(
            "Meta-tool: takes a natural-language A-share research query and "
            "automatically dispatches to the right sub-tools "
            "(resolve_ticker / get_key_ratios / get_three_statements / "
            "get_holder_trades), runs them in parallel, and returns one "
            "consolidated payload.\n\n"
            "## When to use\n"
            "ANY question about a single A-share stock that needs >1 data "
            "category (estimation + financials, valuation + insider). One call "
            "is cheaper than 3 sequential ones.\n\n"
            "## When NOT to use\n"
            "- Pure price / OHLCV → use get_stock_quote directly\n"
            "- Cross-stock screening / market-wide query → not yet supported\n"
            "- The user's own portfolio → use get_portfolio_context (if registered)\n\n"
            "## Output\n"
            "Stitched markdown: ts_code header, then formatted sections from "
            "each sub-tool. The ts_code is auto-resolved if you pass a "
            "Chinese name in `query`."
        ),
        compact_description="A股个股综合数据(估值/财报/高管交易) 一次拉齐, 自动 ts_code 解析",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language query, e.g. '宁德时代估值和近 4 季度财务表现' "
                        "or '茅台 24 vs 25 营收对比'."
                    ),
                },
                "ts_code": {
                    "type": "string",
                    "description": (
                        "Optional. If you already know the ts_code (e.g. "
                        "'600519.SH'), pass it to skip the resolve step."
                    ),
                },
            },
            "required": ["query"],
        },
        fn=fn,
        formatter=_meta_formatter,
        cache_ttl_sec=None,   # don't cache — args are arbitrary text
        timeout_sec=60,        # router LLM + 4 sub-tools
        concurrency_safe=True,
    )
