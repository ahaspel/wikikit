"""The SOURCE side of the lexicon: raw Wikisource `{{template|…}}` grammar.

Companion to :mod:`britannica.markers`, which owns the marker stream WE emit;
this owns the one the source hands us.  What lives here is what every reader of
the raw text needs and four of them had written for themselves: where a template
ENDS.

Templates nest — EB1911's front matter is full of `{{brace2}}`, `{{sc}}`,
`{{fwn}}`, `{{EB1911 Article Link}}` inside other templates — so a non-greedy
``\\{\\{.*?\\}\\}`` stops at the FIRST inner close and truncates the field.  Every
reader here learned that the expensive way and each learned it separately:
the contributor-table reader's non-greedy capture dropped ~2/3 of all entries
and suppressed thousands of front-matter binds; the footer reader's ``([^}]+)``
truncated Pitcher's ``C. {{sc|Wi}}.`` to ``C.`` and collided him with Crewe; the
plate-title read truncated ``{{x-larger|{{uc|TITLE}}}}`` at the inner brace.
Three identical brace counters, three separately-paid tuitions — and the fourth
copy had drifted: it yielded a garbage slice for an unterminated template where
its own twin yielded nothing.

NOT yet a caller, and deliberately: `elements/_ordered_list._balanced_end` walks
the same braces but returns ``len(text)`` when they never balance — it GUESSES a
close and hands the caller the rest of the document as list content.  Adopting
this module there would change behaviour, so it is a fix to adjudicate on its
own evidence, not a move to make quietly.
"""
from __future__ import annotations

import re as _re
from typing import Iterator as _Iterator

TEMPLATE_OPEN, TEMPLATE_CLOSE = "{{", "}}"

# An HTML comment, which MediaWiki removes BEFORE template expansion — so a `{{`
# or `}}` inside one is not a brace at all.  That is a grammar fact, not a
# cleanup preference, and getting it wrong inverts a verdict: SOCIETIES, LEARNED
# carries `<!--{{EB1911 fine print|-->`, and a brace counter that believed the
# commented-out open paired it with a real `}}` five lines later, declared the
# article balanced, and blamed us for a stray `}}` that Wikisource leaks too.
#
# Three byte-identical copies of this pattern already existed
# (`elements/_classifier`, `super_walker`, and `pipeline/stages/source_cleanup`'s
# newline-preserving variant), and the triage tool was about to be the fourth
# ([[project_duplicated_constant_campaign]]).
COMMENT_RE = _re.compile(r"<!--.*?-->", _re.DOTALL)


def blank_comments(text: str) -> str:
    """``text`` with each comment replaced by spaces of the SAME LENGTH.

    Length-preserving because callers report offsets back into the original —
    a triage finding names the character position a human has to go look at.
    """
    return COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)


_MATH_BODY_RE = _re.compile(r"<math\b.*?</math\s*>", _re.DOTALL | _re.IGNORECASE)

# A template PARAMETER — `{{{name}}}` or `{{{name|default}}}`.  A parameter name
# cannot contain braces, and saying so is what makes this total: the pattern here
# was `\{\{\{.*?\}\}\}` with DOTALL, which took the next `}}}` ANYWHERE in the
# page.  EB1911's mathematics opens plenty of literal braces immediately before a
# template — `{{{Polytonic|γ}}/({{Polytonic|γ}} − 1)}` is a brace, then
# `{{Polytonic|γ}}` — and 21 of the corpus's 42 triple-brace sites are that, not a
# parameter.  Each one blanked a stretch of text up to some distant `}}}`,
# swallowing REAL template braces on the way, and a brace counter reading the
# masked text then saw closes with no opens — the masker manufacturing the very
# unbalance its callers exist to detect.
_PARAM_RE = _re.compile(r"\{\{\{[^{}]*\}\}\}")


def mask_non_template(text: str) -> str:
    """``text`` with every brace that is NOT a template delimiter blanked out.

    Three kinds, all length-preserving so offsets stay usable:
    ``<math>`` bodies (LaTeX `x^{y^{z}}` ends in `}}`), ``{{{param}}}`` (a
    parameter, not a template), and COMMENTS (removed before expansion, so a
    `{{` inside one never existed).

    Anything counting braces in wikitext needs all three, and getting the set
    wrong inverts verdicts rather than degrading them — which is why this is one
    function and not a habit each caller re-forms.
    """
    return _PARAM_RE.sub(
        lambda m: " " * len(m.group(0)),
        _MATH_BODY_RE.sub(lambda m: " " * len(m.group(0)), blank_comments(text)))


def unmatched_closes(text: str, open_pat: str, close_pat: str) -> "list[int]":
    """Offsets of every close with no matching open before it, walked left to right.

    A left-to-right walk, not a tally: an article can carry one unmatched OPEN and
    one unmatched CLOSE that cancel to a balanced count while both are real
    defects.  ``text`` should already be masked by the caller when the delimiters
    are braces.
    """
    events = ([(m.start(), 1) for m in _re.finditer(open_pat, text)] +
              [(m.start(), -1) for m in _re.finditer(close_pat, text)])
    depth, out = 0, []
    for pos, kind in sorted(events):
        if kind == 1:
            depth += 1
        elif depth == 0:
            out.append(pos)
        else:
            depth -= 1
    return out


def template_end(text: str, start: int) -> "int | None":
    """Index one past the ``}}`` balancing the ``{{`` at ``start``.

    ``None`` when it never closes — the caller skips it rather than guessing a
    close, because a template left raw is a visible, reportable leak while a
    guessed close is silently wrong text ([[feedback_honesty_surface_failures]]).
    """
    depth, i, n = 0, start, len(text)
    while i < n:
        if text.startswith(TEMPLATE_OPEN, i):
            depth += 1
            i += 2
        elif text.startswith(TEMPLATE_CLOSE, i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return None


# ── The PAIRED WRAPPER — `{{NAME/s[|arg]}}` … `{{NAME/e[|arg]}}` ────────────
#
# A span the source opens and closes with template halves: the centring family
# (`{{c/s}}`), the print-economy blocks (`{{EB1911 fine print/s}}`), and the
# block indent, whose opener carries a WIDTH — `{{left margin/s|3.2em}}`.
#
# That argument is why this is written here.  Six sites spelled the half — the
# walker's opener, its closer, its depth-counting token, `strip_outer`'s peel,
# the CENTER producer's name read, the unpaired-half producer's — and each had
# to learn independently that an argument may follow the name.  Five of them
# had not, so a wrapper that stated its width failed to match, the opener
# echoed into its own content as text and the closer shipped raw (BIRD's
# taxonomy, ARISTOTLE).  One grammar, composed by the callers that need it
# narrowed to a name or a half ([[feedback_dissolve_dont_fix]]).
_HALF_ARG = r"\s*(?:\|([^{}]*))?\}\}"


def paired_half_pattern(names: str = r"[^{}/|]*?", half: str = "[se]") -> str:
    """Regex SOURCE for a paired half — groups: (name, half, arg-or-None).

    ``names`` is a regex fragment (one escaped name, or an alternation of them);
    ``half`` narrows to the opener (``"s"``) or the closer (``"e"``).
    """
    return r"\{\{\s*(" + names + r")\s*/(" + half + r")" + _HALF_ARG


PAIRED_HALF_RE = _re.compile(paired_half_pattern(), _re.IGNORECASE)


def parse_paired_half(raw: str) -> "tuple[str, str, str] | None":
    """``(name, "s"|"e", arg)`` for a `{{NAME/s|arg}}` half; ``None`` if it is
    not one.  The name is space-collapsed and lowercased — the form every
    registry keys on."""
    m = PAIRED_HALF_RE.match(raw.strip())
    if m is None:
        return None
    return (_re.sub(r"\s+", " ", m.group(1).strip().lower()),
            m.group(2).lower(), (m.group(3) or "").strip())


def first_template_body(text: str, name: str, pos: int = 0) -> "str | None":
    """Body of the first `{{name|…}}` at or after ``pos``; ``None`` if there is
    none or it never closes.

    The brace walk is `template_end`'s, so the body comes back WHOLE at any
    nesting depth — `{{x-larger|{{uc|TITLE}}}}` yields `{{uc|TITLE}}`, where a
    `\\{\\{x-larger\\|([^}]+)\\}\\}` truncates at the first inner brace.  Two
    modules wanted exactly this and each wrote its own: the boundary pass a
    `find("{{name|")` helper, the plate-parent reader a nested regex that named
    `c` and `x-larger` together and so knew only that one pairing.
    """
    opener = _re.compile(r"\{\{\s*" + _re.escape(name) + r"\s*\|", _re.IGNORECASE)
    m = opener.search(text, pos)
    if m is None:
        return None
    end = template_end(text, m.start())
    return None if end is None else text[m.end():end - len(TEMPLATE_CLOSE)]


# THE RUNNING HEAD — `{{rh|…}}` / `{{RunningHeader|…}}` / `{{running header|…}}`
# / `{{EB1911 Page Heading|…}}`, the template every EB1911 page carries at its
# top.  Its slots hold the folio and the article name, so three modules read it —
# boundary detection, folio extraction, plate-parent extraction — and each
# spelled the name set itself.  They did not agree: one knew `{{running header}}`
# and the others did not, which is why vol 18's MEDAL plate lost its number.
PAGE_HEAD_RE = _re.compile(
    r"\{\{\s*(?:rh|running\s*header|eb1911\s+page\s+heading)\s*\|", _re.I)


def page_head_fields(text: str) -> "list[str]":
    """The argument slots of the first running head in ``text``, or ``[]``."""
    for _off, body in iter_template_bodies(text, PAGE_HEAD_RE):
        return split_top_pipes(body)
    return []


def iter_template_bodies(text: str,
                         opening: "_re.Pattern") -> "_Iterator[tuple[int, str]]":
    """``(offset, body)`` for every template whose open matches ``opening``.

    ``opening`` must match from the literal ``{{`` (that is where the brace walk
    starts); ``body`` is everything between the end of that match and the
    balancing ``}}``, so the pattern decides how much of the head — name, first
    pipe — the caller has already consumed.
    """
    for m in opening.finditer(text):
        end = template_end(text, m.start())
        if end is not None:
            yield m.start(), text[m.end():end - len(TEMPLATE_CLOSE)]

_MATH_OPEN_RE = _re.compile(r"<math\b", _re.IGNORECASE)
_MATH_CLOSE = "</math>"


def split_top_pipes(s: str) -> "list[str]":
    """Split ``s`` on `|` at NESTING DEPTH 0 — the one argument split.

    A pipe inside `{{…}}`, `[[…]]`, or `<math>…</math>` is CONTENT, never an
    argument boundary: a nested `{{sc|X}}`'s pipe must not shear the slot list,
    a piped `[[target|display]]` link is one slot, and a LaTeX `\\left|…\\right|`
    (an absolute value, a matrix column) is not a separator either.

    THE OWNER.  This existed twice, and each copy was blind where the other
    could see: `_link._split_top_pipes` tracked brackets but split LaTeX pipes,
    `_dual_line._split_top_level_pipe` held math opaque but ignored brackets.
    Fourteen modules imported one or the other, so which of the two blindnesses
    a caller inherited depended on which import it happened to copy
    ([[feedback_tune_dont_fork]]).  Neither was total; this is the union, and it
    lives in the source-side lexicon so `parsers/` can reach it without
    importing pipeline internals.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = i = 0
    n = len(s)
    while i < n:
        if depth == 0 and s[i] == "<" and _MATH_OPEN_RE.match(s, i):
            end = s.lower().find(_MATH_CLOSE, i)
            if end != -1:
                end += len(_MATH_CLOSE)
                buf.append(s[i:end])        # the whole <math>…</math> rides through
                i = end
                continue
        two = s[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            buf.append(two)
            i += 2
        elif two in ("}}", "]]"):
            depth = max(0, depth - 1)
            buf.append(two)
            i += 2
        elif s[i] == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(s[i])
            i += 1
    parts.append("".join(buf))
    return parts


# The PIPE ESCAPE, in every spelling the source uses.  A bare `|` inside a
# wikitable is a CELL SEPARATOR, so `&vert;` is what an editor writes for a
# literal bar in a cell — exactly what `&lt;` is for a literal `<`.  It must
# therefore survive `preprocess` (decoding it early forges separators) and be
# decoded once the structure is parsed; `elements.process_elements_tree` is that
# point.  Lives HERE beside the rest of the source-side grammar so the entity
# list has one owner ([[feedback_tune_dont_fork]]).
PIPE_ENTITY_RE = _re.compile(
    r"&(?:vert|verbar|VerticalLine|#0*124|#[xX]0*7[cC]);", _re.IGNORECASE)


def decode_pipe_entities(text: str) -> str:
    """Every spelling of an escaped pipe -> a literal `|`."""
    return PIPE_ENTITY_RE.sub("|", text)
