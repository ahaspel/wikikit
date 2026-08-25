"""A module-level name that is assigned and never read again.

    uv run python tools/diagnostics/unread_constants.py            # report
    uv run python tools/diagnostics/unread_constants.py --accept   # rewrite baseline

WHY THIS EXISTS.  On 2026-08-24 `populate_classified_toc.py` was found carrying
seven of them — three `Path(...)` constants naming inputs it no longer read (one
pointing at a file that no longer existed), and four regexes and lookups from an
abandoned hand-marked path.  They were inert, and that was the problem: inert
code answers questions.  The module's docstring, describing the same dead path,
told a reader the classified TOC was built from hand-marked boundaries.  It is
not, and has not been since May.  Nothing failed, no test noticed, and the wrong
story survived three months because the only guard was someone remembering.

WHAT COUNTS AS READ.  A name is read if it is loaded anywhere in its own module,
imported by name from another module (`from x import NAME`), or reached as an
attribute (`mod.NAME`).  That last two matter: `EDITION_DOI` is defined in
`tei.py`, never used there, and imported by `download.py` — live, not dead.

Dunders are exempt (`__all__`, `__version__`): they are read by the interpreter,
not by code.

A RATCHET, not a rule.  The count may fall freely; it may not rise without
someone accepting it deliberately.  The existing entries are a baseline, not an
endorsement.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
ROOTS = [ROOT / "src" / "britannica", ROOT / "tools"]
BASELINE = Path(__file__).with_name("unread_constants_baseline.json")
SKIP_PARTS = {"__pycache__", "_scratch", ".git", "node_modules"}


def _files() -> list[Path]:
    out: list[Path] = []
    for base in ROOTS:
        for p in base.rglob("*.py"):
            if SKIP_PARTS & set(p.parts):
                continue
            out.append(p)
    return sorted(out)


def _module_names(tree: ast.AST) -> dict[str, int]:
    """Module-level assigned names → the line they are assigned on."""
    names: dict[str, int] = {}
    for node in tree.body:                      # TOP LEVEL ONLY
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.target:
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                names.setdefault(t.id, node.lineno)
    return names


def _read_names(tree: ast.AST) -> set[str]:
    """Every name LOADED anywhere in the module, plus attribute tails."""
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            read.add(node.id)
        elif isinstance(node, ast.Attribute):
            read.add(node.attr)
        elif isinstance(node, ast.Global):
            read.update(node.names)
    return read


def collect() -> dict[str, list[str]]:
    files = _files()
    trees: dict[Path, ast.AST] = {}
    for p in files:
        try:
            trees[p] = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

    # Names any OTHER module imports by name or reaches as an attribute.
    external: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                external.update(a.name for a in node.names)
            elif isinstance(node, ast.Attribute):
                external.add(node.attr)

    found: dict[str, list[str]] = {}
    for p, tree in trees.items():
        assigned = _module_names(tree)
        read = _read_names(tree)
        dead = [n for n, _ln in sorted(assigned.items())
                if n not in read
                and n not in external
                and not (n.startswith("__") and n.endswith("__"))]
        if dead:
            found[str(p.relative_to(ROOT)).replace("\\", "/")] = dead
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accept", action="store_true",
                    help="rewrite the baseline with what is on disk now")
    args = ap.parse_args()

    found = collect()
    total = sum(len(v) for v in found.values())

    if args.accept:
        BASELINE.write_text(
            json.dumps(found, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  baseline written: {total} unread constant(s) in {len(found)} file(s)")
        return 0

    print(f"  {total} module-level name(s) assigned and never read, "
          f"in {len(found)} file(s)")
    for f, names in sorted(found.items()):
        print(f"   {f}")
        print(f"      {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
