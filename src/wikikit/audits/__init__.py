"""Code-hygiene audits — duplicated constants and functions, unread names,
patterns that spell out nesting depth.  Engine code, run by EVERY repository
over its own tree: the root is the working directory, and each repository
keeps its own ledgers in tests/ledgers/.  (`python -m wikikit.audits.<name>`)
"""
from __future__ import annotations

import json
from pathlib import Path


def ledger(root: Path, name: str):
    """This repository's ledger for one audit: `tests/ledgers/<name>.json`.

    REQUIRED — a missing ledger raises.  A ratchet whose exceptions are read
    "if present" treats a lost ledger as an empty one, and fails every
    acknowledged entry, or passes a staleness check it can no longer make.
    """
    return json.loads((root / "tests" / "ledgers" / f"{name}.json").read_text(encoding="utf-8"))

#: Directories no audit reads: caches, scratch, vendored JavaScript, the venv.
SKIP_PARTS = frozenset({"__pycache__", "_scratch", "node_modules", ".venv", ".git"})


def audited_files(root: Path, scope=("src", "tools"), exclude=()) -> list[Path]:
    """The Python files an audit reads, in a stable order: every `.py` under
    each `scope` directory of `root`, scope by scope, sorted within each, less
    any file in `exclude` (an audit whose own source holds the very patterns it
    hunts leaves itself out).

    ONE walker for every audit.  There were four, with four skip lists that
    had drifted apart (`.venv` in one, `.git` in another, `tests` in a third) —
    harmless only because none of those directories sat where they looked.
    """
    skip = {Path(p).resolve() for p in exclude}
    out: list[Path] = []
    for scope_dir in scope:
        for p in sorted((root / scope_dir).rglob("*.py")):
            if SKIP_PARTS.isdisjoint(p.parts) and p.resolve() not in skip:
                out.append(p)
    return out
