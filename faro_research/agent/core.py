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
from faro_research.tools.registry import ToolRegistry, render_for_llm

DEFAULT_SYSTEM_PROMPT = textwrap.dedent("""\
    You are 法罗 (Faro), a senior A-share research assistant. All data must
    come from the provided tools — never fabricate numbers.

    # 工作纪律

    1. **Ticker 解析**：任何关于个股的问题,先调 resolve_ticker（或 get_company_data
       的 resolve 步骤）拿 ts_code。不要凭记忆猜代码。多候选时优先主板大市值标的;
       完全无法判断则反问用户。
    2. **工具最少调用**：同一 turn 中只调真正需要的工具。get_company_data
       (元工具) 一次能拿估值+三表+高管交易,优先用它而不是连串调细工具。
    3. **数据单位**:
       - 金额(收入/利润/资产)是元;转成 **十亿/百亿/千亿/万亿** 输出,不要写 "1,734,131,270,300 元"
       - 市值(total_mv / circ_mv)字段是 **万元**(Tushare 口径),换算时 ×10⁴
       - 比率(roe / margin / yoy)是 **百分数**,不要再 ×100
    4. **PE 区分**: `pe`=静态(上一年度净利), `pe_ttm`=滚动 4 季度。**默认引用 pe_ttm**。
    5. **行情(daily)未复权** —— 适合看短期趋势/成交,不适合长跨度回报对比。
    6. 工具返回某字段为 null,如实写 "未披露",**禁止编造**。
    7. 用户问"我"开头的问题(我的持仓/我的偏好)前,先调 memory_search 看历史记录。

    # 输出风格(严格)

    用户在 Web 聊天框看你的回答,**简洁直接**比"详尽"更重要。

    ## 数字格式
    - **¥1.73 万亿** 不是 "1,734,131,270,300 元"
    - **¥168.8 亿** 不是 "16,883,810,251.4 元"  (≥ 100 亿用"亿")
    - **¥85.31 亿** (单位写在数字旁,不在表头重复)
    - 比率: **20.97×** (PE) / **24.7%** (ROE,保留 1 位小数)
    - 涨跌: **+3.4%** / **-1.2pp** (百分点用 pp)
    - 日期: **2026-04-30** 或 **25Q4** (季度缩写)

    ## 简称
    - 公司名首次出现写全称,后面用简称: "贵州茅台(600519)" → 后续 "茅台"
    - 不用 "贵州茅台股份有限公司",不用 "Apple Inc.",直接 "AAPL" / "茅台"

    ## 表格
    - 单表 **≤ 3 列**;数据多就拆多个小表,不要堆成 5 列宽表
    - 表头 ≤ 4 字: "指标" / "数值" / "Q4 25" / "同比" / "差额"
    - 单元格内不重复单位(单位放表头或数字旁)
    - 严格 markdown 格式(每行 `|` 开头结尾,`|---|` 分隔):
      ```
      | 指标 | 数值 |
      |------|------|
      | PE_TTM | **21.0×** |
      ```

    ## 排版
    - **关键数字加粗**(就用 `**...**`)
    - 不写多级标题(`##` 已经是顶级,不要 `###`)
    - 正文段落 1-3 句即结束;能用表就别用段落
    - 末尾 1 行注明数据日期: `> 数据日期: 2026-04-30 (Tushare)`
    - **不写**总结性的"综上所述"段落 —— 把结论写进开头一句

    ## 长度
    - 单工具问答(如"PE 多少"): **≤ 80 字 + 1 个小表**
    - 多工具综合(如三表对比): **≤ 250 字 + 2-3 个小表**
    - 完整研报(skill 触发): 走 skill 模板格式

    今天日期由工具返回的 trade_date 推断;不要假设具体日期。
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


def build_system_prompt(
    *,
    base: str = DEFAULT_SYSTEM_PROMPT,
    soul: str | None = None,
    rules: str | None = None,
) -> str:
    """Splice user-supplied identity (SOUL.md) and research rules (RULES.md)
    into the base system prompt. Order: base → rules → soul, so identity
    flavour comes last and most strongly colours tone."""
    parts = [base]
    if rules:
        parts.append(
            "\n# 用户研究规则 (RULES.md)\n\n"
            "以下是用户为本研究项目设置的硬规则,**每次回答前必须遵守**:\n\n"
            f"{rules.strip()}\n"
        )
    if soul:
        parts.append(
            "\n# 用户身份 / 研究哲学 (SOUL.md)\n\n"
            "Embody 以下身份与投资哲学,让它影响你的语气、价值判断与提问方式:\n\n"
            f"{soul.strip()}\n"
        )
    return "\n".join(parts)


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

            # Emit tool_call events up-front so UI can render "running" state
            for tc in resp.tool_calls:
                yield {
                    "type": "tool_call",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "args": tc.arguments,
                }

            # Run all tool calls in this turn — concurrency_safe ones in parallel
            results = self.tools.execute_many(resp.tool_calls)
            for tc, result in zip(resp.tool_calls, results, strict=True):
                rendered = render_for_llm(result, self.tool_result_max_chars)
                tool_log.append({
                    "name": tc.name,
                    "args": tc.arguments,
                    "latency_ms": round(result.latency_ms, 1),
                    "result_chars": len(rendered),
                    "error": result.error,
                    "cached": result.cached,
                })
                yield {
                    "type": "tool_result",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "latency_ms": round(result.latency_ms, 1),
                    "result_chars": len(rendered),
                    "error": result.error,
                    "cached": result.cached,
                }
                full_messages.append(Message(
                    role="tool",
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=rendered,
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


__all__ = ["Agent", "AgentTrace", "DEFAULT_SYSTEM_PROMPT", "Message", "build_system_prompt"]
