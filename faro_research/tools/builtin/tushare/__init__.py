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
        "into a Tushare ts_code (e.g. '600519.SH'). MUST be called before any "
        "stock-specific question — never guess a ts_code from memory. "
        "Returns up to 5 matches; pick the most likely or ask the user if "
        "ambiguous."
    ),
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
)


_GET_STOCK_QUOTE = ToolSpec(
    name="get_stock_quote",
    description=(
        "Recent N trading days of OHLCV (UNADJUSTED). Use for short-term "
        "price/volume trends. Not suitable for long-window return comparisons "
        "(prices not adjusted for splits/dividends)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ts_code": {"type": "string", "description": "Tushare ts_code, e.g. '600519.SH'"},
            "days": {"type": "integer", "default": 20, "description": "Trading days to return; default 20, max 120"},
        },
        "required": ["ts_code"],
    },
    fn=_tool_get_stock_quote,
)


_GET_KEY_RATIOS = ToolSpec(
    name="get_key_ratios",
    description=(
        "Latest valuation snapshot (PE, PE_TTM, PB, PS, market cap, dividend "
        "yield) PLUS most recent 4 quarters of financial indicators (ROE, "
        "margins, YoY growth, leverage). First-choice tool for valuation "
        "questions."
    ),
    parameters={
        "type": "object",
        "properties": {"ts_code": {"type": "string"}},
        "required": ["ts_code"],
    },
    fn=_tool_get_key_ratios,
)


_GET_THREE_STATEMENTS = ToolSpec(
    name="get_three_statements",
    description=(
        "Income statement + balance sheet + cash flow statement (N periods). "
        "period='annual' returns year-end reports only; 'quarterly' returns "
        "all quarters. Use for revenue / profit / asset / cash-flow questions."
    ),
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
)


_GET_HOLDER_TRADES = ToolSpec(
    name="get_holder_trades",
    description=(
        "Insider (executive + related party) buy/sell records over the last "
        "N months. in_de=IN means increase, DE means decrease. Use for "
        "questions about insider activity."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ts_code": {"type": "string"},
            "months": {"type": "integer", "default": 6, "description": "Lookback window in months; default 6"},
        },
        "required": ["ts_code"],
    },
    fn=_tool_get_holder_trades,
)


def tushare_tools() -> list[ToolSpec]:
    """Return all 5 builtin Tushare-backed ToolSpecs as a fresh list."""
    return [
        _RESOLVE_TICKER, _GET_STOCK_QUOTE, _GET_KEY_RATIOS,
        _GET_THREE_STATEMENTS, _GET_HOLDER_TRADES,
    ]


__all__ = ["tushare_tools", "client"]
