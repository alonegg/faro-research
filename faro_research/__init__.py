"""Faro Research — open-source A-share research agent.

Quick start::

    from faro_research import Agent, ToolRegistry, make_provider, Message
    from faro_research.tools.builtin.tushare import tushare_tools

    reg = ToolRegistry()
    reg.register_many(tushare_tools())
    agent = Agent(provider=make_provider(), tools=reg)

    trace = agent.run([Message(role="user", content="贵州茅台 PE_TTM")])
    print(trace.final_answer)

See README.md for plugin authoring + server / Docker deployment.
"""

from faro_research.agent import DEFAULT_SYSTEM_PROMPT, Agent, AgentTrace, Message
from faro_research.audit import SessionStore
from faro_research.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    Provider,
    make_provider,
)
from faro_research.tools import ToolCall, ToolRegistry, ToolResult, ToolSpec

__version__ = "0.3.0"

__all__ = [
    "Agent", "AgentTrace", "Message", "DEFAULT_SYSTEM_PROMPT",
    "Provider", "OpenAICompatibleProvider", "AnthropicProvider", "make_provider",
    "ToolRegistry", "ToolSpec", "ToolCall", "ToolResult",
    "SessionStore",
    "__version__",
]
