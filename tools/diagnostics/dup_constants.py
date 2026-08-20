#!/usr/bin/env python3
"""Duplicated literal constants — the drift ratchet.

    uv run python tools/diagnostics/dup_constants.py            # report
    uv run python tools/diagnostics/dup_constants.py --check    # fail on a NEW duplicate
    uv run python tools/diagnostics/dup_constants.py --accept   # rewrite the baseline

The recurring defect in this codebase is not a bad implementation, it is a SECOND
implementation: `«[^«»]*»` in five modules, `<[^>]+>` in twenty, `[^a-z0-9]+` in
seventeen (as three different rules), two functions named `strip_markers`, two named
`load_corpus`, six hand-written copies of the `«LN»` grammar. Each was found by a
human reading code, usually after it had already shipped a defect — the `«LN»` copies
had drifted into printing filed catalogue titles in running prose.

Enumerating owners (`tests/unit/test_marker_emitters.py`) catches the grammars
someone thought to list. This catches the ones nobody did: it names every literal
that encodes a RULE and appears in more than one maintained location, and fails when
a literal joins that set. The count can fall freely; it cannot rise without a
deliberate `--accept`.

Scope note: generated pages under tools/viewer are excluded. 87 identical copies of
one boilerplate line across the Reader's Guide is a builder repeating itself, not a
constant anyone can drift.
"""
import argparse
import ast
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ast_shapes import docstring_ids          # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ROOTS = [ROOT / "src" / "britannica", ROOT / "tools"]
SKIP_DIRS = {"__pycache__", "_scratch", "node_modules"}
# Hand-written viewer sources only; the rest of tools/viewer is generated (Phase 6.2).
VIEWER_KEEP = {"viewer.html", "index.html", "contributors.html", "maps.html",
               "scans.html"}
BASELINE = Path(__file__).with_name("dup_constants_baseline.json")

MIN_LEN = 6
# A literal worth tracking encodes a RULE: regex metacharacters, a marker
# delimiter, a brace/bracket construct, or a control sentinel.
INTERESTING = re.compile(r"[«»\\\[\](){}|^$*+?]|\x00|\x01|\x03")
# Prose: a plain sentence or phrase carries no rule.
BORING = re.compile(r"^[A-Za-z][A-Za-z0-9 ,.'’\-]+$")
JS_LITERAL = re.compile(
    r"""(['"`])((?:[^'"`\\\n]|\\.){6,200}?)\1|/((?:[^/\\\n]|\\.){6,200}?)/[gimsuy]*""")


def _annotation_ids(tree) -> set:
    """String TYPE ANNOTATIONS (`-> "tuple[str, str]"`, `x: "Foo | None"`).

    A quoted annotation is type syntax that happens to be a string, not a
    rule-encoding constant: two functions both returning `"tuple[str, str]"`
    share a type, not an implementation, and "give it one owner and import
    it" is not advice anyone can act on.  Twice in one day these were the
    ratchet's only complaint, which is the definition of noise."""
    out = set()
    for node in ast.walk(tree):
        for field in ("returns", "annotation"):
            ann = getattr(node, field, None)
            if ann is not None:
                for sub in ast.walk(ann):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        out.add(id(sub))
    return out


def _fstring_part_ids(tree) -> set:
    """Constant fragments INSIDE an f-string — counted via the skeleton, not alone."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out.add(id(v))
    return out


def _skeleton(node: ast.JoinedStr) -> str | None:
    """An f-string reduced to its SHAPE: constant parts kept, interpolations blanked.

    `f"«LN:{fn}|{target}|{display}«/LN»"` → `«LN:{}|{}|{}«/LN»`.

    Whole-literal comparison cannot see this class at all — the six hand-written
    copies of the `«LN»` 3-part grammar in `export/article_json.py` shared no
    literal, only a shape, and each of their fragments (`«LN:`, `|`, `«/LN»`) is
    below the length floor.  That is the duplication that actually cost us: six
    places independently deciding which value went in which slot.
    """
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        else:
            parts.append("{}")
    sk = "".join(parts)
    return sk if any(isinstance(v, ast.FormattedValue) for v in node.values) else None


def _keep(value: str) -> bool:
    return (MIN_LEN <= len(value) <= 200 and "\n" not in value
            and not BORING.match(value) and bool(INTERESTING.search(value)))


def collect() -> dict[str, list[str]]:
    hits: dict[str, list[str]] = defaultdict(list)
    for root in ROOTS:
        for p in sorted(root.rglob("*.py")):
            if any(s in p.parts for s in SKIP_DIRS):
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            skip = docstring_ids(tree) | _annotation_ids(tree)
            rel = p.relative_to(ROOT).as_posix()
            inner = _fstring_part_ids(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and id(node) not in skip and id(node) not in inner
                        and _keep(node.value)):
                    hits[node.value].append(f"{rel}:{node.lineno}")
                elif isinstance(node, ast.JoinedStr):
                    sk = _skeleton(node)
                    if sk is not None and _keep(sk):
                        hits[sk].append(f"{rel}:{node.lineno}")
    for p in sorted((ROOT / "tools" / "viewer").rglob("*")):
        if p.suffix not in (".js", ".html") or p.name not in VIEWER_KEEP:
            continue
        rel = p.relative_to(ROOT).as_posix()
        for i, line in enumerate(p.read_text(encoding="utf-8",
                                             errors="replace").splitlines(), 1):
            for m in JS_LITERAL.finditer(line):
                v = m.group(2) or m.group(3) or ""
                if _keep(v):
                    hits[v].append(f"{rel}:{i}")
    return {v: sorted(set(locs)) for v, locs in hits.items() if len(set(locs)) > 1}


# Names that are legitimately per-module: a script entry point, or a helper whose
# name is generic enough that sharing it implies nothing.
SYMBOL_IGNORE = {"main"}


def collect_symbols() -> dict[str, list[str]]:
    """Module-level function names defined in 2+ LIBRARY modules.

    Catches the class the literal ratchet cannot see: a second IMPLEMENTATION.
    `_balanced_end` exists three times with three incompatible signatures;
    `parse_img_meta` and `_link_display` twice each. None of those share a literal,
    so nothing textual finds them — but a shadow implementation almost always keeps
    the obvious name, which is the handle this pulls on.

    Library only. `tools/` is full of one-off scripts whose local `diff`/`walk`/
    `classify` helpers share names by coincidence, and a check that cries wolf gets
    switched off.
    """
    names: dict[str, list[str]] = defaultdict(list)
    for p in sorted((ROOT / "src" / "britannica").rglob("*.py")):
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = p.relative_to(ROOT / "src").as_posix()
        for n in tree.body:
            if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name not in SYMBOL_IGNORE):
                names[n.name].append(f"{rel}:{n.lineno}")
    return {k: sorted(v) for k, v in names.items() if len(v) > 1}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if a literal has JOINED the duplicated set")
    ap.add_argument("--accept", action="store_true",
                    help="rewrite the baseline to the current set")
    args = ap.parse_args()
    # Rewrap stdout HERE, not at import: the test imports `collect`, and
    # replacing a captured stdout at module scope closes pytest's buffer.
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

    dups, syms = collect(), collect_symbols()
    if args.accept:
        BASELINE.write_text(json.dumps(
            {"literals": sorted(dups), "symbols": sorted(syms)},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"baseline written: {len(dups)} literals, {len(syms)} symbols")
        return 0

    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    known, known_syms = set(base.get("literals", [])), set(base.get("symbols", []))
    new = sorted(set(dups) - known)
    new_syms = sorted(set(syms) - known_syms)
    gone = sorted(known - set(dups)) + sorted(known_syms - set(syms))

    if args.check:
        for v in new:
            print(f"NEW DUPLICATE  {v!r}")
            for loc in dups[v]:
                print(f"    {loc}")
        for nm in new_syms:
            print(f"NEW SHADOW IMPLEMENTATION  {nm}()")
            for loc in syms[nm]:
                print(f"    {loc}")
        print(f"\nliterals: {len(dups)} (baseline {len(known)}) · "
              f"collided symbols: {len(syms)} (baseline {len(known_syms)}) · "
              f"new {len(new) + len(new_syms)}, retired {len(gone)}")
        if new or new_syms:
            print("\nSomething has become a second implementation. Give it ONE owner "
                  "and import it; if the duplication is genuinely intended, re-run "
                  "with --accept.")
            return 1
        if gone:
            print(f"{len(gone)} retired — run --accept to ratchet the baseline down.")
        return 0

    ranked = sorted(dups.items(), key=lambda kv: -len(kv[1]))
    print(f"duplicated literals: {len(dups)}\n")
    for v, locs in ranked[:25]:
        print(f"  {len(locs):>3} copies  {v!r}")
        for loc in locs[:5]:
            print(f"        {loc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
