"""Tushare HTTP client — equity-side endpoints for the research agent.

Goes direct to `http://api.tushare.pro` (POST JSON) so we don't need the
optional `tushare` SDK as a dep. Fully synchronous; the agent loop is
sync as well.

Endpoints exposed:
    stock_basic / daily / daily_basic / fina_indicator / income / balancesheet
    / cashflow / stk_holdertrade  +  resolve_ticker (local fuzzy match).
"""

from __future__ import annotations

import os
import time
from threading import Lock
from typing import Any

import httpx

_API_URL = "http://api.tushare.pro"
_DEFAULT_TIMEOUT = 30.0


class TushareError(RuntimeError):
    """Raised on non-zero `code` or transport failure after retries."""


def _call(
    api_name: str,
    params: dict[str, Any],
    fields: str | None = None,
    *,
    token: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    """Issue one Tushare request. Retries once on rate-limit messages."""
    tok = token or os.getenv("TUSHARE_TOKEN", "")
    if not tok:
        raise TushareError("TUSHARE_TOKEN not set — set it in .env or environment")

    body = {
        "api_name": api_name,
        "token": tok,
        "params": {k: v for k, v in params.items() if v is not None and v != ""},
        "fields": fields or "",
    }

    last_err: str | None = None
    for attempt in range(2):
        try:
            r = httpx.post(_API_URL, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            last_err = f"http error: {e}"
            time.sleep(1.0)
            continue
        if r.status_code != 200:
            last_err = f"{r.status_code}: {r.text[:200]}"
            time.sleep(1.0)
            continue
        try:
            payload = r.json()
        except ValueError as e:
            raise TushareError(f"{api_name}: invalid JSON ({e})") from e
        if payload.get("code") != 0:
            msg = payload.get("msg") or "unknown error"
            # Rate-limit / temporary issues — retry once
            if attempt == 0 and ("超过" in msg or "频次" in msg or "繁忙" in msg):
                time.sleep(1.5)
                last_err = msg
                continue
            raise TushareError(f"{api_name}: {msg}")
        data = payload.get("data") or {}
        cols: list[str] = data.get("fields") or []
        items: list[list] = data.get("items") or []
        return [dict(zip(cols, row, strict=False)) for row in items]
    raise TushareError(f"{api_name}: {last_err or 'request failed'}")


# ────────────────────────────────────────────────────────────────────────────
# stock_basic — cached locally; ~5800 rows (~200 KB)
# ────────────────────────────────────────────────────────────────────────────

_BASIC_CACHE: list[dict] | None = None
_BASIC_LOCK = Lock()


def stock_basic(force_refresh: bool = False) -> list[dict]:
    global _BASIC_CACHE
    if _BASIC_CACHE is not None and not force_refresh:
        return _BASIC_CACHE
    with _BASIC_LOCK:
        if _BASIC_CACHE is not None and not force_refresh:
            return _BASIC_CACHE
        rows = _call(
            "stock_basic",
            {"list_status": "L"},
            fields="ts_code,symbol,name,area,industry,market,list_date",
        )
        _BASIC_CACHE = rows
        return rows


def resolve_ticker(query: str, limit: int = 5) -> list[dict]:
    """Map Chinese name / 6-digit symbol / ts_code → candidate stock_basic rows.

    Match priority: ts_code > symbol > exact name > substring on name.
    Also handles `sh600519` / `sz000001` legacy formats.
    """
    q = (query or "").strip()
    if not q:
        return []
    rows = stock_basic()

    q_upper = q.upper()
    q_norm = q_upper
    if len(q_upper) == 8 and q_upper[:2] in ("SH", "SZ", "BJ") and q_upper[2:].isdigit():
        q_norm = f"{q_upper[2:]}.{q_upper[:2]}"

    if "." in q_norm:
        hits = [r for r in rows if r.get("ts_code") == q_norm]
        if hits:
            return hits[:limit]

    if q_norm.isdigit() and len(q_norm) == 6:
        hits = [r for r in rows if r.get("symbol") == q_norm]
        if hits:
            return hits[:limit]

    hits = [r for r in rows if r.get("name") == q]
    if hits:
        return hits[:limit]

    hits = [r for r in rows if q in (r.get("name") or "")]
    return hits[:limit]


# ────────────────────────────────────────────────────────────────────────────
# Equity research endpoints
# ────────────────────────────────────────────────────────────────────────────


def daily_quote(ts_code: str, start_date: str | None = None,
                end_date: str | None = None, limit: int = 30) -> list[dict]:
    """Daily OHLCV. Dates in YYYYMMDD. UNADJUSTED."""
    rows = _call(
        "daily",
        {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        fields="trade_date,open,high,low,close,vol,amount,pct_chg",
    )
    return rows[:limit]


def daily_basic_latest(ts_code: str) -> dict | None:
    rows = _call(
        "daily_basic",
        {"ts_code": ts_code},
        fields=(
            "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
            "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_mv,circ_mv"
        ),
    )
    return rows[0] if rows else None


def fina_indicator_latest(ts_code: str, limit: int = 4) -> list[dict]:
    rows = _call(
        "fina_indicator",
        {"ts_code": ts_code},
        fields=(
            "ts_code,end_date,roe,roa,grossprofit_margin,netprofit_margin,"
            "debt_to_assets,current_ratio,quick_ratio,or_yoy,netprofit_yoy,"
            "q_sales_yoy,q_profit_yoy"
        ),
    )
    return rows[:limit]


def _statement(api_name: str, ts_code: str, period: str, limit: int,
               fields: str) -> list[dict]:
    rows = _call(api_name, {"ts_code": ts_code}, fields=fields)
    if period == "annual":
        rows = [r for r in rows if str(r.get("end_date") or "").endswith("1231")]
    return rows[:limit]


def income_statement(ts_code: str, period: str = "annual", limit: int = 4) -> list[dict]:
    return _statement(
        "income", ts_code, period, limit,
        fields=(
            "ts_code,end_date,report_type,revenue,oper_cost,operate_profit,"
            "total_profit,income_tax,n_income,n_income_attr_p,basic_eps,diluted_eps,"
            "rd_exp"
        ),
    )


def balance_sheet(ts_code: str, period: str = "annual", limit: int = 4) -> list[dict]:
    return _statement(
        "balancesheet", ts_code, period, limit,
        fields=(
            "ts_code,end_date,report_type,total_assets,total_liab,total_cur_assets,"
            "total_cur_liab,money_cap,accounts_receiv,inventories,fix_assets,"
            "intang_assets,goodwill,st_borr,lt_borr,total_hldr_eqy_inc_min_int"
        ),
    )


def cash_flow(ts_code: str, period: str = "annual", limit: int = 4) -> list[dict]:
    return _statement(
        "cashflow", ts_code, period, limit,
        fields=(
            "ts_code,end_date,report_type,n_cashflow_act,n_cashflow_inv_act,"
            "n_cash_flows_fnc_act,c_pay_acq_const_fiolta,free_cashflow,"
            "c_paid_for_assets"
        ),
    )


def holder_trade(ts_code: str, start_date: str | None = None,
                 end_date: str | None = None, limit: int = 50) -> list[dict]:
    rows = _call(
        "stk_holdertrade",
        {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        fields=(
            "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,"
            "change_ratio,after_share,after_ratio,avg_price,total_share"
        ),
    )
    return rows[:limit]
