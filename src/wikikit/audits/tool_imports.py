"""Every tool's imports must still resolve — a corpse is not an instrument.

`check_table_path_purity.py` and `layout_wrapper_contents.py` were listed as
"Standing QA / regression net (no caller but prized)".  Both had been dead on
IMPORT since the chem sub-classification they audited was deleted.  Nobody
noticed, because a net with no caller is only ever run by someone who already
suspects something — the third way an instrument lies, after "skips silently"
and "reports less than it was given".

Importing each tool to check is impractical — most do their work AT module level.
But the failure mode is static: a `from <package> import X` naming something that
no longer exists.  This checks every such import in every tool of the repository
it runs in.  The ledger (tests/ledgers/tool_imports.json) names the tool
directories and the packages whose imports are checked.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path


def broken(root: Path, tool_dirs, packages) -> list[str]:
    out: list[str] = []
    for d in tool_dirs:
        for f in sorted((root / d).glob("*.py")):
            rel = f"{d}/{f.name}"
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as e:
                out.append(f"{rel}: does not parse — {e}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith(tuple(packages)):
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
