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

from britannica.cleaners.unicode import normalize_unicode, replace_print_artifacts
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

    The body is expected to arrive title-clean: detect_boundaries chops
    the title out at source (see ``_extract_bold_delimited_title`` +
    ``produce_title``).  No title-bold strip happens here.  Articles in
    the DB whose segment_text was persisted BEFORE the chop-up fix will
    show a leading bold title duplicate in their output until they are
    re-detected — that's a documented chop-up failure, not something to
    sweep.
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
    # Source cleaning (noinclude strip, fine-print token strip) now happens
    # ONCE in `preprocess` on the whole volume stream, before slicing — so the
    # segments reaching here arrive clean.  Verified no-op for the article path:
    # 0 `<noinclude>` and 0 fine-print tokens in article segments (the only
    # residual fine-print tokens are in PLATE segments, which go through
    # `parse_plate`, not this function).
    context = ElementContext(volume=volume, page_number=page_number)
    # Author signature footer ({{EB1911 footer [double] initials|…}}, usually
    # wrapped in {{Fs|108%|…}}) is REDUNDANT in the body (the author is shown in
    # the top attribution line, built from the contributor sources; SAFE because
    # extract_contributors reads the signature from segment_text, not this body).
    # Now consumed by the WALKER: the footer is a CONTRIBUTOR_FOOTER element
    # (empty body output) and {{Fs}} a recognized styler that recurses it.  The
    # old named-removal re.sub is DELETED — its greedy trailing `\}+` ate the
    # enclosing {{Fine block}}'s closing braces, unbalancing it (the holdover
    # `{{Fine block|`/`{{Fs|108%|` leak).
    #
    # Author attribution is a FIELD, like the title: cut the footer + {{right}}/
    # {{float right}} signature BEFORE the walker.  They're bound to the DB from the raw
    # by extract_contributors and re-rendered as the export byline, so the in-body copy
    # is redundant.  The cut is SCOPED to the signature shapes, so prose [[Author:…]]
    # citations are untouched.  Subsumes the old CONTRIBUTOR_FOOTER walker element.
    from britannica.pipeline.stages.extract_contributors import strip_attributions
    raw_wikitext = strip_attributions(raw_wikitext)
    text = process_elements(raw_wikitext, context)

    # (chart2 genealogical-tree images are substituted at source in
    # `make_stream`/preprocess now — the old post-walker injection loop is gone.)

    # Page-transition seams are healed ONCE, in `preprocess`, on the continuous
    # stream; the article body is a faithful slice of that clean stream, so the
    # text reaching here is already clean.  No seam handling between preprocess
    # and the walker; the former post-producer reflow/hyphenation passes (illegal
    # — producer output is the final body) are deleted.

    # (No paragraph reflow: a hard-wrapped single \n is already a space to the
    # viewer's `<p>` (white-space:normal), \n\n is preserved as a real break, and
    # block-marker internal whitespace is ignored by KaTeX/HTML.  reflow_paragraphs
    # was therefore render-neutral and is deleted — verify at the rebuild sweep.)

    # (No leading-comma strip: the title cut owns its trailing comma —
    # `produce_title` already lstrips `" \t,."` off the body at the cut — and the
    # body now begins with a `\x01PAGE\x01` marker anyway, so a `^[\s,]+` strip
    # could never match.  Verified dead (0 articles) and deleted.)

    # (No orphan-punctuation cleanup.  The old `, ,`/`, ;`/`, .` collapse claimed
    # to fix templates "stripped without display text" — but honest producers
    # recurse the display, so that never happens now.  What it actually did, across
    # 335 firing articles, was CORRUPT legitimate content: it mangled table ditto
    # marks (`,, ,,` → `,,,`) and editorialised source typos (`S. Cal,;` →
    # `S. Cal;`).  Both unfaithful; source quality is end-stage — render what's
    # there.  Deleted.)

    # (No blank-line collapse: the viewer splits on `\n\n`, so it already treats
    # `\n\n\n+` as a single paragraph break — the surplus newline becomes an empty
    # paragraph that's dropped.  `\n{3,}`->`\n\n` was render-neutral and is
    # deleted; verify at the rebuild sweep.)

    return text




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

            # Plates and articles alike flow through the ONE walker now — the
            # faithless `produce_faithful_figure` shadow-pipeline is deleted.  The
            # walker recurses a plate's stylers and `{|`-grid honestly (the shadow
            # producer leaked them raw, its `TT` being identity).
            #
            # Re-assemble the body from per-page segments FAITHFULLY: each segment
            # is a slice of the clean stream cut at a `\x01PAGE\x01` marker, so
            # concatenating `marker+segment` with NO separator reproduces exactly
            # that stream slice (a `"\n".join` would inject a seam the stream never
            # had — word-joins and seam healing already happened in preprocess).
            joined_raw = "".join(
                f"\x01PAGE:{page_number}\x01{seg.segment_text or ''}"
                for seg, page_number in segments)
            article.body = _transform_text_v2(
                joined_raw, volume,
                segments[0][1] if segments else 0,
            ) if joined_raw else ""
                # (No redundant-title-qualifier strip: the title-formation keeps
                # the title bare ("YORK") with the qualifier in the body paren,
                # the old pattern (title "X, Q" + body "(Q)") no longer occurs.
                # Verified dead (0 of 743 comma-titles) and deleted.)
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
