"""SKILL.md loader + skill-invocation tool.

Each skill is a directory containing `SKILL.md`. The first 30 lines may be
YAML frontmatter::

    ---
    name: dcf-cn
    description: A 股 DCF 估值。当用户问"估值是否合理 / 内在价值 / 公允价格 / 是否高估"时调用。
    ---

    # DCF 估值（A 股口径）
    ...workflow markdown...

The agent is given a `skill` tool whose description includes ALL discovered
skills' (name, description) pairs. Calling `skill(name=...)` returns the full
SKILL.md body — the agent then follows the workflow step by step using its
other tools.

Discovery searches:
  1. `faro_research/skills/builtin/*/SKILL.md`  (shipped with the package)
  2. `$FARO_SKILLS_DIR` environment variable (user override directory)

Borrowed concept from virattt/dexter; the workflow checklist + sanity-check
pattern is what produces report-grade outputs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from faro_research.tools.types import ToolSpec

_BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str         # full SKILL.md body (with frontmatter stripped)
    path: Path

    @property
    def trigger_hint(self) -> str:
        """One-line summary for the registration prompt."""
        return f"- **{self.name}**: {self.description}"


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_skill_md(path: Path) -> Skill | None:
    """Read SKILL.md, parse frontmatter, return a Skill object.

    Frontmatter must be valid YAML with at least `name` and `description`
    keys. We use a regex (no PyYAML dep) because the format is constrained.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm = m.group(1)
    body = text[m.end():]

    name = description = None
    for line in fm.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"\'')
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip('"\'')
    if not name or not description:
        return None
    return Skill(name=name, description=description, body=body, path=path)


def discover_skills(extra_dirs: list[Path] | None = None) -> list[Skill]:
    """Scan all builtin + user skill directories. Returns list ordered by name."""
    dirs: list[Path] = [_BUILTIN_DIR]
    env_dir = os.getenv("FARO_SKILLS_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    if extra_dirs:
        dirs.extend(extra_dirs)

    skills: dict[str, Skill] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            if not sub.is_dir():
                continue
            md = sub / "SKILL.md"
            if not md.is_file():
                continue
            sk = _parse_skill_md(md)
            if sk:
                skills[sk.name] = sk    # later entries override earlier (user > builtin)
    return list(skills.values())


def make_skill_tool(extra_dirs: list[Path] | None = None) -> ToolSpec | None:
    """Return a `skill` ToolSpec listing all discovered skills.

    Returns None if no skills found — caller should not register a tool that
    has zero values it can handle.
    """
    skills = discover_skills(extra_dirs)
    if not skills:
        return None
    skill_map = {s.name: s for s in skills}
    enum = sorted(skill_map.keys())
    catalog = "\n".join(s.trigger_hint for s in skills)

    description = (
        "Invoke a specialised research workflow (skill). Each skill is a "
        "step-by-step checklist with its own input requirements, validation "
        "rules, and output template — use it when the query matches a skill's "
        "trigger description.\n\n"
        "## Available skills\n\n"
        f"{catalog}\n\n"
        "## Usage\n"
        "Call `skill(name='<one-of-above>')` to retrieve the full workflow. "
        "Then execute it step by step using the other tools. Each skill ends "
        "with a structured output template — follow it exactly."
    )

    def fn(name: str) -> dict:
        sk = skill_map.get(name)
        if not sk:
            return {"error": f"unknown skill {name!r}; available: {enum}"}
        return {
            "name": sk.name,
            "description": sk.description,
            "workflow": sk.body,
        }

    return ToolSpec(
        name="skill",
        description=description,
        compact_description=f"Invoke a research workflow ({len(skills)} available: {', '.join(enum[:3])}…)",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": enum,
                    "description": "Exact skill name from the catalog above.",
                },
            },
            "required": ["name"],
        },
        fn=fn,
        formatter=lambda out, args: out.get("workflow", out.get("error", "")),
        cache_ttl_sec=86400,
        timeout_sec=2,
        concurrency_safe=True,
    )
