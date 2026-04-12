"""Build and query an alias table for cross-reference resolution.

Aliases are harvested from the raw wikitext link templates:
- {{EB1911 lkpl|Target|Display}} where Display differs from Target
- {{1911link|Target|Display}} where Display differs from Target

These are human-curated mappings placed by Wikisource editors.
"""

import json
import re
import glob
from collections import defaultdict
from pathlib import Path


RAW_DIRS = [Path("data/raw/wikisource")]


def build_alias_map() -> dict[str, str]:
    """Build a map of alias -> canonical target from raw wikitext files.

    Returns dict mapping uppercased alias to uppercased canonical title.
    When multiple targets exist for an alias, the most common one wins.
    """
    # Collect alias -> list of targets (may have multiple)
    raw_aliases: dict[str, list[str]] = defaultdict(list)

    for raw_dir in RAW_DIRS:
        if not raw_dir.exists():
            continue
        for subdir in sorted(raw_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for path in sorted(subdir.glob("*.json")):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("raw_text", "")
                _extract_aliases_from_wikitext(raw, raw_aliases)

    # Resolve to single target per alias (most frequent)
    alias_map: dict[str, str] = {}
    for alias, targets in raw_aliases.items():
        # Skip noisy aliases
        if len(alias) <= 2:
            continue
        if alias in ("ABOVE", "BELOW", "HERE", "THERE", "FURTHER"):
            continue

        # Pick the most common target
        from collections import Counter
        counts = Counter(targets)
        best_target = counts.most_common(1)[0][0]
        alias_map[alias] = best_target

    return alias_map


def _extract_aliases_from_wikitext(
    raw: str, aliases: dict[str, list[str]]
) -> None:
    """Extract alias mappings from a single page's raw wikitext."""
    # {{EB1911 lkpl|Target|Display}}
    for m in re.finditer(
        r"\{\{(?:EB1911|DNB)\s+lkpl\|([^|}]+)\|([^}]+)\}\}", raw, re.I
    ):
        target = m.group(1).strip().upper()
        display = m.group(2).strip().upper()

        if target == display:
            continue
        if len(display) > 50:
            continue
        # Skip wiki markup fragments
        if any(c in display for c in "'{}|<>"):
            continue

        aliases[display].append(target)

    # {{1911link|Target|Display}}
    for m in re.finditer(
        r"\{\{1911link\|([^|}]+)\|([^}]+)\}\}", raw, re.I
    ):
        target = m.group(1).strip().upper()
        display = m.group(2).strip().upper()

        if target == display:
            continue
        if len(display) > 50:
            continue
        if any(c in display for c in "'{}|<>"):
            continue

        aliases[display].append(target)


_VOL29_DIR = Path("data/raw/wikisource/vol_29")
_VOL29_OCR = Path("data/derived/vol29_ocr.json")


def _strip_vol29_wikitext(text: str) -> str:
    """Normalize vol 29 index page wikitext to a scannable plain form.

    Strips <noinclude>, page-heading + running-header templates, other
    decorative templates, and wiki link wrappers. Preserves punctuation
    that matters to the index grammar (commas, semicolons, dashes).
    """
    text = re.sub(r"<noinclude>.*?</noinclude>", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\{EB1911 Page Heading[^}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{running header[^}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{EB1911 Shoulder Heading[^}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{c\|[^}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{center\|[^}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{xx?-?larger\|[^}]*\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{sc\|([^}]*)\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{11link\|([^|}]+)\|?[^}]*\}\}", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{EB1911 lkpl\|([^|}]+)\|?[^}]*\}\}", r"\1", text, flags=re.IGNORECASE)
    # Strip any remaining templates
    for _ in range(3):
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    # Replace wiki links [[X|Y]] -> Y, [[X]] -> X
    text = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace
    text = re.sub(r"&nbsp;|&emsp;|&thinsp;", " ", text)
    text = re.sub(r"''", "", text)  # drop italic markers
    return text


# Matches one volume-page reference: "1-424" or "19-678 (A4)" or "7-736a".
_VOLPAGE_RE = re.compile(r"\b(\d{1,2})-(\d{1,4})[a-d]?\b")


def build_vol29_index_aliases() -> dict[str, str]:
    """Parse vol 29's transcribed index entries into topic → article title.

    Each entry is a topic (article title or subtopic) followed by one
    or more "vol-page" references. We pick the FIRST reference (that's
    the primary article on the topic per the index's conventions) and
    map to whichever article in our DB covers that vol+page.

    Only topics that map unambiguously and whose article exists are
    included. This catches things like "PENINSULAR WAR -> NAPOLEONIC
    CAMPAIGNS" when the index points there.
    """
    from britannica.db.models import Article
    from britannica.db.session import SessionLocal

    if not _VOL29_DIR.exists():
        return {}

    session = SessionLocal()
    try:
        articles = [
            a for a in session.query(Article).all()
            if a.article_type == "article"
        ]
    finally:
        session.close()

    # Index by (vol, page) -> article title. Each page can belong to
    # multiple articles (when an article spans one page); prefer the
    # article whose page range INCLUDES this page more specifically
    # (earlier start, later end are both fine; we just pick any).
    by_vol_page: dict[tuple[int, int], str] = {}
    for a in articles:
        if a.page_start is None or a.page_end is None:
            continue
        for p in range(a.page_start, a.page_end + 1):
            key = (a.volume, p)
            # First-wins keeps the canonical article for that page.
            if key not in by_vol_page:
                by_vol_page[key] = a.title.strip().upper()

    result: dict[str, str] = {}

    # Load OCR fallback for pages Wikisource hasn't transcribed.
    ocr_data: dict[str, str] = {}
    if _VOL29_OCR.exists():
        try:
            ocr_data = json.loads(_VOL29_OCR.read_text(encoding="utf-8"))
        except Exception:
            pass

    for f in sorted(_VOL29_DIR.glob("vol29-page*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        text = _strip_vol29_wikitext(d.get("raw_text", ""))
        if not text.strip():
            # Fall back to OCR if available.
            ws_match = re.search(r"page(\d{4})", f.name)
            if ws_match:
                text = ocr_data.get(ws_match.group(1), "")
            if not text.strip():
                continue

        # Walk line by line looking for index entries.
        for line in text.splitlines():
            line = line.strip()
            if not line or len(line) < 6:
                continue
            # Entry grammar (approximate):
            #   TOPIC [, sub]*  [qualifier]  vol-page [; vol-page]...
            # We only need TOPIC and the first vol-page.
            m = _VOLPAGE_RE.search(line)
            if not m:
                continue
            vol = int(m.group(1))
            page = int(m.group(2))
            if not (1 <= vol <= 28 and 1 <= page <= 1200):
                continue
            topic = line[:m.start()].strip().rstrip(",;:")
            # Drop trailing qualifier words after the last comma (e.g.
            # "ABEOKUTA, Nig." -> keep only "ABEOKUTA").
            if "," in topic:
                head, _, tail = topic.partition(",")
                # Only drop the tail if it's a short qualifier (not a
                # real name like "NELSON, HORATIO")
                if len(tail.strip()) <= 10 and not tail.strip().isupper():
                    topic = head.strip()
            topic = topic.upper()
            if not topic or len(topic) < 3:
                continue
            # Skip topics that are obviously noise
            if not re.match(r"^[A-Z\u00C0-\u00DE]", topic):
                continue

            target_title = by_vol_page.get((vol, page))
            if target_title is None:
                continue

            # Don't overwrite the canonical article title with itself
            if topic == target_title:
                continue

            # First-wins dedup
            if topic not in result:
                result[topic] = target_title

    return result


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

    from britannica.db.models import Article, ArticleSegment, SourcePage
    from britannica.db.session import SessionLocal

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
        # Join articles → segments → source pages to get raw wikitext.
        rows = (
            session.query(Article.title, SourcePage.wikitext)
            .join(ArticleSegment, ArticleSegment.article_id == Article.id)
            .join(SourcePage, ArticleSegment.source_page_id == SourcePage.id)
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
