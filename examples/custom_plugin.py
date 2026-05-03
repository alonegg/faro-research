"""Example: a custom tool plugin.

Demonstrates how to add a domain-specific tool (here: a fake "portfolio
context" reader) and register it alongside the builtin Tushare tools.

Run with::

    cp .env.example .env  # fill in keys
    python examples/custom_plugin.py "我的组合里 NVDA 占多少"
"""

from __future__ import annotations

import sys

from faro_research import Agent, Message, ToolRegistry, ToolSpec, make_provider
from faro_research.tools.builtin.tushare import tushare_tools


# ──────────────────────────────────────────────────────────────────────────
# 1) Define your tool — a regular Python function returning a dict
# ──────────────────────────────────────────────────────────────────────────


_FAKE_PORTFOLIO = {
    "name": "Demo 组合",
    "as_of": "2026-04-30",
    "holdings": [
        {"ticker": "NVDA", "weight": 0.18, "name": "英伟达"},
        {"ticker": "MSFT", "weight": 0.12, "name": "微软"},
        {"ticker": "600519.SH", "weight": 0.08, "name": "贵州茅台"},
        {"ticker": "300750.SZ", "weight": 0.06, "name": "宁德时代"},
    ],
}


def my_portfolio_tool(ticker: str | None = None) -> dict:
    if not ticker:
        return {"portfolio": _FAKE_PORTFOLIO}
    h = next(
        (h for h in _FAKE_PORTFOLIO["holdings"] if h["ticker"].upper() == ticker.upper()),
        None,
    )
    return {"holding": h, "as_of": _FAKE_PORTFOLIO["as_of"]} if h \
        else {"holding": None, "note": f"{ticker} not in portfolio"}


# ──────────────────────────────────────────────────────────────────────────
# 2) Wrap as ToolSpec — name, description, JSON-schema parameters
# ──────────────────────────────────────────────────────────────────────────


PORTFOLIO_TOOL = ToolSpec(
    name="get_my_portfolio",
    description=(
        "Read the user's current portfolio. Without args, returns all holdings. "
        "Pass `ticker` (e.g. 'NVDA' or '600519.SH') to look up one position."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Optional ticker filter"},
        },
    },
    fn=my_portfolio_tool,
)


# ──────────────────────────────────────────────────────────────────────────
# 3) Register alongside builtins, build agent, run.
# ──────────────────────────────────────────────────────────────────────────


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python examples/custom_plugin.py '<question>'", file=sys.stderr)
        return 2

    reg = ToolRegistry()
    reg.register_many(tushare_tools())
    reg.register(PORTFOLIO_TOOL)
    print(f"registered {len(reg)} tools: {reg.names()}", file=sys.stderr)

    agent = Agent(provider=make_provider(), tools=reg)
    trace = agent.run([Message(role="user", content=" ".join(sys.argv[1:]))])
    print(trace.final_answer)
    print(
        f"\n[turns={trace.turns} tools={[t['name'] for t in trace.tool_calls]} "
        f"latency={trace.latency_total_ms:.0f}ms]",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
