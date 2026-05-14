import json
import re
from collections import Counter
from pathlib import Path

from sqlalchemy import func

from britannica.export.sections import detect_sections
from britannica.db.models import (
    Article, ArticleContributor, ArticleImage,
    Contributor, ContributorInitials, CrossReference, SourcePage,
)
from britannica.db.session import SessionLocal
from britannica.export.body_postprocess import (
    _BIBLIOGRAPHIC_PATTERNS,
    _PROTECTED_SPAN_RES,
    _clean_surface_for_matching,
    _looks_bibliographic,
    _protected_ranges,
    _strip_redundant_title,
)
from britannica.export.pages import (
    _LEAF_OFFSET,
    _get_printed_pages,
    _get_scan_map,
    _leaf_for_ws,
    _load_printed_pages,
    _load_scan_map,
    _printed_page,
)
from britannica.markers import IMG_RE, strip_page_markers
from britannica.captions import clean_caption


_QUALITY_NOTES = {
    0: "Untranscribed page.",
    1: "Unproofread OCR text.",
    2: "Problematic transcription.",
}


_TITLE_PREFIXES = re.compile(
    r"^(Sir |Rev\.? |Colonel |Major-General |Lieut\.-Gen\. |"
    r"Right Hon\.? |The |Hon\.? |Rt\.? Rev\.? |Very Rev\.? |"
    r"Viscount |Lord |Rear-Admiral |Field-Marshal |Mrs |"
    r"Prince |Princess |Earl of |Baron |Dr\.? )+",
    re.IGNORECASE,
)


def _resolve_bio_articles(session, contrib_map: dict[str, dict]) -> None:
    """Add bio_article_filename to contributors with biographical articles."""
    # Build title -> filename lookup from all articles in DB
    all_articles = session.query(Article).all()
    title_map: dict[str, str] = {}
    for a in all_articles:
        title_map[a.title.upper()] = _safe_filename(a, a.title)

    for entry in contrib_map.values():
        desc_raw = entry.get("description") or ""
        desc = desc_raw.lower()
        if "biographical article" not in desc:
            continue

        # If the source description contained an explicit
        # ``{{EB1911 article link|target|display}}`` template, we
        # preserved it as a ``«BIOLINK:target|display«/BIOLINK»``
        # marker in `_clean_description`.  Use the display text
        # (full article title as written in the source) as the
        # primary lookup key — bypasses the surname-inversion path
        # for peerage cases like St. Cyres → Iddesleigh.
        bio_m = re.search(
            r"«BIOLINK:([^|«]*)\|([^«]*)«/BIOLINK»", desc_raw,
        )
        if bio_m:
            link_target, link_display = (
                bio_m.group(1).strip(), bio_m.group(2).strip()
            )
            # Try display first (matches the article title verbatim
            # in the canonical "SURNAME, FIRSTNAMES, ..." form).
            for cand in (link_display, link_target):
                fn = title_map.get(cand.upper())
                if fn:
                    entry["bio_article_filename"] = fn
                    break
            else:
                # No exact match.  Fall through to surname inversion
                # below — the source's link target may differ slightly
                # from how the article is filed.
                pass
            if "bio_article_filename" in entry:
                continue

        full_name = entry["full_name"]
        # Strip parenthetical dates
        clean = re.sub(r"\s*\([^)]*\)", "", full_name).strip()
        # Also strip trailing ordinals like "1st Baron Farnborough"
        clean = re.sub(r",?\s+\d+\w*\s+Baron\s+.*$", "", clean, flags=re.IGNORECASE).strip()
        # Strip titles/honorifics
        stripped = _TITLE_PREFIXES.sub("", clean).strip()

        parts = stripped.split()
        if not parts:
            continue

        last = parts[-1].upper()
        firsts = " ".join(parts[:-1]).upper()

        # Build candidate inversions: without and with honorifics
        candidates: list[str] = []
        if firsts:
            candidates.append(f"{last}, {firsts}")
            # Also try with "Sir", "Rev." etc. between last name and first
            title_match = _TITLE_PREFIXES.match(clean)
            if title_match:
                title = title_match.group(0).strip().upper()
                candidates.append(f"{last}, {title} {firsts}")
        else:
            candidates.append(last)

        fn = None
        for candidate in candidates:
            fn = title_map.get(candidate)
            if fn:
                break
            fn = next(
                (title_map[t] for t in title_map if t.startswith(candidate)),
                None,
            )
            if fn:
                break
        if not fn and len(parts) > 1:
            prefix = f"{last}, {parts[0].upper()}"
            fn = next(
                (title_map[t] for t in title_map if t.startswith(prefix)),
                None,
            )
        # Fallback: word-set containment (A⊆B or B⊆A). Strip brackets
        # and punctuation from both sides so titles with qualifiers
        # ("MORLEY [of Blackburn], JOHN MORLEY") can match peerage-
        # style contributor names ("Blackburn, Viscount Morley of").
        if not fn:
            def _tokens(s):
                s = re.sub(r"[\[\]\(\)]", " ", s)
                return {w.upper().rstrip(".,:;") for w in s.split()
                        if w.strip(".,:;[]()")}
            # Drop filler words that don't help identify the person.
            _filler = {"OF", "THE", "AND", "VISCOUNT", "BARON", "LORD",
                       "LADY", "DUKE", "EARL", "COUNT", "COUNTESS",
                       "MARQUIS", "KING", "QUEEN", "SIR"}
            name_words = _tokens(stripped) - _filler
            if name_words:
                for title, title_fn in title_map.items():
                    if "," not in title:
                        continue
                    title_words = _tokens(title) - _filler
                    if not title_words:
                        continue
                    if name_words <= title_words or title_words <= name_words:
                        fn = title_fn
                        break

        if fn:
            entry["bio_article_filename"] = fn

    # Strip BIOLINK markers from all descriptions: the viewer hides the
    # "See the biographical article…" sentence anyway, but if any case
    # leaks past the regex strip we don't want the raw marker visible.
    # `«BIOLINK:target|display«/BIOLINK»` → `display`.
    for entry in contrib_map.values():
        desc = entry.get("description") or ""
        if "«BIOLINK:" in desc:
            entry["description"] = re.sub(
                r"«BIOLINK:[^|«]*\|([^«]*)«/BIOLINK»",
                r"\1", desc,
            )


def _source_quality(session, article: Article) -> dict:
    """Build source quality metadata from page quality levels."""
    pages = (
        session.query(SourcePage)
        .filter(
            SourcePage.volume == article.volume,
            SourcePage.page_number >= article.page_start,
            SourcePage.page_number <= article.page_end,
        )
        .all()
    )
    levels = Counter()
    for page in pages:
        m = re.search(r'pagequality level="(\d)"', page.wikitext or page.raw_text or "")
        level = int(m.group(1)) if m else 3  # default to proofread
        levels[level] += 1

    lowest = min(levels.keys()) if levels else 3
    note = _QUALITY_NOTES.get(lowest)

    # Per-page quality map for margin indicators (only include non-validated)
    page_quality = {}
    for page in pages:
        m = re.search(r'pagequality level="(\d)"', page.wikitext or page.raw_text or "")
        level = int(m.group(1)) if m else 3
        if level < 3:
            page_quality[str(page.page_number)] = level

    return {
        "page_levels": {str(k): v for k, v in sorted(levels.items())},
        "lowest_level": lowest,
        "note": note,
        "unproofed_pages": page_quality,
    }


from britannica.util.strings import section_slug as _section_slug


def stable_id(article) -> str:
    """Deterministic article identifier: {vol:02d}-{page:04d}-{section}.

    - `volume` and `page_start` are intrinsic source properties — only
      change when the article's physical location in the wikitext moves.
    - Section slug disambiguates the up-to-12 articles that can share a
      (vol, page) on a crowded page. Derived from the article's
      `<section begin="X">` tag; falls back to a slug of the title when
      no section name is stored (plates, legacy rows).

    Stable URLs / S3 keys / Meilisearch doc IDs rely on this form.
    External citations to britannica11.org/article/{stable_id}/{slug}
    survive rebuilds."""
    slug = _section_slug(article.section_name) if article.section_name else ""
    if not slug:
        slug = _section_slug(article.title)
    return f"{article.volume:02d}-{article.page_start:04d}-{slug}"


def _safe_filename(article_id, title: str) -> str:
    """Generate a filename from an Article instance (or precomputed stable_id).

    Numeric int IDs are no longer accepted — callers must pass the full
    Article object or the stable-id string."""
    if isinstance(article_id, Article):
        stable = stable_id(article_id)
    elif isinstance(article_id, str):
        stable = article_id
    else:
        raise TypeError(
            f"_safe_filename expected str stable_id or Article, got "
            f"{type(article_id).__name__}")
    safe_title = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_"
        for ch in title.upper()
    )
    return f"{stable}-{safe_title}.json"


# Structured-content spans we must not wrap inside (breaking them
# would corrupt table / math / preformatted rendering).  Footnotes
# are intentionally NOT protected — their prose deserves the same
# cross-reference treatment as the main body.


def _wrap_resolved_xrefs_in_body(
    body: str, xrefs, self_stable_id: str, session
) -> str:
    """Wrap resolved qv/see/see_also xref targets in body prose with
    «LN:filename|target|display«/LN» markers.

    The extractor has already identified every reliable cross-reference
    signal from the raw source and resolved each one to a target
    article; this pass just propagates those decisions into the body
    text so the prose mentions render as clickable links, not just the
    xref panel at the bottom.

    Guardrails:
      - only types qv / see / see_also (link xrefs are already wrapped
        at their wikilink site by the transform stage)
      - only xrefs with a resolved target_article_id
      - skip self-references
      - skip surfaces that look like bibliographic citations (see
        _looks_bibliographic — catches cases the upstream
        _is_bibliographic filter misses)
      - skip if the xref's own surface_text already contains «LN:»
      - **position-precise wrap**: locate the xref's surface_text in
        body, wrap the target word inside that matched range only.
        This ensures we link the specific mention the xref refers to,
        not a different occurrence of the same surname elsewhere in
        the article.
      - never wrap inside existing LN/HTMLTABLE/MATH/PRE spans
      - one wrap per target per article
    """
    if not body or not xrefs:
        return body

    WRAPPABLE_TYPES = {"qv", "see", "see_also"}
    wrapped_targets: set[str] = set()

    for xref in xrefs:
        if xref.xref_type not in WRAPPABLE_TYPES:
            continue
        if xref.target_article_id is None:
            continue
        nt = (xref.normalized_target or "").strip()
        if not nt or nt in wrapped_targets:
            continue
        # Already-linked at its own site: nothing to do.
        if xref.surface_text and "«LN:" in xref.surface_text:
            wrapped_targets.add(nt)
            continue
        # Bibliographic noise: refuse to propagate a likely-wrong link.
        if xref.xref_type in ("see", "see_also") \
                and _looks_bibliographic(xref.surface_text):
            continue
        target_article = session.get(Article, xref.target_article_id)
        if target_article is None:
            continue
        target_stable = stable_id(target_article)
        if target_stable == self_stable_id:
            continue

        surface_clean = _clean_surface_for_matching(xref.surface_text or "")
        if len(surface_clean) < 3:
            continue

        # Locate the surface_text in body.  Body may have interleaved
        # markers since extraction, so match on the marker-stripped
        # positions; walk body with a token-level approach would be
        # nice but the surface_text is rarely rewritten post-
        # extraction for the types we care about.
        surf_m = re.search(
            re.escape(surface_clean), body, re.IGNORECASE,
        )
        if not surf_m:
            # Surface not found verbatim — skip rather than guess at a
            # different occurrence.
            continue

        region_start, region_end = surf_m.start(), surf_m.end()

        # Candidate probes, longest first (prefer "CLEMENT" over just
        # the first word when both appear inside the surface).
        probes: list[str] = [nt]
        paren = re.match(r"^(.+?)\s*\([^)]*\)\s*$", nt)
        if paren:
            probes.append(paren.group(1).strip())
        if ":" in nt:
            probes.append(nt.split(":", 1)[0].strip())

        filename = _safe_filename(target_article, target_article.title)
        protected = _protected_ranges(body)

        matched: re.Match | None = None
        for probe in probes:
            if len(probe) < 3:
                continue
            pat = re.compile(
                r"\b" + re.escape(probe) + r"\b", re.IGNORECASE,
            )
            for m in pat.finditer(body, region_start, region_end):
                if any(lo <= m.start() < hi for (lo, hi) in protected):
                    continue
                matched = m
                break
            if matched:
                break
        if matched is None:
            continue

        display = matched.group(0)
        marker = f"«LN:{filename}|{nt}|{display}«/LN»"
        body = body[:matched.start()] + marker + body[matched.end():]
        wrapped_targets.add(nt)

    return body


def export_articles_to_json(
    volume: int,
    out_dir: str | Path,
    body_override: dict[int, str] | None = None,
) -> int:
    """Export one volume's articles to JSON.

    ``body_override`` (article.id → body) is a test seam: when given,
    each article's body is taken from the map instead of ``article.body``.
    Used by ``tools/diagnostics/verify_refactor.py --full`` to run the
    full pipeline (transform → export) against an in-memory shadow
    body without writing to the DB.  Production callers pass nothing
    and behavior is unchanged.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    _overrides = body_override or {}

    def _body_for(article: Article) -> str:
        return _overrides.get(article.id, article.body) or ""

    session = SessionLocal()

    try:
        articles = (
            session.query(Article)
            .filter(Article.volume == volume)
            .order_by(Article.page_start, Article.page_end, Article.title)
            .all()
        )

        # Global title → filename map for cross-volume soft-link
        # resolution (e.g. {{EB9link|Atom}} on a vol-17 article wants
        # to link to ATOM in vol 2).  Built once per export run.
        global_title_to_filename: dict[str, str] = {}
        for a in session.query(Article).all():
            if a.article_type == "plate":
                continue
            global_title_to_filename.setdefault(
                a.title.upper(), _safe_filename(a, a.title)
            )

        # Build plate → parent map.
        plate_map = {}  # parent_article_id → [plate_info, ...]
        plates = [a for a in articles if a.article_type == "plate"]
        non_plates = [a for a in articles if a.article_type != "plate"]

        def _find_parent(plate):
            """Find the parent article for a plate.

            When multiple articles share a title (e.g. 3 MAN articles
            in vol 17), prefer the one whose page range contains the
            plate's page. Otherwise fall back to title match, then
            page proximity.
            """
            plate_title = plate.title.upper()
            plate_page = plate.page_start
            # Title match — prefer articles containing the plate's page.
            title_matches = [a for a in non_plates
                             if a.title.upper() == plate_title]
            if title_matches:
                covering = [a for a in title_matches
                            if a.page_start <= plate_page <= a.page_end]
                if covering:
                    return covering[0]
                # No exact coverage: pick nearest by page distance.
                return min(title_matches,
                           key=lambda a: abs(a.page_start - plate_page))
            # Starts-with match (e.g. "DOVE" → "DOVE (BIRD)").
            if len(plate_title) > 3:
                prefix_matches = [a for a in non_plates
                                  if a.title.upper().startswith(plate_title)]
                if prefix_matches:
                    covering = [a for a in prefix_matches
                                if a.page_start <= plate_page <= a.page_end]
                    if covering:
                        return covering[0]
                    return min(prefix_matches,
                               key=lambda a: abs(a.page_start - plate_page))
            # Proximity fallback — nearest preceding article.
            for a in reversed(non_plates):
                if a.page_start <= plate_page and a.page_end >= plate_page - 5:
                    return a
            return None

        for plate in plates:
            parent = _find_parent(plate)
            if parent:
                plate_map.setdefault(parent.id, []).append({
                    "title": plate.title,
                    "filename": _safe_filename(plate, plate.title),
                    "page": _printed_page(plate.volume, plate.page_start),
                })

        exported = 0

        for article in articles:
            xrefs = (
                session.query(CrossReference)
                .filter(CrossReference.article_id == article.id)
                .order_by(CrossReference.id)
                .all()
            )

            xref_list = []
            for xref in xrefs:
                entry = {
                    "surface_text": xref.surface_text,
                    "normalized_target": xref.normalized_target,
                    "xref_type": xref.xref_type,
                    "status": xref.status,
                    "target_article_id": xref.target_article_id,
                }
                if xref.target_section:
                    entry["target_section"] = xref.target_section
                if xref.target_article_id is not None:
                    target = session.get(Article, xref.target_article_id)
                    if target:
                        entry["target_filename"] = _safe_filename(
                            target, target.title
                        )
                xref_list.append(entry)

            quality = _source_quality(session, article)

            # For plates, find the parent article (same logic as plate_map).
            parent_article_info = None
            if article.article_type == "plate":
                parent = _find_parent(article)
                if parent:
                    parent_article_info = {
                        "title": parent.title,
                        "filename": _safe_filename(parent, parent.title),
                    }

            # Resolve inline link markers: embed target filename for resolved xrefs
            # «LN:target|display«/LN» → «LN:filename|target|display«/LN»
            body = _body_for(article)
            # Phase 2 body-wrapping: propagate resolved qv/see/see_also
            # xrefs into the body prose so those mentions render as
            # clickable links instead of only appearing in the xref
            # panel.  Runs before the 2-part → 3-part resolver below,
            # and emits 3-part markers directly so the resolver is a
            # no-op for our new wraps.
            body = _wrap_resolved_xrefs_in_body(
                body, xrefs, stable_id(article), session
            )
            link_targets: dict[str, str] = {}  # normalized_target → filename
            for xref in xrefs:
                if xref.target_article_id is not None and xref.normalized_target:
                    target = session.get(Article, xref.target_article_id)
                    if target:
                        link_targets[xref.normalized_target.lower()] = _safe_filename(
                            target, target.title
                        )

            # \u00abEB9:target|display\u00ab/EB9\u00bb (soft 9th-edition refs):
            # link to the 11th-edition article if one exists, else
            # plain display text \u2014 never a search-link fallback.
            def _resolve_eb9(m: re.Match) -> str:
                target_text, display = m.group(1), m.group(2)
                fn = global_title_to_filename.get(target_text.strip().upper())
                if fn:
                    return f"\u00abLN:{fn}|{target_text}|{display}\u00ab/LN\u00bb"
                return display
            body = re.sub(
                r"\u00abEB9:([^|]*)\|([^\u00ab]*)\u00ab/EB9\u00bb",
                _resolve_eb9,
                body,
            )

            def _resolve_link(m: re.Match) -> str:
                target_text, display = m.group(1), m.group(2)
                fn = link_targets.get(target_text.strip().lower())
                if fn:
                    return f"\u00abLN:{fn}|{target_text}|{display}\u00ab/LN\u00bb"
                return m.group(0)  # leave unresolved as-is (2-part)

            body = re.sub(
                r"\u00abLN:([^|]*)\|([^\u00ab]*)\u00ab/LN\u00bb",
                _resolve_link,
                body,
            )

            # Convert PAGE markers from Wikisource to printed page numbers.
            # A ws page with no direct entry in printed_pages.json is
            # a plate — no printed number — so we drop its marker
            # entirely rather than walking back to the previous page
            # (which creates misleading duplicates like "p. 980" twice
            # with plate content between them).
            pp = _get_printed_pages().get(str(article.volume), {})
            def _replace_page_marker(m):
                ws = int(m.group(1))
                direct = pp.get(str(ws))
                if direct is None:
                    return ""
                return f"\x01PAGE:{direct}\x01"

            body = re.sub(r"\x01PAGE:(\d+)\x01", _replace_page_marker, body)

            # Fill in missing captions on body IMG markers from the
            # ArticleImage table. transform_articles emits
            # `{{IMG:filename}}` (no caption) when the wikitext has
            # the image and caption on separate lines (the wrapper
            # patterns WEIGHING MACHINES / SEWING MACHINES use).
            # extract_images recovers those captions into the DB; here
            # we write them back into the body marker so the viewer
            # can render them.
            _img_caps: dict[str, str] = {}
            for _img in (
                session.query(ArticleImage)
                .filter(ArticleImage.article_id == article.id)
                .all()
            ):
                if _img.caption and _img.filename not in _img_caps:
                    _img_caps[_img.filename] = _img.caption

            if _img_caps:
                def _sanitize_caption(cap: str) -> str:
                    # A MediaWiki image *size* param (`x90px`, `125px`) the
                    # extractor mistook for a caption \u2014 not real caption
                    # text.  Treat as no caption.
                    if re.fullmatch(r"\s*x?\d+px\s*", cap, re.IGNORECASE):
                        return ""
                    # extract_images stored this caption straight off the
                    # raw wikitext, so it may carry templates, entities,
                    # cell-attr strings, stray pipes/braces \u2014 run the same
                    # caption cleaner every {{IMG:fn|cap}} emit site uses.
                    return clean_caption(cap)

                def _patch_img(m):
                    fn = m.group(1)
                    existing = m.group(2)
                    if existing:  # caption already inline — keep it
                        return m.group(0)
                    cap = _img_caps.get(fn)
                    if cap:
                        cap = _sanitize_caption(cap)
                        return f"{{{{IMG:{fn}|{cap}}}}}" if cap else m.group(0)
                    return m.group(0)

                body = re.sub(
                    r"\{\{IMG:([^|}]+)(?:\|([^{}]*))?\}\}",
                    _patch_img,
                    body,
                )

            # Strip redundant bold article title at body start. EB1911
            # articles open with "'''TITLE'''" (rendered as «B»TITLE«/B»);
            # since the viewer already shows the title in the header,
            # the inline repeat is redundant.
            body = _strip_redundant_title(body, article.title)

            # No clean_body: each element is responsible for emitting
            # clean output (split_wiki_row / parse_wiki_table /
            # emit_html_cell consolidation made this possible).  Any
            # remaining stray-pipe artifact is a producer bug to fix
            # at source, not patch over downstream.
            cleaned_body = body

            # word_count and sections describe the *shipped* body, so
            # they're derived from body — not pre-strip text.
            # sections in particular must match what the viewer's
            # detectSections() emits at render time (it runs on the
            # shipped body), or deep-section URLs won't resolve.
            payload = {
                "id": article.id,
                "stable_id": stable_id(article),
                "title": article.title,
                "article_type": article.article_type,
                "volume": article.volume,
                "page_start": _printed_page(article.volume, article.page_start),
                "page_end": _printed_page(article.volume, article.page_end),
                "ws_page_start": article.page_start,
                "ws_page_end": article.page_end,
                "leaf_start": _leaf_for_ws(article.volume, article.page_start),
                "leaf_end": _leaf_for_ws(article.volume, article.page_end),
                "source_quality": quality,
                "word_count": len(cleaned_body.split()),
                "parent_article": parent_article_info,
                "body": cleaned_body,
                "sections": detect_sections(cleaned_body),
                "xrefs": xref_list,
                "images": [
                    {
                        "filename": img.filename,
                        "caption": img.caption,
                        "commons_url": img.commons_url,
                        "source_page_id": img.source_page_id,
                    }
                    for img in (
                        session.query(ArticleImage)
                        .filter(ArticleImage.article_id == article.id)
                        .order_by(ArticleImage.source_page_id, ArticleImage.sequence_in_article)
                        .all()
                    )
                ],
                "plates": [
                    {
                        "title": plate_info["title"],
                        "filename": plate_info["filename"],
                        "page": plate_info["page"],
                    }
                    for plate_info in plate_map.get(article.id, [])
                ],
                "contributors": [
                    {
                        "initials": (
                            session.query(ContributorInitials.initials)
                            .filter(ContributorInitials.contributor_id == contrib.id)
                            .first() or ("",)
                        )[0],
                        "full_name": contrib.full_name,
                        "credentials": contrib.credentials,
                        "description": contrib.description,
                    }
                    for contrib in (
                        session.query(Contributor)
                        .join(ArticleContributor, ArticleContributor.contributor_id == Contributor.id)
                        .filter(ArticleContributor.article_id == article.id)
                        .order_by(ArticleContributor.sequence)
                        .all()
                    )
                ],
            }

            safe_filename = _safe_filename(article, article.title)
            article_json = json.dumps(payload, indent=2, ensure_ascii=False)

            (out_path / safe_filename).write_text(article_json, encoding="utf-8")

            exported += 1

        # Write index file for the viewer
        index = []
        for article in articles:
            xref_count = (
                session.query(CrossReference)
                .filter(CrossReference.article_id == article.id)
                .count()
            )
            resolved_count = (
                session.query(CrossReference)
                .filter(
                    CrossReference.article_id == article.id,
                    CrossReference.status == "resolved",
                )
                .count()
            )
            body = _body_for(article)
            body = _strip_redundant_title(body, article.title)
            # First ~10 words of body for disambiguation in the index.
            # Skip any leading paragraphs that are just image / table /
            # verse markers — the preview should be TEXT, not raw markup
            # (e.g. BEE's body starts with `{{IMG:…}}` followed by a
            # caption; we want the caption/body, not the raw marker).
            preview_source = body
            preview_source = strip_page_markers(preview_source)
            preview_source = IMG_RE.sub("", preview_source)
            preview_source = re.sub(
                r"\{\{TABLE[A-Z]?:[\s\S]*?\}TABLE\}", "", preview_source)
            preview_source = re.sub(
                r"\{\{VERSE:[\s\S]*?\}VERSE\}", "", preview_source)
            preview_source = re.sub(
                r"\{\{LEGEND:[\s\S]*?\}LEGEND\}", "", preview_source)
            preview_source = re.sub(
                r"\u00abHTMLTABLE:[\s\S]*?\u00ab/HTMLTABLE\u00bb",
                "", preview_source)
            # Absorbed-subsection headings aren't part of the preview
            preview_source = re.sub(
                r"\u00abSEC:[^\u00ab]*\u00ab/SEC\u00bb",
                "", preview_source)
            # First non-empty, non-caption line of the preview source.
            # When an article opens with an image whose caption sits in
            # its own paragraph (not bundled into the IMG marker), that
            # caption shouldn't be the preview — e.g. BEE opens with
            # "Fig. 1.—Honey-bee (Apis mellifica)…" which should be
            # skipped so the real body text follows.
            _caption_re = re.compile(
                r"^\s*(?:\u00abSC\u00bb)?\s*(?:Fig|Plate)s?"
                r"(?:\u00ab/SC\u00bb)?\s*\.?\s*"
                r"(?:\d+|[IVX]+)?\b",
                re.IGNORECASE,
            )
            first_line = ""
            for ln in preview_source.split("\n"):
                if not ln.strip():
                    continue
                if _caption_re.match(ln):
                    continue
                first_line = ln
                break
            # Strip footnotes for the preview
            first_line = re.sub(r"\u00abFN(?:\[[^\]]+\])?:.*?\u00ab/FN\u00bb", "", first_line)
            # Strip formatting markers but KEEP the text between them
            first_line = re.sub(r"\u00abB\u00bb(.*?)\u00ab/B\u00bb", r"\1", first_line)
            first_line = re.sub(r"\u00abI\u00bb(.*?)\u00ab/I\u00bb", r"\1", first_line)
            first_line = re.sub(r"\u00abSC\u00bb(.*?)\u00ab/SC\u00bb", r"\1", first_line)
            first_line = re.sub(r"\u00abSH\u00bb(.*?)\u00ab/SH\u00bb", r"\1", first_line)
            # Strip link markers, keep display text
            first_line = re.sub(r"\u00abLN:[^|]*\|([^«]*)\u00ab/LN\u00bb", r"\1", first_line)
            # Strip any remaining markers
            first_line = re.sub(r"\u00ab/?[A-Z]+\u00bb", "", first_line)
            first_line = re.sub(r"  +", " ", first_line).strip()
            words = first_line.split()
            if len(words) > 10:
                body_start = " ".join(words[:10]) + "\u2026"
            else:
                body_start = " ".join(words)

            index.append({
                "id": article.id,
                "stable_id": stable_id(article),
                "title": article.title,
                "article_type": article.article_type,
                "filename": _safe_filename(article, article.title),
                "volume": article.volume,
                "page_start": _printed_page(article.volume, article.page_start),
                "page_end": _printed_page(article.volume, article.page_end),
                "ws_page_start": article.page_start,
                "ws_page_end": article.page_end,
                "leaf_start": _leaf_for_ws(article.volume, article.page_start),
                "leaf_end": _leaf_for_ws(article.volume, article.page_end),
                "body_length": len(body.split()),
                "body_start": body_start,
                "xref_count": xref_count,
                "resolved_count": resolved_count,
            })

        # Merge with existing index (from other volumes)
        index_path = out_path / "index.json"
        if index_path.exists():
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            # Remove entries for this volume, keep other volumes
            existing = [e for e in existing if e.get("volume") != volume]
            index = existing + index

        index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Build contributor index
        contrib_map: dict[str, dict] = {}
        for article in articles:
            contribs = (
                session.query(Contributor)
                .join(ArticleContributor, ArticleContributor.contributor_id == Contributor.id)
                .filter(ArticleContributor.article_id == article.id)
                .order_by(ArticleContributor.sequence)
                .all()
            )
            for c in contribs:
                if c.full_name not in contrib_map:
                    all_initials = [
                        ci.initials for ci in
                        session.query(ContributorInitials)
                        .filter(ContributorInitials.contributor_id == c.id)
                        .all()
                    ]
                    contrib_map[c.full_name] = {
                        "full_name": c.full_name,
                        "initials": ", ".join(all_initials),
                        "credentials": c.credentials or "",
                        "description": c.description or "",
                        "articles": [],
                    }
                contrib_map[c.full_name]["articles"].append({
                    "id": article.id,
                    "stable_id": stable_id(article),
                    "title": article.title,
                    "filename": _safe_filename(article, article.title),
                })

        # Merge with existing contributors (from other volumes)
        contrib_path = out_path / "contributors.json"
        if contrib_path.exists():
            existing_contribs = json.loads(contrib_path.read_text(encoding="utf-8"))
            for ec in existing_contribs:
                name = ec["full_name"]
                if name in contrib_map:
                    # Merge article lists, avoiding duplicates
                    existing_fns = {a["filename"] for a in contrib_map[name]["articles"]}
                    for a in ec["articles"]:
                        if a["filename"] not in existing_fns:
                            contrib_map[name]["articles"].append(a)
                else:
                    contrib_map[name] = ec

        def _split_name_suffix(full_name: str) -> tuple[str, str]:
            """Strip parenthetical dates and split a contributor's full
            name into (head, suffix).  Suffix is anything after the
            first comma — degrees ('Ph.D', 'Lic. Theol', 'Litt.D'),
            titles ('Bart', 'Jr', 'Captain'), or any post-name
            qualifier.  The head is the part to apply Last-First
            rearrangement to; the suffix is re-appended afterwards."""
            import re as _re
            name = _re.sub(r"\s*\([^)]*\)", "", full_name).strip()
            name = name.rstrip(",").strip()
            head, _, tail = name.partition(",")
            return head.strip(), tail.strip()

        def _sort_name(c: dict) -> str:
            head, _ = _split_name_suffix(c["full_name"])
            return head.rsplit(None, 1)[-1].lower() if head else ""

        def _display_name(full_name: str) -> str:
            """Convert 'First Middle Last, Degree' to
            'Last, First Middle, Degree'."""
            head, suffix = _split_name_suffix(full_name)
            parts = head.rsplit(None, 1)
            rearranged = f"{parts[1]}, {parts[0]}" if len(parts) == 2 else head
            return f"{rearranged}, {suffix}" if suffix else rearranged

        for entry in contrib_map.values():
            entry["display_name"] = _display_name(entry["full_name"])

        # Resolve biographical article links for contributors
        _resolve_bio_articles(session, contrib_map)

        contrib_list = sorted(contrib_map.values(), key=_sort_name)
        contrib_path.write_text(
            json.dumps(contrib_list, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return exported

    finally:
        session.close()