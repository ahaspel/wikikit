"""Recursive classifier — turns walker output into a labeled tree
of :class:`ClassifiedElement` records.

This is the new architecture's entry point, dormant until Phase C of
the walker / classifier / producer refactor wires it into
``_transform_text_v2``.  Today it coexists with the legacy three-pass
``_walk_recursive`` / ``_classify_recursive`` / ``_produce_recursive``
pipeline in ``__init__.py``.

The flow (mutually recursive, classifier-driven):

    walker → (shape, raw_bytes)
         │
         ▼
    classifier strips outer delimiters → inner_bytes
         │
         ▼
    classifier asks walker for one-level extracts of inner_bytes
         │
         ▼
    for each (placeholder, child_shape, child_raw):
        classifier recurses → ClassifiedElement
         │
         ▼
    classifier derives own label from (shape, raw, inner_registry)
         │
         ▼
    returns ClassifiedElement(label, raw, inner_text, inner_registry)

Walker output: ``(text_with_placeholders, [(ph, shape, raw), ...])``.
For Phase B-1 the walker is today's :func:`extract`, with a thin
name-to-shape adapter; Phase B-2 swaps in a shape-emitting walker
directly.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._registry import (
    ClassifiedElement,
    ElementRegistry,
    TABLE_LABELS,
    _PH,
    _next_placeholder_id,
)
from britannica.pipeline.stages.elements._shapes import (
    LEAF_SHAPES,
    SHAPE_BODY,
    SHAPE_BRACE_PIPE,
    SHAPE_PAIRED_WRAPPER,
    SHAPE_DOUBLE_BRACE,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_HTML_TAG,
    SHAPE_OUTLINE,
    SHAPE_PAGE,
    SHAPE_TITLE,
    strip_outer,
)
from britannica.pipeline.stages.elements._link import _LINK_LABELS
from britannica.pipeline.stages.elements._walker import (
    walk,
    _OPAQUE_TAGS,
    # Styled-HTML-wrapper attribute recognizers — the SAME regexes the walker
    # uses to BOUND these spans; here they carve the HTML_TAG label.
    _STYLED_WRAPPER_RE,
    _INS_OPEN_RE,
    _SPAN_TITLE_OPEN_RE,
)
from britannica.pipeline.stages.elements._tables import (
    # Styler / heading template-name recognizers — the SAME regexes the walker
    # uses to BOUND these `{{…}}` spans; here they carve the DOUBLE_BRACE label.
    _TEMPLATE_STYLE_RE,
    _TEMPLATE_PARAM_STYLE_RE,
    _SHOULDER_HEADING_RE,
    _RUNNING_HEADER_RE,
    # Styler registries — name-membership routing for the BARE `{{name}}` form
    # (the pipe-requiring regexes above only match `{{name|…}}`; a known styler
    # must route whether or not it carries content).
    _TEMPLATE_STYLE_WRAPPERS,
    _TEMPLATE_PARAM_STYLE_WRAPPERS,
)


# Full DOUBLE_BRACE template name — everything between `{{` and the first `|`
# or `}` (the whitespace-stopping `_TEMPLATE_NAME_RE` mangles multi-word names
# like `block center` → `block`).  Lower-cased, inner whitespace collapsed, so a
# registry lookup matches regardless of arg presence or spacing.
_DB_NAME_RE = re.compile(r"^\{\{\s*([^|{}]+?)\s*(?:[|}]|$)")


def _db_name(raw: str) -> str:
    m = _DB_NAME_RE.match(raw)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1).strip().lower())


# ── Per-shape label derivers ──────────────────────────────────────────
#
# Each function maps (raw bytes [+ inner, + classified children]) to a
# label string.  Atomic shapes derive label from the opening
# identifier in the raw bytes alone.  Composite BRACE_PIPE wikitables
# need their children's labels — composites finalise after the inner
# recursion has unwound.

# The genealogy family inside the PAIRED_WRAPPER shape: a chart2 / familytree /
# tree-chart `…/start`…`…/end` region (the walker's `_GENEALOGY_RE` carves it,
# optionally with a `{{center|…}}` / `{{EB1911 fine print/s}}` prefix, so the
# opener isn't always the first token — match anywhere in the raw).  Everything
# else PAIRED_WRAPPER is the `{{NAME/s}}…{{NAME/e}}` centring family → CENTER.
_CHART2_FAMILY_RE = re.compile(
    r"\{\{\s*(?:chart2|familytree|tree\s*chart)\s*/\s*start", re.IGNORECASE)

_HTML_TAG_NAME_RE = re.compile(r"^<\s*([A-Za-z][A-Za-z0-9]*)", re.IGNORECASE)
_TEMPLATE_NAME_RE = re.compile(r"^\{\{\s*([^|{}<>\n\s]+)")
_BRACKET_PREFIX_RE = re.compile(r"^\[\[\s*([A-Za-z]+)\s*:", re.IGNORECASE)
# The fraction family — matched on the RAW opener (not the first token) so that
# multi-word names like `EB1911 sfrac` route here rather than to the `eb1911`
# CONTRIBUTOR_FOOTER case.  Mirrors the walker's `_FRACTION_RE`.
_FRAC_LABEL_RE = re.compile(
    r"\{\{\s*(?:sfrac\s+nobar|EB1911\s+sfrac|EB1911\s+tfrac"
    r"|EB¹⁹¹¹\s+sfrac|EB¹⁹¹¹\s+tfrac|EB₁₉₁₁\s+ₜfᵣₐc"
    r"|sfracN|sfrac|mfrac|frac|over|binom)\s*\|", re.IGNORECASE)


_HTML_TAG_LABEL: dict[str, str] = {
    "math":  "MATH",
    "poem":  "POEM",
    "ref":   "REF",
    "score": "SCORE",
    "table": "TABLE",
    "hiero": "HIEROGLYPH",
    "nowiki": "NOWIKI",
    "includeonly": "INCLUDEONLY",
}


def _derive_html_tag_label(raw: str) -> str:
    m = _HTML_TAG_NAME_RE.match(raw)
    if not m:
        raise ValueError(
            f"HTML_TAG raw doesn't open with a tag: {raw[:40]!r}")
    tag = m.group(1).lower()
    # `<span style="…{{mirrorH}}…">…</span>` — a horizontally-mirrored glyph span
    # (ALPHABET's left-right-flipped Etruscan / Italic / Cleonae letters).  The
    # `mirrorH` token in the style attribute is the signal; the MIRROR_GLYPH
    # producer emits `«MIRROR:content«/MIRROR»` and the viewer applies
    # `transform: scaleX(-1)`.
    if tag == "span" and re.match(
            r'<span\s+style\s*=\s*"[^"]*\{\{mirrorH\}\}', raw, re.IGNORECASE):
        return "MIRROR_GLYPH"
    # `<span title="T">X</span>` — translit-tooltip / editorial-drop.  Checked
    # BEFORE the styled `_STYLED_WRAPPER_RE` fallthrough (the former
    # `_process_styled` precedence): a `title=` span is excluded from
    # `_STYLED_WRAPPER_RE` by construction, so the two are mutually exclusive,
    # but span-title is the more specific discriminator and is tried first.
    if _SPAN_TITLE_OPEN_RE.match(raw):
        return "SPAN_TITLE"
    # Styled `<div>`/`<p>`/`<span>` (carries `{{Ts}}`/`style=`/`align=`) or
    # `<ins>` → HTML_STYLE.  The producer (`process_html_style`) derives the CSS
    # and recurses the inner.  A BARE `<div>`/`<span>`/`<p>` never reaches the
    # walker as an HTML_TAG (it isn't bounded as one), so this only ever sees the
    # styled forms the walker lifted.
    if _STYLED_WRAPPER_RE.match(raw) or _INS_OPEN_RE.match(raw):
        return "HTML_STYLE"
    if tag not in _HTML_TAG_LABEL:
        raise ValueError(
            f"Unknown HTML tag for HTML_TAG shape: {tag!r}")
    return _HTML_TAG_LABEL[tag]


def _derive_html_self_closing_label(raw: str) -> str:
    if raw[:13].lower().startswith("<pagequality"):
        return "PAGEQUALITY"
    # `<section begin="X"/>` / `<section end/>` — Wikisource transclusion marker;
    # a self-closing structural tag carrying boundary identity, no inner content.
    # The producer reads the raw tag (its name is boundary metadata).
    if raw[:9].lower().startswith("<section"):
        return "SECTION"
    return "REF_SELF"


def _derive_double_bracket_label(raw: str) -> str:
    m = _BRACKET_PREFIX_RE.match(raw)
    if m and m.group(1).lower() in {"file", "image"}:
        # Every `[[File:]]`/`[[Image:]]` is IMAGE — bare or captioned (thumb/frame).
        # The ONE producer detects a caption and lays it out; classify stays structural.
        return "IMAGE"
    if m and m.group(1).lower() == "author":
        return "AUTHOR_LINK"
    if re.match(r"\[\[\s*1911\s+[Ee]ncyclop", raw, re.IGNORECASE):
        return "EB1911_SELFREF"
    # `[[#Section]]` — a bare same-article anchor link (no `prefix:`); the resolver
    # reads the leading `#` as "this article, section Section".
    if re.match(r"\[\[\s*#", raw):
        return "FRAGMENT_LINK"
    # Any other `[[Target]]` / `[[Target|Display]]` — a generic wiki cross-reference.
    # Recognition is shape-only (it's a bracket link); the resolution ladder decides
    # internal «LN» / external «XL» / strip.  No raise — every `[[…]]` is a link.
    return "WIKILINK"


def _derive_double_brace_label(raw: str, inner_text: str = "") -> str:
    # ── The four template-form STYLED-derived structures ──────────────────
    # Drained out of the old SHAPE_STRIP / SHAPE_PARAM / SHAPE_SHOULDER /
    # SHAPE_RUNNING_HEADER walker shapes: recognized by NAME (the SAME regexes
    # the walker used to bound them) rather than by a dedicated shape.  Placed
    # FIRST — the walker tried these openers BEFORE the image / link / spacer
    # families, so the same precedence holds here; none of the four names
    # collides with the specific checks below.  Producers + dispatch labels
    # unchanged (`process_strip` / `process_param` / `process_shoulder` /
    # `process_running_header`).
    #   `{{center|…}}`/`{{csc|…}}`/`{{left|…}}`/… template-form styler → STRIP.
    if _TEMPLATE_STYLE_RE.match(raw):
        return "STRIP"
    #   `{{Fs|108%|X}}`/`{{font size|N%|X}}`/… param-valued styler → PARAM.
    if _TEMPLATE_PARAM_STYLE_RE.match(raw):
        return "PARAM"
    #   `{{EB1911 Shoulder Heading|…}}` (+ HeadingSmall / EB9 Margin Note) → SHOULDER.
    if _SHOULDER_HEADING_RE.match(raw):
        return "SHOULDER"
    #   `{{rh|left|center|right}}` / `{{Running header|…}}` 3-column frame → RUNNING_HEADER.
    if _RUNNING_HEADER_RE.match(raw):
        return "RUNNING_HEADER"
    # Image template spellings — all IMAGE; the one producer parses each to a filename
    # (and, for `plain image with caption`, its `caption=`).  `{{Css image crop}}` is a
    # DjVu crop (geometry-hash filename); `{{raw image}}` a full-page DjVu scan / file;
    # `{{plain image with caption}}` a named-param figure macro.
    if re.match(r"\{\{\s*Css image crop\b", raw, re.IGNORECASE):
        return "IMAGE"
    if re.match(r"\{\{\s*raw\s+image\b", raw, re.IGNORECASE):
        return "IMAGE"
    if re.match(r"\{\{\s*plain image with caption\b", raw, re.IGNORECASE):
        return "IMAGE"
    # (`{{ordered list|…}}` is intercepted upstream as an OUTLINE composite — see
    # `_classify_ordered_list_composite` — so it never reaches this leaf-label path.)
    # `{{EB1911 article link|…}}` — cross-reference link; matched on the raw opener
    # (like the image cases) since the first-token name is the ambiguous "eb1911".
    # `CE article link` (Catholic Encyclopedia cross-edition) joins the family — a
    # link is a link; the «LN» ladder hunts EB11, then external WS, then strips.
    if re.match(r"\{\{\s*(?:EB1911|EB9|CE)\s+article\s+link\b", raw, re.IGNORECASE):
        return "EB1911_ARTICLE_LINK"
    # `{{EB1911 intra-article link|Section}}` — a same-article subsection link (the
    # `[[#Section]]` bracket form's template twin).  Matched on the raw opener.
    if re.match(r"\{\{\s*EB1911\s+intra-article\s+link\b", raw, re.IGNORECASE):
        return "INTRA_ARTICLE_LINK"
    # Target-first link siblings — lkpl / 1911link / EB1911 link, plus the EB9/CE
    # cross-edition family (`{{9link}}`, `{{EB9link}}`, `{{EB9 lkpl}}`, `{{CE lkpl}}`,
    # `{{1911 article link}}` target-first, `{{EB9 Intra-Article Link}}`).  A link is
    # a link: «LN» + the ladder hunts EB11 first, then external WS, then strips —
    # so a 9th-edition ref to a topic EB11 also covers resolves to OUR article.
    # (NOT the display-first "EB9/EB1911 article link" above, NOT the EB1911
    # self-fragment "intra-article link" below.)
    if re.match(r"\{\{\s*(?:(?:EB1911|DNB|EB9|CE|Oxon)\s+lkpl|1911link|11link|EB1911\s+link"
                r"|EB9link|9link|1911\s+article\s+link|EB9\s+intra-article\s+link)\b",
                raw, re.IGNORECASE):
        return "TARGET_FIRST_LINK"
    # `{{EB1911 footer …}}` — the contributor signature footer.  RECOGNIZED as a bounded
    # CONTRIBUTOR_FOOTER node; the byline + removal are the decorator's job, not here.
    if re.match(r"\{\{\s*EB1911\s+footer\b", raw, re.IGNORECASE):
        return "CONTRIBUTOR_FOOTER"
    # `{{EB1911 TAs}}` etc. — the bare-initials sign-off SHORTCUT (capital-led,
    # immediately closed): the squashed twin of the footer.  Same node, so the
    # producer renders its initials and the harvest binds the contributor.
    if re.match(r"\{\{\s*[Ee][Bb]1911\s+[A-Z][A-Za-z*.\-]{0,5}\s*\}\}", raw):
        return "CONTRIBUTOR_FOOTER"
    # `{{EB1911 Coordinates|D|M[|S]|H}}` — a single geographic coordinate (lat or
    # lon).  Real place data; the COORDINATES producer formats it `D°M′[S″]H`.
    if re.match(r"\{\{\s*EB1911\s+Coordinates\b", raw, re.IGNORECASE):
        return "COORDINATES"
    # Spacer / rule / char-escape leaves — em/gap/clear/ditto/dhr/rule/bar/shy
    # and the literal-char escapes ({{=}}, {{(}}, {{...}}, …).  (`anchor` is NOT
    # here — it's a link target, routed to ANCHOR below.)
    if re.match(r"\{\{\s*(?:(?:em|gap|clear|ditto|dhr|rule|bar|shy)\b"
                r"|=|\(|\)|'|!|\*\*\*|\*|–|\.\.\.|…)", raw, re.IGNORECASE):
        return "SPACER"
    # Content extractors — the visible arg is the content; producer unwraps + recurses it.
    # FRAME-dissolution unwrappers (vrl/phn/definition/nsl/wdl/suspect/nodent) join here —
    # metadata wrappers whose content is the arg the producer keeps.
    if re.match(r"\{\{\s*(?:tooltip|abbr|lang|sic|fqm|drop\s?initial"
                r"|definition|nodent|vrl|phn|nsl|wdl|suspect|di)\b", raw, re.IGNORECASE):
        return "CONTENT_EXTRACT"
    # Bar-less `num \over den` fraction (`{{1\over 2}}` / `{{\it a \over b}}` /
    # `{{\kappa\over\kappa'}}`) — NO `name|` token, the whole inner is the fraction.
    # Matched on the raw containing the literal `\over` token (as a whole LaTeX
    # command — NOT `\overline`/`\overrightarrow`, hence the no-letter lookahead),
    # BEFORE the name extractor (which would capture `1\over` and raise).  →
    # FRACTION; the composite chops on `\over` into num/den CELL nodes the producer
    # reassembles.  Guarded by `"|" not in name region`: a real `{{name|…}}` template
    # whose CONTENT happens to carry `\over` (e.g. `{{ne||<math>…\over…</math>}}`)
    # routes by its NAME below, not here.  (In real bodies every bare `\over` rides
    # inside an opaque `<math>` and never reaches here; this routes the bare
    # standalone form the crash-check harness exercises.)
    if re.search(r"\\over(?![A-Za-z])", raw) and not re.match(
            r"\{\{\s*[^|{}]+\|", raw):
        return "FRACTION"
    m = _TEMPLATE_NAME_RE.match(raw)
    if not m:
        raise ValueError(
            f"DOUBLE_BRACE raw doesn't open with a template: {raw[:40]!r}")
    name = m.group(1).lower()
    # `{{section|Name}}` / `{{anchor|…}}` / `{{anchor+|…}}` — Wikisource link-target
    # anchors (no visual output, save anchor+'s display text).  Distinct from the
    # `<section>` boundary tag; CARRIED as «ANCHOR» so same-article `[[#Name]]`,
    # cross-article `…#Name`, and Reader's-Guide deep-links resolve against them.
    if name == "section" or name.startswith("anchor"):
        return "ANCHOR"
    # Fraction family (sfrac/mfrac/frac/over/EB1911 tfrac/…) — a styler lifted
    # as an element; the FRACTION producer recurses its slots.  Matched on the
    # raw opener (so `{{EB1911 sfrac|…}}` routes here, not to the `eb1911`
    # CONTRIBUTOR_FOOTER case below).
    if _FRAC_LABEL_RE.match(raw):
        return "FRACTION"
    # `{{lb-|N}}` / `{{lb-}}` / `{{lb|N}}` — pound-weight glyph leaf (`N lb`),
    # promoted out of the body-text `_convert_lb_dash` re.sub.  Both spellings
    # (`lb` and `lb-`) are the same pound template.
    if name in ("lb-", "lb"):
        return "LB"
    # `{{cite|Work Title}}` — a cited-work-title wrapper (the title may carry a
    # `[[link]]`, extracted as a child).  The producer italicizes it; the walker
    # only extracts `{{cite|` (pipe), so a `{{cite book|…}}` would not reach here.
    if name == "cite":
        return "CITE"
    # `{{sub|x}}` / `{{sup|x}}` — sub/superscript typography (out of body-text's
    # `_convert_sub_sup`); the producer recurses the slot + Unicode-translates.
    if name in {"sub", "sup"}:
        return "SUBSUP"
    # `{{img float|…}}` / `{{figure|…}}` / `{{FI|…}}` — a captioned figure → IMAGE; the one
    # producer lays the caption out centered below the plate (no float).  (`img float`
    # tokenizes as "img" — whitespace stop in the template-name pattern.)
    if name in {"img", "figure", "fi"}:
        return "IMAGE"
    if name == "hieroglyph":
        return "HIEROGLYPH"
    # `{{ppoem|…}}` — preformatted-poem template; the verse analog of `<poem>`.
    # `_process_ppoem` strips the stanza-frame control params and emits VERSE.
    if name == "ppoem":
        return "PPOEM"
    # `{{dual line|A|B}}` — pure layout primitive (two-line stack: table
    # headers, hyphenations, figure-caption splits, stacked math/chem
    # notation).  ONE label → a two-CELL decompose node (`_classify_dual_line_
    # composite`).  The old chem-shaped → CHEM_DUAL / math-shaped → MATH_DUAL
    # sub-classification was speculative specificity — a content-shape predicate
    # routing to byte-identical producers, reserving a home for family-specific
    # rendering that never arrived.  Collapsed: each line's chem/math content is
    # produced by its own producer as a cell child, so the split bought nothing.
    if name == "dual":
        return "DUAL_LINE"
    # Labeled-display-equation templates (`{{equation}}` / `{{MathForm1}}` / `{{ne}}`) —
    # ONE label.  They are not three math things but three arg conventions of one labeling
    # WRAPPER whose body is MIXED content (prose lead-ins, `«I»`, {{Greek}}, {{sfrac}}, an
    # optional opaque `<math>`).  So it decomposes (`_classify_equation_composite`: a NUMBER
    # cell + a BODY cell); the parse-args slicing is standard producer stuff, the `«EQN»`
    # block the one math-qua-math bit.  Corpus: 521/983 `ne` bodies carry no `<math>` at all.
    if name in ("equation", "mathform1", "ne"):
        return "MATH_EQUATION"

    # ── Name-driven routing for the backlog families ──────────────────────────
    # With the ONE generic `{{…}}` recognizer, EVERY double-brace reaches here, so
    # the remaining backlog families route by their FULL name (multi-word safe).
    full = _db_name(raw)

    # Fraction family in the BARE `{{name}}` form (the `_FRAC_LABEL_RE` above is
    # pipe-form; a bare `{{sfrac}}` / `{{over}}` still routes here).
    if full in _FRAC_NAMES:
        return "FRACTION"
    # Contributor sign-off SHORTCUT in the synthetic pipe form (`{{EB1911 TAs|x}}`):
    # the bare-`}}` regex above claims `{{EB1911 TAs}}`, but the harness also probes
    # the pipe form.  `EB1911 <INITIALS>` (capital-led short token) is the squashed
    # footer twin → CONTRIBUTOR_FOOTER.
    if re.match(r"eb1911 [a-z][a-z*.\-]{0,5}$", full):
        return "CONTRIBUTOR_FOOTER"
    # Known stylers in the BARE `{{name}}` form (the pipe-form regexes above only
    # match `{{name|…}}`; a registered styler routes whether or not it has content).
    if full in _TEMPLATE_STYLE_WRAPPERS:
        return "STRIP"
    if full in _TEMPLATE_PARAM_STYLE_WRAPPERS:
        return "PARAM"
    # Shoulder-heading / running-header in the bare form (regexes above are pipe-form).
    # Prefix match (not anchored) tolerates an OCR `&nbsp;`/nbsp tail on the name
    # (`{{EB1911 Shoulder Heading&nbsp;|…}}`).
    if re.match(r"(?:eb1911 shoulder heading|eb9 margin note)", full):
        return "SHOULDER"
    if full in ("rh", "running header", "runningheader"):
        return "RUNNING_HEADER"

    # Spacer / glyph / char-escape leaves (the bracket-escape names handled by the
    # regex above; here the WORD-named spacers the backlog surfaced).  Content-less
    # frame CONTROL markers (`{{multicol-break}}`, `{{col-begin}}`, …) join them —
    # they carry no content, so SPACER (which renders unknown leaves to nothing) is
    # the right empty-leaf home.
    if full in _SPACER_NAMES:
        return "SPACER"

    # Page-split words (hws/hwe/lps/lpe) → one producer: the START marker rejoins
    # the word, the END marker renders empty.
    if full in _SPLIT_WORD_NAMES:
        return "SPLIT_WORD"

    # Hanging indent — `{{hi|W|text}}` / `{{hanging indent|W|text}}`.  The source
    # states the indent width; we RENDER it (a block with that width), never drop
    # it (dropping flattens the list it formats).  One owner for both names — was
    # split: `hi` → hardcoded styler, `hanging indent` → FRAME-drop.
    if full in _HANGING_INDENT_NAMES:
        return "HANGING_INDENT"
    # Row-spanning curly brace — `{{brace2|N|dir}}`.  Render the glyph (stretched
    # to its N-row span), never leak its `N|dir` arguments as text.
    if full in _BRACE_NAMES:
        return "BRACE"
    # Script wrappers — `{{greek|…}}`, `{{polytonic|…}}`, …: unwrap to the content
    # (the glyphs ARE the text).  An explicit producer, NOT a strip-by-name.
    if full in _LANG_NAMES:
        return "LANG"
    # `{{11co|DEG|[MIN|]DIR}}` — a single lat/long coordinate; render the value.
    if full in _COORD_NAMES:
        return "COORD"
    # A standalone `{{familytree|…}}` / `{{chart2|…}}` / `{{tree chart|…}}` — a genealogy grid
    # macro OUTSIDE the paired `/start…/end` region the walker normally captures as CHART2
    # (a page-split-orphaned row) → CHART2, so it still renders the tree image rather than
    # leaking.  The rest of the old FRAME catch-all is dissolved to real homes: columns/flex-
    # wrap → TABLE, ti/margin-left/size → PARAM, outdent → HANGING_INDENT, the metadata
    # unwrappers (vrl/phn/definition/nsl/wdl/suspect/nodent) → CONTENT_EXTRACT.
    if full in ("familytree", "tree chart", "chart2"):
        return "CHART2"

    # Table-of-contents rows — one dotted-leader row each; the producer reads the
    # template params as cells and renders the row content in place.
    if full in _TOC_ROW_NAMES:
        return "TOC_ROW"

    # `{{main other|main-NS|other-NS}}` — namespace switch around split prose.  We
    # assemble the MAIN-namespace article, so keep PARAM 1 (recursed); the p60/p63
    # "Needlepoint lace…" split is the only occupant.
    if full == "main other":
        return "MAIN_OTHER"

    # Footnote-list emitters render to empty (footnotes are inline in this edition).
    if full in _REFS_NAMES:
        return "REFS"

    # Missing-asset placeholders → a visible `[missing …]` stub.
    if full in _MISSING_NAMES:
        return "MISSING"

    # Inline typography (sfrac/frac/mfrac/over/sfracN/EB1911 variants/
    # sub/sup) stays in body-text — the source doesn't declare those
    # as math via the template name; they're rendered as typography
    # whose output flows back into prose.  Walker doesn't lift them.
    #
    # Anything else reaching here is a template we don't yet handle.  It LEAKS —
    # renders raw and visibly — so the build keeps going and the leak audit
    # surfaces it as the signal to write its producer.  Never sweep, never halt:
    # a miss must be loud AND recoverable, which a raise (crash) is not.
    return "DOUBLE_BRACE_LEAK"


# The SPACER vocabulary — word-named spacer/glyph/escape LEAVES + content-less frame
# CONTROL markers — lives with its producer (`_spacer.py`), which is what actually
# renders each name to a glyph or to nothing.  We import it as the single source of
# truth for what this classifier labels SPACER; the dependency points
# classifier → producer, the natural direction.  (`_spacer` imports nothing from the
# element package, so this back-import is cycle-free.)
from britannica.pipeline.stages.elements._spacer import _SPACER_NAMES

# Hanging indent — `{{hi}}` / `{{hanging indent}}` / `{{outdent}}` (synonyms: a
# first-line outdent).  RENDERED, not dropped: the source states the indent width
# (default 2em) and it formats a list, so its producer (`_hanging.py`) reads the
# width and emits the block.  `{{outdent}}` (no width arg) takes the 2em default.
_HANGING_INDENT_NAMES: frozenset[str] = frozenset({"hi", "hanging indent", "outdent"})

# Row-spanning curly brace grouping table rows — `{{brace2|N|dir}}` (4000+ uses).
# Its own producer (`_brace.py`) renders the glyph; we CARRY it, never strip it.
# (It WAS a `{}` "decoration not rendered" entry in the styler registry — the very
# leak this fixes.)  The bare `{{brace}}` stays the spacer-glyph it already is.
_BRACE_NAMES: frozenset[str] = frozenset({"brace2"})

# Script / language wrappers — the glyphs ARE the content; `_lang.py` unwraps to
# it.  Drained from the styler registry's empty-spec `{}` branch (now deleted): a
# script is a DECISION (content bare), not a blind strip-by-name.
_LANG_NAMES: frozenset[str] = frozenset(
    {"greek", "polytonic", "hebrew", "arabic", "he", "latin", "coptic", "grc"})

# `{{11co|DEG|[MIN|]DIR}}` — a single EB1911 geographic coordinate; `_coord.py`
# renders `DEG° [MIN′ ]DIR` (was a mislabeled `{}` "column wrapper" strip entry).
_COORD_NAMES: frozenset[str] = frozenset({"11co"})

# The old FRAME catch-all is DISSOLVED — each shape routed to its real family (2026-07):
# columns / flex-wrap → TABLE, ti / margin-left / size → PARAM, outdent → HANGING_INDENT,
# familytree / tree chart / chart2 → CHART2, the metadata unwrappers (vrl / phn / definition
# / nsl / wdl / suspect / nodent) → CONTENT_EXTRACT.  `process_frame` is gone.

# Page-split words — a word broken across a page break: the START marker owns the
# whole rejoined word, the END marker renders nothing.  Two encodings, one producer
# (`_splitword.process_split_word`): `{{hws|…|WORD}}` / `{{hwe}}` carry the word in
# a POSITIONAL slot; `{{lps|hws=A|hwe=B}}` / `{{lpe}}` carry it as two NAMED halves.
_SPLIT_WORD_NAMES: frozenset[str] = frozenset({
    "hws", "hyphenated word start", "hwe", "hyphenated word end",
    "lps", "lpe",
})

# Table-of-contents rows — `{{Dotted TOC line|num|entry|value}}` /
# `{{Dotted TOC page listing|…|entrytext=…|pagetext=…}}` / `{{TOC line|…}}`.  Each
# is ONE dotted-leader row; its cells are TEMPLATE PARAMS (not divider fields), so
# the producer reads them and renders the row's content (recursed) in place.  The
# `{|` / `<div>` / block-center it sits in supplies the surrounding structure.
_TOC_ROW_NAMES: frozenset[str] = frozenset({
    "dotted toc line", "dotted toc page listing", "toc line",
})

# Footnote-list emitters → empty (footnotes render inline in this edition).
_REFS_NAMES: frozenset[str] = frozenset({
    "smallrefs", "reflist", "ref", "smallref", "blockref",
})

# Missing-asset placeholders → a visible `[missing …]` stub.
_MISSING_NAMES: frozenset[str] = frozenset({
    "missing table", "missing image", "missing math", "formula missing",
})

# Fraction family (BARE `{{name}}` form — `_FRAC_LABEL_RE` above is pipe-form).
# Mirrors `_FRAC_LABEL_RE`'s name list (lower-cased, whitespace-collapsed).
_FRAC_NAMES: frozenset[str] = frozenset({
    "sfrac nobar", "eb1911 sfrac", "eb1911 tfrac",
    "eb¹⁹¹¹ sfrac", "eb¹⁹¹¹ tfrac", "eb₁₉₁₁ ₜfᵣₐc",
    "sfracn", "sfrac", "mfrac", "frac", "over", "binom",
})


# ── BRACE_PIPE composite classifier ───────────────────────────────────
#
# Delegates to today's `_classify_table` predicates via a legacy-shaped
# ElementRegistry view.  Transitional: when the predicates are
# rewritten to take `dict[str, ClassifiedElement]` directly the bridge
# disappears.


def _label_to_legacy_name(label: str) -> str:
    """Map a classifier label back to today's walker source-type name.

    Identity for non-table labels (the walker name equals the
    classifier label for MATH / POEM / IMAGE / etc.).  TABLE_LABELS
    collapse to 'TABLE' — the walker writes 'TABLE' for every
    brace-pipe element regardless of the sub-classification the
    classifier eventually assigns.
    """
    if label in TABLE_LABELS:
        return "TABLE"
    return label


def _to_legacy_registry(
    inner_registry: dict[str, ClassifiedElement]
) -> ElementRegistry:
    """Build a legacy `ElementRegistry` view over a
    `dict[str, ClassifiedElement]`.  Lets today's table predicates
    and producer handlers run unchanged against the new classified
    tree.

    Populates ``markers`` from ``ce.marker`` where available — empty
    during classification, populated bottom-up during the producer
    pass — so producer handlers that inspect inner markers see the
    right state.
    """
    reg = ElementRegistry()
    for ph, ce in inner_registry.items():
        legacy_name = _label_to_legacy_name(ce.label)
        reg.elements[ph] = (legacy_name, ce.raw)
        reg.labels[ph] = ce.label
        reg.inners[ph] = ce.inner_text
        reg.inner_registries[ph] = _to_legacy_registry(ce.inner_registry)
        if ce.marker:
            reg.markers[ph] = ce.marker
    return reg


def _classify_brace_pipe(
    raw: str,
    inner_text: str,
    inner_registry: dict[str, ClassifiedElement],
) -> str:
    """Run today's wikitable predicates over the classified inner
    registry.  Returns one of `TABLE_LABELS`.
    """
    # Late import: `_classify_table` is defined in `__init__.py`
    # alongside the legacy three-pass pipeline.
    from britannica.pipeline.stages.elements import _classify_table
    legacy = _to_legacy_registry(inner_registry)
    label = _classify_table(raw, inner_text, legacy)
    # Figure `{|`-tables are just tables — collapse the dead pairability labels to
    # TABLE (same producer, no shadow recognizer).
    return "TABLE" if label in _FIGURE_TABLE_LABELS else label


# ── `<table>` routing ─────────────────────────────────────────────────
#
# A `<table>` is classified by the SAME `_classify_table` ladder a `{|`
# wikitable gets, so a disguised non-table (figure/math/chem) leaves the
# table path onto its own producer.  Everything the ladder calls a table is
# just "TABLE": the unified table engine (`_process_table_unified`) is
# syntax-neutral — it renders `<table>` and `{|` alike by opener — so the
# else-branch IS "TABLE", not a separate HTML_TABLE label.  Only labels whose
# own producer is verified `<table>`-ready (figure/math/chem) route away; any
# other label (incl. a `<table>` the ladder calls LAYOUT_WRAPPER)
# stays on the table engine — exactly as before the collapse, when the
# else-branch was HTML_TABLE → the same unified engine.
# A figure `{|`-table is NOT a separate family: a plate is processed exactly like
# an article (image leaves + legend prose in a table), fenced off only so the
# page-seam joiner can treat it as one unit — which the `{|` bounding already gives.
# So every figure label collapses to TABLE; the article walker's table path
# (`_process_table_unified`) recurses it identically.  These labels are now dead
# (`_classify_icl_shape` / `_layout` / `_figure_decompose` are the shadow recognizer
# left to delete).
_FIGURE_TABLE_LABELS: frozenset[str] = frozenset({
    "CAPTIONED_FIGURE", "CAPTIONED_FIGURE_INLINE", "UNPAIRED_FIGURE_GROUP",
    "LEGENDED_FIGURE", "LEGENDED_FIGURE_BESIDE", "LEGENDED_FIGURE_CHILD",
    "FIGURE_GROUP", "LAYOUT_WRAPPER",
})
# Only CHEM keeps its own `<table>`-aware producer (`_split_html_chem_row`); MATH
# isn't routed away (`<math>` is a leaf, so a math-cell table is just a TABLE).
_HTML_TABLE_ROUTE_AWAY: frozenset[str] = frozenset({"CHEMISTRY_LAYOUT"})


def _classify_html_table(
    raw: str,
    inner_text: str,
    inner_registry: dict[str, ClassifiedElement],
) -> str:
    """Classify a `<table>` element via the shared table classifier.  A
    non-table producer family (figure/math/chem) keeps its own label and
    leaves the table path; every genuine table is "TABLE" and renders through
    the unified, syntax-neutral table engine."""
    from britannica.pipeline.stages.elements import _classify_table
    legacy = _to_legacy_registry(inner_registry)
    label = _classify_table(raw, inner_text, legacy)
    return label if label in _HTML_TABLE_ROUTE_AWAY else "TABLE"


# ── Table composite: the grid recurses into ROW / CELL nodes in the one tree ──
#
# Was a produce-time re-walk (`_process_table_unified` → `process_elements` per
# cell), which stopped the classify recursion dead at the `{|`/`<table>` leaf and
# hid the whole cell tree from every structural consumer (so "is this heading
# inside a table?" had no home but a raw-text regex).  Now the grid decomposes
# into REAL children AT CLASSIFY TIME: `recognize_table` chops table→rows→cells
# (the ONLY table-specific recognition), each cell's content classifies through
# the ordinary `classify_article` (a cell is prose in a box), and the
# TR/TD/TH/TABLE producers reassemble bottom-up.  `classify_article` runs ONCE
# per article; recognition + attr-fold are reused verbatim, so the emitted
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _mint_ph() -> str:
    """A fresh placeholder key off the shared module counter, so a synthetic
    ROW/CELL/CAPTION key never collides with a walker-minted one."""
    return f"{_PH}ELEM:{_next_placeholder_id()}{_PH}"


def _classify_table_composite(raw: str, grid: str) -> ClassifiedElement:
    """Decompose a `TABLE`-labelled grid into a TABLE node whose children are ROW
    nodes (each holding TD/TH cell nodes) plus, if present, a CAPTION node.  Each
    cell / caption body classifies generically into the tree — a bare `[[File:]]`
    in a cell is an inline image leaf (the old `cell_recurse` contract).  The cell
    body's own `.strip(' \\t')` lives in the TD/TH producer
    (it must run AFTER a `{{spaces|N}}` padding decodes to real space); the
    caption's `.strip()` lives in the TABLE producer, both post-recursion."""
    from britannica.pipeline.stages.elements._table_fold import recognize_table
    caption_raw, rows = recognize_table(_COMMENT_RE.sub("", grid))
    children: dict[str, ClassifiedElement] = {}
    if caption_raw:
        cap_body, cap_reg = classify_article(caption_raw)
        children[_mint_ph()] = ClassifiedElement("CAPTION", "", cap_body, cap_reg)
    row_phs: list[str] = []
    max_cols = 0
    for row_attr, cells in rows:
        if not cells:
            continue
        cell_phs: list[str] = []
        cell_children: dict[str, ClassifiedElement] = {}
        row_cols = 0
        for sep, cell_attr, content in cells:
            cell_body, cell_reg = classify_article(content)
            ph = _mint_ph()
            cell_children[ph] = ClassifiedElement(
                "TH" if sep == "!" else "TD", cell_attr, cell_body, cell_reg)
            cell_phs.append(ph)
            # Count CELLS (content columns), NOT the colspan sum: a lone cell with
            # colspan="35" is a full-width span hack (ALGEBRA's centering table) — one
            # content column, not 35.  `«COLS»` feeds ONLY the wide-table decision, so
            # summing colspan wrongly trips the expand wrap; cell-count is the metric.
            row_cols += 1
        max_cols = max(max_cols, row_cols)
        rph = _mint_ph()
        children[rph] = ClassifiedElement(
            "ROW", row_attr, "".join(cell_phs), cell_children)
        row_phs.append(rph)
    # The column count rides the TABLE node's inner_text as a `«COLS:N»` prefix
    # (consumed by the producer, never emitted) — computed HERE, off the OUTER
    # rows/cells, so a nested table's cells can't inflate it.  The producer needs
    # it for the wide-table decision but only sees (raw, inner_text) at emit.
    return ClassifiedElement(
        "TABLE", raw, f"«COLS:{max_cols}»" + "".join(row_phs), children)


def _classify_outline_composite(raw: str, block: str) -> ClassifiedElement:
    """The OUTLINE decomposer — twin of `_classify_table_composite`.  ONE outline
    whose items nest: split the block into leveled items (`recognize_outline`),
    and let each item own the deeper items that follow it directly as its own
    item-children — not a tree of wrapper outlines.  Each item's own content
    recurses back through `classify_article`, so a `:<math>…` item's math becomes
    a real MATH child, exactly as a table cell's content does."""
    from britannica.pipeline.stages.elements._outline import recognize_outline
    phs, reg = _outline_items(recognize_outline(block))
    return ClassifiedElement("OUTLINE", raw, "".join(phs), reg)


def _classify_ordered_list_composite(raw: str) -> ClassifiedElement:
    """`{{ordered list|…}}` is a degenerate OUTLINE — the same nested-item structure,
    recognized by an explicit `{{…}}` delimiter (like a table's `{|`) instead of by
    `:`-indent, its items pre-labelled (I./a./1.).  `_walk` parses the template into the
    same `(depth, content)` rows a `:`-block yields; run them through the ONE outline
    decomposer so an ordered list produces `«OUTLINE»«OLI»` and renders like any other."""
    from britannica.pipeline.stages.elements._ordered_list import _walk
    rows: list[tuple[int, str]] = []
    _walk(raw, 0, rows)
    phs, reg = _outline_items(rows)
    return ClassifiedElement("OUTLINE", raw, "".join(phs), reg)


_COLUMNS_RE = re.compile(r"\{\{\s*columns\b", re.IGNORECASE)
# `{{flex wrap centre|A|B|…}}` (double-brace cell-row, e.g. vol21 PIG's breed photos) — the
# same row-of-cells shape as `{{columns}}`.  Require the content pipe so the paired
# `{{flex wrap centre/s}}…/e` CENTER wrapper (a different shape) is NOT caught here.
_FLEXWRAP_CELLS_RE = re.compile(r"\{\{\s*flex\s+wrap\s+centre\s*\|", re.IGNORECASE)
_COL_CONTENT_RE = re.compile(r"^\s*col(\d+)\s*=(.*)$", re.DOTALL | re.IGNORECASE)


def _columns_content_slots(raw: str) -> list[str]:
    """The CONTENT columns of a `{{columns|colwidth=…|col1=A|col2=B|…}}` frame — the `colN=`
    slots in N order (plus any bare positional), dropping the layout params
    (`colwidth`/`class`/`style`/`gap`/`rules`/…).  These are the table's cells; the frame is
    presentation the single-column medium renders as a plain row."""
    from britannica.pipeline.stages.elements._link import _split_top_pipes
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))
    numbered: dict[int, str] = {}
    positional: list[str] = []
    for part in _split_top_pipes(inner)[1:]:          # drop the template name
        m = _COL_CONTENT_RE.match(part)
        if m:
            numbered[int(m.group(1))] = m.group(2)
        elif "=" not in part.split("|", 1)[0] and part.strip():
            positional.append(part)                    # bare positional content
        # else: a layout param (colwidth/class/style/…) — dropped
    return [numbered[k] for k in sorted(numbered)] + positional


def _classify_columns_as_table(raw: str) -> ClassifiedElement:
    """`{{columns|col1=A|col2=B|…}}` and `{{flex wrap centre|cell|cell|…}}` ARE tables in
    template syntax — one row of cells.  Decompose straight into the ONE recursive TABLE engine
    (twin of `_classify_table_composite`): each content column / cell becomes a TD whose body
    recurses via `classify_article` exactly like a wikitable cell, the layout params dropped.
    Recovers what `process_frame`'s max-by-len threw away — CASHEW's 8-item legend (col1+col2)
    and PIG's breed photos (2 per flex-wrap row), where only the longest cell used to survive."""
    cols = _columns_content_slots(raw)
    cell_children: dict[str, ClassifiedElement] = {}
    cell_phs: list[str] = []
    for content in cols:
        cell_body, cell_reg = classify_article(content)
        ph = _mint_ph()
        cell_children[ph] = ClassifiedElement("TD", "", cell_body, cell_reg)
        cell_phs.append(ph)
    rph = _mint_ph()
    return ClassifiedElement(
        "TABLE", raw, f"«COLS:{len(cols)}»" + rph,
        {rph: ClassifiedElement("ROW", "", "".join(cell_phs), cell_children)})


def _outline_items(items: "list[tuple[int, str]]"):
    """`(ordered placeholders, registry)` — one flat depth-tagged OUTLINE_ITEM per
    source item.  Nesting is NOT this producer's job: the render's one owner,
    `build_outline_ul`, densifies the sparse depths into nested `<ul>`s.  Each item's
    own content recurses through `classify_article`, so a `:<math>…` item's math
    becomes a real MATH child, exactly as a table cell's content does."""
    phs: list[str] = []
    reg: dict[str, ClassifiedElement] = {}
    for depth, content in items:
        item_body, item_reg = classify_article(content)
        iph = _mint_ph()
        reg[iph] = ClassifiedElement("OUTLINE_ITEM", str(depth), item_body, item_reg)
        phs.append(iph)
    return phs, reg


def _is_table_html_tag(raw: str) -> bool:
    """A `<table>` opener — the one HTML tag that decomposes as a table (the
    OPAQUE `<math>`/`<nowiki>`/`<score>` tags stay leaves via `_is_leaf_html_tag`)."""
    m = re.match(r"\s*<([A-Za-z][A-Za-z0-9]*)", raw)
    return bool(m) and m.group(1).lower() == "table"


def _classify_table_element(shape: str, raw: str) -> ClassifiedElement:
    """A `{|` or `<table>`.  Derive its label from the SAME empty registry the old
    leaf saw (so the label is byte-identical), then: a genuine `TABLE` decomposes
    into the tree; a COMPOUND_TABLE / CHEMISTRY_LAYOUT keeps its
    raw-reading producer and stays a leaf, exactly as before."""
    grid = strip_outer(shape, raw)
    label = _derive_label(shape, raw, grid, {})
    if label == "TABLE":
        return _classify_table_composite(raw, grid)
    return ClassifiedElement(label, raw, grid, {})


# ── Top-level label dispatcher ────────────────────────────────────────


def _derive_label(
    shape: str,
    raw: str,
    inner_text: str,
    inner_registry: dict[str, ClassifiedElement],
) -> str:
    if shape == SHAPE_BRACE_PIPE:
        return _classify_brace_pipe(raw, inner_text, inner_registry)
    if shape == SHAPE_HTML_TAG:
        m = _HTML_TAG_NAME_RE.match(raw)
        if m and m.group(1).lower() == "table":
            return _classify_html_table(raw, inner_text, inner_registry)
        return _derive_html_tag_label(raw)
    if shape == SHAPE_HTML_SELF_CLOSING:
        return _derive_html_self_closing_label(raw)
    if shape == SHAPE_DOUBLE_BRACKET:
        return _derive_double_bracket_label(raw)
    if shape == SHAPE_DOUBLE_BRACE:
        return _derive_double_brace_label(raw, inner_text)
    if shape == SHAPE_OUTLINE:
        return "OUTLINE"
    if shape == SHAPE_PAGE:
        return "PAGE"
    if shape == SHAPE_BODY:
        return "BODY"
    if shape == SHAPE_PAIRED_WRAPPER:
        # One paired open/close structure, two families distinguished by NAME:
        # a chart2 / familytree / tree-chart grid macro → CHART2 (the genealogy
        # producer); every other `{{NAME/s}}…{{NAME/e}}` centring wrapper → CENTER.
        if _CHART2_FAMILY_RE.search(raw):
            return "CHART2"
        return "CENTER"
    if shape == SHAPE_TITLE:
        return "TITLE"  # «TITLE»…«/TITLE» stamp (preprocess_article); producer recurses
    raise ValueError(f"Unknown shape: {shape!r}")


# ── Recursive classifier ──────────────────────────────────────────────


def _is_leaf_html_tag(raw: str) -> bool:
    """True for an `HTML_TAG` the classifier must NOT descend into: a `<table>`
    (the table producer recurses its own grid) or an OPAQUE tag — `<math>`/
    `<nowiki>`/`<score>`/… — whose interior is verbatim (LaTeX braces, lilypond
    chords).  Re-walking either tears a child out of content that owns it; the
    `<math>{{x^p}{y^q}}` brace pair mis-read as a `{{template}}` was that bug."""
    m = re.match(r"\s*<([A-Za-z][A-Za-z0-9]*)", raw)
    if not m:
        return False
    name = m.group(1).lower()
    return name == "table" or name in _OPAQUE_TAGS


def _classify_shoulder_composite(raw: str) -> ClassifiedElement:
    """A shoulder heading `{{EB1911 Shoulder Heading|…LABEL}}` is a COMPOSITE — its LABEL
    decomposes into child nodes (the SAME `classify_article` the old `process_elements` ran).
    `_shoulder_peel` extracts the last-positional label (dropping width=/align= params, its
    margin-wrap `<br>`s → space) before decomposing; the producer mints the «SH» slug off the
    assembled content.  Byte-identical."""
    from britannica.pipeline.stages.elements import _shoulder_peel
    label = _shoulder_peel(raw)
    placeholderized, tree = classify_article(label)
    return ClassifiedElement(
        label="SHOULDER", raw=raw, inner_text=placeholderized,
        inner_registry={ph: tree[ph]
                        for ph in sorted(tree, key=placeholderized.find)},
    )


def _classify_running_header_composite(raw: str) -> ClassifiedElement:
    """A running header `{{rh|left|centre|right}}` DECOMPOSES into three CELL nodes — one row of
    cells, exactly like `{{columns}}`→TABLE and an indent ladder→OUTLINE.  Chop the pipe args
    (`_running_header_cells`) and recurse each cell (its italic equation variables, small-caps
    plate numbers → real nodes); the producer reassembles the flex row from the three markers."""
    from britannica.pipeline.stages.elements import _running_header_cells
    return _decompose_cells("RUNNING_HEADER", raw, _running_header_cells(raw))


def _classify_title_composite(raw: str) -> ClassifiedElement:
    """The «TITLE»…«/TITLE» heading stamp is a COMPOSITE — its content decomposes into child
    nodes (the SAME classification the old `process_elements` ran, but at classify time, so a
    `«FN` in a bracketed title becomes a REF node in the one tree — which lets
    `resolve_ref_bodies` see the title's footnote definition — instead of a produce-time re-parse
    with fresh placeholders it can't match).  `strip_title_joint` removes the trailing display
    joint (comma / terminator period) BEFORE decomposing, exactly where the old producer stripped
    it, so both the DISPLAY marker and the PLAIN field stay joint-free and byte-identical."""
    from britannica.pipeline.stages.elements._title import strip_title_joint
    inner_raw = re.sub(r"^«TITLE»", "", raw)
    inner_raw = re.sub(r"«/TITLE»\s*$", "", inner_raw)
    placeholderized, tree = classify_article(
        strip_title_joint(inner_raw))
    return ClassifiedElement(
        label="TITLE", raw=raw, inner_text=placeholderized,
        inner_registry={ph: tree[ph]
                        for ph in sorted(tree, key=placeholderized.find)},
    )


def _classify_ppoem_composite(raw: str) -> ClassifiedElement:
    """`{{ppoem|…}}` is a COMPOSITE — its verse (peeled of the stanza-frame control params)
    decomposes into child nodes (the SAME classification the old `process_elements` ran, so a
    styler / link / footnote in the verse is a REAL node, not a produce-time re-parse).
    Byte-identical."""
    from britannica.pipeline.stages.elements._leaf import _ppoem_verse
    verse = _ppoem_verse(strip_outer(SHAPE_DOUBLE_BRACE, raw))
    placeholderized, tree = classify_article(verse)
    return ClassifiedElement(
        label="PPOEM", raw=raw, inner_text=placeholderized,
        inner_registry={ph: tree[ph]
                        for ph in sorted(tree, key=placeholderized.find)},
    )


def _classify_hanging_composite(raw: str) -> ClassifiedElement:
    """`{{hi|W|text}}` / `{{hanging indent|…}}` / `{{outdent|…}}` is a COMPOSITE — its content
    (the longest content slot, `<br>`→«BR») decomposes into child nodes (the SAME classification
    the old `process_elements` ran), so a list the hanging indent
    formats keeps its links / stylers / footnotes as REAL nodes.  The width is re-derived in the
    producer.  Byte-identical."""
    from britannica.pipeline.stages.elements._hanging import _hanging_peel
    _width, content = _hanging_peel(raw)
    placeholderized, tree = classify_article(content)
    return ClassifiedElement(
        label="HANGING_INDENT", raw=raw, inner_text=placeholderized,
        inner_registry={ph: tree[ph]
                        for ph in sorted(tree, key=placeholderized.find)},
    )


def _classify_image_composite(raw: str) -> ClassifiedElement:
    """An IMAGE with a caption is a COMPOSITE — the CAPTION decomposes into child nodes (the
    SAME classify the old `process_elements(caption_raw)` ran), so a
    caption's links / stylers / footnotes are REAL nodes in the one tree.  The fn / width /
    align stay a pure leaf parse in the producer; only the caption is a child.  A caption-less
    image → empty children → the producer emits the bare leaf.  Byte-identical."""
    from britannica.pipeline.stages.elements import _parse_image
    _fn, _w, _align, caption_raw = _parse_image(raw)
    placeholderized, tree = classify_article(caption_raw or "")
    return ClassifiedElement(
        label="IMAGE", raw=raw, inner_text=placeholderized,
        inner_registry={ph: tree[ph]
                        for ph in sorted(tree, key=placeholderized.find)},
    )


# Every peel/recurse/wrap label routed through the ONE classify path (`_classify_recurse_slot`):
# the leaf single-slot ones + the whole «LN» link family (`_LINK_LABELS`, whose recursed slot is
# the DISPLAY).  `_recurse_slot_content` (in `__init__`) is the shared PEEL side; `_PR_WRAP` the
# WRAP side.
_RECURSE_SLOT_LABELS: frozenset[str] = frozenset(
    {"LANG", "LB", "CITE", "SPLIT_WORD", "MAIN_OTHER",
     "STRIP", "PARAM", "HTML_STYLE"}) | _LINK_LABELS


def _classify_recurse_slot(raw: str, label: str) -> ClassifiedElement:
    """A single-slot leaf producer whose ONE recursive slot decomposes into child nodes (the SAME
    classify the old `process_elements(slot)` ran).  `_recurse_slot_content` returns the slot; the
    producer substitutes.  Byte-identical."""
    from britannica.pipeline.stages.elements import _recurse_slot_content
    slot = _recurse_slot_content(raw, label)
    placeholderized, tree = classify_article(slot)
    return ClassifiedElement(
        label=label, raw=raw, inner_text=placeholderized,
        inner_registry={ph: tree[ph]
                        for ph in sorted(tree, key=placeholderized.find)},
    )


def _decompose_cells(label: str, raw: str, slots: "list[str]") -> ClassifiedElement:
    """Decompose `raw` into a row of CELL nodes — one per slot string, each recursed via
    `classify_article`, exactly like a table cell.  The container producer
    reassembles its OWN layout from the ordered cell markers (RUNNING_HEADER's flex row,
    DUAL_LINE's stack, TOC_ROW's dotted leader, FRACTION's vulgar glyph).

    The multi-slot twin of `_classify_columns_as_table`: same chop → recurse-each →
    reassemble, minus the TABLE engine's ROW/`«COLS»` wrapping — these producers own their
    reassembly instead of going through the table path.  A CELL is a passthrough node (its
    marker IS its recursed content); iterate `inner_registry.elements` to read them back so
    an empty slot keeps its position."""
    cells: dict[str, ClassifiedElement] = {}
    phs: list[str] = []
    for slot in slots:
        body, reg = classify_article(slot)
        ph = _mint_ph()
        cells[ph] = ClassifiedElement("CELL", slot, body, reg)
        phs.append(ph)
    return ClassifiedElement(
        label=label, raw=raw, inner_text="".join(phs), inner_registry=cells)


def _classify_dual_line_composite(raw: str) -> ClassifiedElement:
    """`{{dual line|A|B}}` decomposes into a row of two stacked CELL nodes."""
    from britannica.pipeline.stages.elements._dual_line import _dual_line_cells
    return _decompose_cells("DUAL_LINE", raw, _dual_line_cells(raw))


def _classify_toc_row_composite(raw: str) -> ClassifiedElement:
    """A dotted-TOC-line template decomposes into a left-label CELL and a right-value CELL."""
    from britannica.pipeline.stages.elements._toc import _toc_row_cells
    return _decompose_cells("TOC_ROW", raw, _toc_row_cells(raw))


def _classify_fraction_composite(raw: str) -> ClassifiedElement:
    """A `{{sfrac|n|d}}`-family fraction decomposes into a CELL per slot (numerator /
    denominator, plus an optional whole part or the bar-less `\\over` pair)."""
    from britannica.pipeline.stages.elements._fraction import _fraction_parse
    return _decompose_cells("FRACTION", raw, _fraction_parse(raw)[1])


def _classify_equation_composite(raw: str) -> ClassifiedElement:
    """A labeled display equation `{{equation|…}}` / `{{MathForm1|…}}` / `{{ne|…}}` decomposes
    into a NUMBER cell and a BODY cell (`_eqn_parse` slices which arg is which per template);
    the producer decodes the number and renders the body into the `«EQN»` block.  The body's
    mixed content — prose lead-ins, `«I»`, {{Greek}}, {{sfrac}}, an opaque `<math>` — recurses
    to real nodes, so the equation is standard parse-args + decompose, not a math flatten."""
    from britannica.pipeline.stages.elements._math import _eqn_parse
    number_slot, body_slot = _eqn_parse(raw)
    return _decompose_cells("MATH_EQUATION", raw, [number_slot, body_slot])


def classify(
    shape: str, raw: str, _allow_outline: bool = True
) -> ClassifiedElement:
    """Classify one element.

    Strips the shape's outer delimiters, asks the walker for the
    next-level extracts, recursively classifies each child, then
    derives the label for this element from the assembled
    inner_registry.
    """
    # A table (`{|` or `<table>`) is a COMPOSITE, not a leaf: its grid decomposes
    # into ROW / TD / TH child nodes in this ONE tree (was a produce-time re-walk
    # that stopped the recursion and hid the cell tree).  Intercept before the
    # generic leaf path; the label ladder still decides genuine-TABLE (decompose)
    # vs DJVU/COMPOUND/CHEM (raw-reading producer, still a leaf) inside.
    if shape == SHAPE_BRACE_PIPE or (
            shape == SHAPE_HTML_TAG and _is_table_html_tag(raw)):
        return _classify_table_element(shape, raw)
    # An OUTLINE is a COMPOSITE too: its indented block decomposes into a single
    # outline of nested OUTLINE_ITEM nodes (each item's content recursed), exactly
    # as a table's grid decomposes into rows/cells.  Intercept before the leaf path
    # below — which would hand the raw block to the old flattening producer.
    if shape == SHAPE_OUTLINE:
        return _classify_outline_composite(raw, raw)
    # `{{ordered list|…}}` is that same OUTLINE with an explicit delimiter instead of
    # `:`-indent — decompose it through the one outline path, not a separate leaf.
    if re.match(r"\{\{\s*ordered\s+list\b", raw, re.IGNORECASE):
        return _classify_ordered_list_composite(raw)
    # `{{ppoem|…}}` is VERSE in template form — decompose its verse (control params dropped)
    # into nodes here, not a produce-time re-parse in the leaf producer.
    if re.match(r"\{\{\s*ppoem\b", raw, re.IGNORECASE):
        return _classify_ppoem_composite(raw)
    # `{{hi|W|text}}` / `{{hanging indent|…}}` / `{{outdent|…}}` — decompose the indented
    # content into nodes here (leaf otherwise), the width stated by the source preserved.
    if shape == SHAPE_DOUBLE_BRACE and _db_name(raw) in _HANGING_INDENT_NAMES:
        return _classify_hanging_composite(raw)
    # `{{columns|col1=…|col2=…}}` / `{{flex wrap centre|cell|cell|…}}` are TABLEs in template
    # syntax — one row of cells. Decompose through the ONE table engine, NOT the FRAME producer
    # that kept only the longest cell (dropping CASHEW's legend, half of PIG's breed photos).
    if _COLUMNS_RE.match(raw) or _FLEXWRAP_CELLS_RE.match(raw):
        return _classify_columns_as_table(raw)
    # (The styler family — STRIP `{{center|X}}`, PARAM `{{Fs|108%|X}}`, and the HTML-form
    # `<div|span style>` — folds through the `_RECURSE_SLOT_LABELS` label-branch below: their
    # content is the recursed slot, the style
    # shell a wrap re-derives from raw.)
    # A shoulder heading `{{EB1911 Shoulder Heading|…}}` is a COMPOSITE too — its label
    # decomposes into nodes, not a producer-flattened string.
    if shape == SHAPE_DOUBLE_BRACE and _SHOULDER_HEADING_RE.match(raw):
        return _classify_shoulder_composite(raw)
    # A running header `{{rh|left|centre|right}}` DECOMPOSES into three CELL nodes (one row of
    # cells, like `{{columns}}`→TABLE); the producer reassembles the flex row from the cells.
    if shape == SHAPE_DOUBLE_BRACE and _RUNNING_HEADER_RE.match(raw):
        return _classify_running_header_composite(raw)
    # The «TITLE»…«/TITLE» heading stamp is a COMPOSITE — decompose its joint-stripped content
    # into nodes at classify time (before the generic path, which peels WITHOUT the joint strip
    # and whose decomposition the old producer threw away to re-`process_elements` the raw), so a
    # title-embedded «FN is a REF node in the one tree `resolve_ref_bodies` walks.
    if shape == SHAPE_TITLE:
        return _classify_title_composite(raw)
    # An OPAQUE `<math>`/`<nowiki>`/`<score>`/… owns its verbatim interior (LaTeX
    # braces, lilypond chords) — a LEAF: we do NOT placeholderize its children, so
    # the re-walk never tears a `{{…}}` out of a `<math>` to mis-read as a template.
    # A CHART2-family paired wrapper ({{familytree/start}}…{{familytree/end}}) is
    # chart-grammar we never walk (its `/start`/`/end` delimiters aren't the CENTER
    # peel's `/s`/`/e`, so `strip_outer` can't reduce it — walking would re-extract
    # it forever); a LEAF, its producer reads raw.  Only CENTER paired wrappers are
    # composites, decomposed by the generic path below.
    if shape in LEAF_SHAPES or (
            shape == SHAPE_HTML_TAG and _is_leaf_html_tag(raw)) or (
            shape == SHAPE_PAIRED_WRAPPER and _CHART2_FAMILY_RE.search(raw)):
        # Leaf shapes own their entire payload — the producer reads
        # `raw` (CHART2, REF_SELF) or `inner_text` (OUTLINE) directly
        # and does whatever internal parsing it needs.  We still call
        # `strip_outer` so `inner_text` reflects each leaf's own
        # contract: CHART2 / REF_SELF return "", OUTLINE returns the
        # indented-line ladder unchanged.
        inner_text = strip_outer(shape, raw)
        inner_registry: dict[str, ClassifiedElement] = {}
    else:
        peeled = strip_outer(shape, raw)
        # `_allow_outline=False` on the next descent if we're inside
        # an OUTLINE — prevents the outline extractor from
        # re-triggering on its own bytes (today's `recurse_safe`).
        next_allow_outline = _allow_outline and shape != SHAPE_OUTLINE
        # Figures are body-level only — never recognize one inside an
        # already-extracted element (incl. the FIGURE producer's own
        # re-processing of its span, which would recurse forever).
        inner_text, extracts = walk(
            peeled, _allow_outline=next_allow_outline)
        inner_registry = {}
        for ph, child_shape, child_raw in extracts:
            inner_registry[ph] = classify(
                child_shape, child_raw,
                _allow_outline=next_allow_outline,
            )
    label = _derive_label(shape, raw, inner_text, inner_registry)

    # An IMAGE with a caption is a COMPOSITE — the label is known now (IMAGE spans several
    # shapes: `[[File:]]`, `{{plain image with caption}}`, `{{img float}}`, …, so branching on
    # the derived label beats replicating that detection in a pre-leaf intercept).  The caption
    # decomposes into child nodes instead of the producer re-`process_elements`-ing it; the
    # file-ref / width / align stay a leaf parse in the producer.
    if label == "IMAGE":
        return _classify_image_composite(raw)
    # The multi-slot decompose family — a row of CELL nodes, one per slot, the producer
    # reassembling its own layout (twin of `{{columns}}`→TABLE, but each owns its reassembly):
    # DUAL_LINE stacks two, TOC_ROW lays out label|leader|value, FRACTION picks a vulgar glyph.
    # Branch on the derived label (their detection already lives in `_derive_double_brace_label`,
    # esp. FRACTION's three forms) rather than replicating it in a pre-walk intercept.
    if label == "DUAL_LINE":
        return _classify_dual_line_composite(raw)
    if label == "TOC_ROW":
        return _classify_toc_row_composite(raw)
    if label == "FRACTION":
        return _classify_fraction_composite(raw)
    if label == "MATH_EQUATION":
        return _classify_equation_composite(raw)
    # A peel/recurse/wrap label — the single-slot leaves (LANG / LB / CITE / SPLIT_WORD /
    # MAIN_OTHER) AND the whole «LN» link family — decompose their one recursive slot into nodes
    # here; the producer (a `_PR_WRAP` row) substitutes rather than re-`process_elements`.
    if label in _RECURSE_SLOT_LABELS:
        return _classify_recurse_slot(raw, label)

    # NOTE: the walker is conservative — what comes in goes out unchewed.
    # An IMAGE element therefore carries its raw `[[File:…]]` VERBATIM; the
    # file-ref/params parse is the producer's job (`_process_image` → `_parse_image`),
    # not a classify-time fold.  No per-label inner_text rewriting happens here.

    return ClassifiedElement(
        label=label,
        raw=raw,
        inner_text=inner_text,
        inner_registry=inner_registry,
    )


def classify_article(
    text: str,
) -> tuple[str, dict[str, ClassifiedElement]]:
    """Top-level entry: classify every embedded element in an article
    body.

    Returns ``(placeholderized_text, top_level_registry)`` where
    ``top_level_registry`` is ``dict[placeholder, ClassifiedElement]``
    — one record per top-level placeholder, recursively populated.

    The walk emits a SHAPE_BODY extract for every body-text run between
    elements — at this depth and (via ``classify``'s recursion) every deeper
    one — so after this call the placeholderized text contains only
    placeholders: every byte is owned by some classified element, body
    text included.
    """
    placeholderized_text, extracts = walk(text)
    registry: dict[str, ClassifiedElement] = {}
    for ph, shape, raw in extracts:
        registry[ph] = classify(shape, raw)
    return placeholderized_text, registry


# ── Producer pass over the classified tree ────────────────────────────


def produce_tree(
    tree: dict[str, ClassifiedElement], context
) -> None:
    """Producer pass: bottom-up over the classified tree.

    For each element, recurses into children first, then runs the
    label's producer via the legacy ``ElementRegistry`` bridge.
    Substitutes child markers into the producer's output afterwards.
    Stores the final marker on ``ce.marker``.

    Mutates ``tree`` in place by populating each element's
    ``marker`` field.
    """
    from britannica.pipeline.stages.elements import (
        _PRODUCER_DISPATCH, _passthrough_inner,
    )

    for ph, ce in tree.items():
        # Recurse first — children's markers must be populated
        # before this element's producer runs and before
        # `_to_legacy_registry` is called below (which copies
        # children's markers into the registry view).
        if ce.inner_registry:
            produce_tree(ce.inner_registry, context)

        legacy_inner_reg = (
            _to_legacy_registry(ce.inner_registry)
            if ce.inner_registry else None
        )
        handler = _PRODUCER_DISPATCH.get(ce.label, _passthrough_inner)
        marker = handler(
            ce.raw, ce.inner_text, context, legacy_inner_reg)

        # Substitute child markers into the producer's output.
        # Multi-pass because a substituted child marker can itself
        # carry a placeholder for another child (cross-references).
        # Empty markers SUBSTITUTE NORMALLY — they're legitimate
        # producer output (`_process_ref_self` returns "" when a ref
        # name isn't resolved per its drop-silently contract; SECTION /
        # `<ref follow=>` continuations return "" too).
        # The previous `if child_ce.marker and ...` check short-
        # circuited on empty markers and leaked the placeholder
        # bytes (`\x03ELEM:N\x03`) into output — AFRICA's territorial
        # tables had ~17 such leaks from unresolved-`<ref name=X/>`
        # citation reuses inside `«TABLE[…]»` blocks.
        if ce.inner_registry:
            for _ in range(5):
                changed = False
                for child_ph, child_ce in ce.inner_registry.items():
                    if child_ph in marker:
                        marker = marker.replace(child_ph, child_ce.marker)
                        changed = True
                if not changed:
                    break

        ce.marker = marker


def substitute_top_level_markers(
    text: str, tree: dict[str, ClassifiedElement]
) -> str:
    """Substitute top-level markers into the article body.

    Multi-pass to handle sibling cross-references — a top-level
    marker may carry a placeholder for another top-level marker.
    Mutates marker strings on `tree` entries during substitution.
    """
    for _ in range(3):
        changed = False
        for ph, ce in tree.items():
            marker = ce.marker
            if ph in text:
                text = text.replace(ph, marker)
                changed = True
            for other_ph, other_ce in tree.items():
                if other_ph != ph and ph in other_ce.marker:
                    other_ce.marker = other_ce.marker.replace(ph, marker)
        if not changed:
            break
    return text
