import json
import re
from pathlib import Path

from britannica.db.models import (
    Article, ArticleContributor, Contributor, ContributorInitials, SourcePage,
)
from britannica.db.session import SessionLocal


_RAW_DIRS = [
    Path("data/raw/wikisource"),
]

# Matches: {{EB1911 footer initials|Full Name|Initials|name2=Name2|initials2=Init2}}
#   or:    {{EB1911 footer double initials|Name1|Init1|Name2|Init2}}
# The "double" variant uses four positional args instead of named
# parameters; _parse_contributors handles both shapes.
_FOOTER_PATTERN = re.compile(
    r"\{\{EB1911 footer(?: double)? initials\|([^}]+)\}\}",
    re.IGNORECASE,
)


def _clean_footer_initials(initials: str) -> list[str]:
    """Clean and split footer initials string.

    Handles compound entries (E. H. P.; X.), brackets, crosses,
    leaked markup, and HTML entities. Returns a list of clean
    initials strings (excluding anonymous X. markers).
    """
    s = initials.strip()
    # Strip brackets, parentheses, and crosses
    s = s.strip("[]()").lstrip("✠").strip()
    # Decode HTML entities
    s = s.replace("&thinsp;", "").replace("&nbsp;", " ")
    # Unwrap wiki templates (e.g. {{small-caps|He}} → He)
    s = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}", r"\1", s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\{\{[^{}]*", "", s)
    s = re.sub(r"\}\}", "", s)
    # Split on semicolons (compound entries like "E. H. P.; X.")
    parts = [p.strip() for p in s.split(";")]
    # Discard anonymous markers
    parts = [p for p in parts if p and p not in ("X.", "X")]
    # Source-typo fix: initials always end with a period in the DB,
    # but some article signatures are missing it (e.g. `A. H. J. G}}`
    # in AGRARIAN LAWS). Append the period when the string ends in a
    # letter — never when it ends in `.`, `*`, or other punct.
    parts = [p + "." if p and p[-1].isalpha() else p for p in parts]
    return parts


def _parse_contributors(template_content: str) -> list[dict[str, str]]:
    """Parse contributor names and initials from a footer template.

    Handles both the single-author form (positional name+init, with
    optional name2=/initials2=... named-parameter second author) and
    the `footer double initials` variant which uses four positional
    arguments (name1|init1|name2|init2).
    """
    results = []
    parts = template_content.split("|")

    # Positional args (skip font-size sentinels like "108%")
    positional = [p.strip() for p in parts if "=" not in p and "%" not in p]

    # First contributor: first two positional args.
    if len(positional) >= 2:
        for clean_init in _clean_footer_initials(positional[1]):
            results.append({
                "full_name": positional[0],
                "initials": clean_init,
            })

    # Second contributor: the `footer double initials` template uses
    # positional args 3 and 4 for (name2, init2). Detect by looking
    # for a name-shaped third arg (has a space / capital letters).
    if len(positional) >= 4:
        for clean_init in _clean_footer_initials(positional[3]):
            results.append({
                "full_name": positional[2],
                "initials": clean_init,
            })

    # Additional contributors via named params: name2=…|initials2=…
    named = {}
    for part in parts:
        if "=" in part:
            key, _, value = part.partition("=")
            named[key.strip()] = value.strip()

    for n in range(2, 10):
        name_key = f"name{n}"
        init_key = f"initials{n}"
        if name_key in named and init_key in named:
            for clean_init in _clean_footer_initials(named[init_key]):
                results.append({
                    "full_name": named[name_key],
                    "initials": clean_init,
                })

    return results


def _load_raw_wikitext(volume: int, page_number: int) -> str | None:
    """Load the original wikitext from the cached JSON file on disk."""
    padded = f"vol{volume:02d}-page{page_number:04d}.json"
    for raw_dir in _RAW_DIRS:
        for subdir in sorted(raw_dir.iterdir()) if raw_dir.exists() else []:
            if not subdir.is_dir():
                continue
            path = subdir / padded
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("raw_text", "")
    return None


def _normalize_initials(initials: str) -> str:
    """Normalize initials for matching: strip markup, normalize spacing."""
    import re
    # Strip leaked wiki/HTML markup
    s = re.sub(r"\{\{[^{}]*", "", initials)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\}\}", "", s)
    # Normalize asterisk placement: "O*.", "O. *", "O.*" → "O.*"
    s = re.sub(r"\*\s*\.", ".*", s)
    s = re.sub(r"\.\s*\*", ".*", s)
    # Deduplicate repeated punctuation
    s = re.sub(r"\*+", "*", s)
    s = re.sub(r"\.+", ".", s)
    # Normalize spacing: "A.N." → "A. N.", but keep ".*" together
    s = re.sub(r"\.([A-Za-z])", r". \1", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


_initials_cache: dict[str, Contributor | None] = {}


def _find_contributor(session, initials: str) -> Contributor | None:
    """Find a contributor by initials via the contributor_initials table.

    The contributor table is pre-built from front matter. This function
    only looks up — it never creates records. Uses normalization as
    a fallback when exact match fails.
    """
    norm = _normalize_initials(initials)

    if norm in _initials_cache:
        return _initials_cache[norm]

    # Try exact match against contributor_initials table
    ci = (
        session.query(ContributorInitials)
        .filter(ContributorInitials.initials == initials)
        .first()
    )
    if ci:
        contributor = session.query(Contributor).get(ci.contributor_id)
        _initials_cache[norm] = contributor
        return contributor

    # Try normalized match
    for ci in session.query(ContributorInitials).all():
        if _normalize_initials(ci.initials) == norm:
            contributor = session.query(Contributor).get(ci.contributor_id)
            _initials_cache[norm] = contributor
            return contributor

    _initials_cache[norm] = None
    return None


def extract_contributors_for_volume(volume: int) -> int:
    session = SessionLocal()

    try:
        pages = (
            session.query(SourcePage)
            .filter(SourcePage.volume == volume)
            .order_by(SourcePage.page_number)
            .all()
        )

        # Pre-load all segments per page so we can match each footer
        # signature to the article whose segment contains it.
        from britannica.db.models import ArticleSegment
        segments_by_page: dict[int, list[ArticleSegment]] = {}
        for seg in (
            session.query(ArticleSegment)
            .join(Article, ArticleSegment.article_id == Article.id)
            .filter(Article.volume == volume)
            .all()
        ):
            segments_by_page.setdefault(seg.source_page_id, []).append(seg)

        created = 0

        for page in pages:
            raw = _load_raw_wikitext(volume, page.page_number)
            if not raw:
                continue

            page_segs = segments_by_page.get(page.id, [])
            if not page_segs:
                continue

            for match in _FOOTER_PATTERN.finditer(raw):
                # Attribute this footer to the article whose segment
                # contains it. Multiple articles can share a page
                # (MALONIC ACID ends, MALORY ends, MALOT ends on ws513);
                # each footer belongs to the article it sits within.
                article_id = _article_for_footer(
                    match, raw, page_segs)
                if article_id is None:
                    continue

                contributors = _parse_contributors(match.group(1))

                for i, contrib in enumerate(contributors):
                    contributor = _find_contributor(
                        session, contrib["initials"]
                    )
                    if not contributor:
                        continue

                    existing = (
                        session.query(ArticleContributor)
                        .filter(
                            ArticleContributor.article_id == article_id,
                            ArticleContributor.contributor_id == contributor.id,
                        )
                        .first()
                    )
                    if existing:
                        continue

                    session.add(
                        ArticleContributor(
                            article_id=article_id,
                            contributor_id=contributor.id,
                            sequence=i + 1,
                        )
                    )
                    created += 1

        session.commit()
        return created

    finally:
        session.close()


def _article_for_footer(match, raw: str, page_segs: list) -> int | None:
    """Find which article's segment contains this footer match.

    A footer `{{EB1911 footer initials|…}}` at the end of an article
    is preceded by that article's content. We find the segment whose
    text ends with content near the footer's position.
    """
    footer_start = match.start()
    footer_text = match.group(0)

    # Try each segment: does its text contain this exact footer?
    # (segment_text has the footer literal since it hasn't been
    # transformed yet at contributor-extraction time.)
    candidates = []
    for seg in page_segs:
        if seg.segment_text and footer_text in seg.segment_text:
            candidates.append(seg)

    if not candidates:
        # Fallback: attribute to the article whose segment ends closest
        # to (but not past) the footer position in raw text.
        best = None
        best_dist = None
        for seg in page_segs:
            if not seg.segment_text:
                continue
            # Find where this segment's text ends in raw.
            tail = seg.segment_text[-40:] if len(seg.segment_text) >= 40 else seg.segment_text
            pos = raw.rfind(tail)
            if pos < 0:
                continue
            seg_end = pos + len(tail)
            if seg_end <= footer_start:
                dist = footer_start - seg_end
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = seg
        return best.article_id if best else None

    # Pick the most specific match (usually only one)
    return candidates[0].article_id
