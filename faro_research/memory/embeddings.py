"""Optional embedding-based semantic recall layer for MemoryStore.

Activation: set `FARO_EMBED_BASE_URL` and `FARO_EMBED_API_KEY` env vars to
any OpenAI-compatible embeddings endpoint:

  - OpenAI:    base=https://api.openai.com/v1, model=text-embedding-3-small
  - Moonshot:  base=https://api.moonshot.cn/v1, model=moonshot-v1-embedding
  - Ollama:    base=http://127.0.0.1:11434/v1, model=bge-m3 (or qwen embedding)
  - Voyage AI: base=https://api.voyageai.com/v1, model=voyage-3

If unset, MemoryStore falls back to its v0.2 LIKE-based search (still
covers ~90% of recall use cases).

Embeddings are computed lazily on `add_chunk` and stored as JSON-array
floats inside SQLite (one BLOB column on a sibling table). Query path
computes a single embedding for the query and ranks by cosine similarity.

The code is small (~150 LoC) and intentionally avoids heavy deps like
sentence-transformers / faiss; for personal-scale memory (1K–100K chars)
in-process numpy cosine on every search is plenty fast.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time

import httpx

log = logging.getLogger(__name__)

_DIM_MISMATCH_LOGGED: set[int] = set()


def is_enabled() -> bool:
    return bool(
        os.getenv("FARO_EMBED_BASE_URL", "").strip()
        and os.getenv("FARO_EMBED_API_KEY", "").strip()
    )


def _config() -> tuple[str, str, str]:
    return (
        os.getenv("FARO_EMBED_BASE_URL", "").strip().rstrip("/"),
        os.getenv("FARO_EMBED_API_KEY", "").strip(),
        os.getenv("FARO_EMBED_MODEL", "text-embedding-3-small").strip(),
    )


def embed(texts: list[str]) -> list[list[float]] | None:
    """OpenAI-compatible /embeddings call. Returns one vector per input text.
    Returns None on failure (caller should degrade gracefully)."""
    if not texts:
        return []
    base, key, model = _config()
    if not (base and key):
        return None
    try:
        r = httpx.post(
            f"{base}/embeddings",
            json={"model": model, "input": texts},
            headers={
                "authorization": f"Bearer {key}",
                "content-type": "application/json",
            },
            timeout=20.0,
        )
        if r.status_code >= 400:
            log.warning("embed %s returned %d: %s", base, r.status_code, r.text[:200])
            return None
        data = r.json()
        return [item["embedding"] for item in data.get("data", [])]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("embed %s failed: %s", base, e)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        # Don't spam logs; remember per-dim that we mismatched
        sig = (len(a) << 16) | len(b)
        if sig not in _DIM_MISMATCH_LOGGED:
            log.warning("embedding dim mismatch: %d vs %d", len(a), len(b))
            _DIM_MISMATCH_LOGGED.add(sig)
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ────────────────────────────────────────────────────────────────────────────
# SQLite-side helpers — sibling table to mem_fts
# ────────────────────────────────────────────────────────────────────────────


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mem_emb (
            file        TEXT NOT NULL,
            line_start  INTEGER NOT NULL,
            line_end    INTEGER NOT NULL,
            content     TEXT NOT NULL,
            vec_json    TEXT NOT NULL,
            created_at  REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_emb_file ON mem_emb(file)")


def upsert_chunks(
    conn: sqlite3.Connection,
    file: str,
    chunks: list[tuple[int, int, str]],
) -> int:
    """Replace all embeddings for `file` with embeddings of `chunks`.

    `chunks` are (line_start, line_end, content) tuples. Returns # of vectors
    written; 0 if embedding API failed (cache stays unchanged).
    """
    conn.execute("DELETE FROM mem_emb WHERE file = ?", (file,))
    if not chunks:
        return 0
    vectors = embed([c[2] for c in chunks])
    if vectors is None:
        # API down or not configured — skip silently
        return 0
    now = time.time()
    rows = [
        (file, ls, le, content, json.dumps(vec, default=str), now)
        for (ls, le, content), vec in zip(chunks, vectors, strict=True)
    ]
    conn.executemany(
        "INSERT INTO mem_emb (file, line_start, line_end, content, vec_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    k: int,
) -> list[tuple[float, str, int, int, str]]:
    """Return [(score, file, line_start, line_end, snippet)] top-k by cosine.

    Returns empty list if embedding API can't be reached.
    """
    qvecs = embed([query])
    if not qvecs:
        return []
    qv = qvecs[0]
    rows = conn.execute(
        "SELECT file, line_start, line_end, content, vec_json FROM mem_emb"
    ).fetchall()
    scored: list[tuple[float, str, int, int, str]] = []
    for r in rows:
        try:
            v = json.loads(r["vec_json"])
        except (json.JSONDecodeError, KeyError):
            continue
        s = cosine(qv, v)
        scored.append((s, r["file"], r["line_start"], r["line_end"], r["content"][:400]))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]
