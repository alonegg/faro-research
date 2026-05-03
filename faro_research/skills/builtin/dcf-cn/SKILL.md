---
name: dcf-cn
description: A 股 DCF 估值（10Y 国债无风险利率 + 申万行业 β）。当用户问"X 是否高估/低估""内在价值是多少""公允价格""估值合不合理""值不值得买"时调用。
---

# DCF 估值（A 股口径）

## 进度清单

每完成一步划掉一项：

```
DCF 进度:
- [ ] 1. 拉数据(get_company_data + get_stock_quote)
- [ ] 2. 计算 5Y FCF CAGR
- [ ] 3. 估算 WACC(A 股口径: 10Y 国债 + 申万 β)
- [ ] 4. 预测未来 5 年 FCF + 终值
- [ ] 5. 折现求企业价值 → 每股内在价值
- [ ] 6. 敏感性分析(WACC ±1% × 永续增长 2/2.5/3%)
- [ ] 7. 合理性校验
- [ ] 8. 结构化输出
```

## 步骤 1：拉数据

调 `get_company_data(query="<公司名> 估值和近 5 年三表")` 一次拿齐：
- 估值快照（市值、现价、PE_TTM、PB）
- 5 年现金流（`n_cashflow_act`, `c_pay_acq_const_fiolta`, `free_cashflow`）
- 5 年利润（`n_income_attr_p`, `revenue`）
- 资产负债表（`money_cap`, `total_liab`, `lt_borr + st_borr`）
- 季度财务指标（`roe`, `debt_to_assets`）

调 `get_stock_quote(ts_code, days=1)` 拿最新收盘价。

**关键字段提取**：
- FCF 历史：优先用 `free_cashflow` 字段；缺失时按 `n_cashflow_act + c_pay_acq_const_fiolta`（CapEx 在 Tushare 是负数，所以是加）
- 净债务：`(lt_borr + st_borr) - money_cap`（手头现金抵债）
- 股本：用 `total_mv / close` 推算总股本（更可靠，避免不同口径）

## 步骤 2：FCF 增长率

5 年 FCF 计算 CAGR：`(FCF_{t} / FCF_{t-4}) ^ (1/4) - 1`。

**交叉校验**：
- 营收 CAGR（`revenue` 5 年）
- ROE 趋势（如果 ROE 持续下降 → 慎用历史 CAGR 推未来）

**增长率选择**：
- FCF 稳定（5 年 std/mean < 30%）→ CAGR 打 8 折用
- FCF 波动（std/mean ≥ 30%）→ 用最近 3 年中位数 × 0.7
- **硬上限 12%**（A 股盈利持续 >15% 增速极少超过 5 年）

## 步骤 3：WACC（A 股口径）

### 默认参数
- **无风险利率 Rf**：10 年期国债 ~2.5%（v0.2 写死，v0.3 接 wbond_yield）
- **市场风险溢价 ERP**：5%（A 股 wind 估算）
- **税率 t**：25%（一般 A 股；金融除外）

### β 值（按申万一级行业）
| 行业 | 推荐 β | 备注 |
|---|---|---|
| 银行 / 保险 / 公用 | 0.7-0.9 | 防御性 |
| 食品饮料 / 家电 | 0.8-1.0 | 消费稳定 |
| 医药 / 计算机 | 1.1-1.3 | 成长 |
| 半导体 / 新能源 / 军工 | 1.3-1.5 | 高弹性 |
| 房地产 / 钢铁 / 煤炭 | 1.0-1.2 | 周期 |

### Cost of equity (CAPM)
```
Ke = Rf + β × ERP
```

### Cost of debt
- 取近 1 年新发公司债票面利率，或用 `int_exp / (lt_borr + st_borr)`
- 如缺数据，用 4.5% 作为 default A 股 AAA 信用利差代理
- 税后 Kd = Kd_pre × (1 - 25%)

### 资本结构
```
D = lt_borr + st_borr
E = total_mv × 10⁴   (万元 → 元)
WACC = (E/(D+E)) × Ke + (D/(D+E)) × Kd_after_tax
```

**合理性检查**：WACC 应在 ROIC 下方 2-4pp（创造价值的公司）。

## 步骤 4：预测未来 FCF

- **Year 1-5**：FCF_n = FCF_0 × (1 + g_n)，g_n = g_base × decay_n
  decay_n = [1.00, 0.95, 0.90, 0.85, 0.80]（增长率每年衰减 5%，反映竞争）
- **终值**：Gordon 模型，永续增长 g_term = 2.5%（GDP 长期增速代理）
  TV = FCF_5 × (1 + g_term) / (WACC - g_term)

## 步骤 5：折现 → 每股内在价值

```
EV = Σ_{n=1..5} FCF_n / (1+WACC)^n + TV / (1+WACC)^5
Equity = EV - 净债务
Per share = Equity / 总股本
```

**与现价对比**：
- per_share / current_price - 1 = 安全边际
- > 30%：低估
- -10% ~ 30%：合理
- < -10%：高估

## 步骤 6：敏感性分析

3×3 矩阵：WACC 取 base ±1%，永续增长取 [2%, 2.5%, 3%]。

| WACC ↓ \ g → | 2.0% | 2.5% | 3.0% |
|---|---|---|---|
| WACC-1% | ¥X | ¥X | ¥X |
| WACC | ¥X | **¥X** | ¥X |
| WACC+1% | ¥X | ¥X | ¥X |

**警示**：如果敏感性矩阵中 9 个值跨度 > 当前价 ±50%，说明假设太脆弱，不应给单一价格目标。

## 步骤 7：合理性校验

输出之前必须过这 3 道：

1. **EV 反推**：算出的 EV 应在 Tushare 报告的 EV ±30% 内（否则 WACC 或 g 错了）
2. **终值占比**：TV 应占 EV 的 50-80%（>90% → 增长率太高；<40% → 近期假设太激进）
3. **PE 反算**：内在价值 / EPS 应该在该公司历史 PE band 内（用 `get_company_data` 的 4Q ROE 反推 PE）

任一不通过 → 重做对应步骤的假设。

## 步骤 8：输出格式

```markdown
## {ticker} DCF 估值

### 结论
**内在价值 ¥X / 股 vs 现价 ¥Y → 安全边际 ±Z%**
评级: [低估 / 合理 / 高估]

### 关键假设
| 输入 | 值 | 来源 |
|---|---|---|
| FCF 5Y CAGR | X% | 历史 |
| g_base | X% | CAGR × 0.8 |
| g_terminal | 2.5% | GDP 代理 |
| Rf | 2.5% | 10Y 国债 |
| β | X | 申万{行业} |
| WACC | X% | CAPM |

### 现金流预测
| 年份 | FCF |
|------|-----|
| Y1 | ¥X 亿 |
| ... | ... |
| TV | ¥X 亿 |

### 敏感性矩阵
[3×3 表]

### 风险提示
- 关键假设依赖 …
- 敏感性最大的是 …
- 不适用于：[周期股 / 资源股 / 重组股 / 亏损股]
```

末尾标 `> DCF 模型为粗糙估算; 不构成投资建议; 数据日期 YYYY-MM-DD`
