"""Alias sources for cross-reference resolution — the generic one.

The resolver's overlay merges the maps a book lists in
``Corpus.alias_sources`` (alias -> canonical article title).  This module
holds the source any Wikisource book can use: ``<section begin>`` names
mapped to their containing article.  EB1911's own sources — its link-template
harvest and its vol-29 index — live in eb1911/aliases.py.
"""

import re


def build_section_alias_map() -> dict[str, str]:
    """Map `<section begin="X" />` names to their containing article.

    An xref target like "CLEMENT I" is legitimate — it points to a
    section within the CLEMENT (POPES) article. This harvests those
    section names from raw wikitext and maps each to the article title.

    Only unambiguous names (appearing as a section in exactly one
    article) are returned — generic names like HISTORY or LITERATURE
    appear in many articles and would be ambiguous.

    Returns dict mapping UPPER(section_name) → UPPER(article_title).
    """
    from collections import defaultdict

    from wikikit.db.models import Article, SourcePage
    from wikikit.db.session import SessionLocal

    # `section_name → set of article_titles` — we'll keep only the
    # entries that map to exactly one article.
    raw_map: dict[str, set[str]] = defaultdict(set)

    # Match `<section begin="X" />` (quoted or unquoted).
    section_re = re.compile(
        r'<section\s+begin=(?:"([^"]+)"|([A-Za-z][^/>\s]*))\s*/?>',
        re.IGNORECASE,
    )

    session = SessionLocal()
    try:
        # Articles → their source pages, by page RANGE.  This used to hop through
        # ArticleSegment, which existed only as a per-page bridge to SourcePage;
        # the article's own (volume, page_start..page_end) reaches the same pages
        # directly ([[project_page_position_out_of_band]]).
        rows = (
            session.query(Article.title, SourcePage.wikitext)
            .join(SourcePage,
                  (SourcePage.volume == Article.volume)
                  & (SourcePage.page_number >= Article.page_start)
                  & (SourcePage.page_number <= Article.page_end))
            .filter(Article.article_type == "article")
            .all()
        )

        for title, wikitext in rows:
            if not wikitext:
                continue
            title_upper = title.strip().upper()
            for m in section_re.finditer(wikitext):
                name = (m.group(1) or m.group(2) or "").strip()
                if not name:
                    continue
                upper = name.upper()
                # Skip generic section IDs (s1, s2, …)
                if re.match(r"^S\d+$", upper):
                    continue
                # Section whose name matches its containing article is a
                # Wikisource continuation, not an alias.
                if upper == title_upper:
                    continue
                raw_map[upper].add(title_upper)
    finally:
        session.close()

    # Keep only unambiguous mappings
    return {name: next(iter(titles))
            for name, titles in raw_map.items()
            if len(titles) == 1}
