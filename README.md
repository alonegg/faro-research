# Faro Research

> 中国 A 股 / 港股 / 美股 ADR 深度研究 Agent，工具可插拔，多 LLM provider，自带 Web UI。

[![CI](https://github.com/alonegg/faro-research/actions/workflows/ci.yml/badge.svg)](https://github.com/alonegg/faro-research/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)

dexter / autogen 等开源 agent 默认面向美股 + I/B/E/S，中国市场几乎空白。
Faro Research 把 agent 内核 + 5 个 Tushare 工具 + 多会话 SQLite + 聊天 UI 打包成
**docker compose up 一行就跑** 的产品；用户挂自家工具即可适配 FOF / 私募 / 研报
等场景。

```
┌───────────┐      ┌────────────┐      ┌────────────────────┐
│ Chat UI   │──SSE→│ FastAPI    │──→──▶│ Agent (LLM loop)   │
│ (React)   │      │ /api/...   │      │  ├─ Provider       │
└───────────┘      └────────────┘      │  └─ ToolRegistry   │
                          │             │     ├─ Tushare ×5  │
                          ▼             │     └─ <你的插件>   │
                   ┌────────────┐       └────────────────────┘
                   │ SQLite     │
                   │ sessions / │
                   │ messages / │
                   │ audit      │
                   └────────────┘
```

## 这个项目解决什么

| 痛点 | 现状 | Faro Research |
|---|---|---|
| 国内 LLM agent 没好用的市场数据接入 | 只能去抓 Yahoo / Alpha Vantage | 内建 Tushare HTTP，5 个工具开箱即用 |
| 单 provider 锁定 | 大多绑 OpenAI | DeepSeek / Moonshot / Ark / OpenAI / Anthropic / Ollama 都行 |
| 工具难以扩展 | langchain 学习曲线陡 | 5 行代码加一个 `ToolSpec`，立刻被 LLM 调到 |
| 没多会话/历史 | 一次性 prompt | SQLite 自动持久化、SSE 流式 trace |

## 快速开始

```bash
git clone https://github.com/your-org/faro-research.git
cd faro-research
cp .env.example .env
# 在 .env 里至少填 FARO_OPENAI_API_KEY 和 TUSHARE_TOKEN
```

### 用 Docker（推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:5173
```

### 本地直接跑（开发）

```bash
# 后端
python -m venv .venv && source .venv/bin/activate
pip install -e ".[server]"
uvicorn faro_research.server.app:app --reload --port 8000

# 另开终端：前端
cd web && pnpm install && pnpm dev
# 浏览器: http://localhost:5173
```

### 纯 CLI

```bash
pip install -e .
faro-research "贵州茅台最近的 PE_TTM"
faro-research "宁德时代过去 6 个月有无董监高减持"
```

### 作为 Python 库嵌入

```python
from faro_research import Agent, Message, ToolRegistry, make_provider
from faro_research.tools.builtin.tushare import tushare_tools

reg = ToolRegistry()
reg.register_many(tushare_tools())
agent = Agent(provider=make_provider(), tools=reg)
trace = agent.run([Message(role="user", content="比亚迪 2024 vs 2025 营收")])
print(trace.final_answer)
```

## 内建工具（Tushare）

| 工具 | 数据 | Tushare 端点 |
|---|---|---|
| `resolve_ticker` | 中文公司名 / 6 位 / 后缀代码 → ts_code | `stock_basic`（本地缓存） |
| `get_stock_quote` | 最近 N 日 OHLCV（未复权） | `daily` |
| `get_key_ratios` | PE/PB/PS/总市值 + 季度 ROE/毛利率/同比 | `daily_basic` + `fina_indicator` |
| `get_three_statements` | 利润表 + 资产负债表 + 现金流量表 | `income` / `balancesheet` / `cashflow` |
| `get_holder_trades` | 董监高及关联人增减持 | `stk_holdertrade` |

需要 Tushare 积分 ≥ 2000（基本面端点门槛）。在 [tushare.pro](https://tushare.pro) 申请。

## 自定义工具（30 行示范）

```python
from faro_research import Agent, Message, ToolRegistry, ToolSpec, make_provider
from faro_research.tools.builtin.tushare import tushare_tools

def my_portfolio(ticker: str | None = None) -> dict:
    """读取你自家的持仓。返回 dict 即可。"""
    return {"ticker": ticker, "weight": 0.18, "as_of": "2026-04-30"}

PORTFOLIO_TOOL = ToolSpec(
    name="get_my_portfolio",
    description="读取用户当前持仓。可选传 ticker 过滤。",
    parameters={
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
    },
    fn=my_portfolio,
)

reg = ToolRegistry()
reg.register_many(tushare_tools())
reg.register(PORTFOLIO_TOOL)

agent = Agent(provider=make_provider(), tools=reg)
print(agent.run([Message(role="user", content="我组合里 NVDA 多少")]).final_answer)
```

完整示例见 [`examples/custom_plugin.py`](examples/custom_plugin.py)。

## LLM Provider

| Provider | env | 备注 |
|---|---|---|
| DeepSeek (默认) | `FARO_PROVIDER=openai_compat` + `FARO_OPENAI_BASE_URL=https://api.deepseek.com` | 推理模型 OK |
| Moonshot Kimi | 同上，base_url 换成 Moonshot | |
| Volcengine Ark | 同上，base_url 换成 Ark | GLM / Doubao / Kimi 都行 |
| OpenAI 官方 | `FARO_OPENAI_BASE_URL=https://api.openai.com/v1` | |
| Ollama 本地 | `FARO_OPENAI_BASE_URL=http://127.0.0.1:11434/v1` | qwen2.5 / llama3.2 已验证 |
| Anthropic Claude | `FARO_PROVIDER=anthropic` | 走 Messages API |

## 路线图

- [x] **v0.1** — 当前：单租户、SSE、2 provider、5 内建工具、Docker compose
- [ ] **v0.2** — 多用户登录（OIDC / API key），entry_points 插件发现
- [ ] **v0.3** — 工具结果缓存（Redis），并行 tool 执行
- [ ] **v0.4** — Markdown / PDF 研报导出
- [ ] **v0.5** — 多 agent 协作（研究员 / 风控 / 编辑）

## License

MIT — 见 [LICENSE](LICENSE)。

## Acknowledgements

- 设计模式借鉴自 [virattt/dexter](https://github.com/virattt/dexter)
- 数据源 [Tushare](https://tushare.pro)
- LLM provider：DeepSeek / Anthropic / OpenAI / Moonshot / Volcengine
