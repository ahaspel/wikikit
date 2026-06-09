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
    SHAPE_CENTER,
    SHAPE_CHART2,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_FIGURE,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_INLINE_IMAGE,
    SHAPE_MIRROR_GLYPH,
    SHAPE_ORDERED_LIST,
    SHAPE_OUTLINE,
    SHAPE_PAGE,
    SHAPE_SECTION,
    SHAPE_STYLED,
    SHAPE_TITLE,
)
from britannica.pipeline.stages.elements._figure import (
    figure_tail_end,
    figure_wrapper_end,
    html_float_figure_end,
    html_ts_figure_end,
)
from britannica.pipeline.stages.elements._tables import (
    _TEMPLATE_STYLE_RE, _TEMPLATE_PARAM_STYLE_RE, _SHOULDER_HEADING_RE)

# An image whose trailing caption run the figure rule may absorb: a bracket
# `[[File:]]`/`[[Image:]]` or a `{{img float}}`/`{{figure}}`/`{{FI}}` template
# (hieroglyph excluded).
_FIG_IMAGE_RAW = re.compile(
    r"\[\[(?:File|Image):|\{\{\s*(?:img\s*float|figure|FI)\b", re.IGNORECASE)


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
# `<hiero>` keeps a regex only because its content alphabet is `[^<]` (glyph
# codes, no nesting) — a genuine leaf, not a balanced container.
_HIEROGLYPH_TAG_RE = re.compile(r"<hiero>[^<]*</hiero>", re.IGNORECASE)

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


# DOUBLE_BRACE template recognizers.  Each has a fixed template-name OPENER
# (`{{img float|`, more specific than bare `{{`); the span is closed by the one
# balanced matcher (`_construct_end`), so the body nests to ANY depth and an
# OPAQUE `<math>` arg (single LaTeX braces) is skipped whole — no hand-rolled
# brace-balancer, no per-corpus depth cap.
_IMAGE_FLOAT_OPENER_RE = re.compile(
    r"\{\{(?:img float|figure|FI)\s*\|", re.IGNORECASE)
_HIEROGLYPH_TMPL_RE = re.compile(
    r"\{\{hieroglyph\|([^{}]*)\}\}", re.IGNORECASE)
# `{{EB1911 footer initials|…}}` / `{{EB1911 footer double initials|…}}` — the
# contributor signature footer.  RECOGNIZED as a bounded CONTRIBUTOR_FOOTER node so the
# contributor decorator has ONE clean handle (its byline + removal logic is the
# decorator's job, not the walker's).  Opener-only; span closes via `_construct_end`.
# (The bare `{{EB1911 XYZ}}` sign-off shortcut is NOT recognized here — decorator's too.)
_CONTRIBUTOR_FOOTER_OPENER_RE = re.compile(
    r"\{\{\s*EB1911\s+footer\b", re.IGNORECASE)
# `{{section|Name}}` — Wikisource subsection ANCHOR (link target, no visual output;
# distinct from the `<section>` boundary tag).  Opener-only; span closes via
# `_construct_end`.  Carried so same-article `[[#Name]]` / cross-article `…#Name`
# xrefs resolve against it.
_SECTION_ANCHOR_OPENER_RE = re.compile(
    r"\{\{\s*section\s*\|", re.IGNORECASE)
# `{{EB1911 article link|Display|Target}}` — a cross-reference LINK.  OPENER-only regex;
# the span is closed by the one balanced matcher (`_construct_end`, exactly like the
# fraction family below), so the display's nested `{{sc|…}}` is bounded at ANY depth —
# no hand-rolled brace-balancer.  The producer recurses the display.
_EB1911_ARTICLE_LINK_OPENER_RE = re.compile(
    r"\{\{\s*EB1911\s+article\s+link\s*\|", re.IGNORECASE)
# Target-first link siblings — `{{EB1911/DNB lkpl|…}}`, `{{1911link|…}}`,
# `{{11link|…}}`, `{{EB1911 link|…}}` (NOT `EB1911 article link`, which is display-first
# above).  Opener-only; the one matcher bounds the span at any nesting depth.
_TARGET_FIRST_LINK_OPENER_RE = re.compile(
    r"\{\{\s*(?:(?:EB1911|DNB)\s+lkpl|1911link|11link|EB1911\s+link)\s*\|",
    re.IGNORECASE)
# Spacer / layout-primitive LEAVES — `{{em}}`/`{{gap}}` (space), `{{ditto}}` (″),
# `{{dhr}}`/`{{rule}}` (rule markers), `{{clear}}`/`{{anchor}}` (no glyph).  Atomic;
# the producer emits a char/marker, nothing recurses.
_SPACER_OPENER_RE = re.compile(
    r"\{\{\s*(?:(?:em|gap|clear|anchor|ditto|dhr|rule|bar|shy)\b"
    r"|=|\(|\)|'|!|\*\*\*|\*|–|\.\.\.|…)", re.IGNORECASE)
# Content extractors — the display arg IS the content, the rest is metadata
# (tooltip string / language code / title); the producer unwraps + recurses it.
_CONTENT_EXTRACT_OPENER_RE = re.compile(
    r"\{\{\s*(?:tooltip|abbr|lang|sic|fqm|drop\s?initial)\b", re.IGNORECASE)
# `<span title="T">X</span>` — a transliteration TOOLTIP when X is Greek/Hebrew (T is the
# romanization, carried as «SPAN[title:T]» — the HTML twin of the {{tooltip}} template);
# any other title= is editorial provenance, dropped (content kept).  Re-promotes the
# gutted body-text `_handle_title_spans`; `_process_styled` applies the carry/drop split.
_SPAN_TITLE_OPEN_RE = re.compile(
    r'<span\b[^>]*?\btitle\s*=\s*(?:"(?P<q>[^"]*)"|(?P<uq>[^\s">]+))[^>]*>',
    re.IGNORECASE)
_TRANSLIT_CONTENT_RE = re.compile(
    r"\{\{\s*(?:Greek|Hebrew|polytonic)|[Ͱ-Ͽἀ-῿֐-׿]", re.IGNORECASE)
# `{{dual line|A|B}}` — pure layout primitive that stacks two lines
# (`A<br>B`).  Args A and B can carry any inline content including
# nested templates (chem `C{{sub|6}}H{{sub|5}}`, layout `{{gap}}`,
# refs `<ref>…</ref>`) and opaque `<math>` with single LaTeX braces.
# Opener-only; the one balanced matcher closes the span at ANY depth.
# Lifting this template at the walker level gives the classifier a
# bounded unit to inspect; if its content is chem/math-shaped, the
# classifier will route accordingly (future predicate), instead of
# body-text smuggling the rendering in via a regex pass.
_DUAL_LINE_OPENER_RE = re.compile(
    r"\{\{\s*dual\s+line\s*\|", re.IGNORECASE)
# The fraction family — `{{sfrac|n|d}}` / `{{mfrac|i|n|d}}` / `{{frac}}` /
# `{{over}}` / `{{EB1911 tfrac}}` / … — is a STYLER, not body-text typography:
# it imposes a numerator-over-denominator presentation on content that may
# itself be structure (`<math>` numerators, nested fractions).  Lifted as a
# bounded DOUBLE_BRACE unit (like `dual line`) so its slots RECURSE through the
# one dispatch instead of being flattened by a body-text regex that breaks the
# moment a slot holds an extracted element or spans a `\n\n`.  Only the OPENER is
# a regex; the span is closed by `_construct_end` (the one balanced matcher) so a
# `<math>\frac{a}{b}</math>` numerator — single LaTeX braces inside an OPAQUE
# `<math>` — is skipped whole, not mis-counted by a naive brace scan.  Names
# longest-first so `sfrac nobar` wins over `sfrac`.
_FRACTION_OPENER_RE = re.compile(
    r"\{\{\s*(?:sfrac\s+nobar|EB1911\s+sfrac|EB1911\s+tfrac"
    r"|EB¹⁹¹¹\s+sfrac|EB¹⁹¹¹\s+tfrac|EB₁₉₁₁\s+ₜfᵣₐc"
    r"|sfracN|sfrac|mfrac|frac|over|binom)\s*\|", re.IGNORECASE)
# `{{lb-|N}}` → `N lb` / bare `{{lb-}}` → `lb`: the pound-weight glyph (℔
# unwrapped to literal "lb" for search/copy-paste).  A LEAF, recognized at the
# walker so it carries in EVERY context — not just the body-text pass it used to
# live in, which leaked it inside math/italic/centred blocks (the lb-/sup
# context-leak: a body-text handler is context-dependent).
_LB_RE = re.compile(r"\{\{\s*lb-\s*(?:\|[^{}]*)?\}\}", re.IGNORECASE)
# `{{sub|x}}` / `{{sup|x}}` — subscript/superscript TYPOGRAPHY (chem subscripts,
# math exponents, ordinals, footnote markers).  Content can nest (`{{sup|{{sfrac
# |1|n}}}}`); opener-only, the one balanced matcher bounds it at any depth.  The
# producer recurses the slot and translates flat runs to Unicode around element
# markers.  Promoted out of body-text's `_convert_sub_sup` (which only fired in
# the flat body-text pass, leaking sup inside math/italic/centred blocks — the lb-/sup
# context-leak).
_SUBSUP_OPENER_RE = re.compile(
    r"\{\{\s*(?:sub|sup)\s*\|", re.IGNORECASE)
# `{{ppoem|…}}` — Wikisource preformatted-poem template (verse analog of
# `<poem>`).  Multiline verse with shallow inline templates (`{{fqm|"}}` quote
# marks, `{{em|N}}`/`{{gap|Nem}}` indents, `{{sc|…}}`); opener-only, bounded by
# the one balanced matcher at any depth.  → PPOEM, extracted + emitted as VERSE.
_PPOEM_OPENER_RE = re.compile(
    r"\{\{\s*ppoem\s*\|", re.IGNORECASE)
# Labeled display-equation templates — `{{equation|content}}`,
# `{{MathForm1|label|content}}`, `{{ne|content}}`.  These are
# structurally declared as math by their template name — the source's
# own assertion "this is a math expression with its own paragraph
# context."  Walker bounds the template (purely structural); classifier
# applies a math label; producer emits `«EQN:LABEL»content«/EQN»` with
# `\n\n` paragraph margins.
#
# Inline-typography templates (`{{sfrac|...}}`, `{{sub|...}}`,
# `{{sup|...}}`, the fraction variants) are NOT walker chunks even
# though their content is mathematical — they're typography whose
# rendered output flows back into prose, and body-text owns rendering.
# See the chunk-vs-typography principle: the source must declare math
# via the template name itself for the walker to recognize it.
#
# Closer is found via `_find_balanced_template_end` — a depth-counting
# scanner that masks `<math>`/`<nowiki>`/HTML-comment spans so the
# literal `{`/`}` inside LaTeX (e.g. `\text{A}` in `{{ne|<math>…}}`)
# don't trip brace depth.  Regex with `[^{}]` would lose those.
_LABELED_EQUATION_TEMPLATE_NAMES_PATTERN = (
    r"equation"
    r"|MathForm1"
    r"|ne"
)
_LABELED_EQUATION_TEMPLATE_OPENER_RE = re.compile(
    r"\{\{\s*(" + _LABELED_EQUATION_TEMPLATE_NAMES_PATTERN + r")\s*\|",
    re.IGNORECASE,
)

# `{{ordered list|…}}` opener — matched with the balanced-brace scanner (not a
# fixed-depth regex) because the classification nests several levels deep
# (GEOGRAPHY: 4).  → SHAPE_ORDERED_LIST (a leaf; the producer owns the recursion).
_ORDERED_LIST_OPENER_RE = re.compile(r"\{\{\s*ordered\s+list\b", re.IGNORECASE)


def _find_balanced_template_end(text: str, start: int) -> int | None:
    """Find the position one past the balanced ``}}`` closing the
    `{{...}}` template that begins at ``start``.

    Returns None if no balanced close exists.

    Treats ``<math>…</math>``, ``<nowiki>…</nowiki>`` and ``<!--…-->``
    as opaque atoms — `{`/`}` inside them don't count toward brace
    depth.  Needed for `{{ne|<math>\\text{A}</math>}}` and similar
    where LaTeX content carries literal braces a regex `[^{}]` would
    refuse to cross.
    """
    if text[start:start + 2] != "{{":
        return None
    n = len(text)
    depth = 1
    i = start + 2
    while i < n:
        ch = text[i]
        if ch == "<":
            tail = text[i:i + 6].lower()
            if tail.startswith("<math"):
                end = text.lower().find("</math>", i + 5)
                if end >= 0:
                    i = end + 7
                    continue
            elif tail.startswith("<nowik"):
                end = text.lower().find("</nowiki>", i + 6)
                if end >= 0:
                    i = end + 9
                    continue
            elif text[i:i + 4] == "<!--":
                end = text.find("-->", i + 4)
                if end >= 0:
                    i = end + 3
                    continue
            i += 1
        elif ch == "{" and i + 1 < n and text[i + 1] == "{":
            depth += 1
            i += 2
        elif ch == "}" and i + 1 < n and text[i + 1] == "}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return None
# `{{Css image crop|…}}` — a STANDALONE DjVu crop (multi-line params).  Opener-
# only; the one balanced matcher bounds the template at any depth.  A following
# caption (`{{center|…}}` / `{{csc|…}}`) is NOT absorbed — it's the styled
# sibling it already is.  In-table crops route via the table classifier instead.
_DJVU_CROP_OPENER_RE = re.compile(
    r"\{\{\s*Css image crop\b", re.IGNORECASE)
# `{{raw image|X}}` — EB1911's bare full-image syntax (a DjVu page-ref → full-
# page render, or a plain filename).  Opener-only; the one balanced matcher
# bounds the template.  A following `{{c|…}}` caption is NOT absorbed — it's a
# styled sibling.  Recognized as one DOUBLE_BRACE image element (→ RAW_IMAGE).
_RAW_IMAGE_OPENER_RE = re.compile(
    r"\{\{\s*raw\s+image\s*\|", re.IGNORECASE)

# `{{Plain image with caption|image=File:…|align=…|width=…|caption=…|caption
# position=…}}` — Wikisource named-parameter figure macro (MAP's cartography
# plates).  Caption nests arbitrarily (`{{center|{{nowrap|{{smallcaps|…}}}}}}`);
# opener-only, bounded by the one balanced matcher at any depth.  Recognized as
# one DOUBLE_BRACE image element (→ PLAIN_IMAGE).
_PLAIN_IMAGE_OPENER_RE = re.compile(
    r"\{\{\s*plain image with caption\s*\|", re.IGNORECASE)

# DOUBLE_BRACKET image — `[[File:…]]` / `[[Image:…]]`, a pure LEAF.  No caption
# absorption: a following caption block is its own sibling, recursed in place
# (image = leaf, caption = sibling — there is no figure unit to fold into).
# Inline-vs-block is decided structurally by `_is_inline_image_position` at
# dispatch; the walker advances past `]]` and surrounding bytes stay intact.
_IMAGE_RE = re.compile(
    r"\[\[(?:File|Image):[^\]]+\]\]", re.IGNORECASE)

# DOUBLE_BRACKET self-reference — `[[1911 Encyclopædia Britannica/Article#Sec|Disp]]`,
# an internal EB1911 cross-link in raw bracket form (vs the `{{EB1911 article link}}`
# template form).  Recognized here, classified EB1911_SELFREF, produced as «LN».  The
# `1911 Encyclop` prefix is the signal — EB9 "Ninth Edition" refs do NOT match (those
# are external Wikisource links, handled by the external-link producer).
_EB1911_SELFREF_RE = re.compile(
    r"\[\[\s*1911\s+[Ee]ncyclop[^\]]*\]\]", re.IGNORECASE)

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
    (SHAPE_CHART2,            _CHART2_RE),
    (SHAPE_SECTION,           _SECTION_RE),
    (SHAPE_MIRROR_GLYPH,      _MIRROR_GLYPH_RE),
    (SHAPE_HTML_SELF_CLOSING, _REF_SELF_RE),
    (SHAPE_HTML_SELF_CLOSING, _PAGEQUALITY_RE),
    # `<table>`/`<ref>`/`<poem>`/`<math>`/`<score>` are bounded by the one
    # balanced `_construct_end` rule (see the `_ELEMENT_TAGS` handler in
    # `_walk_balanced_shapes`), NOT by per-tag non-greedy regexes — those
    # embedded a false no-nesting assumption (table-in-table orphaned the
    # outer tail into body-text).
    (SHAPE_HTML_TAG,          _HIEROGLYPH_TAG_RE),
    (SHAPE_DOUBLE_BRACE,      _HIEROGLYPH_TMPL_RE),
    (SHAPE_DOUBLE_BRACE,      _LB_RE),
    (SHAPE_DOUBLE_BRACKET,    _IMAGE_RE),
    (SHAPE_DOUBLE_BRACKET,    _EB1911_SELFREF_RE),
    (SHAPE_DOUBLE_BRACKET,    _AUTHOR_RE),
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
    r"|\{\{chart2/start"             # CHART2
    r"|\{\|"                        # BRACE_PIPE
    r"|<ref\b"                      # HTML_SELF_CLOSING ref / HTML_TAG ref
    r"|<pagequality\b"              # HTML_SELF_CLOSING pagequality metadata
    r"|<section\s+(?:begin|end)\b"  # SECTION transclusion marker
    r"|<(?:table|poem|math|score|hiero)\b"  # HTML_TAG tag variants
    r"|<span\s+style\s*=\s*\"[^\"]*\{\{mirrorH"  # MIRROR_GLYPH span
    r"|<(?:span|div)\b[^>]*\bfloat\s*:"  # FIGURE HTML float-wrapper
    r"|<div\b"  # any <div> — styled ones lift to STYLED, bare ones fall through
    r"|<p\b"    # any <p> — styled ones lift to STYLED, bare/OCR ones fall through
    r"|<ins\b"  # any <ins> — Wikisource insertion, lifted UNGATED (always a styler)
    r"|<span\b[^>]*(?:\{\{\s*[Tt]s\b|style\s*=|align\s*=|title\s*=)"  # STYLED / transliteration-title <span>
    r"|\[\[(?:File|Image):"         # DOUBLE_BRACKET image
    r"|\[\[\s*1911\s+[Ee]ncyclop"   # DOUBLE_BRACKET EB1911 self-reference cross-link
    r"|\[\[\s*Author:"              # DOUBLE_BRACKET Author link (contributor or xref)
    r"|\{\{\s*(?:center|block\s*center|c|c?sc|small-caps)\s*\|"  # FIGURE wrapper (image inside)
    r"|\{\{\s*(?:c|block\s*center|center\s*block)\s*/s\s*\}\}"  # CENTER paired-wrapper
    r"|\{\{\s*(?:img float|figure|FI|hieroglyph|Css image crop|raw\s+image|dual\s+line|ppoem|plain\s+image\s+with\s+caption|ordered\s+list|EB1911|DNB|1911link|11link)\b"  # DOUBLE_BRACE templates
    r"|\{\{\s*section\s*\|"  # DOUBLE_BRACE {{section|Name}} subsection anchor
    r"|\{\{\s*(?:(?:em|gap|clear|anchor|ditto|dhr|rule|bar|shy)\b|=|\(|\)|'|!|\*\*\*|\*|–|\.\.\.|…)"  # SPACER / char-escape leaves
    r"|\{\{\s*(?:tooltip|abbr|lang|sic|fqm|drop\s?initial)\b"  # content extractors
    r"|\{\{\s*(?:sfrac\s+nobar|sfracN|sfrac|mfrac|frac|over|binom)\b"  # FRACTION family (EB1911 sfrac/tfrac covered by EB1911 above)
    r"|\{\{\s*lb-"  # lb- pound-weight glyph (ends in '-', no \b)
    r"|\{\{\s*(?:sub|sup)\s*\|"  # sub/sup typography
    r"|\{\{\s*(?:" + _LABELED_EQUATION_TEMPLATE_NAMES_PATTERN + r")\s*\|"  # labeled-equation templates
    r"|" + _TEMPLATE_STYLE_RE.pattern  # template-form style wrappers (registry-driven, auto-syncs)
    + r"|" + _TEMPLATE_PARAM_STYLE_RE.pattern  # param font-size stylers ({{Fs|N%|X}})
    + r"|" + _SHOULDER_HEADING_RE.pattern,  # shoulder headings (EB9 margin note isn't covered by EB1911 above)
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
    "math", "nowiki", "score", "pre", "syntaxhighlight", "source", "timeline"})
_TAG_START_RE = re.compile(r"<([A-Za-z][A-Za-z0-9]*)\b")
# The block-level HTML tags the walker lifts as their own SHAPE_HTML_TAG
# element (every one bounded by the same `_construct_end` rule; the old
# per-tag non-greedy regexes are gone).  Inline markup (`<i>`,`<sup>`,…) is
# NOT here — it stays in body-text.  Self-closing `<ref…/>` is routed to
# SHAPE_HTML_SELF_CLOSING by the regex recognizers, not here.
_ELEMENT_TAGS = frozenset({"table", "ref", "poem", "math", "score"})
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
    "center block", "block center", "c",
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
    text: str, _allow_figure: bool = True,
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
    figures = _allow_figure

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

        # (SHAPE_FIGURE recognition removed — imposed taxonomy.  A `{{center|…}}`/
        # `{{csc|…}}`/`<div style="float:…">` enclosing an image is just a STYLER,
        # recognized as SHAPE_STYLED below and recursed by `_process_styled` (image
        # → leaf, caption → recursed content); a bare `[[File:…]]` is an IMAGE.)

        # Paired-wrapper span `{{NAME/s}}…{{NAME/e}}` → one CENTER node.
        # Before the regex recognizers (so `{{c/s}}` isn't mis-read as a bare
        # template) AND before the inner figure/table is carved out — the
        # inner becomes a recursively-classified child of this node.
        if matched is None:
            pe = _paired_wrapper_end(text, opener_pos)
            if pe is not None:
                matched = (pe, SHAPE_CENTER, text[opener_pos:pe])

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

        # Styled `<div>` / `<p>` / `<span>` (carries `{{Ts}}` / `style=` /
        # `align=`) → STYLED, the ONE styled-wrapper element: the producer
        # carries the style as a marker and recurses the content through the
        # main dispatch (so a table / MATH / CHEM / figure inside is handled by
        # its own producer, not leaked).  A BARE `<div>` / inline `<span>` /
        # paragraph `<p>` is layout noise and stays transparent (body-text
        # unwrap).  Bounded by the one matcher (depth-aware over nested
        # same-tags); unbalanced → None → falls through, no swallow.  Image-
        # bearing styled wrappers are already claimed above by the figure
        # recognizers (`html_ts_figure_end` / `html_float_figure_end`); this
        # catches the styled-TEXT wrappers (centered captions/titles, small-
        # print credits, indented keys, inline over-bar / limit-notation spans)
        # that body-text used to rewrite via `_p_ts`/`_div_ts`/`_span_ts`.
        #
        # UNGATED — recognized at EVERY depth (a styled wrapper is recursive: a
        # styled `<div>` nests inside another, a styled `<span>` sits in a table
        # cell or a CHEM/MATH layout).  Style is orthogonal to structure, so the
        # walker carries it uniformly and the producer recurses the content; no
        # depth flag.  This was historically gated to `figures` (article level)
        # because extracting a styled `<span>` inside a CHEM/MATH/figure element
        # replaced it with a placeholder in the inner text those classifiers
        # inspected, FLIPPING their classification (PRIMULINE's chem grid, the
        # equation layouts).  The fix was at the root: the chem classifier now
        # reads RAW (`_is_chemistry_layout_pred` scans raw for bracket/reaction
        # content), and math equation layouts stopped being a special
        # classification at all (`<math>` is a self-labeling leaf, so a math-cell
        # table is just a TABLE) — so extraction can no longer perturb
        # classification, and the gate is unnecessary.  Image-bearing styled
        # wrappers are still claimed above by the figure recognizers.
        if matched is None and (
                _STYLED_WRAPPER_RE.match(text, opener_pos)
                or _INS_OPEN_RE.match(text, opener_pos)
                or _SPAN_TITLE_OPEN_RE.match(text, opener_pos)
                or _TEMPLATE_STYLE_RE.match(text, opener_pos)
                or _TEMPLATE_PARAM_STYLE_RE.match(text, opener_pos)
                or _SHOULDER_HEADING_RE.match(text, opener_pos)):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_STYLED, text[opener_pos:end])

        # Fraction family (`{{sfrac|…}}` …) — opener matched by regex, span
        # closed by the one balanced matcher so an OPAQUE `<math>` numerator
        # (single LaTeX braces) is skipped whole.  → DOUBLE_BRACE, classified
        # FRACTION, slots recurse in the producer.
        if matched is None and _FRACTION_OPENER_RE.match(text, opener_pos):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE, text[opener_pos:end])

        # `{{EB1911 article link|…}}` — cross-reference link; opener matched, span
        # closed by the ONE balanced matcher so a nested `{{sc|…}}` display is bounded
        # at any depth (no hand-rolled brace-balancer).  → DOUBLE_BRACE, classified
        # EB1911_ARTICLE_LINK; the display recurses in the producer.
        if matched is None and _EB1911_ARTICLE_LINK_OPENER_RE.match(text, opener_pos):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE, text[opener_pos:end])

        if matched is None and _TARGET_FIRST_LINK_OPENER_RE.match(text, opener_pos):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE, text[opener_pos:end])

        if matched is None and _SPACER_OPENER_RE.match(text, opener_pos):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE, text[opener_pos:end])

        if matched is None and _CONTENT_EXTRACT_OPENER_RE.match(text, opener_pos):
            end = _construct_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE, text[opener_pos:end])

        # Named DOUBLE_BRACE templates whose args nest arbitrarily and may carry
        # an opaque `<math>` (single LaTeX braces) — opener-matched, span closed
        # by the one balanced matcher, exactly like the fraction family above.
        # Migrated off hand-rolled 2-4 level brace regexes that capped depth and
        # broke on a lone `{` inside `<math>`.
        if matched is None:
            for _opener in (_IMAGE_FLOAT_OPENER_RE, _PLAIN_IMAGE_OPENER_RE,
                            _DUAL_LINE_OPENER_RE, _SUBSUP_OPENER_RE,
                            _PPOEM_OPENER_RE, _RAW_IMAGE_OPENER_RE,
                            _DJVU_CROP_OPENER_RE, _CONTRIBUTOR_FOOTER_OPENER_RE,
                            _SECTION_ANCHOR_OPENER_RE):
                if _opener.match(text, opener_pos):
                    end = _construct_end(text, opener_pos)
                    if end is not None:
                        matched = (end, SHAPE_DOUBLE_BRACE, text[opener_pos:end])
                    break

        if matched is None:
            for shape, pattern in _REGEX_RECOGNIZERS:
                m = pattern.match(text, opener_pos)
                if m is not None:
                    matched = (m.end(), shape, m.group(0))
                    break

            # (Figure-tail upgrade removed — a bare `[[File:…]]` stays an IMAGE
            # leaf; a following `{{center|…}}` caption is its own SHAPE_STYLED
            # sibling, recursed in place, not folded into a FIGURE span.)

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

        # Ordered-list classification — own LEAF shape, balanced-brace
        # scanner (handles the arbitrary nesting depth a fixed-depth regex
        # would miss; the producer parses the whole nested template).
        if matched is None and _ORDERED_LIST_OPENER_RE.match(text, opener_pos):
            end = _find_balanced_template_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_ORDERED_LIST, text[opener_pos:end])

        # Labeled-display-equation templates use a balanced-brace
        # scanner (not a regex) because `{{ne|<math>...</math>}}`
        # carries LaTeX with literal `{`/`}` (`\text{A}` etc.) that a
        # `[^{}]`-based regex can't span.  The scanner masks
        # `<math>`/`<nowiki>`/comment spans before counting braces.
        if matched is None and _LABELED_EQUATION_TEMPLATE_OPENER_RE.match(
                text, opener_pos):
            end = _find_balanced_template_end(text, opener_pos)
            if end is not None:
                matched = (end, SHAPE_DOUBLE_BRACE,
                           text[opener_pos:end])

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

# (Paired `{{NAME/s}}…{{NAME/e}}` wrappers are now recognized as the CENTER
# shape in `_walk_balanced_shapes` via `_paired_wrapper_end` — extracted as a
# balanced node, not kept-atomic-then-unwrapped.  Former `_PAIRED_WRAPPER_NAMES`
# removed with the atomic-span paired loop.)


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
    _wrap_body: bool = False,
) -> tuple[str, list[tuple[str, str, str]]]:
    """One-level shape-emitting walker.

    Linear scan + optional OUTLINE phase + optional BODY-wrap phase.
    Returns the placeholderized text plus `(placeholder, shape, raw)`
    tuples.

    ``_allow_outline=False`` is passed when the parent shape is
    OUTLINE so the outline scanner doesn't re-trigger on its own
    bytes.  ``_allow_figure=False`` is passed for every recursive
    (inside-an-element) descent so a figure — a body-level construct —
    is only recognized at the article level, never inside another
    element or the figure producer's own re-processing of its span.
    ``_wrap_body=True`` is passed at the article entry point so
    residual prose between extracted elements becomes its own SHAPE_BODY
    extracts; recursive walks (inside cells, captions, figure interiors)
    pass False — those contexts don't have an article-level "body."
    """
    text, extracts = _walk_balanced_shapes(text, _allow_figure)
    if _allow_outline:
        text, outline_extracts = _walk_outline(text)
        extracts.extend(outline_extracts)
    if _wrap_body:
        text, extracts = _wrap_body_runs(text, extracts)
    return text, extracts
