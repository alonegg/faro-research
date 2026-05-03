# Changelog

All notable changes to **Faro Research** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org).

## [0.3.0] — 2026-05-03 — 生态扩展

让别人能 `pip install + 用上`,而不只是 fork 改源码。

### Added — 插件生态

- **`entry_points` 自动插件发现** —— 第三方包在 `pyproject.toml` 写一行
  `[project.entry-points."faro_research.tools"] foo = "my_pkg:tools"`,
  `pip install` 后 Faro CLI / server 启动时自动注册。无需手改 registry。
- **fund/ FOF plugin 实例**: `app.faro_plugin.fof_tools` 暴露
  `get_portfolio_context` + `simulate_solver` ToolSpec,演示真实场景。
- **Pluggable Cache 抽象** —— `ToolRegistry(cache=...)`:
  - `InMemoryCache` (默认): 进程内 dict + TTL,零依赖
  - `RedisCache`: 跨进程共享,激活方式 `FARO_CACHE=redis://host:6379/0`,
    依赖 `pip install "faro-research[redis]"`. Redis 不可用自动降级到内存。

### Added — Memory 增强

- **混合搜索 (LIKE ∪ embeddings)**: `MemoryStore.search` 加权合并:
  - LIKE 层(默认): 中文短查询精确匹配
  - Embedding 层(opt-in): 设 `FARO_EMBED_BASE_URL` + `FARO_EMBED_API_KEY` +
    `FARO_EMBED_MODEL` 启用语义召回; 兼容 OpenAI / Moonshot / Ollama / Voyage
- 嵌入向量存 SQLite `mem_emb` 表,无需 sentence-transformers / faiss

### Changed
- 默认 `_default_registry` 自动调 `discover_external_tools()` 注册外部插件
- README + CHANGELOG 更新到 v0.3 范围
- `faro-research` 包版本 0.2.0 → 0.3.0


## [0.2.0] — 2026-05-03 — "卧槽" 输出质量

The big one. v0.1 had the right plumbing; v0.2 has the right reports.

### Added — agent infrastructure

- `ToolSpec` gains four optional fields: `compact_description` (token-tight
  prompt), `formatter` (raw dict → markdown), `cache_ttl_sec` (per-tool
  memoisation), `timeout_sec` (per-tool hard cap). Backward compatible.
- `ToolRegistry.execute_many()` runs concurrency-safe tool calls in a
  thread pool; non-safe ones run after, sequentially.
- Per-tool in-memory cache (LRU-ish; expires by TTL). Tushare snapshots
  cached 1h, statements 24h, listings 24h, prices 5m.
- `Agent.build_system_prompt(soul=, rules=)` splices user identity / rules
  into the base prompt.

### Added — output quality

- **Result formatters for all Tushare tools** — convert raw JSON into
  compact CN markdown tables (¥1.73 万亿 / 25Q4 / **PE_TTM 20.97×**).
  Token usage drops 5–10x; output quality jumps to dexter level.
- **Strict output profile** in default system prompt: tables ≤ 3 columns,
  headers ≤ 4 chars, bold key numbers, no over-precision, ticker not full
  company name, end with data date.
- **Meta-tool `get_company_data`** — LLM router that consolidates
  resolve_ticker + key_ratios + three_statements + holder_trades behind
  one natural-language interface. Main agent now sees 2 finance tools
  instead of 5; one call ≈ 3 sequential calls of work.

### Added — skills

- **Skills system** — markdown-defined research workflows discoverable from
  `faro_research/skills/builtin/*/SKILL.md` (or `$FARO_SKILLS_DIR/*`).
- 3 builtin skills:
  - `dcf-cn` — A-share DCF (10Y CN treasury risk-free rate, 申万 industry β,
    Gordon terminal model, 3×3 sensitivity, EV / TV-share / PE-band
    sanity checks)
  - `research-report` — full single-stock report template with one-line
    conclusion → business → financials → valuation → catalysts → signals
  - `screener` — industry/factor stock-picking workflow

### Added — memory

- **SQLite-backed memory store** at `data/memory/` with
  `long-term.md` + `daily/YYYY-MM-DD.md` + `identity/SOUL.md` + `RULES.md`.
- 3 memory tools: `memory_search` (LIKE-based, OR-of-tokens, CJK-friendly),
  `memory_get` (read range), `memory_update` (append / edit / delete).
- `SOUL.md` (identity) and `RULES.md` (research rules) auto-loaded into
  the agent's system prompt when present.

### Changed

- CLI / server default tool layout shifted from "5 raw Tushare tools" to
  "meta + quote + skill + 3 memory tools" (still 6 total, but cleaner
  delegation). Pass `--legacy-tools` to CLI to restore the v0.1 layout.

### Fixed

- Provider `reasoning_content` echo — re-confirmed working across all turns
  after the Phase A refactor.

### Migration

- No code-level breaks. Existing `ToolRegistry` users keep their tools
  exactly as before; new optional fields default to `None` / `False`.
- `truncate_result()` helper kept for backward compat; new code should
  prefer `render_for_llm(result, limit)` which honours formatters.


## [0.1.0] — 2026-05-03 — initial drop

- Provider-agnostic agent loop (DeepSeek / OpenAI / Anthropic / Moonshot /
  Volcengine / Ollama via 2 providers)
- 5 Tushare tools (resolve_ticker / get_stock_quote / get_key_ratios /
  get_three_statements / get_holder_trades)
- ToolSpec / ToolRegistry plugin protocol
- SQLite multi-session store + audit log
- React/Vite chat UI with SSE
- Docker compose stack + GitHub Actions CI
- MIT license
