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
    SHAPE_BODY,
    SHAPE_BRACE_PIPE,
    SHAPE_PAIRED_WRAPPER,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_INLINE_IMAGE,
    SHAPE_OUTLINE,
    SHAPE_PAGE,
    SHAPE_TITLE,
)
# (The `_figure` helpers and the `_tables` styler/heading openers are no longer
#  imported here: the figure recognizer was removed earlier, and with the ONE
#  generic `{{…}}` recognizer the opener-hint regex no longer enumerates per-name
#  styler openers — a bare `\{\{` hint covers every double-brace template, the
#  classifier carves the STRIP / PARAM / SHOULDER / RUNNING_HEADER label off the raw.)


# ── Recognizer patterns ───────────────────────────────────────────────
#
# Each entry is `(shape, pattern)`.  `pattern.match(text, pos)`
# returns the full-extract match starting at `pos`, or None.  The
# scanner tries each recognizer at every "opener hint" position in
# *opener-specificity* order — most-specific opener first.

# GENEALOGY: the multi-template region for a chart2 / familytree / tree-chart
# grid macro (covers `{{missing table}}`, `{{center|...}}`, `{{EB1911 fine
# print/s}}` wrappers before the `…/start`…`…/end` block, plus an optional
# trailing `<poem>` OCR-garbage and `{{EB1911 fine print/e}}`).  Most-specific
# opener — the `…/start` of the three families is fully unambiguous.
_GENEALOGY_RE = re.compile(
    r"(?:\{\{missing table\}\}\s*(?:\x01PAGE:\d+\x01)?\s*)?"
    r"(?:\{\{center\|[^}]*\}\}\s*)?"
    r"(?:\{\{EB1911 fine print/s\}\}\s*)?"
    r"\{\{(?:chart2|familytree|tree\s*chart)/start[^}]*\}\}.*?"
    r"\{\{(?:chart2|familytree|tree\s*chart)/end\}\}"
    r"(?:\s*<poem>.*?</poem>)?"
    r"(?:\s*\{\{EB1911 fine print/e\}\})?",
    re.DOTALL | re.IGNORECASE,
)

# HTML self-closing `<ref ... />` — must match before HTML_TAG's
# `<ref...>...</ref>` because both share the `<ref` opener but only
# self-closing has the `/>` terminator with no `</ref>` to find.
_REF_SELF_RE = re.compile(r"<ref\s[^>]*/\s*>", re.IGNORECASE)

# Wikisource `<pagequality level="N" user="X" />` — page-quality metadata,
# previously inside `<noinclude>` and swept by NOINCLUDE.  With noinclude
# tags wiped upstream, this self-closing tag sits naked in body raw.
# Lifted as its own HTML_SELF_CLOSING element; producer returns "".
_PAGEQUALITY_RE = re.compile(
    r"<pagequality\b[^>]*/\s*>", re.IGNORECASE)

# `<ref>`/`<table>`/`<poem>`/`<math>`/`<score>` no longer have per-tag
# recognizer regexes — they are bounded by the one balanced `_construct_end`
# rule (see `_ELEMENT_TAGS`).  The old `<tag\b[^>]*>.*?</tag>` non-greedy
# forms each embedded a false no-nesting assumption and have been deleted.
# `<hiero>` is now an OPAQUE `_ELEMENT_TAGS` tag (verbatim glyph-code interior,
# `</hiero>`-bounded) like `<math>`/`<score>`.  Its old `<hiero>[^<]*</hiero>`
# regex assumed no internal `<` and so dropped any block with one — 298 raw
# leaks the viewer's `[hieroglyph:…]` decoder never saw.

# Wikisource transclusion marker `<section begin="X"/>` / `<section end/>`.
# A self-closing structural tag carrying boundary identity (the name), no inner
# content.  Recognized so it becomes an owned SECTION element carried raw, rather
# than being swept by the text producer's catch-all HTML strip.
_SECTION_RE = re.compile(r"<section\s+(?:begin|end)\b[^>]*>", re.IGNORECASE)

# `<noinclude>` tags are stripped upstream in `_transform_text_v2`
# before the article walker runs — see the comment there.  The article
# pipeline therefore never sees them.  The plate pipeline still uses
# noinclude as a structural anchor for plate-title extraction and is
# unaffected (it has its own walker).

# Wikisource `<span style="…{{mirrorH}}…">content</span>` — a horizontally
# mirrored glyph (used in ALPHABET to show left-right-flipped letters of
# early/regional alphabets like Etruscan, Italic, Cleonae's reversed E).
# Recognized as its own shape so the mirror SEMANTIC survives end-to-end
# (catch-all stripping silently lost the styling, leaving glyphs displayed
# un-mirrored).  The producer emits `«MIRROR:content«/MIRROR»`; the viewer
# applies `transform: scaleX(-1)`.
_MIRROR_GLYPH_RE = re.compile(
    r'<span\s+style\s*=\s*"[^"]*\{\{mirrorH\}\}[^"]*">(?:[^<]|<(?!/span>))*?</span>',
    re.IGNORECASE | re.DOTALL,
)


# DOUBLE_BRACE templates no longer have per-name opener regexes — the ONE generic
# `{{…}}` recognizer in `_walk_balanced_shapes` bounds EVERY double-brace as a unit
# (via `_construct_end`, the one balanced matcher), and the classifier routes by
# name.  The former dozen openers (img float / footer / section anchor / link
# families / coordinates / cite / spacer / content-extract / dual line / fraction /
# lb- / sub-sup / ppoem / labeled-equation / ordered-list / Css crop / raw image /
# plain image) plus `_find_balanced_template_end` are gone — `_construct_end`
# already skips an opaque `<math>` interior whole, so it subsumes the LaTeX-brace
# scanner the labeled-equation / ordered-list blocks used.

# `<span title="T">X</span>` — a transliteration TOOLTIP when X is Greek/Hebrew (T is the
# romanization, carried as «SPAN[title:T]» — the HTML twin of the {{tooltip}} template);
# any other title= is editorial provenance, dropped (content kept).  Re-promotes the
# gutted body-text `_handle_title_spans`; `process_span_title` applies the carry/drop split.
# Still a per-shape opener: a styled `<span title=>` is HTML_TAG, NOT DOUBLE_BRACE, so it
# can't ride the generic `{{…}}` recognizer — it keeps its own opener block.
_SPAN_TITLE_OPEN_RE = re.compile(
    r'<span\b[^>]*?\btitle\s*=\s*(?:"(?P<q>[^"]*)"|(?P<uq>[^\s">]+))[^>]*>',
    re.IGNORECASE)
# Greek/Hebrew transliteration-content signal — used by the SPAN_TITLE producer to
# decide carry-vs-drop (imported by the producer in `__init__.py`).
_TRANSLIT_CONTENT_RE = re.compile(
    r"\{\{\s*(?:Greek|Hebrew|polytonic)|[Ͱ-Ͽἀ-῿֐-׿]", re.IGNORECASE)

# DOUBLE_BRACKET image — `[[File:…]]` / `[[Image:…]]`, a pure LEAF.  No caption
# absorption: a following caption block is its own sibling, recursed in place
# (image = leaf, caption = sibling — there is no figure unit to fold into).
# Inline-vs-block is decided structurally by `_is_inline_image_position` at
# dispatch; the walker advances past `]]` and surrounding bytes stay intact.
_IMAGE_RE = re.compile(
    r"\[\[(?:File|Image):[^\]]+\]\]", re.IGNORECASE)

# DOUBLE_BRACKET generic wiki cross-reference — any `[[Target]]` / `[[Target|Disp]]`
# NOT claimed by the specific recognizers (File/Image, SELFREF, Author, `#`).  LAST in
# the bracket recognizer order so it's the catch for plain links; classified WIKILINK,
# produced as «LN», resolved by the internal → external → strip ladder.
_GENERIC_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")

# DOUBLE_BRACKET self-reference — `[[1911 Encyclopædia Britannica/Article#Sec|Disp]]`,
# an internal EB1911 cross-link in raw bracket form (vs the `{{EB1911 article link}}`
# template form).  Recognized here, classified EB1911_SELFREF, produced as «LN».  The
# `1911 Encyclop` prefix is the signal — EB9 "Ninth Edition" refs do NOT match (those
# are external Wikisource links, handled by the external-link producer).
_EB1911_SELFREF_RE = re.compile(
    r"\[\[\s*1911\s+[Ee]ncyclop[^\]]*\]\]", re.IGNORECASE)

# DOUBLE_BRACKET bare anchor link — `[[#Section]]` / `[[#Section|Display]]`, a
# same-article subsection reference.  Recognized here, classified FRAGMENT_LINK,
# produced as «LN:#Section».  The leading `#` (no `prefix:`) is the signal.
_FRAGMENT_LINK_RE = re.compile(r"\[\[\s*#[^\]]*\]\]")

# DOUBLE_BRACKET Author link — `[[Author:Name|Display]]`.  Recognized here,
# classified AUTHOR_LINK, routed by the producer: a contributor's initials
# (in the index) → render the initials; else → «LN» xref to the author.
_AUTHOR_RE = re.compile(
    r"\[\[\s*Author:[^\]]*\]\]", re.IGNORECASE)


# Inline-image structural recognition.  At the moment the walker has just
# matched an `[[File:…]]` (no EXTCAP), it checks the text immediately AFTER
# `]]` for an inline-glyph signal: same-line content that ISN'T a line-ender
# (`\n` / `<br>`) and ISN'T a wikitable cell separator (`|`).  No bytes
# consumed; the placeholderized text keeps its surrounding context.  When the
# signal is present the walker emits SHAPE_INLINE_IMAGE instead of
# SHAPE_DOUBLE_BRACKET; the classifier maps that shape to its own label and
# the dedicated producer stamps `align=inline`.
_BR_TAG_RE = re.compile(r"<br\s*/?\s*>", re.IGNORECASE)


def _is_inline_image_position(text: str, pos: int) -> bool:
    """True iff position ``pos`` (right after a matched `]]`) sits in an
    inline-prose context — same-line non-structural content follows.

    Structural separators / closers are NOT inline; they indicate the
    image sits at a container boundary where the container owns layout:
      * line-ender ``\\n`` or ``<br>`` — paragraph / line break
      * wikitable cell pipe ``|`` (``|`` or ``||`` or ``|-``)
      * template close ``}`` (``}}`` of a wrapper template)
      * template open ``{`` (``{{brace2|…}}`` decoration, ``{{Ts|…}}``
        cell styling — non-prose; an inline content template would be
        inside a body sentence and the image would have at least one
        separating character, e.g. punctuation or space-then-alpha)
      * HTML close tag ``</…>`` (``</td>``, ``</tr>``, ``</span>``, …)
        — the image is the LAST thing in its enclosing HTML element.
    Inline-element OPEN tags (``<ref>``, ``<sub>``, etc.) are NOT
    structural separators here — same-line elements after an image
    are part of the inline flow.
    """
    end = pos
    n = len(text)
    while end < n and text[end] in " \t":
        end += 1
    if end >= n:
        return False
    nxt = text[end]
    if nxt == "\n" or nxt == "|" or nxt == "}" or nxt == "{":
        return False
    if nxt == "<":
        if _BR_TAG_RE.match(text, end):
            return False
        if end + 1 < n and text[end + 1] == "/":
            return False
    return True


# PAGE — the injected page-break marker (\x01PAGE:N\x01), recognized as a leaf
# so it rides the tree as a child element instead of a raw sentinel the outline
# scanner has to reach around; the producer re-emits the raw marker, and the
# export reads it off the tree.
_PAGE_RE = re.compile(r"\x01PAGE:\d+\x01")

# TITLE — the «TITLE»…«/TITLE» stamp injected by `preprocess_article` around the carved
# title span, so the title rides the ONE walk (recursed into the title node) instead of
# a re-walked side field.  Non-greedy: a title never nests another «TITLE».
_TITLE_RE = re.compile(r"«TITLE».*?«/TITLE»", re.DOTALL)


# Recognizer dispatch table in opener-specificity order.  At each
# opener-hint position the linear scanner walks this list and uses
# the first recognizer whose pattern matches.
_REGEX_RECOGNIZERS: list[tuple[str, re.Pattern]] = [
    # (Genealogy `…/start…/end` is bounded in a dedicated block above the generic
    #  `{{…}}` recognizer — it can't ride this loop, which runs AFTER the generic.)
    # `<section begin/end/>` — a self-closing structural transclusion marker.
    # Carved by the generic HTML_SELF_CLOSING shape; the classifier routes the
    # `section` tag name → SECTION label (its producer reads the raw tag).
    (SHAPE_HTML_SELF_CLOSING, _SECTION_RE),
    # `<span style="…{{mirrorH}}…">…</span>` — a horizontally-mirrored glyph span.
    # Carved by the generic HTML_TAG shape (a balanced `<span>…</span>`); the
    # classifier routes a `mirrorH`-styled span → MIRROR_GLYPH label.
    (SHAPE_HTML_TAG,          _MIRROR_GLYPH_RE),
    (SHAPE_HTML_SELF_CLOSING, _REF_SELF_RE),
    (SHAPE_HTML_SELF_CLOSING, _PAGEQUALITY_RE),
    # `<table>`/`<ref>`/`<poem>`/`<math>`/`<score>` are bounded by the one
    # balanced `_construct_end` rule (see the `_ELEMENT_TAGS` handler in
    # `_walk_balanced_shapes`), NOT by per-tag non-greedy regexes — those
    # embedded a false no-nesting assumption (table-in-table orphaned the
    # outer tail into body-text).
    # (`<hiero>` now rides the `_ELEMENT_TAGS` opaque path (no per-tag regex),
    #  and `{{hieroglyph|…}}` the generic `{{…}}` recognizer; both → HIEROGLYPH.)
    (SHAPE_DOUBLE_BRACKET,    _IMAGE_RE),
    (SHAPE_DOUBLE_BRACKET,    _EB1911_SELFREF_RE),
    (SHAPE_DOUBLE_BRACKET,    _AUTHOR_RE),
    (SHAPE_DOUBLE_BRACKET,    _FRAGMENT_LINK_RE),
    (SHAPE_DOUBLE_BRACKET,    _GENERIC_WIKILINK_RE),  # catch-all bracket link — LAST
    (SHAPE_PAGE,              _PAGE_RE),
    (SHAPE_TITLE,             _TITLE_RE),
]


# Combined opener-hint regex.  Used only for efficiency — `re.search`
# jumps to the next position that could possibly begin an extract,
# so we don't iterate position-by-position over megabytes of prose.
# The actual recognizer dispatch happens via `.match()` at the
# hinted position.  Order doesn't affect correctness (any match
# triggers dispatch); the order below is just readable grouping.
_OPENER_HINT_RE = re.compile(
    r"\x01PAGE:"                    # PAGE break bookkeeping marker
    r"|«TITLE"                      # TITLE stamp (preprocess_article)
    r"|\{\{"                        # ANY `{{…}}` — the ONE generic DOUBLE_BRACE
                                    #   recognizer (and the PAIRED_WRAPPER / chart2
                                    #   recognizers ahead of it) own every double-brace
                                    #   template; no per-name hint is needed any more.
    r"|\{\|"                        # BRACE_PIPE
    r"|<ref\b"                      # HTML_SELF_CLOSING ref / HTML_TAG ref
    r"|<pagequality\b"              # HTML_SELF_CLOSING pagequality metadata
    r"|<section\s+(?:begin|end)\b"  # SECTION transclusion marker
    r"|<(?:table|poem|math|score|hiero|nowiki|includeonly)\b"  # HTML_TAG tag variants
    r"|<span\s+style\s*=\s*\"[^\"]*\{\{mirrorH"  # MIRROR_GLYPH span
    r"|<(?:span|div)\b[^>]*\bfloat\s*:"  # FIGURE HTML float-wrapper
    r"|<div\b"  # any <div> — styled ones lift to STYLED, bare ones fall through
    r"|<p\b"    # any <p> — styled ones lift to STYLED, bare/OCR ones fall through
    r"|<ins\b"  # any <ins> — Wikisource insertion, lifted UNGATED (always a styler)
    r"|<span\b[^>]*(?:\{\{\s*[Tt]s\b|style\s*=|align\s*=|title\s*=)"  # STYLED / transliteration-title <span>
    r"|\[\[",                       # DOUBLE_BRACKET — any wikilink; the recognizer table dispatches by kind (File/Author/SELFREF/#/generic)
    re.IGNORECASE,
)


# ── The one balanced-span matcher (syntactic; shape-blind) ──────────────
#
# The walker's ONLY span-bounding rule.  It knows bracket SYNTAX and nothing
# else — not "table", not "ref", not "figure".  An opener is matched to its
# closer by scanning forward and SKIPPING every nested construct whole
# (recursively, via the same function); the first closer not inside a nested
# span is ours.  Because the skip is uniform over every bracket kind, nesting
# of ANY construct inside ANY other (table-in-table, table-in-ref,
# poem-in-table, …) is bounded correctly by this one rule — there are no
# per-shape bounders left to whack.  Balanced-matching IS the leaf/recurse
# decision: a span with nested brackets recurses, one without is a leaf.
#
# OPAQUE tags (`<math>`/`<nowiki>`/`<score>`/comments) carry verbatim content
# — LaTeX braces, lilypond `<c e g>` chords — that are NOT wiki brackets, so
# their interior is skipped without interpretation.
_BRACKET_CLOSE = {"{{": "}}", "{|": "|}", "[[": "]]"}
_OPAQUE_TAGS = frozenset({
    "math", "nowiki", "score", "hiero", "pre", "syntaxhighlight", "source",
    "timeline"})
_TAG_START_RE = re.compile(r"<([A-Za-z][A-Za-z0-9]*)\b")
# The block-level HTML tags the walker lifts as their own SHAPE_HTML_TAG
# element (every one bounded by the same `_construct_end` rule; the old
# per-tag non-greedy regexes are gone).  Inline markup (`<i>`,`<sup>`,…) is
# NOT here — it stays in body-text.  Self-closing `<ref…/>` is routed to
# SHAPE_HTML_SELF_CLOSING by the regex recognizers, not here.
_ELEMENT_TAGS = frozenset({"table", "ref", "poem", "math", "score", "hiero",
                           "nowiki", "includeonly"})
# Tags the one matcher will BOUND by depth (superset of the auto-extracted
# elements).  `div`/`p`/`span` are boundable so a styled wrapper can be matched
# to its right `</div>`/`</p>`/`</span>` and nested same-tags skipped — but they
# are NOT auto-extracted (bare layout `<div>`, inline `<span>`/`<p>` stay
# transparent; only a *styled* wrapper is lifted, via the gated recognizer
# below).  Corpus-verified balanced: `<p>` 407 open / 403 close (5 unbalanced
# articles, all OCR-garbage `<p.u(kp)` non-tags or close-less stubs that
# fail-close → fall through); `<span>` 15706 / 15704 (1 unbalanced article).
_BALANCED_TAGS = _ELEMENT_TAGS | {"div", "p", "span", "ins"}
# A `<div>`/`<p>`/`<span>` that carries styling ({{Ts}} shorthand, inline
# style=, or align=) — the gate that distinguishes a meaningful styled wrapper
# (lift → STYLED, which carries the style and recurses its content) from a bare
# layout `<div>` / inline `<span>` / paragraph `<p>` (transparent unwrap, left
# to body-text).  Structural, not a guess — recognition is on the PRESENCE of a
# style attribute, never on what it means; the raw bytes pass through unchewed
# and the CSS is derived later in the producer.
#
# TWO exclusions, both ownership hand-offs to a sibling recognizer (NOT meaning
# judgments):
#   * `{{mirrorH}}` span → its own MIRROR_GLYPH shape (recognized before this).
#   * any `title=` span → owned by body-text's `_handle_title_spans`.  A
#     `title=` span is a Wikisource editorial mark: 1237 corpus spans carry BOTH
#     `style="border-bottom:1px dashed red"` AND `title="amended from …"` (the
#     red-dashed OCR-correction highlight) — provenance, not real styling, to be
#     UNWRAPPED (text kept, decoration dropped); plus the Greek/Hebrew
#     transliteration tooltips `_handle_title_spans` carries as «SPAN[title:…]».
#     Both are that function's job, so the styled gate must not claim them.
_STYLED_WRAPPER_RE = re.compile(
    r"<(?:div|p|span)\b(?![^>]*\{\{\s*mirrorH)(?![^>]*\btitle\s*=)"
    r"[^>]*(?:\{\{\s*[Tt]s\b|style\s*=|align\s*=)", re.IGNORECASE)
# `<ins>` — Wikisource insertion.  UNLIKE the gated div/p/span above, EVERY <ins>
# lifts to STYLED (ungated): its tag is editorial (the UA-default underline is dropped),
# but its content is kept and any explicit style carried — a plain `<ins>s</ins>` → `s`,
# `<ins style="…overline">y</ins>` → «SPAN[style:…]».  A bare <div> is left transparent;
# a bare <ins> must be actively unwrapped, so there is no style gate.
_INS_OPEN_RE = re.compile(r"<ins\b", re.IGNORECASE)


def _construct_end(text: str, start: int) -> int | None:
    """Byte position one past the close of the bracket construct opening at
    ``start`` (HTML element, ``{{…}}``, ``{|…|}``, ``[[…]]``, or an opaque /
    self-closing / comment atom), or None if ``start`` is not an opener or
    the construct is unbalanced.  The single span-bounding primitive."""
    if text.startswith("<!--", start):
        e = text.find("-->", start + 4)
        return e + 3 if e >= 0 else None
    if start < len(text) and text[start] == "<":
        m = _TAG_START_RE.match(text, start)
        if m:
            gt = text.find(">", start)
            if gt < 0:
                return None
            if text[gt - 1] == "/":               # self-closing → atomic
                return gt + 1
            name = m.group(1).lower()
            if name in _OPAQUE_TAGS:              # verbatim interior → no scan
                close = f"</{name}>"
                e = text.lower().find(close, gt + 1)
                return e + len(close) if e >= 0 else None
            if name in _BALANCED_TAGS:            # balanced block element
                return _scan_balanced(text, gt + 1, f"</{name}", tag=True)
            # Any OTHER tag (`<td>`/`<tr>`/`<div>`/`<i>`/…) is NOT a construct
            # the walker bounds — return None so the scanner steps over the
            # `<` as a literal char.  (Chasing a `</td>` that the source often
            # omits would run to end-of-text, per cell — quadratic.)
            return None
    two = text[start:start + 2]
    if two in _BRACKET_CLOSE:
        # Table notation is equivalent at the delimiter level: a `{|` table may
        # be closed by a wiki `|}` OR an HTML `</table>` — source mixes the
        # spellings (LAMPROPHYRES/POST open `{|`, close `</table>`).  The alt
        # closer is only ever seen at depth 0 (nested constructs are skipped
        # whole), so a nested table's closer can't truncate the outer span.
        # (The reverse — `<table>` closed by `|}` — does not occur in the
        # corpus, so it is not accepted: a stray `|}` must not end a `<table>`.)
        alt = ("</table", True) if two == "{|" else None
        return _scan_balanced(
            text, start + 2, _BRACKET_CLOSE[two], tag=False, alt=alt)
    return None


def _scan_balanced(
    text: str, i: int, closer: str, *, tag: bool,
    alt: tuple[str, bool] | None = None,
) -> int | None:
    """Scan from ``i`` for ``closer``, skipping every nested construct whole
    (``_construct_end`` recursion).  ``tag`` closers (``</name``) tolerate
    attributes/space before ``>``; literal closers (``}}`` / ``|}`` / ``]]``)
    match exactly.  ``alt`` is an optional second acceptable closer
    ``(string, is_tag)`` for the table delimiter equivalence (`{|`↔`</table>`);
    because nested constructs are skipped whole, ``alt`` is only ever tested at
    depth 0, so a nested table's closer can't end the outer span."""
    n = len(text)
    cl = closer.lower()
    alt_cl = alt[0].lower() if alt else None
    while i < n:
        if tag:
            if text[i:i + len(closer)].lower() == cl:
                gt = text.find(">", i)
                if gt >= 0:
                    return gt + 1
        elif text.startswith(closer, i):
            return i + len(closer)
        if alt is not None:
            a_str, a_tag = alt
            if a_tag:
                if text[i:i + len(a_str)].lower() == alt_cl:
                    gt = text.find(">", i)
                    if gt >= 0:
                        return gt + 1
            elif text.startswith(a_str, i):
                return i + len(a_str)
        j = _construct_end(text, i)               # skip any nested construct
        if j is not None and j > i:
            i = j
        else:
            i += 1
    return None


def _new_placeholder() -> str:
    return f"{_PH}ELEM:{_next_placeholder_id()}{_PH}"


# Paired-wrapper spans `{{NAME/s}}…{{NAME/e}}` (centring / small-type block
# wrappers).  Recognized as the CENTER shape — ONE balanced node whose inner
# is recursively classified — so a figure/table/math/nested-pair inside
# becomes its CHILD instead of being carved out from under it (which orphaned
# the `/s`-`/e` halves under the old `.*?` overlay).  Per-name DEPTH counting
# matches the correct `/e` (nested c-in-c; c-wrapping-fine-print).
# Center-family ONLY — these produce the «CTR» marker, so they must be nodes.
# The print-economy small-type families (EB1911 fine print / fine block /
# smaller block) are TRANSPARENT (strip-keep-content, no marker) and are
# deleted as noise tokens pre-walker (see `_transform_text_v2`), letting their
# block inner (figures/tables) flow into the normal walk — making them CENTER
# elements re-processed the block inner badly and dropped those children.
_CENTER_PAIRED_NAMES: tuple[str, ...] = (
    "center block", "block center", "c", "bc",
    # Print-economy small-type block wrappers — STYLERS (font-size), not centring.
    # `_process_center` dispatches by name via `_TEMPLATE_STYLE_WRAPPERS`; the inner
    # is classified to placeholders first, so a nested figure/table is a preserved
    # CHILD (was preprocess-stripped before, which dropped the styling).
    "fine block", "eb1911 fine print", "smaller block",
)
_PAIRED_OPENER_RE = re.compile(
    r"\{\{\s*(" + "|".join(re.escape(n) for n in _CENTER_PAIRED_NAMES)
    + r")\s*/s\s*\}\}", re.IGNORECASE)


def _paired_wrapper_end(text: str, pos: int) -> int | None:
    """If a registered ``{{NAME/s}}`` opens at ``pos``, return the byte
    position one past its depth-balanced ``{{NAME/e}}`` (same-name counting);
    else ``None`` (no opener, or unbalanced → left for fall-through)."""
    m = _PAIRED_OPENER_RE.match(text, pos)
    if m is None:
        return None
    esc = re.escape(m.group(1))
    tok = re.compile(r"\{\{\s*" + esc + r"\s*/([se])\s*\}\}", re.IGNORECASE)
    depth = 0
    for tm in tok.finditer(text, pos):
        depth += 1 if tm.group(1).lower() == "s" else -1
        if depth == 0:
            return tm.end()
    return None


def _walk_balanced_shapes(
    text: str, _allow_outline: bool = True, _allow_figure: bool = True,
) -> tuple[str, list[tuple[str, str, str]]]:
    """Single linear walk over `text`, at one depth.

    Walks gap → element → gap → element.  Every construct is emitted as an
    extract — and so is every run of text BETWEEN constructs: that run is body
    text, an element in its own right, emitted as a SHAPE_BODY extract the
    instant the walk passes it.  An outline (a line-list) living in such a run
    is its own element too, recognized here so body text doesn't grab it.

    The walk recognizes everything at this depth — brackets, outlines, body —
    and `classify` runs it at every depth, so body text is recognized
    everywhere by the same code.  No later wrap, no flag, no scoop.
    """
    extracts: list[tuple[str, str, str]] = []
    output: list[str] = []
    body_buf: list[str] = []
    pos = 0
    n = len(text)
    figures = _allow_figure

    def _flush_body() -> None:
        """Emit the body-text run accumulated so far.  An outline inside it is
        its own element (extracted first); the remaining prose becomes
        SHAPE_BODY.  Called the moment the walk reaches a construct (and at the
        end) — the body run before it is complete."""
        if not body_buf:
            return
        run = "".join(body_buf)
        body_buf.clear()
        if not run:
            return
        if _allow_outline:
            run, outline_extracts = _walk_outline(run)
            extracts.extend(outline_extracts)
        last = 0
        for m in _PLACEHOLDER_RE.finditer(run):
            prose = run[last:m.start()]
            if prose:
                bph = _new_placeholder()
                output.append(bph)
                extracts.append((bph, SHAPE_BODY, prose))
            output.append(m.group(0))
            last = m.end()
        tail = run[last:]
        if tail:
            bph = _new_placeholder()
            output.append(bph)
            extracts.append((bph, SHAPE_BODY, tail))

    while pos < n:
        hint = _OPENER_HINT_RE.search(text, pos)
        if hint is None:
            body_buf.append(text[pos:])
            break

        opener_pos = hint.start()
        if opener_pos > pos:
            body_buf.append(text[pos:opener_pos])

        # Try every recognizer at this position in specificity order.
        # First successful match wins.
        matched: tuple[int, str, str] | None = None

        # (Figure recognition removed — imposed taxonomy.  A `{{center|…}}`/
        # `{{csc|…}}`/`<div style="float:…">` enclosing an image is just a STYLER,
        # bounded below as a generic DOUBLE_BRACE / HTML_TAG span (the classifier
        # carves the STRIP / HTML_STYLE label) and recursed by its own producer
        # (image → leaf, caption → recursed content); a bare `[[File:…]]` is an
        # IMAGE.)

        # Paired-wrapper span `{{NAME/s}}…{{NAME/e}}` → one PAIRED_WRAPPER node
        # (the classifier routes the name → CENTER).  Before the regex
        # recognizers (so `{{c/s}}` isn't mis-read as a bare template) AND
        # before the inner figure/table is carved out — the CENTER producer
        # recurses its own inner so the inner figure/table becomes a child.
        if matched is None:
            pe = _paired_wrapper_end(text, opener_pos)
            if pe is not None:
                matched = (pe, SHAPE_PAIRED_WRAPPER, text[opener_pos:pe])

        # Genealogy grid macro `{{chart2/start}}…/end` (+ familytree / tree-chart)
        # — also a PAIRED_WRAPPER span (classifier routes → CHART2), but it uses
        # `…/start`…`…/end`, not `/s`…`/e`, so `_paired_wrapper_end` above won't
        # catch it.  MUST be here, before the generic `{{…}}` recognizer below, or
        # that claims the bare `{{chart2/start}}` opener as a lone DOUBLE_BRACE and
        # the block shatters into per-row spacers/frames.
        if matched is None:
            gm = _GENEALOGY_RE.match(text, opener_pos)
            if gm is not None:
                matched = (gm.end(), SHAPE_PAIRED_WRAPPER, gm.group(0))

        # Block HTML element (`<table>`/`<ref>`/`<poem>`/`<math>`/`<score>`)
        # — bounded by the one balanced rule, so a nested construct can't
        # truncate the span.  Self-closing forms fall through to the
        # SHAPE_HTML_SELF_CLOSING recognizers below.
        if matched is None and opener_pos < n and text[opener_pos] == "<":
            tm = _TAG_START_RE.match(text, opener_pos)
            if tm and tm.group(1).lower() in _ELEMENT_TAGS:
                gt = text.find(">", opener_pos)
                if gt > opener_pos and text[gt - 1] != "/":
                    end = _construct_end(text, opener_pos)
                    if end is not None:
                        matched = (end, SHAPE_HTML_TAG, text[opener_pos:end])

        # The two STYLED-`<tag>` structures (HTML_STYLE / SPAN_TITLE) are NOT
        # covered by the block-HTML recognizer above — that only lifts
        # `_ELEMENT_TAGS` (table/ref/poem/math/score/nowiki/includeonly), never a
        # styled `<div>`/`<p>`/`<span>`/`<ins>`.  So they keep their own opener
        # blocks here (same opener regexes, same `_construct_end` span); the
        # classifier's `_derive_html_tag_label` carves HTML_STYLE / SPAN_TITLE.
        # They MUST precede the generic `{{…}}` recognizer because both produce a
        # different shape (HTML_TAG, not DOUBLE_BRACE).
        #
        #   Styled HTML `<div>`/`<p>`/`<span>` (carries `{{Ts}}`/`style=`/`align=`)
        #   or `<ins>` → HTML_TAG → classifier HTML_STYLE.  A BARE `<div>` /
        #   inline `<span>` / paragraph `<p>` is layout noise that doesn't match
        #   `_STYLED_WRAPPER_RE` and stays transparent (body-text unwrap).  Bounded
        #   by the one matcher (depth-aware over nested same-tags); unbalanced →
        #   None → falls through, no swallow.
        if matched is None and (
                _STYLED_WRAPPER_RE.match(text, opener_pos)
                or _INS_OPEN_RE.match(text, opener_pos)):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_HTML_TAG, text[opener_pos:end])

        #   `<span title="T">X</span>` translit-tooltip / editorial-drop
        #   (`_SPAN_TITLE_OPEN_RE`) → HTML_TAG → classifier SPAN_TITLE.  After the
        #   styled gate above, mirroring the former `_process_styled` precedence.
        if matched is None and _SPAN_TITLE_OPEN_RE.match(text, opener_pos):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_HTML_TAG, text[opener_pos:end])

        # ── The ONE generic `{{…}}` recognizer ────────────────────────────────
        # Every `{{…}}` is bounded as a single DOUBLE_BRACE unit by the one
        # balanced matcher (`_construct_end`), which skips every nested construct
        # whole — an opaque `<math>` numerator (single LaTeX braces), a `[[File:…]]`
        # inside `{{familytree|…}}`, a nested `{{sc|…}}` display — so NOTHING inside
        # a `{{…}}` is ever torn out from under it.  Replaces the former dozen
        # type-specific opener blocks (STRIP / PARAM / SHOULDER / RUNNING_HEADER /
        # FRACTION / link families / spacer / content-extract / image / etc.) plus
        # the ordered-list and labeled-equation balanced-scanner blocks: the
        # classifier (`_derive_double_brace_label`) routes EVERY name and `raise`s
        # on an unknown — that raise is the permanent guard, so every corpus
        # template must route.
        #
        # Runs AFTER the PAIRED_WRAPPER (`{{x/s}}…{{x/e}}` / chart2) recognizer and
        # the block-HTML / styled-`<tag>` recognizers above (so a `{{c/s}}` paired
        # opener and a styled `<div>` aren't mis-claimed), and BEFORE the regex
        # recognizers / BRACE_PIPE below (which own `[[…]]` / `{|` / page / title).
        #
        # A degenerate `{{{name|…}}` (triple-open, double-close — a source/OCR stray
        # leading `{`, e.g. `{{{Polytonic|ρ}}`) would bound from the first `{{` and
        # leave a stray `{` inside the inner, crashing the name extractor.  When the
        # clean `{{…}}` one char later balances, treat the leading `{` as a literal
        # (it joins the body buffer) and recognize the inner template — faithful: the
        # stray brace stays as text, the real `{{Polytonic|…}}` styler is recognized.
        if matched is None and text[opener_pos:opener_pos + 3] == "{{{":
            inner_end = _construct_end(text, opener_pos + 1)
            # Only the degenerate triple-open/DOUBLE-close form (the close is NOT
            # itself part of a `}}}` triple — i.e. NOT valid `{{{param}}}` syntax).
            if inner_end is not None and not text.startswith("}", inner_end):
                body_buf.append("{")
                pos = opener_pos + 1
                continue
        if matched is None and text[opener_pos:opener_pos + 2] == "{{":
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE, text[opener_pos:end])

        if matched is None:
            for shape, pattern in _REGEX_RECOGNIZERS:
                m = pattern.match(text, opener_pos)
                if m is not None:
                    matched = (m.end(), shape, m.group(0))
                    break

            # (Figure-tail upgrade removed — a bare `[[File:…]]` stays an IMAGE
            # leaf; a following `{{center|…}}` caption is its own STRIP-classified
            # DOUBLE_BRACE sibling, recursed in place, not folded into a FIGURE span.)

            # Inline-image lookahead: a bare `[[File:…]]` (no EXTCAP, no
            # figure-tail upgrade) sitting in same-line prose is structurally
            # an inline glyph.  Lookahead-only — no bytes absorbed; the
            # surrounding text keeps its newlines and separators intact.
            if (matched is not None
                    and matched[1] == SHAPE_DOUBLE_BRACKET
                    and matched[2].endswith("]]")
                    and _IMAGE_RE.match(matched[2])  # images only — not self-ref links
                    and _is_inline_image_position(text, matched[0])):
                matched = (matched[0], SHAPE_INLINE_IMAGE, matched[2])

        # (The former per-name `{{ordered list|…}}` and labeled-equation
        # `{{ne|…}}`/`{{equation|…}}`/`{{MathForm1|…}}` opener blocks are gone —
        # the ONE generic `{{…}}` recognizer above bounds them as DOUBLE_BRACE via
        # `_construct_end`, which skips an opaque `<math>` interior whole exactly as
        # the old `_find_balanced_template_end` did.  The classifier routes the name
        # → ORDERED_LIST / MATH_EQUATION / MATH_FORMULA_LABELED / MATH_NE.)

        # BRACE_PIPE doesn't have a regex pattern — its closer
        # requires balanced depth tracking.  Try it last (lowest
        # specificity) only if no regex recognizer fired.
        if matched is None and text[opener_pos:opener_pos+2] == "{|":
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_BRACE_PIPE, text[opener_pos:end])

        if matched is None:
            # Opener-hint matched (the bytes LOOK like an opener) but
            # no recognizer fully matched (no closer found, or the
            # full pattern didn't fit).  Advance past one character
            # and continue scanning — same fail-safe as today's
            # regex.sub which silently skips no-match positions.  The
            # char is body text, so it joins the body buffer.
            body_buf.append(text[opener_pos])
            pos = opener_pos + 1
            continue

        end_pos, shape, raw = matched
        _flush_body()  # the body element before this construct is complete
        ph = _new_placeholder()
        output.append(ph)
        extracts.append((ph, shape, raw))
        pos = end_pos

    _flush_body()  # trailing body element
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


_PLACEHOLDER_RE = re.compile(rf"{re.escape(_PH)}ELEM:\d+{re.escape(_PH)}")

# Template names whose ``{{name|…}}`` wrappers must stay ATOMIC during
# body-splitting — placeholders inside them belong to the wrapper's body
# run, not to a separate split.  Otherwise the wrapper's ``{{`` opener
# and ``}}`` closer land in different BODY runs and the body producer's
# template-handler regexes (which require both ends visible) can't match.
#
# Two families:
#
#   * Layout wrappers — ``_unwrap_layout_templates`` strips them to inner
#     content (``{{center|…}}``, ``{{larger|…}}``, …).  Same names as
#     ``body_text._LAYOUT_TEMPLATES``.
#   * Content-transform wrappers — ``_unwrap_content_templates`` converts
#     them to markers (``{{ne||content|(N)}}`` → ``«EQN:N»content«/EQN»``).
#     Without atomicity, the BODY-as-element refactor splits these at any
#     embedded element placeholder (e.g. ``<math>`` extracted from inside
#     ``{{ne|}}``) and the regex no longer sees both ends.
_LAYOUT_WRAPPER_NAMES: tuple[str, ...] = (
    # Layout (unwrap-to-content)
    "block center", "center", "c", "fine block",
    "EB1911 Fine Print", "larger", "smaller", "nowrap",
    "Fine", "sm",
    # Content-transform (rewrite to marker — must stay whole around
    # placeholders for the rewrite regex to match)
    "ne",
    "sans-serif",
    "xx-larger", "x-larger",
)

# HTML wrapper tags whose body content includes extracted-element
# placeholders (e.g. ``<div style="float:right">[[File:…]]</div>`` in
# ACCUMULATOR).  Same atomic-span discipline as the wikitext layout
# wrappers above — keeping the wrapper whole lets the body producer's
# Family A wrapper-strip rule pair its open/close across the embedded
# placeholder.  Enumerated; mirrors ``body_text``'s wrapper-strip set.
_HTML_WRAPPER_TAGS: tuple[str, ...] = (
    "div", "span", "small", "big", "p", "ins",
)

# (Paired `{{NAME/s}}…{{NAME/e}}` wrappers are now recognized as the
# PAIRED_WRAPPER shape in `_walk_balanced_shapes` via `_paired_wrapper_end` —
# extracted as a balanced node, not kept-atomic-then-unwrapped; the classifier
# routes the name → CENTER.  Former `_PAIRED_WRAPPER_NAMES` removed with the
# atomic-span paired loop.)


def _find_atomic_wrapper_spans(text: str) -> list[tuple[int, int]]:
    """Find every balanced wrapper span that should stay atomic during
    body-splitting.  Returns ``[(start, end), …]`` sorted by start.

    Two wrapper families covered:

      * ``{{NAME|…}}`` wikitext layout wrappers (``{{center|…}}``,
        ``{{larger|…}}``, …) — brace-counted matching so an inner
        template with a literal single ``{`` (e.g. ``{{xx-larger|√ {}}``
        in STEAM_ENGINE's Q=V equation) doesn't break the pairing.
      * ``<TAG…>…</TAG>`` HTML wrappers (``<div style="float:right">``
        around ``[[File:…]]`` in ACCUMULATOR, similar inline spans).
        Non-greedy DOTALL match — sufficient for the non-nested
        wrapper cases that exist in the corpus today; nested cases
        would need depth tracking, but the body producer's wrapper-
        strip already handles content correctness, so we only need
        ``_wrap_body_runs`` to KEEP the span together.

    Placeholders inside any returned span stay in the surrounding BODY
    run; the producer's wrapper-strip then sees the whole wrapper and
    can pair its open/close cleanly."""
    spans: list[tuple[int, int]] = []
    text_lower = text.lower()
    n = len(text)
    for name in _LAYOUT_WRAPPER_NAMES:
        prefix = "{{" + name.lower() + "|"
        pos = 0
        while pos < n:
            idx = text_lower.find(prefix, pos)
            if idx < 0:
                break
            content_start = idx + len(prefix)
            depth = 1
            j = content_start
            while j < n - 1:
                if text[j:j + 2] == "{{":
                    depth += 1
                    j += 2
                elif text[j:j + 2] == "}}":
                    depth -= 1
                    if depth == 0:
                        break
                    j += 2
                else:
                    j += 1
            if depth == 0:
                spans.append((idx, j + 2))
                pos = j + 2
            else:
                pos = content_start
    for tag in _HTML_WRAPPER_TAGS:
        pattern = re.compile(
            rf"<{tag}\b[^>]*>.*?</{tag}>",
            re.IGNORECASE | re.DOTALL)
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    # (Paired `{{NAME/s}}…{{NAME/e}}` spans are no longer kept atomic here —
    # they're extracted upstream as the CENTER element, step 2.)
    spans.sort()
    return spans


def _wrap_body_runs(
    text: str,
    extracts: list[tuple[str, str, str]],
) -> tuple[str, list[tuple[str, str, str]]]:
    """Wrap residual prose runs in ``text`` as SHAPE_BODY extracts.

    After ``_walk_balanced_shapes`` + ``_walk_outline``, the article-level
    placeholderized text is a mix of placeholders and residual body prose.
    This phase makes it homogeneous: each prose run becomes its own
    BODY-shape extract with its own placeholder, so the body producer is
    just another producer in the dispatch table and article assembly
    collapses to ordered concatenation of element markers.

    Layout-template wrappers (``{{center|…}}``, ``{{larger|…}}``, …) are
    treated as ATOMIC — a placeholder inside such a wrapper stays in the
    surrounding BODY run rather than splitting it.  The body producer's
    brace-counted ``_unwrap_layout_templates`` then peels the wrapper
    cleanly; the inner placeholder rides through into the BODY marker,
    where ``substitute_top_level_markers`` resolves it cross-reference-
    style (the same mechanism a child marker inside a parent marker
    already uses)."""
    atomic_spans = _find_atomic_wrapper_spans(text)

    def _is_in_atomic_span(pos: int) -> bool:
        for s, e in atomic_spans:
            if s <= pos < e:
                return True
            if pos < s:
                return False
        return False

    out: list[str] = []
    body_buf: list[str] = []
    n = len(text)
    pos = 0

    def _flush_body() -> None:
        if not body_buf:
            return
        body_raw = "".join(body_buf)
        body_buf.clear()
        if not body_raw:
            return
        ph = _new_placeholder()
        out.append(ph)
        extracts.append((ph, SHAPE_BODY, body_raw))

    while pos < n:
        m = _PLACEHOLDER_RE.match(text, pos)
        if m and not _is_in_atomic_span(pos):
            _flush_body()
            out.append(m.group(0))
            pos = m.end()
        else:
            body_buf.append(text[pos])
            pos += 1
    _flush_body()
    return "".join(out), extracts


def walk(
    text: str,
    _allow_outline: bool = True,
    _allow_figure: bool = True,
) -> tuple[str, list[tuple[str, str, str]]]:
    """One-level walk: emit every element at this depth — brackets, outlines,
    AND the body-text runs between them — in a single scan.

    ``classify`` runs this at every depth, so body text is recognized
    everywhere by the same code: no article-level special case, no later
    body-wrap, no flag.  ``_allow_outline=False`` when the parent is OUTLINE so
    the outline recognizer doesn't re-trigger on its own bytes.
    ``_allow_figure=False`` for every recursive descent so a figure — a
    body-level construct — is recognized only at the article level.
    """
    return _walk_balanced_shapes(
        text, _allow_outline=_allow_outline, _allow_figure=_allow_figure)
