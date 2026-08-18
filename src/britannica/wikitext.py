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
_PARAM_RE = _re.compile(r"\{\{\{.*?\}\}\}", _re.DOTALL)


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
