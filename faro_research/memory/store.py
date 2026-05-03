"""Memory subsystem — file-based markdown + SQLite FTS5 index.

Layout::

    {data_dir}/memory/
    ├── long-term.md            # facts about user (risk tolerance, goals)
    ├── daily/
    │   ├── 2026-05-03.md       # today's notes
    │   └── 2026-05-02.md
    └── identity/
        ├── SOUL.md             # optional: user's persona / philosophy
        └── RULES.md            # optional: research rules (always check X)

The agent reads memories via 3 tools:
  - memory_search(query, k=5)  → BM25 search over all .md files
  - memory_get(file, line_start, line_end)  → read a specific section
  - memory_update(file, action="append"|"edit"|"delete", content, ...)

FTS5 is rebuilt incrementally — on startup + on every memory_update. SQLite
ships with FTS5 since 3.9; no extra dep.

Embeddings are NOT used in v0.2 (FTS5 covers keyword + Chinese unigram via
the `unicode61` tokenizer with `tokenchars` workaround). v0.3 will add
optional embeddings for fuzzy semantic recall.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from faro_research.config import settings

log = logging.getLogger(__name__)


def _today_iso() -> str:
    return _dt.date.today().isoformat()


@dataclass(frozen=True)
class MemoryHit:
    file: str         # relative path under memory_dir
    line_start: int
    line_end: int
    snippet: str
    rank: float       # BM25 rank (smaller = better; sqlite returns negative)


class MemoryStore:
    """File-backed memory store with SQLite FTS5 index.

    Files are the source of truth; the index is a derived cache and gets
    rebuilt whenever a file is mutated through this class. If the user edits
    files directly outside this class, call `reindex_all()` to refresh.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (settings.db_path.parent / "memory")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "daily").mkdir(exist_ok=True)
        (self.root / "identity").mkdir(exist_ok=True)
        self._db_path = self.root / ".index.sqlite"
        # isolation_level=None → autocommit, avoids the implicit transactions
        # that Python's sqlite3 wraps around DML; combined with WAL, this lets
        # multiple MemoryStore connections (CLI / server / tests) coexist on
        # the same file without deadlock.
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False,
            timeout=10.0, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        # IMPORTANT: must fetchone() the PRAGMA result for WAL to actually apply
        self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._init_schema()

    # ── schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        # FTS5 over (file, chunk_text) — chunked by paragraph for snippet
        # quality. The unicode61 tokenizer handles CJK as individual codepoints
        # which works fine for keyword search of short Chinese terms.
        self._conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
                file UNINDEXED,
                line_start UNINDEXED,
                line_end UNINDEXED,
                content,
                tokenize='unicode61 remove_diacritics 0'
            );
        """)
        # v0.3: optional embeddings table — populated when FARO_EMBED_*
        # env vars are configured.
        from faro_research.memory import embeddings as _emb
        _emb.init_schema(self._conn)
        self._conn.commit()

    # ── file IO helpers ─────────────────────────────────────────────────

    def _resolve(self, rel: str) -> Path:
        """Resolve `rel` to an absolute path, refusing `..` escape."""
        rel = rel.strip().lstrip("/")
        if rel.startswith("identity/SOUL.md") or rel.startswith("identity/RULES.md"):
            pass
        elif rel == "long-term":
            rel = "long-term.md"
        elif rel == "daily":
            rel = f"daily/{_today_iso()}.md"
        elif rel.startswith("daily/") and not rel.endswith(".md"):
            rel = rel + ".md"
        elif "/" not in rel and not rel.endswith(".md"):
            rel = rel + ".md"
        path = (self.root / rel).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError(f"path escapes memory dir: {rel!r}")
        return path

    def list_files(self) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*.md")):
            out.append(str(p.relative_to(self.root)))
        return out

    # ── reindexing ──────────────────────────────────────────────────────

    def _index_file(self, path: Path) -> None:
        rel = str(path.relative_to(self.root))
        # Drop existing rows for this file
        self._conn.execute("DELETE FROM mem_fts WHERE file = ?", (rel,))
        if not path.is_file():
            self._conn.commit()
            return
        text = path.read_text(encoding="utf-8")
        # Chunk by blank-line paragraphs; track line numbers
        lines = text.splitlines()
        chunks: list[tuple[int, int, str]] = []
        buf: list[str] = []
        start = 0
        for i, line in enumerate(lines, start=1):
            if line.strip() == "":
                if buf:
                    chunks.append((start, i - 1, "\n".join(buf)))
                    buf = []
                continue
            if not buf:
                start = i
            buf.append(line)
        if buf:
            chunks.append((start, len(lines), "\n".join(buf)))
        for s, e, content in chunks:
            self._conn.execute(
                "INSERT INTO mem_fts (file, line_start, line_end, content) VALUES (?, ?, ?, ?)",
                (rel, s, e, content),
            )
        # v0.3: also embed if configured (no-op otherwise)
        from faro_research.memory import embeddings as _emb
        if _emb.is_enabled():
            try:
                _emb.upsert_chunks(self._conn, rel, chunks)
            except Exception as e:
                log.warning("embedding upsert for %s failed: %s", rel, e)
        self._conn.commit()

    def reindex_all(self) -> int:
        self._conn.execute("DELETE FROM mem_fts")
        n = 0
        for p in self.root.rglob("*.md"):
            self._index_file(p)
            n += 1
        return n

    # ── public ops ──────────────────────────────────────────────────────

    def search(self, query: str, k: int = 5) -> list[MemoryHit]:
        """Hybrid search: keyword (LIKE OR-of-tokens) ∪ semantic (embeddings).

        - LIKE layer (always on): handles short Chinese queries that FTS5's
          unicode61 tokenizer misses; precise for keywords that appear verbatim.
        - Embedding layer (opt-in via FARO_EMBED_*): captures synonyms,
          paraphrases, conceptual hits ("风险偏好" → 稳健型).

        Hits from both layers are merged and re-ranked. Each chunk gets a
        weighted score: 0.6 × normalised_like + 0.4 × cosine_sim. Pure-LIKE
        installs (default) just see LIKE scores.
        """
        import re
        q = (query or "").strip()
        if not q:
            return []
        # ── LIKE layer ─────────────────────────────────────────────────
        tokens: list[str] = []
        tokens.extend(t.lower() for t in re.findall(r"[A-Za-z0-9_]+", q))
        tokens.extend(re.findall(r"[一-鿿]", q))
        if not tokens:
            return []
        rows = self._conn.execute(
            "SELECT file, line_start, line_end, content FROM mem_fts"
        ).fetchall()
        like_scores: dict[tuple[str, int, int], tuple[float, str]] = {}
        q_lower = q.lower()
        for r in rows:
            content = r["content"]
            content_lower = content.lower()
            hit_count = sum(1 for t in tokens if t in content_lower)
            if hit_count == 0:
                continue
            score = hit_count / max(1, len(tokens))            # 0..1 frac matched
            if q_lower in content_lower:
                score += 0.5                                    # phrase bonus
            score -= 0.001 * len(content)                       # tiebreak shorter
            key = (r["file"], r["line_start"], r["line_end"])
            like_scores[key] = (score, content[:400])

        # ── Embedding layer (opt-in) ───────────────────────────────────
        from faro_research.memory import embeddings as _emb
        emb_scores: dict[tuple[str, int, int], tuple[float, str]] = {}
        if _emb.is_enabled():
            top_emb = _emb.semantic_search(self._conn, q, k=k * 3)
            for score, file, ls, le, snippet in top_emb:
                emb_scores[(file, ls, le)] = (score, snippet)

        # ── Merge & re-rank ────────────────────────────────────────────
        all_keys = set(like_scores) | set(emb_scores)
        if not all_keys:
            return []
        merged: list[tuple[float, MemoryHit]] = []
        for key in all_keys:
            file, ls, le = key
            ls_score, ls_snippet = like_scores.get(key, (0.0, ""))
            em_score, em_snippet = emb_scores.get(key, (0.0, ""))
            snippet = ls_snippet or em_snippet
            # weight: LIKE 0.6 / embeddings 0.4 — LIKE is more trustworthy when
            # it fires, embeddings cover the gap where LIKE misses
            final = 0.6 * ls_score + 0.4 * em_score
            merged.append((final, MemoryHit(
                file=file, line_start=ls, line_end=le,
                snippet=snippet, rank=-final,
            )))
        merged.sort(key=lambda x: -x[0])
        return [hit for _, hit in merged[:k]]

    def get(self, file: str, line_start: int = 1, line_end: int | None = None) -> str:
        path = self._resolve(file)
        if not path.is_file():
            raise FileNotFoundError(f"memory file not found: {file!r}")
        lines = path.read_text(encoding="utf-8").splitlines()
        e = line_end if line_end is not None else len(lines)
        s = max(1, line_start)
        e = min(len(lines), e)
        return "\n".join(lines[s - 1:e])

    def append(self, file: str, content: str) -> str:
        path = self._resolve(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.is_file()
        with path.open("a", encoding="utf-8") as f:
            if existed and not content.startswith("\n"):
                f.write("\n\n")
            f.write(content.rstrip() + "\n")
        self._index_file(path)
        return str(path.relative_to(self.root))

    def edit(self, file: str, old_text: str, new_text: str) -> str:
        path = self._resolve(file)
        if not path.is_file():
            raise FileNotFoundError(f"memory file not found: {file!r}")
        text = path.read_text(encoding="utf-8")
        if old_text not in text:
            raise ValueError(f"old_text not found verbatim in {file!r}")
        if text.count(old_text) > 1:
            raise ValueError(f"old_text matches >1 places in {file!r}; refine to disambiguate")
        path.write_text(text.replace(old_text, new_text), encoding="utf-8")
        self._index_file(path)
        return str(path.relative_to(self.root))

    def delete(self, file: str, old_text: str) -> str:
        return self.edit(file, old_text, "")

    # ── identity ────────────────────────────────────────────────────────

    def soul(self) -> str | None:
        p = self.root / "identity" / "SOUL.md"
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip() or None

    def rules(self) -> str | None:
        p = self.root / "identity" / "RULES.md"
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip() or None
