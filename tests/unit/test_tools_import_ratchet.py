"""Every tool's imports must still resolve — a corpse is not an instrument.

`check_table_path_purity.py` and `layout_wrapper_contents.py` were listed in
`docs/one-true-path-audit.md` under "Standing QA / regression net (no caller but
prized)".  Both had been dead on IMPORT since the chem sub-classification they
audited was deleted: they imported `_chem_row_is_reaction` / `_has_chem_brackets`
from `_tables`, and those names were gone.  Running one printed a traceback
before it printed a finding.  Nobody noticed, because a net with no caller is
only ever run by someone who already suspects something.

That is the third way an instrument lies, after "skips silently" and "reports
less than it was given": it does not run at all, while a document says it does.

Importing each tool to check is impractical — most do their work AT module level,
so "import" means "scan the corpus".  But the failure mode is static: a
`from britannica… import X` naming something that no longer exists.  This checks
every such import in every tool, in about a second.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIRS = ("tools/diagnostics", "tools/qa", "tools/pipeline", "tools/db",
             "tools/vol29", "tools/fetch", "tools/viewer")


def _broken() -> list[str]:
    out: list[str] = []
    for d in TOOL_DIRS:
        for f in sorted((ROOT / d).glob("*.py")):
            rel = f"{d}/{f.name}"
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as e:
                out.append(f"{rel}: does not parse — {e}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith("britannica"):
                    continue
                try:
                    mod = importlib.import_module(node.module)
                except Exception as e:            # noqa: BLE001
                    out.append(f"{rel}: cannot import {node.module} "
                               f"({type(e).__name__})")
                    continue
                for alias in node.names:
                    if alias.name != "*" and not hasattr(mod, alias.name):
                        out.append(f"{rel}: {node.module} has no "
                                   f"{alias.name!r}")
    return out


def test_every_tool_still_resolves_its_imports():
    broken = _broken()
    assert not broken, (
        "these tools import names that no longer exist — they crash before they "
        "can report anything:\n  " + "\n  ".join(broken)
        + "\nFix the import or DELETE the tool; a diagnostic that cannot run is "
          "worse than none, because docs and habit keep counting it as coverage.")
