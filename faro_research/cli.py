"""CLI: `faro-research "..."` or `python -m faro_research.cli "..."`.

Wires up the default tool set (Tushare) + provider (from env) and runs the
agent in streaming mode, printing tool calls to stderr and the final answer
to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys

from faro_research.agent import Agent, Message
from faro_research.providers import make_provider
from faro_research.tools import ToolRegistry
from faro_research.tools.builtin.tushare import tushare_tools


def _build_default_agent() -> Agent:
    reg = ToolRegistry()
    reg.register_many(tushare_tools())
    return Agent(provider=make_provider(), tools=reg)


def main() -> int:
    p = argparse.ArgumentParser(
        prog="faro-research",
        description="Faro Research — A-share research agent CLI.",
    )
    p.add_argument("query", nargs="+", help="Natural-language question")
    p.add_argument(
        "--quiet", "-q", action="store_true",
        help="Hide tool-call trace, only print final answer.",
    )
    args = p.parse_args()
    query = " ".join(args.query)

    try:
        agent = _build_default_agent()
    except Exception as e:
        print(f"setup failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    final_answer = ""
    error = None
    turns = 0
    n_tools = 0
    for ev in agent.stream([Message(role="user", content=query)]):
        et = ev["type"]
        if et == "turn_start":
            if not args.quiet:
                print(f"[turn {ev['turn']}]", file=sys.stderr)
        elif et == "tool_call":
            n_tools += 1
            if not args.quiet:
                print(
                    f"  → {ev['name']}({json.dumps(ev['args'], ensure_ascii=False)})",
                    file=sys.stderr,
                )
        elif et == "tool_result":
            if not args.quiet:
                err = f" ✗ {ev['error']}" if ev.get("error") else ""
                print(f"    ← {ev['latency_ms']:.0f} ms{err}", file=sys.stderr)
        elif et == "final":
            final_answer = ev["answer"]
            turns = ev["turns"]
        elif et == "error":
            error = ev["message"]

    if error and not final_answer:
        print(f"agent failed: {error}", file=sys.stderr)
        return 1
    print(final_answer)
    if not args.quiet:
        print(f"\n---\n[turns={turns}, tools={n_tools}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
