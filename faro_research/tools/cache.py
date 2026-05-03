"""Pluggable cache backend for tool results.

Two implementations:
  - InMemoryCache (default) — per-process dict + TTL, no extra deps
  - RedisCache (opt-in) — cross-process, ideal for multi-worker / Docker swarm

Selection (in priority order):
  1. `cache=` kwarg on ToolRegistry()
  2. `FARO_CACHE=redis://host:6379/0` env var → RedisCache
  3. Otherwise → InMemoryCache

Cache keys are `(tool_name, frozen_args)` tuples. Values are the raw `dict`
returned by the tool's `fn` — formatters re-run on cache hit so that schema
changes to a formatter don't require a cache flush.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger(__name__)


def freeze_args(args: dict) -> tuple:
    """Stable hashable form of kwargs for cache keys."""
    def _f(v):
        if isinstance(v, dict):
            return tuple(sorted((k, _f(x)) for k, x in v.items()))
        if isinstance(v, list):
            return tuple(_f(x) for x in v)
        return v
    return tuple(sorted((k, _f(v)) for k, v in args.items()))


@dataclass
class _Entry:
    output: dict
    expires_at: float


class Cache(ABC):
    @abstractmethod
    def get(self, key: tuple) -> dict | None:
        """Return cached output dict if present and unexpired, else None."""

    @abstractmethod
    def set(self, key: tuple, value: dict, ttl_sec: float) -> None:
        """Store output with TTL. Caller decides whether to cache (None TTL = skip)."""


class InMemoryCache(Cache):
    """Process-local cache. Simple, fast, lost on restart."""

    def __init__(self) -> None:
        self._store: dict[tuple, _Entry] = {}

    def get(self, key: tuple) -> dict | None:
        e = self._store.get(key)
        if e is None or e.expires_at <= time.time():
            return None
        return e.output

    def set(self, key: tuple, value: dict, ttl_sec: float) -> None:
        if ttl_sec is None or ttl_sec <= 0:
            return
        self._store[key] = _Entry(output=value, expires_at=time.time() + ttl_sec)


class RedisCache(Cache):
    """Cross-process cache backed by Redis. Activated by FARO_CACHE=redis://...

    Falls back to InMemoryCache (with a log warning) if `redis` package is
    not installed or if the server isn't reachable. This keeps the agent
    runnable in environments without Redis even when the env var is set.
    """

    def __init__(self, url: str) -> None:
        try:
            import redis  # type: ignore
        except ImportError as e:
            raise ImportError(
                "RedisCache requires the `redis` package. "
                "Install via `pip install redis>=5.0`"
            ) from e
        self._client = redis.from_url(url, decode_responses=True)
        # Ping so misconfig errors surface at startup, not on first tool call
        self._client.ping()
        self._prefix = "faro:tool:"

    @staticmethod
    def _key_str(key: tuple) -> str:
        # tuples → stable JSON string for redis key
        return json.dumps(key, ensure_ascii=False, default=str)

    def get(self, key: tuple) -> dict | None:
        raw = self._client.get(self._prefix + self._key_str(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set(self, key: tuple, value: dict, ttl_sec: float) -> None:
        if ttl_sec is None or ttl_sec <= 0:
            return
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        self._client.set(
            self._prefix + self._key_str(key), payload, ex=int(ttl_sec) or 1,
        )


def make_cache(url: str | None = None) -> Cache:
    """Factory honoring `url` arg → FARO_CACHE env → InMemoryCache fallback."""
    target = url or os.getenv("FARO_CACHE", "").strip()
    if target.startswith("redis://") or target.startswith("rediss://"):
        try:
            return RedisCache(target)
        except Exception as e:
            log.warning(
                "RedisCache(%s) unavailable (%s); falling back to InMemoryCache",
                target, e,
            )
    return InMemoryCache()
