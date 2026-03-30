"""Build and query an alias table for cross-reference resolution.

Aliases are harvested from the raw wikitext link templates:
- {{EB1911 lkpl|Target|Display}} where Display differs from Target
- {{1911link|Target|Display}} where Display differs from Target

These are human-curated mappings placed by Wikisource editors.
"""

import json
import re
import glob
from collections import defaultdict
from pathlib import Path


RAW_DIRS = [Path("data/raw/wikisource")]


def build_alias_map() -> dict[str, str]:
    """Build a map of alias -> canonical target from raw wikitext files.

    Returns dict mapping uppercased alias to uppercased canonical title.
    When multiple targets exist for an alias, the most common one wins.
    """
    # Collect alias -> list of targets (may have multiple)
    raw_aliases: dict[str, list[str]] = defaultdict(list)

    for raw_dir in RAW_DIRS:
        if not raw_dir.exists():
            continue
        for subdir in sorted(raw_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for path in sorted(subdir.glob("*.json")):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("raw_text", "")
                _extract_aliases_from_wikitext(raw, raw_aliases)

    # Resolve to single target per alias (most frequent)
    alias_map: dict[str, str] = {}
    for alias, targets in raw_aliases.items():
        # Skip noisy aliases
        if len(alias) <= 2:
            continue
        if alias in ("ABOVE", "BELOW", "HERE", "THERE", "FURTHER"):
            continue

        # Pick the most common target
        from collections import Counter
        counts = Counter(targets)
        best_target = counts.most_common(1)[0][0]
        alias_map[alias] = best_target

    return alias_map


def _extract_aliases_from_wikitext(
    raw: str, aliases: dict[str, list[str]]
) -> None:
    """Extract alias mappings from a single page's raw wikitext."""
    # {{EB1911 lkpl|Target|Display}}
    for m in re.finditer(
        r"\{\{(?:EB1911|DNB)\s+lkpl\|([^|}]+)\|([^}]+)\}\}", raw, re.I
    ):
        target = m.group(1).strip().upper()
        display = m.group(2).strip().upper()

        if target == display:
            continue
        if len(display) > 50:
            continue
        # Skip wiki markup fragments
        if any(c in display for c in "'{}|<>"):
            continue

        aliases[display].append(target)

    # {{1911link|Target|Display}}
    for m in re.finditer(
        r"\{\{1911link\|([^|}]+)\|([^}]+)\}\}", raw, re.I
    ):
        target = m.group(1).strip().upper()
        display = m.group(2).strip().upper()

        if target == display:
            continue
        if len(display) > 50:
            continue
        if any(c in display for c in "'{}|<>"):
            continue

        aliases[display].append(target)
