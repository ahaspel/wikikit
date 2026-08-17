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
