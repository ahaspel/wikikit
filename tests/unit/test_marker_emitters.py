"""A marker grammar is written in ONE place, and this fails when it isn't.

The recurring defect in this codebase is not a bad implementation — it is a SECOND
implementation. `«LN»` had one producer emitter and six hand-written copies of the
3-part form in the export, each independently deciding which value went in which
slot; that is how a filed catalogue title ends up printed in running prose. The
same shape has appeared as a shadow Markdown parser, `«[^«»]*»` in five modules,
`<[^>]+>` in twenty, two functions named `strip_markers`, and two named
`load_corpus`.

Every one of those was found by a human reading code. This test is the cheap part
of not needing to: it scans for marker CONSTRUCTION outside the module that owns
the grammar, and fails with the offending line.

It deliberately checks construction, not use. Reading, matching and stripping
markers happens everywhere and should; MINTING one is what belongs to an owner.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "britannica"

# marker name -> the module allowed to build it
OWNERS = {
    "LN": "pipeline/stages/elements/_link.py",
}
# A string literal that BUILDS the marker: it opens with `«NAME:` and the literal
# is not the whole, closed token (which would be a pattern or a comparison).
_BUILD = re.compile(r"«(?P<name>[A-Z][A-Za-z0-9_]*):(?![^«»]*»\Z)")


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


def _string_literals(path: Path):
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


@pytest.mark.parametrize("name,owner", sorted(OWNERS.items()))
def test_marker_is_built_only_by_its_owner(name, owner):
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == owner:
            continue
        for lineno, text in _string_literals(path):
            for m in _BUILD.finditer(text):
                if m.group("name") == name:
                    offenders.append(f"{rel}:{lineno}  {text[:70]!r}")
    assert not offenders, (
        f"«{name}» is constructed outside {owner}. A marker grammar written in "
        f"more than one place drifts — that is how «LN»'s target/display order "
        f"came to disagree between the producer and the export. Build it through "
        f"the owner's emitter instead:\n  " + "\n  ".join(offenders))
