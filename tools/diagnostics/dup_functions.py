"""Duplicated FUNCTIONS — the second drift mechanism, beside duplicated literals.

`dup_constants.py` finds one VALUE written in N places; this finds one
PROCEDURE written in N places.  Same disease, bigger blast radius: a literal
drifts when someone edits one copy, but a duplicated function drifts the
moment either copy grows a branch — and the copies are usually far enough
apart that nobody notices they were ever the same.

    uv run python tools/diagnostics/dup_functions.py            # exact clones
    uv run python tools/diagnostics/dup_functions.py --near     # + near-clones
    uv run python tools/diagnostics/dup_functions.py --min 4    # size floor

METHOD.  Each `def` is reduced to a STRUCTURAL fingerprint: the AST dumped
with every identifier, constant, and docstring erased, so two functions match
when they have the same shape and operations regardless of what they call
their variables.  That deliberately ignores names (a rename is not a
difference) and deliberately keeps operators, control flow, and call arity
(those ARE the procedure).

  * EXACT     — identical structure AND identical constants.  One of these is
                dead weight or a missing import, essentially always.
  * NEAR      — identical structure, DIFFERENT constants (`--near`).  Judgment
                required: often one function with a parameter waiting to be
                extracted ([[feedback_tune_dont_fork]]), sometimes two genuinely
                different rules that happen to share a shape.

The floor (`--min`, default 3 statements) keeps one-line accessors and
`__init__` shims out of the report — they are structurally identical by
nature and consolidating them buys nothing.

Scope matches dup_constants: `src/britannica` + `tools`, skipping
`tools/_scratch` (throwaway probes) and `__pycache__`.  Test files are
excluded: two tests SHOULD be able to share a shape.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCOPE = ("src/britannica", "tools")
SKIP_PARTS = ("__pycache__", "_scratch", "tests")


class _Blank(ast.NodeTransformer):
    """Erase identifiers, constants, and docstrings — keep the shape."""

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        return ast.copy_location(
            ast.Attribute(value=node.value, attr="_", ctx=node.ctx), node)

    def visit_arg(self, node):
        return ast.copy_location(ast.arg(arg="_", annotation=None), node)

    def visit_keyword(self, node):
        self.generic_visit(node)
        return ast.copy_location(ast.keyword(arg="_", value=node.value), node)

    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value="_"), node)

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        node.name = "_"
        node.decorator_list = []
        return node


def _strip_docstring(fn: ast.AST) -> list:
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _size(fn: ast.AST) -> int:
    return sum(1 for _ in ast.walk(ast.Module(body=_strip_docstring(fn),
                                              type_ignores=[])))


def _fingerprints(fn: ast.AST) -> "tuple[str, str]":
    """(structure-only, structure+constants) hashes for one function.

    The blanking transformer MUTATES, so it runs on a fresh parse of the
    unparsed body — never on the tree the caller still holds."""
    body = _strip_docstring(fn)
    with_consts = ast.dump(ast.Module(body=body, type_ignores=[]))
    copy = ast.parse(ast.unparse(ast.Module(body=body, type_ignores=[])))
    structure = ast.dump(ast.fix_missing_locations(_Blank().visit(copy)))

    def h(s: str) -> str:
        return hashlib.sha1(s.encode()).hexdigest()[:12]

    return h(structure), h(with_consts)


def _files():
    for scope in SCOPE:
        for p in sorted((ROOT / scope).rglob("*.py")):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            yield p


def collect(min_stmts: int):
    exact = defaultdict(list)     # (structure, consts) -> [(file, line, name)]
    near = defaultdict(list)      # structure -> [...]
    for path in _files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _size(node) < min_stmts * 3:      # ~3 AST nodes per statement
                continue
            try:
                structure, consts = _fingerprints(node)
            except (SyntaxError, RecursionError, ValueError):
                continue
            where = (rel, node.lineno, node.name)
            exact[(structure, consts)].append(where)
            near[structure].append(where)
    return exact, near


def _report(title: str, groups: dict, seen_exact=None) -> int:
    rows = [(len(v), k, v) for k, v in groups.items() if len(v) > 1]
    rows.sort(reverse=True)
    shown = 0
    for n, key, sites in rows:
        if seen_exact is not None and key in seen_exact:
            continue
        names = {name for _f, _l, name in sites}
        shown += 1
        print(f"\n  {n} copies  ({'same name' if len(names) == 1 else '/'.join(sorted(names)[:4])})")
        for f, line, name in sorted(sites):
            print(f"        {f}:{line}  {name}()")
    print(f"\n{title}: {shown} group(s)")
    return shown


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--near", action="store_true",
                    help="also report same-structure/different-constant clones")
    ap.add_argument("--min", type=int, default=3,
                    help="minimum statements (default 3)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    exact, near = collect(args.min)
    _report("EXACT clones (identical structure AND constants)", exact)
    if args.near:
        # a near group whose members are all one exact group is not news
        exact_structs = {k[0] for k, v in exact.items() if len(v) > 1
                         and len({(f, l) for f, l, _n in v}) == len(v)}
        near_only = {k: v for k, v in near.items()
                     if len(v) > 1 and k not in exact_structs}
        _report("NEAR clones (identical structure, different constants)",
                near_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
