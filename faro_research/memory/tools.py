"""Memory tools — wraps MemoryStore as 3 ToolSpecs the agent can call."""

from __future__ import annotations

from faro_research.memory.store import MemoryStore
from faro_research.tools.types import ToolSpec


def make_memory_tools(store: MemoryStore | None = None) -> list[ToolSpec]:
    """Return [memory_search, memory_get, memory_update] bound to a store.

    If `store` is None, uses the package default at $FARO_DB_PATH/memory/.
    """
    s = store or MemoryStore()
    # Initial index build (cheap, idempotent)
    s.reindex_all()

    # ── search ──────────────────────────────────────────────────────────

    def _search(query: str, k: int = 5) -> dict:
        hits = s.search(query, k=max(1, min(k, 20)))
        return {
            "query": query,
            "n_hits": len(hits),
            "hits": [
                {
                    "file": h.file,
                    "lines": f"{h.line_start}-{h.line_end}",
                    "snippet": h.snippet,
                    "rank": round(h.rank, 4),
                }
                for h in hits
            ],
        }

    def _fmt_search(out: dict, args: dict) -> str:
        if not out.get("hits"):
            return f"无匹配 (query={args.get('query', '')!r})"
        parts = [f"找到 **{out['n_hits']}** 条相关记忆 (query={args.get('query', '')!r}):"]
        for h in out["hits"]:
            parts.append(f"\n**{h['file']}** (行 {h['lines']}):\n```\n{h['snippet']}\n```")
        return "\n".join(parts)

    search_tool = ToolSpec(
        name="memory_search",
        description=(
            "Full-text search over the user's persistent memory files "
            "(long-term facts, daily notes, identity / rules).\n\n"
            "## When to use\n"
            "BEFORE giving any personalised advice — buy/sell suggestions, "
            "portfolio recommendations, position sizing — call this to recall "
            "the user's risk tolerance, goals, prior decisions, and watchlist.\n"
            "Also call when the user references something from past conversations "
            "(e.g. '我之前说过的那只股票').\n\n"
            "## Output\n"
            "Top-K matching paragraphs with file path + line range so you can "
            "use memory_get to read more context."
        ),
        compact_description="搜索用户长期记忆(偏好/目标/历史决策)。给个性化建议前必先调。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词或短语"},
                "k": {"type": "integer", "default": 5, "description": "返回 top K (1-20)"},
            },
            "required": ["query"],
        },
        fn=_search,
        formatter=_fmt_search,
        cache_ttl_sec=None,
        timeout_sec=5,
        concurrency_safe=True,
    )

    # ── get ─────────────────────────────────────────────────────────────

    def _get(file: str, line_start: int = 1, line_end: int | None = None) -> dict:
        try:
            content = s.get(file, line_start=line_start, line_end=line_end)
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}
        return {"file": file, "line_start": line_start, "line_end": line_end, "content": content}

    get_tool = ToolSpec(
        name="memory_get",
        description=(
            "Read a specific section of a memory file. Use after memory_search "
            "when you need full text around a hit (the search snippet is "
            "truncated).\n\n"
            "Files: `long-term`, `daily/YYYY-MM-DD`, `identity/SOUL`, "
            "`identity/RULES`, or any path returned by memory_search."
        ),
        compact_description="读取记忆文件指定行段(配合 memory_search 用)",
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line_start": {"type": "integer", "default": 1},
                "line_end": {"type": "integer"},
            },
            "required": ["file"],
        },
        fn=_get,
        formatter=lambda out, args: (
            f"**{out.get('file', '')}** ({out.get('line_start')}-{out.get('line_end') or '末尾'})\n\n"
            f"```\n{out.get('content', out.get('error', ''))}\n```"
        ),
        cache_ttl_sec=None,
        timeout_sec=2,
        concurrency_safe=True,
    )

    # ── update ──────────────────────────────────────────────────────────

    def _update(action: str, file: str = "long-term",
                content: str = "", old_text: str = "") -> dict:
        try:
            if action == "append":
                if not content:
                    return {"error": "append requires `content`"}
                rel = s.append(file, content)
                return {"action": "append", "file": rel, "added_chars": len(content)}
            if action == "edit":
                if not old_text or not content:
                    return {"error": "edit requires `old_text` and `content`"}
                rel = s.edit(file, old_text, content)
                return {"action": "edit", "file": rel}
            if action == "delete":
                if not old_text:
                    return {"error": "delete requires `old_text`"}
                rel = s.delete(file, old_text)
                return {"action": "delete", "file": rel}
            return {"error": f"unknown action: {action!r}"}
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}

    update_tool = ToolSpec(
        name="memory_update",
        description=(
            "Add, edit, or delete a memory entry.\n\n"
            "## Actions\n"
            "- `append` (default file: long-term): add a new fact / preference. "
            "  Use for things the user explicitly tells you to remember, OR for "
            "  things you infer that will be useful next session.\n"
            "- `edit`: replace `old_text` (must match verbatim) with `content`.\n"
            "- `delete`: remove `old_text` (must match verbatim).\n\n"
            "## When to use\n"
            "- User says '记住...' / 'remember that...' → append\n"
            "- User corrects a stored fact → edit\n"
            "- User says '忘掉...' → delete\n\n"
            "## Default behaviour\n"
            "If file is omitted, appends to long-term. Use `daily` for daily "
            "logs, or `identity/RULES` for research rules."
        ),
        compact_description="增/改/删用户长期记忆。用户说'记住...'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": ["append", "edit", "delete"],
                    "default": "append",
                },
                "file": {
                    "type": "string", "default": "long-term",
                    "description": "Logical name: long-term / daily / identity/SOUL / identity/RULES",
                },
                "content": {"type": "string", "description": "(append/edit) 新内容"},
                "old_text": {"type": "string", "description": "(edit/delete) 要匹配的原文"},
            },
            "required": ["action"],
        },
        fn=_update,
        formatter=lambda out, args: (
            f"✓ {out.get('action')} → {out.get('file', '')}"
            if out.get("action") else f"⚠ {out.get('error', 'failed')}"
        ),
        cache_ttl_sec=None,
        timeout_sec=2,
        concurrency_safe=False,   # writes — keep sequential
    )

    return [search_tool, get_tool, update_tool]
