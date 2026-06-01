"""Channel taxonomy loader and matcher.

Reads groups.yml from the project root (or wherever DISCORD_GROUPS_FILE points)
and lets server tools group channels into categories like prep / post / fondeo.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


@dataclass(frozen=True)
class GroupRule:
    name: str
    description: str
    exact_names: tuple[str, ...]
    prefixes: tuple[str, ...]

    def matches(self, channel_name: str) -> bool:
        if channel_name in self.exact_names:
            return True
        return any(channel_name.startswith(p) for p in self.prefixes)


def _find_groups_file() -> Path | None:
    override = os.getenv("DISCORD_GROUPS_FILE")
    if override:
        p = Path(override)
        return p if p.exists() else None

    # Project root next to src/
    candidate = Path(__file__).resolve().parents[2] / "groups.yml"
    if candidate.exists():
        return candidate
    return None


@lru_cache(maxsize=1)
def load_groups() -> list[GroupRule]:
    """Load groups from groups.yml. Cached for the process lifetime."""
    path = _find_groups_file()
    if path is None:
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    groups_raw = raw.get("groups", {})
    result: list[GroupRule] = []
    for name, spec in groups_raw.items():
        result.append(GroupRule(
            name=name,
            description=str(spec.get("description", "")),
            exact_names=tuple(spec.get("names", []) or []),
            prefixes=tuple(spec.get("prefix", []) or []),
        ))
    return result


def channels_for_group(group_name: str, all_channels: list[dict]) -> list[dict]:
    """Filter the channel list down to those matching a group."""
    rules = load_groups()
    rule = next((r for r in rules if r.name == group_name), None)
    if rule is None:
        return []
    return [c for c in all_channels if rule.matches(c.get("name") or "")]
