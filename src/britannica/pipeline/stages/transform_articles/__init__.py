"""Transform raw wikitext article bodies into internal marker format.

This stage runs after boundary detection.  Each article's body contains
raw Wikisource wikitext at this point.  We convert it to the internal
marker format (``«B»``, ``«FN:``, ``{{IMG:``, etc.) by running the same
26 fetch stages and prepare_wikitext transformations — but per-article instead
of per-page, and skipping stage 3 (section-tag conversion) since
boundaries have already been determined.

Articles are processed one at a time and committed individually so that
only one article body is in memory at any point.
"""
from __future__ import annotations

import re

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal

from britannica.cleaners.hyphenation import fix_hyphenation
from britannica.cleaners.reflow import reflow_paragraphs
from britannica.cleaners.unicode import normalize_unicode, replace_print_artifacts
from britannica.pipeline.stages.transform_articles.body_text import (
    _FMT,
    _FRAKTUR_MAP,
    _LNK,
    _SH,
    _apply_markup,
    _convert_hieroglyphs,
    _convert_links,
    _convert_shoulder_headings,
    _convert_small_caps,
    _convert_sub_sup,
    _decode_entities,
    _finalize_markers,
    _strip_templates,
    _to_fraktur,
    _transform_body_text,
    _unwrap_content_templates,
    _unwrap_layout_templates,
)
from britannica.pipeline.stages.transform_articles.plate_legacy import (
    _transform_plate,
)
from britannica.pipeline.stages.transform_articles.djvu_refs import (
    _DJVU_PAGE_REF_RE,
    _normalize_djvu_page_refs,
)


# ── Body text processing stages ──────────────────────────────────────
#
# Each function handles one kind of wiki markup.  They run on body text
# AFTER embedded elements have been extracted, so they never see tables,
# images, footnotes, poems, math, or scores.

# Control characters for intermediate markers.
# \x03 is used by elements.py for placeholders, so we avoid it.

# _FRAKTUR_MAP initialization moved to body_text.py





# Plain-ASCII label shape after italic markers have been stripped.
# Caps label at 4 chars so the regex doesn't greedily eat real words
# (HEXAPODA Fig. 58 `H, Air compressing cylinder` → label `H`, not
# `H, Air`).  Multi-label chains (`K, L. Round-nose tools.`) require
# a period terminator to distinguish them from single-label + comma
# + prose.
#
# Two variants: PERMISSIVE allows a single-label with NO separator
# (used inside VERSE/TABLE/POEM container content where `A text.` is
# legitimate — TOOL Fig. 65).  STRICT requires `,` or `.` after
# single-label (used for body paragraphs where `a drilling…` is an
# English article, not a label — TOOL Fig. 47).

# EB1911 inline section-heading pattern: ``LABEL. ''italic title.''—prose``
# (HARMONY vol 13: ``III. ''Modern Harmony and Tonality.''—In the harmonic
# system of Palestrina…``).  Label + italic-wrapped title + em-dash is
# the distinguishing shape — legend captions with Roman-numeral labels
# (CENTIPEDE "I. Mandibles", HYDRAULICS "VI. STEADY FLOW…") do NOT
# italicize their text or use em-dashes, so this regex misses them.

# Bare label cell (label without text in same cell): `a,` / `At,` /
# `br.s,` / `br f,` / `g.s.`.  Up to ~12 chars; may contain dots,
# hyphens, and a single internal space (TUNICATA Fig. 25 uses `br f,`
# `i l,` for biological abbreviations).  Uses \w (Unicode-aware) so
# Latin ligatures (œ, æ) and accented letters survive.  Requires a
# trailing `,` or `.` so the label is unambiguously a legend label
# and not just a short word in a data table cell.


# Page-cosmetic 2-column body-layout wrapper: ``{|cellpadding="N"
# rules="cols"`` (in either attribute order) immediately followed by a
# ``|width="NN%" valign="top"|`` cell with no body content.  This is
# the Wikisource convention for typesetting a single page's prose in
# two columns; it has nothing to do with the article's data.  Real
# multi-page data tables (like INDIANS, NORTH AMERICAN's ``{|{{Ts|ma}}
# rules=cols border="1"``) have ``border=`` and header cells, so
# requiring "no border, then column-width-only first cell" cleanly
# distinguishes the two.
_LAYOUT_NOINCLUDE_OPENER_RE = re.compile(
    r"<noinclude>"                                # block opener
    r"(?P<head>(?:(?!</noinclude>).)*?)"          # everything up to
    r"\{\|(?P<attrs>(?![^\n]*\bborder\s*=)"       # `{|` with NO border
    r"[^\n]*\bcellpadding\s*="                    # has cellpadding
    r"[^\n]*\brules\s*=\s*[\"']?cols[^\n]*"       # AND rules=cols
    r"|(?![^\n]*\bborder\s*=)"                    # OR (alt order)
    r"[^\n]*\brules\s*=\s*[\"']?cols"
    r"[^\n]*\bcellpadding\s*=[^\n]*)\n"
    r"\s*\|width\s*=\s*[\"']?\d+%[\"']?"          # then width=NN%
    r"\s+valign\s*=\s*[\"']?top[\"']?\|"          # valign=top cell
    r"(?P<tail>(?:(?!</noinclude>).)*?)"
    r"</noinclude>",
    re.DOTALL | re.IGNORECASE,
)

# Bare-line variant — by the time `_transform_text_v2` runs against a
# stored ArticleSegment, ``detect_boundaries._strip_noinclude_preserve_tables``
# has already stripped ``<noinclude>`` wrappers but kept ``{|…`` opener
# lines.  So in segment-text the JESUS CHRIST opener appears as just a
# bare ``{|cellpadding="5" rules="cols"`` line at the start of its page
# segment, no surrounding noinclude.  Same attribute fingerprint.
_LAYOUT_BARE_OPENER_RE = re.compile(
    # Use a look-behind for `^`, `\n`, or a PAGE marker (`\x01PAGE:NNN\x01`)
    # so the substitution leaves the lead intact — the layout opener can
    # sit immediately after a page marker in a joined segment stream
    # (JESUS CHRIST p358), and we must keep the marker.
    #
    # CRITICAL: also require the NEXT line not to start with `|` — a
    # real data table opener (BEAUFORT SCALE `{|rules=cols cellpadding=3`)
    # is immediately followed by wiki-table row content (`|-` /
    # `|cell...`).  A page-layout wrapper opener is followed by plain
    # article prose.  Without this lookahead, the regex stripped real
    # data tables corpus-wide (BEAUFORT, FUNCTION, PROBABILITY, ROOT,
    # ROME, TROCHAIC, TURKEY, ZEUXIS — all leaked their pipe rows).
    r"(?<=\n)\s*\{\|(?:"
    r"(?![^\n]*\bborder\s*=)[^\n]*\bcellpadding\s*=[^\n]*\brules\s*=\s*[\"']?cols[^\n]*"
    r"|(?![^\n]*\bborder\s*=)[^\n]*\brules\s*=\s*[\"']?cols[^\n]*\bcellpadding\s*=[^\n]*"
    r")\n(?!\s*\|)"
    r"|(?<=\x01)\s*\{\|(?:"
    r"(?![^\n]*\bborder\s*=)[^\n]*\bcellpadding\s*=[^\n]*\brules\s*=\s*[\"']?cols[^\n]*"
    r"|(?![^\n]*\bborder\s*=)[^\n]*\brules\s*=\s*[\"']?cols[^\n]*\bcellpadding\s*=[^\n]*"
    r")\n(?!\s*\|)",
    re.IGNORECASE,
)

# Matching closer in either form — wrapped noinclude or bare ``|}``
# line.  Stripping any solo ``|}`` would risk a real multi-page data-
# table closer, so closer-stripping is GATED by opener presence (see
# `_strip_page_layout_noinclude_wrappers`).
_LAYOUT_NOINCLUDE_CLOSER_RE = re.compile(
    r"<noinclude>\s*\|\}\s*</noinclude>",
    re.IGNORECASE,
)
_LAYOUT_BARE_CLOSER_RE = re.compile(
    r"(?:^|\n)\s*\|\}(?!\})\s*(?=\n|$)",
)


def _strip_page_layout_noinclude_wrappers(text: str) -> str:
    """Strip Wikisource per-page 2-column display wrappers.

    These wrappers are typographic only (``cellpadding="5" rules="cols"``
    with a single ``width="NN%" valign="top"`` cell carrying the page's
    prose).  Without this pass, the opener is preserved as plain ``{|…``
    text and the orphaned closer fakes a wrapper around the article body
    (``_wrap_orphaned_table_rows`` adds a synthetic ``{|``).  The shape
    cannot be confused with a real multi-page data table — those carry
    ``border=`` plus header cells, both of which we exclude.

    Two opener forms exist depending on where in the pipeline this is
    invoked: ``<noinclude>{|cellpadding=…|width=…</noinclude>`` (raw
    page wikitext) and a bare ``{|cellpadding=…`` line (segment-text,
    after ``detect_boundaries`` has stripped noinclude wrappers but
    preserved the ``{|`` opener).  Both are handled.  Strips opener and
    matching closer in tandem so the body is left bare, as Wikisource's
    standalone-page renderer intends when transcluded.
    """
    has_wrapped = _LAYOUT_NOINCLUDE_OPENER_RE.search(text) is not None
    has_bare = _LAYOUT_BARE_OPENER_RE.search(text) is not None
    if not has_wrapped and not has_bare:
        return text
    if has_wrapped:
        text = _LAYOUT_NOINCLUDE_OPENER_RE.sub("", text)
        text = _LAYOUT_NOINCLUDE_CLOSER_RE.sub("", text)
    if has_bare:
        text = _LAYOUT_BARE_OPENER_RE.sub("", text)
        text = _LAYOUT_BARE_CLOSER_RE.sub("", text)
    return text


# ── Layer-A audit instrumentation (TEMPORARY) ─────────────────────────
# Default-empty: production behavior is byte-identical.  An audit harness
# assigns this module global to a {pass-name} set to measure each
# preprocessing pass's MARGINAL render effect (does it do anything the
# producers don't already do?).  Remove after the Layer-A migration.
_AUDIT_SKIP: frozenset = frozenset()


def _transform_text_v2(raw_wikitext: str, volume: int, page_number: int) -> str:
    """Transform an article's raw wikitext body into the internal marker
    format.

    Hand-off is honest: corrections-only raw goes straight to
    ``process_elements`` (walker → classifier → producer).  Producers
    transform; classifiers may call Layer-A utilities internally as
    diagnostics but pass the raw they received to the producer unchanged.
    Post-extraction steps below (chart injection, hyphenation rejoin,
    paragraph reflow, blank-line collapse) operate on the producer
    output's body shape, not on the raw.
    """
    from britannica.pipeline.stages.elements import (
        ElementContext, process_elements)

    # Source-text corrections (transcription typos in wikisource) are
    # applied once during prepare_wikitext, mutating `wikitext` so all
    # downstream stages — including this one — operate on already-
    # corrected text.  Producers receive the corrected raw and transform
    # internally; Layer-A utilities remain available for producers /
    # classifiers to call privately, but the pipeline-style chain that
    # used to thread their outputs across stages here has been deleted.
    context = ElementContext(volume=volume, page_number=page_number)
    text = process_elements(raw_wikitext, _apply_markup, context)

    # Inject chart images for pages where chart2 markup was lost during import
    from britannica.image_assets import CHART2_IMAGES
    for (v, p), filename in CHART2_IMAGES.items():
        if v == volume and f"IMG:{filename}" not in text:
            marker = f"\x01PAGE:{p}\x01"
            if marker in text:
                text = text.replace(marker, f"{marker}\n\n{{{{IMG:{filename}|Genealogical table}}}}\n\n", 1)

    # Per-paragraph regularize: split any multi-paragraph `«FINE:…«/FINE»`
    # into a sequence of single-paragraph FINE blocks.  The viewer's
    # paragraph splitter operates on `\n\n` boundaries and matches the
    # FINE marker per paragraph; a multi-paragraph FINE block would
    # fragment with no balanced ends in any single paragraph and leak
    # as raw text.  Block-element paragraphs (TABLE/LEGEND/IMG/VERSE/
    # PRE/OUTLINE) inside the fine-print run stay UNWRAPPED — wrapping
    # a block element in `«FINE:…«/FINE»` would suppress its own
    # renderer (whose regex matches only at paragraph start).
    _FINE_BLOCK_PREFIX = re.compile(
        r"^\s*(?:\{\{(?:TABLE|LEGEND|IMG|VERSE)|«PRE[\[:]|«OUTLINE:|«HTMLTABLE)")
    def _split_fine(m: re.Match) -> str:
        out_parts = []
        for p in re.split(r"\n\n+", m.group(1)):
            p = p.strip()
            if not p:
                continue
            if _FINE_BLOCK_PREFIX.match(p):
                out_parts.append(p)
            else:
                out_parts.append(f"«FINE:{p}«/FINE»")
        return "\n\n".join(out_parts)
    text = re.sub(r"«FINE:([\s\S]*?)«/FINE»", _split_fine, text)

    # Rejoin words split by line-break hyphenation (`trans- \nlation` →
    # `translation`).  Must run before reflow_paragraphs, which would
    # otherwise convert the line break to a space and freeze the broken
    # form in place.
    text, _ = fix_hyphenation(text)

    # Reflow paragraphs — join lines that were hard-wrapped in the source
    text = reflow_paragraphs(text)


    # Strip leading comma/space left after title+descriptor stripping
    # (e.g. "'''BISMARCK,''' {{sc|Prince}}, duke..." → ", duke..." after transform)
    text = re.sub(r"^[\s,]+", "", text)

    # Defensive cleanup for orphan punctuation left when a template
    # gets stripped without its display text (e.g. a malformed
    # `{{1911link|X|Y}}` previously dropped, leaving `…, , Y…`):
    #   ", , , "  → ", "
    #   ", ;"     → ";"
    #   ", ."     → "."
    # Preserve `,,` adjacent (ditto marks in tables).
    text = re.sub(r",(\s+,)+", ",", text)
    text = re.sub(r",\s*([;.])", r"\1", text)

    # Final blank-line collapse.  Element-marker insertions
    # (`{{IMG:…}}`, `{{LEGEND:…}LEGEND}`, etc.) each wrap themselves in
    # `\n\n…\n\n`, so two adjacent blocks can produce 3+ consecutive
    # newlines.  Collapsing here means the transform produces its
    # body cleanly in isolation, no downstream cleanup needed.
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text




def _wrap_orphaned_table_rows(text: str) -> str:
    """Wrap orphaned wiki table rows (|- and | lines) that lack a {| opener.

    Multi-page wiki tables have {| in <noinclude> on continuation pages.
    After noinclude stripping, the rows are left bare.  Wrap them in
    {|...|} so the table converter can process them.

    Also detects runs of |lines without |- separators (two-column tables
    spanning page boundaries).
    """
    # Quick check: any lines starting with |?
    has_pipe_rows = any(
        line.strip().startswith("|") and len(line.strip()) > 3
        for line in text.split("\n")
    )
    if not has_pipe_rows:
        return text

    # Count opens and closes
    opens = len(re.findall(r"\{\|", text))
    closes = len(re.findall(r"\|\}", text))

    if "{|" in text:
        if opens > closes:
            # Unclosed table — add |} at end so balanced extractor can find it
            text = text + "\n|}"
        elif opens < closes:
            # Orphaned |} — wrap preceding rows in {|
            first_close = text.find("|}")
            prefix = text[:first_close]
            rest = text[first_close + 2:]
            text = "{|\n" + prefix + "\n|}" + rest
        # Also handle orphaned rows before the first {|
        first_table = text.find("{|")
        prefix = text[:first_table]
        rest = text[first_table:]
        if prefix.strip() and ("\n|-" in prefix or prefix.strip().startswith("|-")):
            wrapped_prefix = _wrap_orphaned_table_rows(prefix)
            return wrapped_prefix + rest
        return text

    # Find runs of |lines and wrap them
    lines = text.split("\n")
    first_row = None
    last_row = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_table_line = (
            (stripped.startswith("|-") or stripped.startswith("|"))
            and len(stripped) > 3
            and not stripped.startswith("|}")
        )
        if is_table_line:
            if first_row is None:
                first_row = i
            last_row = i

    if first_row is None:
        return text

    # Wrap the table rows
    before = "\n".join(lines[:first_row])
    table = "\n".join(lines[first_row:last_row + 1])
    after = "\n".join(lines[last_row + 1:])
    parts = []
    if before.strip():
        parts.append(before)
    parts.append("{|\n" + table + "\n|}")
    if after.strip():
        parts.append(after)
    return "\n".join(parts)




def transform_articles(volume: int) -> int:
    """Transform raw wikitext to internal marker format for all articles in a volume.

    Transforms each segment (page-sized) individually, then joins them
    into article.body with \\x01PAGE:N\\x01 markers at page boundaries.
    The markers are injected after transformation so they survive the
    control-character stripping in prepare_wikitext.

    Processes one article at a time with per-article commits.
    """
    session = SessionLocal()
    try:
        article_ids = [
            aid for (aid,) in session.query(Article.id)
            .filter(Article.volume == volume)
            .all()
        ]

        for aid in article_ids:
            article = session.get(Article, aid)
            segments = (
                session.query(ArticleSegment)
                .join(SourcePage, ArticleSegment.source_page_id == SourcePage.id)
                .filter(ArticleSegment.article_id == aid)
                .order_by(ArticleSegment.sequence_in_article)
                .add_columns(SourcePage.page_number)
                .all()
            )

            is_plate = article.article_type == "plate"

            if is_plate:
                # Plates are single pages — process directly
                raw = segments[0][0].segment_text if segments else ""
                from britannica.parsers.plate import parse_plate
                article.body = parse_plate(raw) if raw else ""
            else:
                # Join raw segments with page markers, then transform once.
                raw_parts = []
                for seg, page_number in segments:
                    raw = seg.segment_text or ""
                    # Always emit the page marker, even for empty/untranscribed pages
                    raw_parts.append(f"\x01PAGE:{page_number}\x01{raw}")
                joined_raw = "\n".join(raw_parts)

                # Fix cross-page hyphenation: con-\n\x01PAGE:N\x01tinuation
                joined_raw = re.sub(
                    r"(\w)-\n(\x01PAGE:\d+\x01)(\w)",
                    r"\1\2\3", joined_raw,
                )
                article.body = _transform_text_v2(
                    joined_raw, volume,
                    segments[0][1] if segments else 0,
                ) if joined_raw else ""
                # Strip redundant title qualifier from body start.
                # e.g. title "YORK, HOUSE OF" → body starts "(House of),"
                if article.body and ", " in article.title:
                    qualifier = article.title.split(", ", 1)[1]
                    # Strip formatting markers for matching
                    body_clean = re.sub(
                        r"[\u00ab\u00bb](?:SC|/SC|I|/I|B|/B)[\u00ab\u00bb]",
                        "", article.body[:200],
                    )
                    paren_q = f"({qualifier})"
                    if body_clean.lstrip("\x01PAGE:0123456789").lstrip().lower().startswith(paren_q.lower()):
                        # Strip the parenthetical qualifier from actual body
                        article.body = re.sub(
                            r"^(\x01PAGE:\d+\x01)?\s*\([^)]*\)[,;\s]*",
                            r"\1", article.body,
                        )
            # Title producer: run the carved title span (markers/footnote
            # intact) through the SAME transform as the body — «B»/«I»/«SC»
            # are kept and a title <ref> becomes «FN» (the footnote
            # producer), exactly like body text.  Store the display title
            # only when it carries something the plain title lost
            # (formatting or a footnote); plain bold titles leave it None
            # and the viewer falls back to `title`.  Keeps the blast
            # radius to the ~2% of titles that actually differ.
            if article.title_raw:
                _disp = _transform_text_v2(
                    article.title_raw, volume,
                    segments[0][1] if segments else article.page_start,
                ).strip()
                article.title_display = _disp if (
                    _disp and (re.search(r"«(?:I|SC|FN)", _disp)
                               or _disp.count("«B»") > 1)) else None

            session.commit()
            session.expire_all()

        return len(article_ids)
    finally:
        session.close()
