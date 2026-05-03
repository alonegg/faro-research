"""Result formatters for the 5 builtin Tushare tools.

Each function takes (raw_output_dict, args_dict) and returns a compact
Chinese-markdown string sized for an LLM context. The point isn't to be
pretty for humans — it's to give the model **already-digested** facts in
a layout it can reason about cheaply, instead of dumping nested JSON.

Token win: typically 5-10x reduction vs raw `json.dumps`.
Reasoning win: model spends less attention parsing field names and more
on actually answering.
"""

from __future__ import annotations

from typing import Any

# ────────────────────────────────────────────────────────────────────────────
# Number / date helpers — A-share specific (元 → 亿 / 万亿; 万元 → 亿)
# ────────────────────────────────────────────────────────────────────────────


def _num(n: Any, *, unit: str = "yuan", precision: int = 2) -> str:
    """Format a CNY amount.

    unit:
      - "yuan" : raw 元 (utility / price)
      - "wan"  : 万元 (Tushare market_cap convention)
    Returns string with appropriate suffix (亿 / 万亿) and `¥` prefix.
    """
    if n is None or n == "":
        return "—"
    try:
        x = float(n)
    except (TypeError, ValueError):
        return "—"
    if unit == "wan":
        x *= 1e4   # 万元 → 元
    sign = "-" if x < 0 else ""
    a = abs(x)
    if a >= 1e12:
        return f"{sign}¥{a / 1e12:.{precision}f} 万亿"
    if a >= 1e8:
        return f"{sign}¥{a / 1e8:.{precision}f} 亿"
    if a >= 1e4:
        return f"{sign}¥{a / 1e4:.{precision}f} 万"
    return f"{sign}¥{a:.0f}"


def _pct(n: Any, *, precision: int = 2, suffix: str = "%") -> str:
    """Tushare ratio fields (roe / margin / yoy) are already percentages."""
    if n is None or n == "":
        return "—"
    try:
        x = float(n)
    except (TypeError, ValueError):
        return "—"
    return f"{x:.{precision}f}{suffix}"


def _ratio(n: Any, *, precision: int = 2, suffix: str = "×") -> str:
    """Plain multiples (PE / PB / PS)."""
    if n is None or n == "":
        return "—"
    try:
        x = float(n)
    except (TypeError, ValueError):
        return "—"
    return f"{x:.{precision}f}{suffix}"


def _date(s: Any) -> str:
    """20260430 → 2026-04-30; 20251231 → 25Q4."""
    if not s:
        return "—"
    s = str(s)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _quarter(s: Any) -> str:
    """20260331 → 26Q1, 20251231 → 25Q4. For statement period headers."""
    if not s:
        return "—"
    s = str(s)
    if len(s) == 8 and s.isdigit():
        yr = s[2:4]
        mm = int(s[4:6])
        q = (mm - 1) // 3 + 1
        return f"{yr}Q{q}"
    return s


# ────────────────────────────────────────────────────────────────────────────
# resolve_ticker
# ────────────────────────────────────────────────────────────────────────────


def fmt_resolve_ticker(out: dict, args: dict) -> str:
    matches = out.get("matches") or []
    if not matches:
        return f"无匹配 ts_code (查询: {args.get('query', '')!r})"
    if len(matches) == 1:
        m = matches[0]
        return (
            f"**{m.get('name', '?')}** = `{m.get('ts_code', '?')}` "
            f"({m.get('industry', '—')} · {m.get('market', '—')})"
        )
    lines = [f"匹配到 {len(matches)} 个 (按权重排序):", "", "| 公司 | ts_code | 行业 |", "|------|---------|------|"]
    for m in matches[:5]:
        lines.append(
            f"| {m.get('name', '?')} | `{m.get('ts_code', '?')}` | {m.get('industry') or '—'} |"
        )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# get_stock_quote
# ────────────────────────────────────────────────────────────────────────────


def fmt_stock_quote(out: dict, args: dict) -> str:
    ts_code = out.get("ts_code", args.get("ts_code", ""))
    rows = out.get("rows") or []
    if not rows:
        return f"{ts_code} — 无行情数据"
    head = [
        f"**{ts_code}** 近 {len(rows)} 个交易日 (未复权)",
        "",
        "| 日期 | 收盘 | 涨跌 | 成交额 |",
        "|------|------|------|--------|",
    ]
    body = []
    for r in rows[:30]:
        d = _date(r.get("trade_date"))
        close = r.get("close")
        chg = r.get("pct_chg")
        amt = r.get("amount")  # Tushare daily.amount is in 千元
        amt_yuan = float(amt) * 1000 if amt is not None else None
        body.append(
            f"| {d} | ¥{float(close):.2f} | "
            f"{('+' + str(chg)) if chg and float(chg) >= 0 else chg}% | "
            f"{_num(amt_yuan)} |"
        )
    return "\n".join(head + body)


# ────────────────────────────────────────────────────────────────────────────
# get_key_ratios
# ────────────────────────────────────────────────────────────────────────────


def fmt_key_ratios(out: dict, args: dict) -> str:
    ts_code = out.get("ts_code", args.get("ts_code", ""))
    snap = out.get("valuation_snapshot") or {}
    fina = out.get("financial_indicators_quarterly") or []

    parts: list[str] = []
    if snap:
        parts += [
            f"## {ts_code} 估值快照 ({_date(snap.get('trade_date'))})",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| **PE_TTM** | **{_ratio(snap.get('pe_ttm'))}** |",
            f"| PE (静态) | {_ratio(snap.get('pe'))} |",
            f"| PB | {_ratio(snap.get('pb'))} |",
            f"| PS_TTM | {_ratio(snap.get('ps_ttm'))} |",
            f"| 股息率 TTM | {_pct(snap.get('dv_ttm'))} |",
            f"| 收盘价 | ¥{snap.get('close', '—')} |",
            f"| 总市值 | {_num(snap.get('total_mv'), unit='wan', precision=2)} |",
            f"| 流通市值 | {_num(snap.get('circ_mv'), unit='wan', precision=2)} |",
            f"| 换手率 | {_pct(snap.get('turnover_rate'))} |",
        ]

    if fina:
        if parts:
            parts.append("")
        parts += [
            f"## 季度财务指标 (近 {len(fina)} 期)",
            "",
            "| 报告期 | ROE | 毛利率 | 净利率 | 营收 YoY | 净利 YoY | 资产负债率 |",
            "|--------|-----|--------|--------|----------|----------|------------|",
        ]
        for r in fina:
            parts.append(
                f"| {_quarter(r.get('end_date'))} | "
                f"**{_pct(r.get('roe'))}** | "
                f"{_pct(r.get('grossprofit_margin'))} | "
                f"{_pct(r.get('netprofit_margin'))} | "
                f"{_pct(r.get('or_yoy'))} | "
                f"{_pct(r.get('netprofit_yoy'))} | "
                f"{_pct(r.get('debt_to_assets'))} |"
            )

    return "\n".join(parts) if parts else f"{ts_code} — 无估值数据"


# ────────────────────────────────────────────────────────────────────────────
# get_three_statements
# ────────────────────────────────────────────────────────────────────────────


def _row_value(rows: list[dict], end_date: str, field: str) -> Any:
    for r in rows:
        if str(r.get("end_date")) == end_date:
            return r.get(field)
    return None


def fmt_three_statements(out: dict, args: dict) -> str:
    ts_code = out.get("ts_code", args.get("ts_code", ""))
    period = out.get("period", "annual")
    inc = out.get("income") or []
    bs = out.get("balance_sheet") or []
    cf = out.get("cash_flow") or []

    if not (inc or bs or cf):
        return f"{ts_code} — 无三表数据"

    # Take the union of end_dates across statements (most-recent first)
    all_dates = sorted(
        {str(r.get("end_date")) for r in (inc + bs + cf) if r.get("end_date")},
        reverse=True,
    )[:5]
    if not all_dates:
        return f"{ts_code} — 三表无 end_date"

    cols = " | ".join(_quarter(d) for d in all_dates)
    sep = " | ".join(["---"] * len(all_dates))
    parts = [f"## {ts_code} 三表 ({period})"]

    # Income
    if inc:
        parts += ["", "### 利润表 (¥)", "", f"| 指标 | {cols} |", f"|------|{sep}|"]
        for label, field in [
            ("营收", "revenue"),
            ("营业成本", "oper_cost"),
            ("营业利润", "operate_profit"),
            ("**归母净利**", "n_income_attr_p"),
            ("EPS", "basic_eps"),
            ("研发费用", "rd_exp"),
        ]:
            cells = " | ".join(
                _ratio(_row_value(inc, d, field), suffix="") if field == "basic_eps"
                else _num(_row_value(inc, d, field))
                for d in all_dates
            )
            parts.append(f"| {label} | {cells} |")

    # Balance sheet (key items)
    if bs:
        parts += ["", "### 资产负债表 (¥)", "", f"| 指标 | {cols} |", f"|------|{sep}|"]
        for label, field in [
            ("总资产", "total_assets"),
            ("总负债", "total_liab"),
            ("**归母权益**", "total_hldr_eqy_inc_min_int"),
            ("货币资金", "money_cap"),
            ("存货", "inventories"),
            ("固定资产", "fix_assets"),
            ("商誉", "goodwill"),
        ]:
            cells = " | ".join(_num(_row_value(bs, d, field)) for d in all_dates)
            parts.append(f"| {label} | {cells} |")

    # Cash flow
    if cf:
        parts += ["", "### 现金流量表 (¥)", "", f"| 指标 | {cols} |", f"|------|{sep}|"]
        for label, field in [
            ("**经营 CF**", "n_cashflow_act"),
            ("投资 CF", "n_cashflow_inv_act"),
            ("筹资 CF", "n_cash_flows_fnc_act"),
            ("CapEx", "c_pay_acq_const_fiolta"),
            ("FCF", "free_cashflow"),
        ]:
            cells = " | ".join(_num(_row_value(cf, d, field)) for d in all_dates)
            parts.append(f"| {label} | {cells} |")

    return "\n".join(parts)


# ────────────────────────────────────────────────────────────────────────────
# get_holder_trades
# ────────────────────────────────────────────────────────────────────────────


def fmt_holder_trades(out: dict, args: dict) -> str:
    ts_code = out.get("ts_code", args.get("ts_code", ""))
    window = out.get("window", "")
    trades = out.get("trades") or []
    if not trades:
        return f"**{ts_code}** {window}: **无董监高及关联人交易记录**"

    # Aggregate buy / sell counts and net change
    n_in = sum(1 for t in trades if t.get("in_de") == "IN")
    n_de = sum(1 for t in trades if t.get("in_de") == "DE")
    summary = f"**{ts_code}** {window}: 共 **{len(trades)}** 笔 (增持 {n_in} / 减持 {n_de})"

    parts = [
        summary,
        "",
        "| 公告日 | 方向 | 持有人 | 类型 | 变动股数 | 占比 | 均价 |",
        "|--------|------|--------|------|----------|------|------|",
    ]
    htype_map = {"C": "高管", "G": "关联", "P": "个人"}
    for t in trades[:20]:
        d = _date(t.get("ann_date"))
        side = "**增**" if t.get("in_de") == "IN" else "**减**"
        name = (t.get("holder_name") or "—")[:20]
        htype = htype_map.get(t.get("holder_type") or "", t.get("holder_type") or "—")
        vol = t.get("change_vol")
        vol_s = f"{int(vol):,}" if vol is not None else "—"
        ratio = _pct(t.get("change_ratio"))
        avg = t.get("avg_price")
        avg_s = f"¥{float(avg):.2f}" if avg is not None else "—"
        parts.append(f"| {d} | {side} | {name} | {htype} | {vol_s} | {ratio} | {avg_s} |")

    if len(trades) > 20:
        parts.append(f"| ... | | (省略 {len(trades) - 20} 笔) | | | | |")
    return "\n".join(parts)
