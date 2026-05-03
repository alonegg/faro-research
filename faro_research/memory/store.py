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
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from faro_research.config import settings


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
        """Substring search with simple OR-of-tokens scoring.

        Why not FTS5: the unicode61 tokenizer treats CJK runs as single
        tokens, so a search for '茅' against a doc containing '茅台' returns
        zero hits. trigram tokenizer doesn't help short queries either.
        For a personal-scale memory (1K-100K chars), straight LIKE scans on
        the indexed chunks are plenty fast — and recall correctness matters
        more than micro-perf here.

        Scoring: # of distinct query tokens that appear in the chunk,
        breaking ties by length-of-chunk (shorter first → more precise).
        """
        import re
        q = (query or "").strip()
        if not q:
            return []
        # Tokens: ASCII words (case-insensitive) + Chinese single chars
        tokens: list[str] = []
        tokens.extend(t.lower() for t in re.findall(r"[A-Za-z0-9_]+", q))
        tokens.extend(re.findall(r"[一-鿿]", q))
        # Whole-query bonus: if it appears verbatim, score boost
        if not tokens:
            return []

        rows = self._conn.execute(
            "SELECT file, line_start, line_end, content FROM mem_fts"
        ).fetchall()
        scored: list[tuple[float, MemoryHit]] = []
        q_lower = q.lower()
        for r in rows:
            content = r["content"]
            content_lower = content.lower()
            hits = sum(1 for t in tokens if t in content_lower)
            if hits == 0:
                continue
            score = hits / max(1, len(tokens))            # fraction matched
            if q_lower in content_lower:
                score += 1.0                              # phrase bonus
            score -= 0.001 * len(content)                  # tiebreak: shorter wins
            scored.append((score, MemoryHit(
                file=r["file"], line_start=r["line_start"],
                line_end=r["line_end"], snippet=content[:400],
                rank=-score,    # negative so smaller=better matches FTS convention
            )))
        scored.sort(key=lambda x: -x[0])
        return [hit for _, hit in scored[:k]]

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
