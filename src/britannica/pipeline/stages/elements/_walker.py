"""One-level shape-emitting walker.

Linear left-to-right scanner with shape-keyed recognizers.  At every
text position the walker asks "does any recognizer's opener pattern
match here?" — when one does, the recognizer finds its balanced close
and the whole region becomes one extract.  The scanner advances past
the close and continues.

No priority order.  No per-shape passes.  When two recognizers' openers
could match the same position, the one with the more specific opener
(longer match) wins — same semantics any tokenizer uses.  Each
recognizer is a pure function of `(text, position)`.

After the linear scan, an OUTLINE pass runs over the
placeholderized text.  OUTLINE is line-pattern based (indentation
profile), not position-keyed delimiter-balanced, so it operates on
a different alphabet — it runs as a separate phase, the one genuine
exception to "every shape is a recognizer in the linear scan."

Output: ``(text_with_placeholders, [(placeholder, shape, raw), ...])``.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._registry import (
    ElementRegistry, _PH, _next_placeholder_id,
)
from britannica.pipeline.stages.elements._shapes import (
    SHAPE_BRACE_PIPE,
    SHAPE_CHART2,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_OUTLINE,
)


# ── Recognizer patterns ───────────────────────────────────────────────
#
# Each entry is `(shape, pattern)`.  `pattern.match(text, pos)`
# returns the full-extract match starting at `pos`, or None.  The
# scanner tries each recognizer at every "opener hint" position in
# *opener-specificity* order — most-specific opener first.

# CHART2: the multi-template region carved out by today's bespoke
# regex (covers `{{missing table}}`, `{{center|...}}`, `{{EB1911 fine
# print/s}}` wrappers before `{{chart2/start}}…{{chart2/end}}`, plus
# an optional trailing `<poem>` OCR-garbage and `{{EB1911 fine
# print/e}}`).  Most-specific opener — `{{chart2/start` is fully
# unambiguous.
_CHART2_RE = re.compile(
    r"(?:\{\{missing table\}\}\s*(?:\x01PAGE:\d+\x01)?\s*)?"
    r"(?:\{\{center\|[^}]*\}\}\s*)?"
    r"(?:\{\{EB1911 fine print/s\}\}\s*)?"
    r"\{\{chart2/start[^}]*\}\}.*?\{\{chart2/end\}\}"
    r"(?:\s*<poem>.*?</poem>)?"
    r"(?:\s*\{\{EB1911 fine print/e\}\})?",
    re.DOTALL | re.IGNORECASE,
)

# HTML self-closing `<ref ... />` — must match before HTML_TAG's
# `<ref...>...</ref>` because both share the `<ref` opener but only
# self-closing has the `/>` terminator with no `</ref>` to find.
_REF_SELF_RE = re.compile(r"<ref\s[^>]*/\s*>", re.IGNORECASE)

# HTML tag recognizers — each one specific to a single tag name.
# `<ref>` placed AFTER `<ref…/>` (specificity: longer match preferred
# means we try self-closing first; only fall through to the
# `<TAG>…</TAG>` form when self-closing doesn't match).
_REF_RE         = re.compile(r"<ref(?:\s[^>]*)?>.*?</ref>",
                              re.DOTALL | re.IGNORECASE)
_HTML_TABLE_RE  = re.compile(r"<table\b[^>]*>.*?</table>",
                              re.DOTALL | re.IGNORECASE)
_POEM_RE        = re.compile(r"<poem>.*?</poem>",
                              re.DOTALL | re.IGNORECASE)
_MATH_RE        = re.compile(r"<math[^>]*>.*?</math>",
                              re.DOTALL | re.IGNORECASE)
_SCORE_RE       = re.compile(r"<score[^>]*>.*?</score>",
                              re.DOTALL)
_HIEROGLYPH_TAG_RE = re.compile(r"<hiero>[^<]*</hiero>", re.IGNORECASE)

# DOUBLE_BRACE template recognizers — named templates with up to four
# levels of `{{…}}` nesting (CASTLE Fig. 9 cap parameters reach four
# deep).  Specificity: each has a fixed template-name opener like
# `{{img float|` or `{{hieroglyph|`, more specific than bare `{{`.
_IMAGE_FLOAT_RE = re.compile(
    r"\{\{(?:img float|figure|FI)\s*\|"
    r"(?:[^{}]|\{\{(?:[^{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})*\}\})*"
    r"\}\}",
    re.DOTALL | re.IGNORECASE,
)
_HIEROGLYPH_TMPL_RE = re.compile(
    r"\{\{hieroglyph\|([^{}]*)\}\}", re.IGNORECASE)

# DOUBLE_BRACKET image — `[[File:…]]` or `[[Image:…]]` with optional
# trailing caption block (see today's IMAGE regex for the caption
# shapes recognized).
_IMAGE_RE = re.compile(
    r"\[\[(?:File|Image):([^\]]+)\]\]"
    r"(?:\s*\n\n?("
    r"(?:<[a-z]+[^>\n]*>\s*)?"
    r"(?:"
    r"(?:\{\{sm\||\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
    r"|Fig[. ]\d|Plate\s|\d+\.\s*[—–])"
    r"[^\n]*"
    r"(?:\n(?:<[a-z]+[^>\n]*>\s*)?"
    r"(?:\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
    r"|Fig[. ]\d|\d+\.\s*[—–])"
    r"[^\n]*)?"
    r"|"
    r"(?:From|After|Photo|Copyright|Modified)\s[^\n]*"
    r"\n(?:<[a-z]+[^>\n]*>\s*)?"
    r"(?:\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
    r"|Fig[. ]\d|\d+\.\s*[—–])"
    r"[^\n]*"
    r")"
    r"))?",
    re.IGNORECASE,
)


# Recognizer dispatch table in opener-specificity order.  At each
# opener-hint position the linear scanner walks this list and uses
# the first recognizer whose pattern matches.
_REGEX_RECOGNIZERS: list[tuple[str, re.Pattern]] = [
    (SHAPE_CHART2,            _CHART2_RE),
    (SHAPE_HTML_SELF_CLOSING, _REF_SELF_RE),
    (SHAPE_HTML_TAG,          _REF_RE),
    (SHAPE_HTML_TAG,          _HTML_TABLE_RE),
    (SHAPE_HTML_TAG,          _POEM_RE),
    (SHAPE_HTML_TAG,          _MATH_RE),
    (SHAPE_HTML_TAG,          _SCORE_RE),
    (SHAPE_HTML_TAG,          _HIEROGLYPH_TAG_RE),
    (SHAPE_DOUBLE_BRACE,      _IMAGE_FLOAT_RE),
    (SHAPE_DOUBLE_BRACE,      _HIEROGLYPH_TMPL_RE),
    (SHAPE_DOUBLE_BRACKET,    _IMAGE_RE),
]


# Combined opener-hint regex.  Used only for efficiency — `re.search`
# jumps to the next position that could possibly begin an extract,
# so we don't iterate position-by-position over megabytes of prose.
# The actual recognizer dispatch happens via `.match()` at the
# hinted position.  Order doesn't affect correctness (any match
# triggers dispatch); the order below is just readable grouping.
_OPENER_HINT_RE = re.compile(
    r"\{\{chart2/start"             # CHART2
    r"|\{\|"                        # BRACE_PIPE
    r"|<ref\b"                      # HTML_SELF_CLOSING ref / HTML_TAG ref
    r"|<(?:table|poem|math|score|hiero)\b"  # HTML_TAG tag variants
    r"|\[\[(?:File|Image):"         # DOUBLE_BRACKET image
    r"|\{\{(?:img float|figure|FI|hieroglyph)\b",  # DOUBLE_BRACE templates
    re.IGNORECASE,
)


# Spans whose `{|` / `|}` are NOT wiki-table syntax: LaTeX inside
# `<math>`, verbatim `<nowiki>`, HTML comments.  Masked off before the
# balanced-table scanner runs so a stray `\frac{|…}` in math doesn't
# read as a table opener.  See `_NON_TABLE_BRACE_SPAN_RE` in
# `__init__.py` for the same masking against today's extract().
_NON_TABLE_BRACE_SPAN_RE = re.compile(
    r"<math\b[^>]*>.*?</math\s*>"
    r"|<nowiki\b[^>]*>.*?</nowiki\s*>"
    r"|<!--.*?-->",
    re.DOTALL | re.IGNORECASE,
)


def _find_brace_pipe_end(text: str, start: int) -> int | None:
    """Find the byte position one past the balanced ``|}`` (or
    ``</table>``) closing the wikitable that begins at ``start``.

    Returns None if no balanced close exists.

    Mirrors the legacy balanced scanner's depth tracking: ``{{…}}``
    template blocks are skipped wholesale, nested ``{|…|}`` increment
    depth, ``<table>…</table>`` HTML pairs maintain their own depth
    counter so a ``</table>`` inside a nested HTML table doesn't
    masquerade as a wiki-table close.
    """
    masked = _NON_TABLE_BRACE_SPAN_RE.sub(
        lambda m: " " * len(m.group(0)), text)
    if masked[start:start+2] != "{|":
        return None
    depth = 0
    html_depth = 0
    i = start
    n = len(text)
    while i < n - 1:
        if masked[i:i+2] == "{{":
            tdepth = 1
            j = i + 2
            while j < n - 1 and tdepth > 0:
                if masked[j:j+2] == "{{":
                    tdepth += 1
                    j += 2
                elif masked[j:j+2] == "}}":
                    tdepth -= 1
                    j += 2
                else:
                    j += 1
            if tdepth == 0:
                i = j
            else:
                # Malformed `{{` without matching `}}` — treat as
                # literal and keep scanning for the table close.
                i += 2
            continue
        if (i + 7 < n and masked[i:i+6].lower() == "<table"
                and masked[i+6] in (" ", ">", "\t", "\n")):
            html_depth += 1
            j = text.find(">", i)
            i = j + 1 if j >= 0 else i + 6
            continue
        if masked[i:i+8].lower() == "</table>" and html_depth > 0:
            html_depth -= 1
            i += 8
            continue
        if masked[i:i+2] == "{|":
            depth += 1
            i += 2
        elif (masked[i:i+2] == "|}"
              or (masked[i:i+8].lower() == "</table>" and depth > 0)):
            depth -= 1
            closer_len = 2 if masked[i:i+2] == "|}" else 8
            if depth == 0:
                return i + closer_len
            i += closer_len
        else:
            i += 1
    return None


def _new_placeholder() -> str:
    return f"{_PH}ELEM:{_next_placeholder_id()}{_PH}"


def _walk_balanced_shapes(
    text: str,
) -> tuple[str, list[tuple[str, str, str]]]:
    """Single linear pass: find every position where a recognizer's
    opener could match, dispatch in opener-specificity order, take
    the first successful match.  Returns the placeholderized text
    plus the extract tuples in source order.
    """
    extracts: list[tuple[str, str, str]] = []
    output: list[str] = []
    pos = 0
    n = len(text)

    while pos < n:
        hint = _OPENER_HINT_RE.search(text, pos)
        if hint is None:
            output.append(text[pos:])
            break

        opener_pos = hint.start()
        if opener_pos > pos:
            output.append(text[pos:opener_pos])

        # Try every recognizer at this position in specificity order.
        # First successful match wins.
        matched: tuple[int, str, str] | None = None
        for shape, pattern in _REGEX_RECOGNIZERS:
            m = pattern.match(text, opener_pos)
            if m is not None:
                matched = (m.end(), shape, m.group(0))
                break

        # BRACE_PIPE doesn't have a regex pattern — its closer
        # requires balanced depth tracking.  Try it last (lowest
        # specificity) only if no regex recognizer fired.
        if matched is None and text[opener_pos:opener_pos+2] == "{|":
            end = _find_brace_pipe_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_BRACE_PIPE, text[opener_pos:end])

        if matched is None:
            # Opener-hint matched (the bytes LOOK like an opener) but
            # no recognizer fully matched (no closer found, or the
            # full pattern didn't fit).  Advance past one character
            # and continue scanning — same fail-safe as today's
            # regex.sub which silently skips no-match positions.
            output.append(text[opener_pos])
            pos = opener_pos + 1
            continue

        end_pos, shape, raw = matched
        ph = _new_placeholder()
        output.append(ph)
        extracts.append((ph, shape, raw))
        pos = end_pos

    return "".join(output), extracts


def _walk_outline(
    text: str,
) -> tuple[str, list[tuple[str, str, str]]]:
    """Run the OUTLINE line-pattern scanner over `text` (already
    placeholderized for balanced shapes).  Returns the further-
    placeholderized text and the new outline extracts.

    OUTLINE is the one shape that isn't position-keyed delimiter-
    balanced — it operates on indentation profile across lines —
    so it runs as a separate phase rather than as a recognizer in
    the linear scan.
    """
    from britannica.pipeline.stages.elements._outline import _extract_outlines
    # `_extract_outlines` registers into an `ElementRegistry`; we
    # convert its output to the shape-tagged tuple form afterwards.
    bucket = ElementRegistry()
    text = _extract_outlines(text, bucket)
    outline_extracts = [
        (ph, SHAPE_OUTLINE, raw)
        for ph, (_name, raw) in bucket.elements.items()
    ]
    return text, outline_extracts


def walk(
    text: str, _allow_outline: bool = True
) -> tuple[str, list[tuple[str, str, str]]]:
    """One-level shape-emitting walker.

    Linear scan + optional OUTLINE phase.  Returns the
    placeholderized text plus `(placeholder, shape, raw)` tuples.

    ``_allow_outline=False`` is passed when the parent shape is
    OUTLINE so the outline scanner doesn't re-trigger on its own
    bytes.
    """
    text, extracts = _walk_balanced_shapes(text)
    if _allow_outline:
        text, outline_extracts = _walk_outline(text)
        extracts.extend(outline_extracts)
    return text, extracts
