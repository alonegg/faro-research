"""External tool plugin discovery via setuptools entry_points.

Third-party packages can register tools by adding to their pyproject.toml::

    [project.entry-points."faro_research.tools"]
    my_portfolio = "my_pkg.faro:my_tools"
    research_notes = "my_pkg.faro:notes_tools"

Each entry point must resolve to either:
  - A `ToolSpec` instance, OR
  - A callable returning `list[ToolSpec]` (preferred — matches the
    `tushare_tools()` pattern), OR
  - A list of `ToolSpec`.

`pip install <pkg>` is enough — no explicit `register()` call needed.
The server / CLI calls `discover_external_tools()` at boot to load them all.

Errors are logged but never raise: a broken third-party plugin can't
prevent the agent from starting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

from faro_research.tools.types import ToolSpec

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "faro_research.tools"


def _resolve(value: Any) -> list[ToolSpec]:
    """Normalise an entry-point return into list[ToolSpec]."""
    if isinstance(value, ToolSpec):
        return [value]
    if isinstance(value, list) and all(isinstance(v, ToolSpec) for v in value):
        return value
    if isinstance(value, Callable):
        try:
            inner = value()
        except Exception as e:
            log.warning("entry-point factory raised: %s", e)
            return []
        return _resolve(inner)
    log.warning("entry-point returned unexpected type: %r", type(value))
    return []


def discover_external_tools() -> list[ToolSpec]:
    """Walk all registered `faro_research.tools` entry points; return their
    ToolSpecs. Never raises."""
    out: list[ToolSpec] = []
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:
        # Python < 3.10 returned a dict; we require >=3.11 but be defensive
        eps = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]

    for ep in eps:
        try:
            target = ep.load()
        except Exception as e:
            log.warning("entry point %s failed to load: %s", ep.name, e)
            continue
        specs = _resolve(target)
        if specs:
            log.info("loaded %d tool(s) from entry point %s", len(specs), ep.name)
        out.extend(specs)
    return out
