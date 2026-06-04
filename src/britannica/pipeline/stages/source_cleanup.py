"""Shared byte-level preparers for article wikitext.

Each function here is a pure ``text -> text`` transformation that
runs before any semantic element producer.  Concerns are byte-level
or transport-level: stripping decorative templates, fixing line
endings, dropping HTML comments, removing source-only markup
(noinclude blocks, section tags) — never anything that BINDS data to
an element.  Element-specific preparation lives with the producer
that needs it (see [[feedback_dont_grow_catchalls]]: the rule is
"shared by multiple producers → preparer; specific to one producer →
part of that producer's module").

These functions migrated from `_transform_text_v2` 2026-05-18 as the
first step of the `_transform_text_v2` decomposition.  Same code,
same order — call sites in `_transform_text_v2` now invoke these by
name rather than inlining the bodies.
"""
from __future__ import annotations

import re

# ── word-spacing strip ─────────────────────────────────────────────
# `{{word-spacing|<size>|content}}` is a CSS-only styling template
# used in FISHERIES vol 10 p448 (and similar) to align currency
# columns.  The wrapper interferes with `unfold_folded_rows` because
# its template-internal `<br>` would otherwise be treated as a real
# row separator, chopping the wrapper open and leaving
# `{{word-spacing|3px|6 7` fragments in cells.  Stripping early keeps
# the unfold pass structurally correct AND avoids per-cell template
# handling downstream.
_WORD_SPACING_RE = re.compile(
    r"\{\{word-spacing\|[^{}|]*\|([^{}]*)\}\}", re.IGNORECASE
)


def strip_word_spacing_templates(text: str) -> str:
    return _WORD_SPACING_RE.sub(r"\1", text)


# ── section tag strip ──────────────────────────────────────────────
# `<section begin="…"/>` and `<section end="…"/>` mark
# Wikisource-side article boundaries.  Boundary detection has already
# consumed them by the time `_transform_text_v2` runs; what remains
# in the joined article text is noise that should not survive into
# rendered output.
_SECTION_TAG_RE = re.compile(
    r'<section\s+(?:begin|end)="[^"]*"\s*/?>', re.IGNORECASE
)


def strip_section_tags(text: str) -> str:
    return _SECTION_TAG_RE.sub("", text)


# ── noinclude strip (table-marker preserving) ──────────────────────
# Strip `<noinclude>…</noinclude>` blocks (page headers, quality
# tags) BUT preserve any `{|` opener or `|}` closer inside them.
# EB1911 pages routinely put a table wrapper opener `{|...` in the
# header noinclude and the `|}` closer in the footer noinclude so
# that the page displays standalone on Wikisource.  Stripping the
# whole block would orphan the table rows; the balanced-table
# extractor later pairs a `{|` on one page with a `|}` many pages
# later, swallowing all intermediate prose (this was silently eating
# Climate / Fauna / Population sections of UNITED STATES, THE).
# `detect_boundaries` applies the same logic at its own preprocess
# step; this is defence in depth.
_NOINCLUDE_BLOCK_RE = re.compile(
    r"<noinclude>.*?</noinclude>", re.DOTALL | re.IGNORECASE
)
_NOINCLUDE_KEEP_OPENER_RE = re.compile(r"(?:^|\n)\s*\{\|[^\n<]*")
_NOINCLUDE_KEEP_CLOSER_RE = re.compile(r"(?:^|\n)\s*\|\}(?!\})")


def _replace_noinclude(m: re.Match) -> str:
    block = m.group(0)
    kept: list[str] = []
    for om in _NOINCLUDE_KEEP_OPENER_RE.finditer(block):
        kept.append(om.group(0).strip())
    if _NOINCLUDE_KEEP_CLOSER_RE.search(block):
        kept.append("|}")
    return ("\n" + "\n".join(kept) + "\n") if kept else ""


def strip_noinclude_blocks(text: str) -> str:
    return _NOINCLUDE_BLOCK_RE.sub(_replace_noinclude, text)


# ── unclosed-template strip ────────────────────────────────────────
# Unclosed `{{name|…` openers (malformed wikitext in sources like
# EGYPT vol 9 p76) confuse cell parsing because their inner `|`
# leaks as a cell separator.  Scan for each known-bad opener; if it
# has no matching `}}`, strip just the opener.  Balanced cases
# (including nested templates) are left alone for `_unwrap_balanced`
# downstream.
#
# Opener list grew from quality-report sweep 2026-05-08 which found
# unclosed instances in HOOD, TANCRED, THEODORE OF MOPSUESTIA,
# SARAVIA, ST LOUIS articles.
_UNCLOSED_TEMPLATES: tuple[str, ...] = (
    "nowrap", "ppoem", "right", "float right", "fine block", "anchor",
)


_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")


def close_unclosed_attr_quotes(text: str) -> str:
    """Repair a malformed tag whose attribute quote was never closed before the
    `>`: `<span style="…;>` → `<span style="…;">` (363 corpus-wide; the missing
    `"` made the figtable DOMParser swallow the rest of the cell — ABBEY Fig. 10).
    A tag with an ODD number of `"` has an unclosed quote; close it just inside
    the `>`.  (A literal `>` inside a quoted value would false-positive, but in
    EB1911 those are `&gt;` — none in the corpus.)"""
    def _fix(m: "re.Match[str]") -> str:
        tag = m.group(0)
        if tag.count('"') % 2 == 1:
            return tag[:-1] + '">'
        return tag
    return _TAG_RE.sub(_fix, text)


def strip_unclosed_templates(text: str) -> str:
    out: list[str] = []
    i = 0
    low = text.lower()
    n = len(text)
    while i < n:
        matched_opener: str | None = None
        for name in _UNCLOSED_TEMPLATES:
            opener_with_pipe = "{{" + name + "|"
            opener_with_space = "{{" + name + " "
            if low[i:i + len(opener_with_pipe)] == opener_with_pipe:
                matched_opener = opener_with_pipe
                break
            # Allow whitespace between name and pipe ("{{right |…"),
            # but only if a pipe follows shortly.
            if low[i:i + len(opener_with_space)] == opener_with_space:
                pipe_idx = text.find("|", i + len(opener_with_space))
                if 0 <= pipe_idx <= i + len(opener_with_space) + 20:
                    matched_opener = text[i:pipe_idx]
                    break
        if matched_opener is None:
            out.append(text[i])
            i += 1
            continue
        # Find matching }} by depth counting.
        depth = 1
        j = i + 2
        matched_close = False
        while j < n - 1:
            if text[j:j + 2] == "{{":
                depth += 1
                j += 2
            elif text[j:j + 2] == "}}":
                depth -= 1
                j += 2
                if depth == 0:
                    matched_close = True
                    break
            else:
                j += 1
        if matched_close:
            out.append(text[i:j])
            i = j
        else:
            pipe_idx = text.find("|", i)
            if pipe_idx >= 0:
                i = pipe_idx + 1
            else:
                i += 2
    return "".join(out)


# ── line-ending normalization ──────────────────────────────────────
def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ── HTML comment strip ─────────────────────────────────────────────
# Strip `<!-- … -->` comments.  Preserve newline(s) adjacent to the
# comment so a line-end comment doesn't glue the next line into the
# current one — TURKEY's railway table had ``</ref><!-- ... -->\n
# |align="center"|815``, and collapsing the trailing `\n` to a space
# joined the next cell's ``|align="..."|`` into the previous cell's
# content, leaking the attribute prefix into body text.  When both
# sides have newlines we keep the longer run, so a comment bracketing
# a real paragraph break (`\n\n<!-- ... -->\n\n`) still leaves the
# paragraph break intact; an inline comment with no adjacent newline
# collapses to a single space as before.
_HTML_COMMENT_RE = re.compile(r"(\n*)<!--.*?-->(\n*)", re.DOTALL)


def _replace_comment(m: re.Match) -> str:
    pre, post = m.group(1) or "", m.group(2) or ""
    if not pre and not post:
        return " "
    return pre if len(pre) >= len(post) else post


def strip_html_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub(_replace_comment, text)
