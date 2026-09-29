"""A marker grammar is written in ONE place — the instrument that finds a second.

The recurring defect in this codebase is not a bad implementation — it is a SECOND
implementation. `«LN»` had one producer emitter and six hand-written copies of the
3-part form in the export, each independently deciding which value went in which
slot; that is how a filed catalogue title ends up printed in running prose.  This
scans for marker CONSTRUCTION outside the module that owns the grammar.

It deliberately checks construction, not use. Reading, matching and stripping
markers happens everywhere and should; MINTING one is what belongs to an owner.
The owners are the ENGINE's (they own the grammar), and every repository checks
its own src/ against them: a book minting `«LN:` is as wrong as an engine module.
The ledger is tests/ledgers/marker_emitters.json: marker name -> owning module,
relative to src/.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# A string literal that BUILDS the marker: it opens with `«NAME:` and the literal
# is not the whole, closed token (which would be a pattern or a comparison).
BUILD = re.compile(r"«(?P<name>[A-Z][A-Za-z0-9_]*):(?![^«»]*»\Z)")


def _docstring_ids(tree) -> set[int]:
    """Node ids of every docstring — PROSE about the grammar, not code building it.

    Without this the check fires on `markdown.py`'s policy list and on
    `_link.py`'s own explanation, which is the difference between describing a
    marker and minting one.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def string_literals(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):          # f-string
            parts = "".join(v.value for v in node.values
                            if isinstance(v, ast.Constant)
                            and isinstance(v.value, str))
            if parts:
                yield node.lineno, parts


def offenders(root: Path, name: str, owner: str) -> list[str]:
    """Every literal under root/src that builds `«name:` outside `owner`."""
    src = root / "src"
    out = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel == owner:
            continue
        for lineno, text in string_literals(path):
            for m in BUILD.finditer(text):
                if m.group("name") == name:
                    out.append(f"{rel}:{lineno}  {text[:70]!r}")
    return out
