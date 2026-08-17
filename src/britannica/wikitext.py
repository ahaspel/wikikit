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
