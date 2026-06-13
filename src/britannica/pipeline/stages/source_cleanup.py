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

# `{{word-spacing|<size>|content}}` was stripped here (CSS-only currency-column
# alignment, for the now-retired `unfold_folded_rows`).  It is a STYLER, not chrome:
# it now rides the param-styler registry (`word-spacing:{v}`) in `_tables.py` and is
# carried, not dropped.  Removed so preprocess stops hiding the styler work.


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
# Tolerate a malformed opener (`<noinclude">` — a stray quote is a verified
# source/OCR typo in ~6 articles); it is still the editorial noinclude layer, so
# strip it like the clean form rather than leak the broken tag.
_NOINCLUDE_BLOCK_RE = re.compile(
    r"<noinclude\b[^>]*>.*?</noinclude>", re.DOTALL | re.IGNORECASE
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
