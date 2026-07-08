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
from britannica.pipeline.stages.elements._title import (
    produce_title, decode_title, recover_title_from_section)



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
    """Per-article preprocessing — the SOLE title site (MOVE 2).

    Assemble the article's segments into one raw text, run ``produce_title`` on
    it to carve the title↔body cut, and stamp ``«TITLE»…«/TITLE»`` around the
    carved title span so the title rides the SINGLE walk as a recognized node
    instead of a pre-computed side field.  Returns the walk-ready text (the body
    with the «TITLE» node stamped in); ``walk_article`` decodes the walked node
    to the plain ``title`` field.

    Both the title bracket and the title extraction are transforms legitimately
    placed here: ``preprocess_article`` runs per article, so ``beginning`` and
    ``once`` are free (the article IS the unit, there is no "first" to hunt), and
    it is preprocessing — one of the two transform homes.  Detection no longer
    extracts the title (MOVE 2); the title is produced here, in exactly one place.

    The «PAGE» marker is NOT stamped here: ``super_detect`` slaps the leaf onto
    each segment at the cut, where the leaf is known; this function only
    concatenates the already-marked segments and hands the whole thing to
    ``produce_title``, which OWNS the title↔body cut: it steps over the leading
    ``\\x01PAGE:N\\x01`` leaf to reach the heading and keeps it on the body
    (never dropping it — it carries the page number), and uses ``section_name``
    to recover a fuller title when the bold run was a partial capture.
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
    if not joined:
        return ""

    body, title_raw = produce_title(joined, article.section_name)
    # Section anchors are NO LONGER stamped here: recognition needs the article's
    # structure (top-level vs inside-a-table), which doesn't exist until after the
    # walk.  It moved to `stamp_section_anchors`, a tree_hook on the outermost walk
    # in `walk_article` — the post-walk extraction family where that context lives.
    if title_raw:
        return f"«TITLE»{title_raw}«/TITLE»{body}"
    return body


_TITLE_NODE_RE = re.compile(r"«TITLE:(.*?)«/TITLE»", re.DOTALL)


_CONTRIBUTOR_INITIALS_CACHE: frozenset[str] | None = None


def _contributor_initials(session) -> frozenset[str]:
    """Normalized initials of every known contributor — loaded once and cached.

    Built upstream from the per-volume front-matter tables + the vol-29 master
    index (``ContributorInitials``), static for a run.  The Author-link producer
    routes on membership: a display whose initials are in here is a contributor
    signature → render the initials; everything else → «LN» xref."""
    global _CONTRIBUTOR_INITIALS_CACHE
    if _CONTRIBUTOR_INITIALS_CACHE is None:
        from britannica.db.models import ContributorInitials
        _CONTRIBUTOR_INITIALS_CACHE = frozenset(
            ci.initials for ci in session.query(ContributorInitials).all())
    return _CONTRIBUTOR_INITIALS_CACHE


def walk_article(session, article) -> str:
    """Walk one article in a SINGLE pass and split off its title node.

    ``preprocess_article`` (the SOLE title site, MOVE 2) runs ``produce_title``,
    stamps «TITLE» around the carved title, and hands back the walk-ready text;
    the walk recurses the «TITLE» bracket into a ``«TITLE:…«/TITLE»`` node
    alongside the body; we split that node off here.  This function writes the
    plain ``title`` back onto the Article — produced and persisted from the
    transform stage, never at detection.  The marked-up heading is not a field:
    it rides the «TITLE» node in the returned body, which the viewer renders
    in-stream as the H1.

    Replaces ``produce_article``'s two-walk shape (body walk + a separate
    ``title_raw`` walk) — the title now rides the ONE walk.

    (The footer is no longer pre-stripped — it rides through and is carried as
    recognized CONTRIBUTOR_FOOTER nodes / Author-link signatures; the contributor
    harvest reads the initials off those downstream.)
    """
    from britannica.pipeline.stages.elements import (
        ElementContext, process_elements)
    from britannica.pipeline.stages.elements._section_anchors import (
        stamp_section_anchors)

    raw = preprocess_article(session, article)
    if not raw:
        return ""
    walked = process_elements(
        raw, ElementContext(volume=article.volume,
                            page_number=article.page_start,
                            contributor_initials=_contributor_initials(session)))
    # Section recognition runs post-walk over the finished body — the post-walk
    # extraction family, beside the contributor / xref extractors — where the whole
    # produced structure is in view: every «CTR»«SC» heading (fine-print ones too)
    # plus balanced «TABLE[…]» table spans.  A section is a heading NOT inside a
    # table; the pre-walk `stamp_sections` regex never had this structure.
    walked = stamp_section_anchors(walked)
    m = _TITLE_NODE_RE.search(walked)
    if not m:
        return walked
    marker = m.group(1)
    # The plain field IS the walked «TITLE» marker decoded (recursion =
    # recognition — ONE title source, so the field can't diverge from the rendered
    # heading, and the regnal period `clean_title` used to eat is kept).  EXCEPT
    # plates: a plate's title is a `_split_out_plates`/`_compose_plate_title`
    # product (AMERICA, PLATE II), NOT a `produce_title` shape — leave the
    # detection-set plate title alone.
    if article.article_type != "plate":
        field = recover_title_from_section(
            decode_title(marker), article.section_name or "")
        if field:
            article.title = field
    # Keep the «TITLE:…«/TITLE» node in the body: the viewer renders it
    # in-stream as the H1, so the title's footnote joins the body's footnote
    # stream (numbered + collected in Notes) instead of a severed parallel one.
    return walked
