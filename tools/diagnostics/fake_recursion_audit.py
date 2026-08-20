"""The scoreboard `canonical_path.md` specified and nobody built.

    uv run python tools/diagnostics/fake_recursion_audit.py [--all] [--json]

FAKE RECURSION is a pattern that recognises NESTED structure by writing the
levels out.  It handles the depths its author typed and fails at the next one —
not loudly, which would be a leak someone reports, but by matching less than it
should and handing back a truncated field, or by not matching and letting the
caller believe there was nothing there ([[feedback_recursion_is_recognition]]).

The campaign named this class at the start — "**16 fake-recursion regexes** ->
one shared balanced scanner", with THIS FILE as the measure and
`fake_recursion_audit = 0` in the proof-of-done — and then the measure was never
written.  So the class ran unmeasured while every class that DID have a
scoreboard (`strip_scan` -> 0, and `_strip_templates` deleted behind it) closed
and stayed closed.  What it cost, in the one week anybody looked: CARNIVORA
rendered seven figures with no legend under any of them, because a caption
pattern spelled two levels of `{{...}}` and EB1911 writes its figure legends
three deep (`{{c|{{Fs|92%|{{sc|Fig}}. 3.-Skull of ''Eupleres goudoti''.}}}}`).
53 figures corpus-wide, silent, for as long as the pattern had been there.

THE SHARED BALANCED SCANNER ALREADY EXISTS: `wikitext.template_end` walks braces
with a counter and has no depth to exceed, and `wikitext.split_top_pipes` does
the argument split the same way.  A finding here is nearly always "call the one
that exists".

WHAT COUNTS, and why the severities differ:

  TRUNCATING  `\\{\\{.*?\\}\\}` — a non-greedy span between literal delimiters.
              Stops at the FIRST inner close, so a nested field comes back CUT.
              This is the one that lies quietly: the caller gets a string, uses
              it, and nothing anywhere reports a short read.  `wikitext`'s own
              docstring records four readers that each learned it the expensive
              way — 2/3 of contributor entries dropped, Pitcher's `C. {{sc|Wi}}.`
              truncated to `C.` and collided with Crewe.

  ENUMERATED  `(?:[^{}]|\\{\\{(?:[^{}]|\\{\\{[^{}]*\\}\\})*\\}\\})*` — emulated
              balance: level 1 and level 2 written by hand.  Beyond the last
              level written it matches nothing, which reads to the caller as
              "absent" rather than "too deep".  The CARNIVORA caption, and the
              walker's own `# template (<=3 deep)`.

  FIXED_SHAPE Two literal opens matching one KNOWN composite
              (`{{c|{{x-larger|...}}}}`).  Reported, not gated: it does not
              truncate — it simply fails to match, and a template that fails to
              match leaks visibly instead of silently
              ([[feedback_honesty_surface_failures]]).  Still worth converting,
              but it is not the defect this gate exists to stop, and a gate that
              cries about everything gets ignored — which is exactly how the
              EPUB's missing-image line got ignored.

              ALTERNATION IS NOT NESTING, and the two are separated exactly
              rather than by eye: `_alternatives` splits a pattern on its own
              top-level `|` and judges each branch alone, so a marker scanner
              listing TABLE, VERSE and IMG shapes side by side is three shapes and
              not one nested one.  Counting opens without that split reported ten
              nested patterns on this tool's first run, of which three were real
              ([[feedback_verify_the_counter]]).

Detection reads CODE, never prose: `wikitext.py`'s module docstring EXPLAINS the
truncating idiom by quoting it, and a scanner that matched that sentence would
accuse the one module that fixed the problem.  String literals stay — the
pattern IS a string literal — so only docstrings are excluded.

Acknowledge a finding in `data/fake_recursion_exceptions.json` as
``{"<path>::<sha8-of-pattern>": "<why it stays>"}``.  The key carries a hash of
the pattern rather than a line number, so moving code keeps its acknowledgement
and EDITING the pattern revokes it — a changed pattern is a new decision.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
SCOPE = ("src/britannica", "tools")
SKIP_PARTS = {"__pycache__", ".venv", "_scratch", "node_modules"}
EXCEPTIONS = ROOT / "data" / "fake_recursion_exceptions.json"

# This file quotes every idiom it hunts for; scanning itself would report each
# detector as a finding.
SELF = Path(__file__).resolve()

BRACE_OPEN, BRACE_CLOSE = r"\{\{", r"\}\}"
BRACK_OPEN, BRACK_CLOSE = r"\[\[", r"\]\]"
NONGREEDY = ".*?"


def _alternatives(pat: str) -> "list[str]":
    """A regex split on its TOP-LEVEL `|` — each branch judged on its own.

    `\\{\\{TABLE…\\}TABLE\\}|\\{\\{VERSE…\\}VERSE\\}|\\{\\{IMG:[^}]*\\}\\}` names three
    marker shapes SIDE BY SIDE; nothing is inside anything.  Judging the whole
    pattern called that nested, because one branch supplied an opener and a later
    branch supplied the closer.  Alternation is the regex's own `|`, so splitting
    on it is the exact answer, not a heuristic — and it is the same "split at
    depth 0" the pipeline uses on template arguments.
    """
    parts, buf, depth, i, n = [], [], 0, 0, len(pat)
    in_class = False
    while i < n:
        c = pat[i]
        if c == "\\" and i + 1 < n:
            buf.append(pat[i:i + 2])
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _classify(pat: str) -> str | None:
    """-> "TRUNCATING" | "ENUMERATED" | "FIXED_SHAPE" | None for one pattern."""
    verdicts = [v for branch in _alternatives(pat)
                if (v := _classify_branch(branch)) is not None]
    for kind in ("TRUNCATING", "ENUMERATED", "FIXED_SHAPE"):
        if kind in verdicts:
            return kind
    return None


def _classify_branch(pat: str) -> str | None:
    """The same question for ONE alternation branch."""
    for opener, closer, negated in ((BRACE_OPEN, BRACE_CLOSE, "[^{}]"),
                                    (BRACK_OPEN, BRACK_CLOSE, "[^")):
        opens = pat.count(opener)
        if not opens:
            continue
        # A non-greedy span BETWEEN the delimiters — cut at the first inner close.
        i = pat.find(opener)
        j = pat.find(closer, i + len(opener))
        if j != -1 and NONGREEDY in pat[i:j]:
            return "TRUNCATING"
        if opens >= 2:
            # TWO OPENS IS NOT NESTING.  `\{\{TABLE…\}\}|\{\{VERSE…\}\}` is an
            # ALTERNATION — two shapes side by side, neither inside the other —
            # and counting opens alone called ten of those nested on this tool's
            # first run.  The second open has to fall INSIDE the first one's span
            # to be a level; if it comes after the first close, they are siblings
            # ([[feedback_verify_the_counter]]).
            # A nested SHAPE has to be complete: open, inner open, close.  A
            # pattern with no closer at all is naming OPENERS — `«TITLE|\{\{|\{\|`
            # is the walker's list of things that start something, not a level.
            second = pat.find(opener, i + len(opener))
            if j == -1 or second > j:
                continue
            # Emulated balance always pairs the nested open with a negated class;
            # a fixed shape names real template text between its braces instead.
            return "ENUMERATED" if negated in pat else "FIXED_SHAPE"
    return None


# ── the same disease, written in code instead of in a pattern ───────────────
#
# A regex is not the only way to name a depth.  `for _ in range(5):` around an
# unwrap is a depth cap with a number in it — it handles five levels and returns
# the sixth still wrapped, silently — and a hand-rolled `depth += 1` brace walk is
# `template_end` re-derived by someone who did not know it existed, with its own
# behaviour at the edges (`_ordered_list._balanced_end` returns `len(text)` when
# the braces never balance, which hands the caller the rest of the document as
# list content).
#
# This half was missing when the audit first ran, so it reported ZERO while
# `plate_parent._strip_formatting` carried both — a `range(5)` cap AND its own
# walk — three functions below the pattern it had just cleared.  A scoreboard
# that only sees one spelling of a class certifies the other one clean.
_DEPTH_NAMES = {"depth", "level", "nesting", "nest", "d"}
_BRACE_TOKENS = {"{{", "}}", "[[", "]]"}
_MUTATORS = {"sub", "subn", "replace"}


def _bounded_iteration(fn: ast.AST) -> "tuple[int, int] | None":
    """`(lineno, N)` for a `for _ in range(N)` loop that rewrites a string."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if not (isinstance(it, ast.Call) and getattr(it.func, "id", "") == "range"
                and len(it.args) == 1 and isinstance(it.args[0], ast.Constant)
                and isinstance(it.args[0].value, int)):
            continue
        # THE LOOP VARIABLE MUST BE UNUSED.  `for _ in range(5)` repeats a
        # transform — the 5 is a depth.  `for i in range(9)` that USES `i` is
        # iterating over values, and `find_quality_strays` does exactly that over
        # control characters; flagging it called an ordinary loop fake recursion
        # ([[feedback_verify_the_counter]]).
        target = getattr(node.target, "id", None)
        if target is None or any(isinstance(u, ast.Name) and u.id == target
                                 and isinstance(u.ctx, ast.Load)
                                 for u in ast.walk(node)):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and getattr(inner.func, "attr", "") in _MUTATORS):
                return node.lineno, it.args[0].value
    return None


def _hand_walk(fn: ast.AST) -> "int | None":
    """Lineno of a hand-rolled brace walk — a depth counter beside `{{`/`[[`."""
    counts = any(isinstance(n, ast.AugAssign)
                 and isinstance(n.op, (ast.Add, ast.Sub))
                 and getattr(n.target, "id", "") in _DEPTH_NAMES
                 for n in ast.walk(fn))
    if not counts:
        return None
    for n in ast.walk(fn):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value in _BRACE_TOKENS):
            return fn.lineno
    return None


def _docstring_ids(tree: ast.AST) -> set[int]:
    """`id()` of every Constant that is a docstring — prose, not a pattern."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _files():
    for scope in SCOPE:
        for p in sorted((ROOT / scope).rglob("*.py")):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            if p.resolve() == SELF:
                continue
            yield p


def audit() -> list[dict]:
    """Every depth-enumerating pattern in scope, worst kind first."""
    findings = []
    for path in _files():
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError) as exc:
            # A file we cannot read is a FINDING, never a silent skip: that is
            # how a scanner reports clean on the module it failed to open.
            findings.append({"path": str(path.relative_to(ROOT)).replace("\\", "/"),
                             "line": 0, "kind": "UNREADABLE",
                             "pattern": f"{type(exc).__name__}: {exc}"})
            continue
        skip = _docstring_ids(tree)
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            bounded = _bounded_iteration(fn)
            if bounded:
                line, n = bounded
                findings.append({"path": rel, "line": line, "kind": "BOUNDED_ITERATION",
                                 "pattern": f"{fn.name}(): for _ in range({n}) around a "
                                            f"string rewrite"})
            walk_line = _hand_walk(fn)
            if walk_line is not None:
                findings.append({"path": rel, "line": walk_line, "kind": "HAND_WALK",
                                 "pattern": f"{fn.name}(): own brace walk with a depth "
                                            f"counter"})
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Constant)
                    or not isinstance(node.value, str) or id(node) in skip):
                continue
            kind = _classify(node.value)
            if kind:
                findings.append({
                    "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "line": node.lineno, "kind": kind, "pattern": node.value})
    order = {"UNREADABLE": 0, "TRUNCATING": 1, "ENUMERATED": 2,
         "BOUNDED_ITERATION": 3, "HAND_WALK": 4, "FIXED_SHAPE": 5}
    findings.sort(key=lambda f: (order[f["kind"]], f["path"], f["line"]))
    return findings


def key_for(f: dict) -> str:
    """`path::sha8` — survives a move, revoked by an edit to the pattern."""
    return f"{f['path']}::{hashlib.sha1(f['pattern'].encode()).hexdigest()[:8]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="also list acknowledged findings and FIXED_SHAPE")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    findings = audit()
    allowed = json.loads(EXCEPTIONS.read_text(encoding="utf-8")) if EXCEPTIONS.exists() else {}
    for f in findings:
        f["key"] = key_for(f)
        f["acknowledged"] = f["key"] in allowed

    if args.json:
        print(json.dumps(findings, indent=2))
        return 0

    gated = [f for f in findings
             if f["kind"] in ("TRUNCATING", "ENUMERATED", "UNREADABLE",
                              "BOUNDED_ITERATION", "HAND_WALK")
             and not f["acknowledged"]]
    shape = [f for f in findings if f["kind"] == "FIXED_SHAPE"]
    ack = [f for f in findings if f["acknowledged"]]

    print(f"  scanned {sum(1 for _ in _files())} files in {' + '.join(SCOPE)}")
    if ack:
        print(f"  {len(ack)} acknowledged in {EXCEPTIONS.name}")
    if shape and (args.all or not gated):
        print(f"\n  {len(shape)} FIXED_SHAPE (reported, not gated — fails to match "
              f"rather than truncating):")
        for f in shape:
            print(f"    {f['path']}:{f['line']}")
            print(f"      {f['pattern'][:96]}")

    if not gated:
        print("\n  OK: no pattern enumerates nesting depth.")
        return 0

    print(f"\n  {len(gated)} pattern(s) enumerate nesting depth:")
    for f in gated:
        print(f"\n    {f['kind']}  {f['path']}:{f['line']}")
        print(f"      {f['pattern'][:110]}")
        print(f"      key: {f['key']}")
    print("\n  Fix: call wikitext.template_end() / wikitext.split_top_pipes() — a "
          "brace-balanced\n       walk has no depth to exceed.  Or acknowledge it in "
          f"{EXCEPTIONS.name}\n       as {{\"<key>\": \"<why it stays>\"}}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
