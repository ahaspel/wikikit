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


def preprocess_article(session, article) -> str:
    """Per-article preprocessing: assemble the article's segments into one raw text
    and stamp ``«TITLE»…«/TITLE»`` around the carved title span, so the title rides the
    SINGLE walk as a recognized node instead of a re-walked side field.

    The title bracket is a transform, legitimately placed here: ``preprocess_article``
    runs per article, so ``beginning`` and ``once`` are free (the article IS the unit,
    there is no "first" to hunt), and it is preprocessing — one of the two transform
    homes.  The «PAGE» marker is NOT stamped here anymore: ``super_detect`` slaps the
    leaf onto each segment at the cut, where the leaf is known; this function only
    concatenates the already-marked segments.
    """
    segments = (
        session.query(ArticleSegment)
        .join(SourcePage, ArticleSegment.source_page_id == SourcePage.id)
        .filter(ArticleSegment.article_id == article.id)
        .order_by(ArticleSegment.sequence_in_article)
        .add_columns(SourcePage.page_number)
        .all()
    )
    # The «PAGE» marker is already materialized into each segment at detection
    # (super_detect slaps the current leaf on as it cuts the page-fragment).  Just
    # concatenate — never re-stamp a leaf-fact at the article level.
    joined = "".join(seg.segment_text or "" for seg, page_number in segments)
    if article.title_raw:
        return f"«TITLE»{article.title_raw}«/TITLE»{joined}"
    return joined


_TITLE_NODE_RE = re.compile(r"«TITLE:(.*?)«/TITLE»", re.DOTALL)


def walk_article(session, article) -> tuple[str, str | None]:
    """Walk one article in a SINGLE pass and split off its title node.

    ``preprocess_article`` stamps «TITLE» around the carved title; the walk
    recurses it into a ``«TITLE:…«/TITLE»`` node alongside the body; we split
    that node off here.  Replaces ``produce_article``'s two-walk shape (body walk
    + a separate ``title_raw`` walk) — the title now rides the ONE walk, verified
    byte-identical title_display + body.

    (``strip_attributions`` is still applied here as the interim footer cut; it
    moves to the contributor decorator, which will consume the recognized
    CONTRIBUTOR_FOOTER nodes instead of pre-stripping the footer.)
    """
    from britannica.pipeline.stages.elements import (
        ElementContext, process_elements)
    from britannica.pipeline.stages.extract_contributors import strip_attributions

    raw = strip_attributions(preprocess_article(session, article))
    if not raw:
        return "", None
    walked = process_elements(
        raw, ElementContext(volume=article.volume,
                            page_number=article.page_start))
    m = _TITLE_NODE_RE.search(walked)
    if not m:
        return walked, None
    title_disp = m.group(1)
    title_display = title_disp if (
        re.search(r"«(?:I|SC|FN)", title_disp)
        or title_disp.count("«B»") > 1) else None
    body = _TITLE_NODE_RE.sub("", walked, count=1)
    return body, title_display


def _transform_text_v2(raw_wikitext: str, volume: int, page_number: int) -> str:
    """Walk a raw article body to the internal marker format — thin shim.

    The PRODUCTION body producer is now ``walk_article`` (one walk, title as a
    recognized node).  This remains ONLY for the tests/diagnostics that walk a raw
    string directly; it is ``strip_attributions`` + ``process_elements`` and nothing
    else.  It collapses to a bare ``process_elements`` call — at which point those
    callers use that directly — once the contributor decorator removes the
    ``strip_attributions`` pre-cut.
    """
    from britannica.pipeline.stages.elements import (
        ElementContext, process_elements)
    from britannica.pipeline.stages.extract_contributors import strip_attributions

    return process_elements(
        strip_attributions(raw_wikitext),
        ElementContext(volume=volume, page_number=page_number))
