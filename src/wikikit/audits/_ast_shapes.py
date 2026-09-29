"""Shared helpers for the code + element-tree audits.

The audits read CODE, not prose, and every one of them needs the same
distinction to do it: which string literals in a module are DOCSTRINGS.

`dup_constants` needs it so a docstring quoting a value is not counted as a
second definition of it; `fake_recursion_audit` needs it because `wikitext.py`'s
module docstring EXPLAINS the truncating `{{.*?}}` idiom by quoting it, and a
scanner that matched that sentence would accuse the one module that fixed the
problem.  Same question, and it was answered twice — the second copy written
while building an audit whose whole subject is duplicated procedures
([[feedback_tune_dont_fork]]).
"""
from __future__ import annotations

import ast


def docstring_ids(tree: ast.AST) -> set:
    """`id()` of every Constant node that is a docstring.

    Identity rather than value: two docstrings with the same text are different
    nodes, and the caller is filtering nodes it already holds.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def walk_labels(tree, path: str = ""):
    """Yield ``(path, label)`` for every element in a classified-element tree.

    The classifier's output is a tree of `ClassifiedElement`, each with an
    optional `inner_registry` of children; every audit that reports on LABELS
    walks it the same way.  `label_distribution_snapshot` and `table_label_dist`
    each wrote the walk out, identically apart from a local variable name.
    """
    for idx, (_ph, ce) in enumerate(tree.items()):
        node_path = f"{path}/{idx}" if path else str(idx)
        if ce.label:
            yield node_path, ce.label
        if ce.inner_registry:
            yield from walk_labels(ce.inner_registry, node_path)
