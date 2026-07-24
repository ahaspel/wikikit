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


# ── noinclude strip ────────────────────────────────────────────────
# Strip `<noinclude>…</noinclude>` blocks whole.  `<noinclude>` means "not
# transcluded": MediaWiki drops the block from mainspace, so the article is
# raw_text MINUS noinclude — including any `{|`/`|}` table delimiters inside
# (a cross-page table's standalone-view close/reopen chrome, and the 2-column
# page-layout tables Wikisource uses to mimic the print).  The mainspace table
# is one continuous `{|`…`|}` span across pages, and the whole-volume balanced
# matcher pairs it correctly.
#
# A keep-the-table-markers rescue lived here until 2026-07-23 (J1 of
# docs/sweeper_removal.md).  It guarded against a table extractor that predated
# the whole-volume stream; the A/B over all 83 affected articles showed the
# plain strip loses ZERO words — while the rescue itself was SWALLOWING whole
# pages (a kept 2-column layout opener wrapped the page's mainspace prose in a
# bogus table whose parse dropped it: LIBRARIES ws 573/584 missing from the
# shipped body, ~25 articles affected) and chopping continuous tables into
# per-page fragments (INDIANS, NORTH AMERICAN 19→10).
# Tolerate a malformed opener (`<noinclude">` — a stray quote is a verified
# source/OCR typo in ~6 articles); it is still the editorial noinclude layer, so
# strip it like the clean form rather than leak the broken tag.
_NOINCLUDE_BLOCK_RE = re.compile(
    r"<noinclude\b[^>]*>.*?</noinclude>", re.DOTALL | re.IGNORECASE
)


def strip_noinclude_blocks(text: str) -> str:
    return _NOINCLUDE_BLOCK_RE.sub("", text)


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
