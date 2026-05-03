"""5 builtin Tushare-backed tools, exposed as a `tushare_tools()` factory.

Usage::

    from faro_research import ToolRegistry
    from faro_research.tools.builtin.tushare import tushare_tools

    reg = ToolRegistry()
    reg.register_many(tushare_tools())
"""

from __future__ import annotations

from datetime import date, timedelta

from faro_research.tools.builtin.tushare import client as ts
from faro_research.tools.builtin.tushare.formatters import (
    fmt_holder_trades,
    fmt_key_ratios,
    fmt_resolve_ticker,
    fmt_stock_quote,
    fmt_three_statements,
)
from faro_research.tools.types import ToolSpec


def _tool_resolve_ticker(query: str) -> dict:
    hits = ts.resolve_ticker(query, limit=5)
    return {
        "matches": [
            {"ts_code": r.get("ts_code"), "name": r.get("name"),
             "industry": r.get("industry"), "market": r.get("market")}
            for r in hits
        ]
    }


def _tool_get_stock_quote(ts_code: str, days: int = 20) -> dict:
    rows = ts.daily_quote(ts_code, limit=max(1, min(days, 120)))
    return {
        "ts_code": ts_code,
        "note": "OHLCV unadjusted; use adj_factor for long-window comparisons",
        "rows": rows,
    }


def _tool_get_key_ratios(ts_code: str) -> dict:
    snap = ts.daily_basic_latest(ts_code)
    fina = ts.fina_indicator_latest(ts_code, limit=4)
    return {
        "ts_code": ts_code,
        "valuation_snapshot": snap,
        "financial_indicators_quarterly": fina,
        "field_glossary": {
            "pe": "static P/E (last year's earnings)",
            "pe_ttm": "trailing twelve months P/E",
            "pb": "price-to-book",
            "total_mv": "total market cap (10k CNY)",
            "circ_mv": "free-float market cap (10k CNY)",
            "roe": "ROE (%)",
            "grossprofit_margin": "gross margin (%)",
            "or_yoy": "revenue YoY (%)",
            "netprofit_yoy": "net income YoY (%)",
        },
    }


def _tool_get_three_statements(ts_code: str, period: str = "annual",
                               limit: int = 4) -> dict:
    if period not in ("annual", "quarterly"):
        period = "annual"
    return {
        "ts_code": ts_code,
        "period": period,
        "income": ts.income_statement(ts_code, period=period, limit=limit),
        "balance_sheet": ts.balance_sheet(ts_code, period=period, limit=limit),
        "cash_flow": ts.cash_flow(ts_code, period=period, limit=limit),
        "note": "amounts in CNY (元); attributable net income at income.n_income_attr_p",
    }


def _tool_get_holder_trades(ts_code: str, months: int = 6) -> dict:
    end = date.today()
    start = end - timedelta(days=int(months) * 31)
    rows = ts.holder_trade(
        ts_code,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        limit=50,
    )
    return {
        "ts_code": ts_code,
        "window": f"{start} → {end}",
        "trades": rows,
        "field_glossary": {
            "in_de": "IN=increase / DE=decrease",
            "holder_type": "C=executive / G=related party / P=individual",
            "change_vol": "share count changed",
            "change_ratio": "% of total shares",
            "avg_price": "average price (CNY); often null",
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# ToolSpec definitions
# ────────────────────────────────────────────────────────────────────────────


_RESOLVE_TICKER = ToolSpec(
    name="resolve_ticker",
    description=(
        "Resolve a Chinese company name / 6-digit symbol / suffixed ts_code "
        "into a Tushare ts_code (e.g. '600519.SH').\n\n"
        "## When to use\n"
        "MUST be called before any stock-specific question — never guess a "
        "ts_code from memory. Returns up to 5 matches.\n\n"
        "## When NOT to use\n"
        "If the user already gave a fully-qualified ts_code like '600519.SH', "
        "you can skip the call.\n\n"
        "## Output\n"
        "If 1 match: use it. If multiple: pick the most plausible (prefer "
        "main-board / large cap); if truly ambiguous, ask the user."
    ),
    compact_description="A股代码解析:中文公司名/6位代码 → ts_code(如 600519.SH)。问个股前必先调。",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Company name (e.g. '贵州茅台'), symbol ('600519'), or ts_code ('600519.SH')",
            },
        },
        "required": ["query"],
    },
    fn=_tool_resolve_ticker,
    formatter=fmt_resolve_ticker,
    cache_ttl_sec=86400,   # listings rarely change; safe to cache 24h
    timeout_sec=10,
)


_GET_STOCK_QUOTE = ToolSpec(
    name="get_stock_quote",
    description=(
        "Recent N trading days of OHLCV (UNADJUSTED prices).\n\n"
        "## When to use\n"
        "Short-term price / volume / trend questions; explaining recent moves.\n\n"
        "## When NOT to use\n"
        "Long-window return comparisons (prices are NOT adjusted for splits / "
        "dividends — use a quant pipeline for that).\n\n"
        "## Output\n"
        "Compact CN markdown table: date / close / pct_chg / amount."
    ),
    compact_description="近 N 日 OHLCV(未复权)。看短期价格/成交趋势用。",
    parameters={
        "type": "object",
        "properties": {
            "ts_code": {"type": "string", "description": "Tushare ts_code, e.g. '600519.SH'"},
            "days": {"type": "integer", "default": 20, "description": "Trading days; default 20, max 120"},
        },
        "required": ["ts_code"],
    },
    fn=_tool_get_stock_quote,
    formatter=fmt_stock_quote,
    cache_ttl_sec=300,   # 5min — intraday refresh window
    timeout_sec=10,
)


_GET_KEY_RATIOS = ToolSpec(
    name="get_key_ratios",
    description=(
        "Latest valuation snapshot + most recent 4 quarters of financial "
        "indicators in ONE call.\n\n"
        "## Returns\n"
        "- Snapshot: PE, PE_TTM, PB, PS_TTM, dividend yield, market cap, "
        "  turnover rate (from daily_basic)\n"
        "- Quarterly: ROE, gross / net margin, revenue YoY, profit YoY, "
        "  debt-to-assets (from fina_indicator, last 4 periods)\n\n"
        "## When to use\n"
        "First-choice tool for valuation / profitability questions. Cheaper "
        "than calling daily_basic + fina_indicator separately."
    ),
    compact_description="估值快照(PE/PB/股息/市值)+ 4 季度财务指标(ROE/毛利率/同比)。估值问题首选。",
    parameters={
        "type": "object",
        "properties": {"ts_code": {"type": "string"}},
        "required": ["ts_code"],
    },
    fn=_tool_get_key_ratios,
    formatter=fmt_key_ratios,
    cache_ttl_sec=3600,   # 1h — daily_basic refreshes EOD
    timeout_sec=15,
)


_GET_THREE_STATEMENTS = ToolSpec(
    name="get_three_statements",
    description=(
        "Income statement + balance sheet + cash flow statement, last N "
        "periods.\n\n"
        "## Parameters\n"
        "- period='annual': only year-end (12-31) reports\n"
        "- period='quarterly': all quarter-ends\n"
        "- limit: 1-12 periods (default 4)\n\n"
        "## When to use\n"
        "Revenue / profit / asset / cash-flow / margin trends across periods.\n\n"
        "## Output\n"
        "Three small tables — income, balance, cashflow — columns are quarters / "
        "years, key items per row."
    ),
    compact_description="利润表+资产负债表+现金流量表(N期)。营收/利润/资产/现金流问题用。",
    parameters={
        "type": "object",
        "properties": {
            "ts_code": {"type": "string"},
            "period": {"type": "string", "enum": ["annual", "quarterly"], "default": "annual"},
            "limit": {"type": "integer", "default": 4, "description": "Number of periods, 1-12"},
        },
        "required": ["ts_code"],
    },
    fn=_tool_get_three_statements,
    formatter=fmt_three_statements,
    cache_ttl_sec=86400,   # statements are immutable once disclosed
    timeout_sec=20,
)


_GET_HOLDER_TRADES = ToolSpec(
    name="get_holder_trades",
    description=(
        "Insider trades (executives + related parties) over the last N months.\n\n"
        "## Returns\n"
        "Per trade: announcement date, direction (IN=increase / DE=decrease), "
        "name, role (高管 / 关联人 / 个人), share count, % of total shares, "
        "average price.\n\n"
        "## When to use\n"
        "Questions about management conviction / risk signals."
    ),
    compact_description="近 N 月董监高及关联人增减持记录。看高管行为信号用。",
    parameters={
        "type": "object",
        "properties": {
            "ts_code": {"type": "string"},
            "months": {"type": "integer", "default": 6, "description": "Lookback months; default 6"},
        },
        "required": ["ts_code"],
    },
    fn=_tool_get_holder_trades,
    formatter=fmt_holder_trades,
    cache_ttl_sec=3600,   # ann_date is daily-grain
    timeout_sec=10,
)


def tushare_tools() -> list[ToolSpec]:
    """Return all 5 builtin Tushare-backed ToolSpecs as a fresh list."""
    return [
        _RESOLVE_TICKER, _GET_STOCK_QUOTE, _GET_KEY_RATIOS,
        _GET_THREE_STATEMENTS, _GET_HOLDER_TRADES,
    ]


__all__ = ["tushare_tools", "client"]
