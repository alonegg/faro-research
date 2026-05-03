# Show HN: Faro Research — open-source A-share research agent

> Tight Hacker News-style version of the v0.1 launch post.
> ~250 words; the longer Chinese version lives in
> [`v0.1-launch.md`](./v0.1-launch.md).

---

**Show HN: Faro Research — open-source A-share research agent (Tushare + multi-LLM)**

I read [virattt/dexter](https://github.com/virattt/dexter) over a weekend and
realised every open-source "financial research agent" out there assumes US
markets + I/B/E/S consensus + SEC 10-K item structure. The Chinese A-share
side is a wasteland.

So I wrote one. Two days, MIT-licensed: <https://github.com/alonegg/faro-research>

What's in v0.1:
- Provider-agnostic agent loop (DeepSeek / OpenAI / Anthropic / Moonshot /
  Volcengine Ark / Ollama via two providers — OpenAI-compat + Anthropic-native)
- 5 built-in Tushare tools: `resolve_ticker`, `get_stock_quote`,
  `get_key_ratios`, `get_three_statements`, `get_holder_trades`
- `ToolSpec` plugin protocol — bring your own tool in 30 lines
  (`examples/custom_plugin.py` shows a portfolio-context plugin)
- SQLite multi-session store + audit log
- React/Vite chat UI with live SSE tool-trace
- `docker compose up` to run locally; CI builds + boots both images on every
  push

What's intentionally NOT in v0.1: multi-user auth, entry-points plugin
discovery, Redis result caching, PDF report export. All on the v0.2-0.4
roadmap.

Why I didn't fork dexter: 60% of the tools needed endpoint+field rewrites,
30% needed semantic rewrites (GICS → 申万 industry, ex-rights / suspension /
ST-stock flags), and the SEC-Item-based filings tools were ~10% irreplaceable.
Cleaner to borrow the design and rewrite.

Curious for feedback from anyone running quant or fundamental research on
Chinese equities.
