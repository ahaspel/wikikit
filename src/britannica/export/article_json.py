import json
import re
from collections import Counter
from pathlib import Path

from sqlalchemy import func

from britannica.export.sections import detect_sections
from britannica.db.models import (
    Article, ArticleContributor, ArticleSegment,
    Contributor, ContributorInitials, CrossReference, SourcePage,
)
from britannica.db.session import SessionLocal
from britannica.export.body_postprocess import (
    _BIBLIOGRAPHIC_PATTERNS,
    _PROTECTED_SPAN_RES,
    _clean_surface_for_matching,
    _looks_bibliographic,
    _protected_ranges,
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
from britannica.markers import markers_to_text, strip_title_markers
from britannica.export.plate_parent import find_parent_by_signal
from britannica.render.article import render_article


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


# Deterministic collision-suffix registry.  A section slug can collide within a (vol, page):
# BOG and BOGÓ both slug to "04-0131-bog" (the accent drops to nothing).  While the title rode
# in the filename this was invisible (…-BOG.json vs …-BOGÓ.json); with the title-independent
# `{stable_id}.json` key the stable_id must itself be unique.  `register_stable_id_dedup` runs
# ONCE over the whole corpus at export start; the loser of each collision (deterministic order)
# takes a `-2`/`-3` suffix — the only source of non-uniqueness once the filename dropped the title.
_STABLE_ID_SUFFIX: dict[int, str] = {}


def _section_slug_for(article) -> str:
    """The article's raw section slug — the identity discriminator that WAS the visible
    stable_id tail, and is still what an OLD `/article/{vol}-{page}-{slug}` URL carries.
    Hashed into the id below; the forwarder recomputes the same hash from this slug."""
    slug = _section_slug(article.section_name) if article.section_name else ""
    return slug or _section_slug(article.title)


def _base_stable_id(article) -> str:
    # Hash the section slug to an opaque 6-hex tail: keeps the id stable + article-anchored,
    # but off the URL go the accent-mangling (poincar), the cruft (algebrab), and the readable
    # name (which returns, correctly, as the cosmetic title slug).  A forwarder recomputes this
    # SAME hash from an old URL's slug — table-free.  `hashlib` is deterministic (no Date/random).
    import hashlib
    h = hashlib.sha1(_section_slug_for(article).encode("utf-8")).hexdigest()[:6]
    return f"{article.volume:02d}-{article.page_start:04d}-{h}"


def register_stable_id_dedup(articles) -> int:
    """Assign deterministic collision suffixes so every article's stable_id is unique.  Call
    once over the FULL corpus before any stable_id / filename / «LN» baking.  Returns how many
    articles received a suffix (0 in a clean corpus)."""
    from collections import defaultdict as _dd
    _STABLE_ID_SUFFIX.clear()
    by_base: dict[str, list] = _dd(list)
    for a in articles:
        by_base[_base_stable_id(a)].append(a)
    n = 0
    for _base, arts in by_base.items():
        if len(arts) <= 1:
            continue
        # Deterministic: sort by (title, id).  First keeps the bare id; the rest get -2, -3…
        for i, a in enumerate(sorted(arts, key=lambda x: ((x.title or ""), x.id))[1:], start=2):
            _STABLE_ID_SUFFIX[a.id] = f"-{i}"
            n += 1
    return n


def stable_id(article) -> str:
    """Deterministic, UNIQUE article identifier: {vol:02d}-{page:04d}-{section}[-N].

    - `volume` and `page_start` are intrinsic source properties — only change when the
      article's physical location in the wikitext moves.
    - The section slug disambiguates the up-to-12 articles that can share a (vol, page).
      Derived from the article's `<section begin="X">` tag (article-anchored, so it never
      shifts when a page-mate is added/removed — unlike a positional ordinal); falls back to
      a slug of the title when no section name is stored (plates, legacy rows).
    - A rare within-page slug collision (BOG vs BOGÓ) takes a deterministic `-N` suffix from
      `register_stable_id_dedup` — see above.

    Stable URLs / S3 keys / Meilisearch doc IDs rely on this form.  External citations to
    britannica11.org/article/{stable_id} survive rebuilds (the title is title-independent)."""
    return _base_stable_id(article) + _STABLE_ID_SUFFIX.get(getattr(article, "id", None), "")


def _safe_filename(article_id, title: str = "") -> str:
    """Article JSON filename = ``{stable_id}.json`` — TITLE-INDEPENDENT, so a title change
    (ALGEBRAB→ALGEBRA) or a title-formatting difference never moves the file or the URL; the
    viewer routes on the stable_id alone.  Accepts an Article instance or a precomputed
    stable-id string.  (``title`` is retained for call-site compatibility but is no longer
    part of the key.)"""
    if isinstance(article_id, Article):
        stable = stable_id(article_id)
    elif isinstance(article_id, str):
        stable = article_id
    else:
        raise TypeError(
            f"_safe_filename expected str stable_id or Article, got "
            f"{type(article_id).__name__}")
    return f"{stable}.json"


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
      - never wrap inside existing LN/TABLE/MATH/PRE spans
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


def _xrefs_from_body(body, article_id, link_index):
    """The candidate-source half of the xref decorator: extract every
    reference from the body and resolve it off the in-memory index,
    returning transient (un-persisted) CrossReference rows.  No DB read.

    No index (``link_index is None`` — a single-article look-render that skips
    the corpus-wide resolution) means nothing to resolve against, so return no
    xrefs; the body's «LN» markers then strip to their display text downstream."""
    if link_index is None:
        return []
    from britannica.xrefs.extractor import extract_xrefs
    from britannica.pipeline.stages.resolve_xrefs import resolve_one
    xrefs = []
    for m in extract_xrefs(body):
        xr = CrossReference(
            article_id=article_id,
            surface_text=m["surface_text"],
            normalized_target=m["normalized_target"],
            xref_type=m["xref_type"],
        )
        xr.target_article_id, xr.target_section = resolve_one(xr, link_index)
        xr.status = ("resolved" if xr.target_article_id is not None
                     else "unresolved")
        xrefs.append(xr)
    return xrefs


def _link_xrefs_in_body(body, xrefs, self_stable_id, session,
                        global_title_to_filename):
    """The body-linking half of the xref decorator, lifted out of
    export_articles_to_json: resolve the 2-part «LN»/«EB9» PRODUCER markers
    to 3-part filename links FIRST, then wrap resolved qv/see prose in place.
    Order matters — see the comment on the resolve pass below."""
    link_targets: dict[str, str] = {}  # normalized_target → filename
    for xref in xrefs:
        if xref.target_article_id is not None and xref.normalized_target:
            target = session.get(Article, xref.target_article_id)
            if target:
                link_targets[xref.normalized_target.lower()] = _safe_filename(
                    target, target.title
                )

    # Resolve the 2-part producer markers («LN:target|display», «EB9:…») BEFORE any
    # 3-part «LN:filename|target|display» exists.  `_resolve_link`'s 2-part regex would
    # otherwise re-match a freshly-written 3-part marker — capturing `target|display` as
    # its display group ([^«]* spans the inner `|`), missing the filename lookup, and
    # stripping to that — leaking the pipe (`PLATE|Plate`).  So: producer «LN» first,
    # then «EB9», then the prose wraps LAST; every 3-part marker is created strictly
    # after `_resolve_link` has finished.  (A regex guard forbidding `|` in the display
    # can't work — a legit 2-part display can hold a `|` from an inline `{{IMG:…|…}}`.)
    def _resolve_link(m: re.Match) -> str:
        from britannica.xrefs.normalizer import normalize_xref_target
        target_text, display = m.group(1), m.group(2)
        # Normalize before the lookup so a `#section` target collapses to the same
        # `ARTICLE: SECTION` key extract_xrefs stored in link_targets — a `#`-bearing
        # target would otherwise miss (the key is normalized, the raw target isn't).
        fn = link_targets.get(normalize_xref_target(target_text).lower())
        if fn:
            return f"«LN:{fn}|{target_text}|{display}«/LN»"   # internal — the link target
        # Internal miss → external fallback: link out to Wikisource IFF the page really
        # exists (verified against the all-titles dump), else strip to plain display.
        # Interwiki (w:/wikt:/d:) aren't WS pages → strip.  «XL» never reaches the panel
        # (it harvests «LN» only), so external links stay inline-only — the EB11 web in
        # the panel stays internal.
        from britannica.xrefs.ws_titles import is_ws_page
        if is_ws_page(target_text):
            url = ("https://en.wikisource.org/wiki/"
                   + target_text.strip().replace(" ", "_"))
            return f"«XL:{url}|{display}«/XL»"
        return display  # unresolvable — strip the markup, keep the text
    body = re.sub(
        r"«LN:([^|]*)\|([^«]*)«/LN»",
        _resolve_link, body,
    )

    def _resolve_eb9(m: re.Match) -> str:
        target_text, display = m.group(1), m.group(2)
        fn = global_title_to_filename.get(target_text.strip().upper())
        if fn:
            return f"«LN:{fn}|{target_text}|{display}«/LN»"
        return display
    body = re.sub(
        r"«EB9:([^|]*)\|([^«]*)«/EB9»",
        _resolve_eb9, body,
    )

    # Prose-scan wraps LAST: its 3-part markers are final and must not be re-scanned by
    # `_resolve_link` above (the leak this ordering fixes).
    body = _wrap_resolved_xrefs_in_body(body, xrefs, self_stable_id, session)
    return body


def export_articles_to_json(
    volume: int,
    out_dir: str | Path,
    body_override: dict[int, str] | None = None,
    only_article_id: int | None = None,
    link_index=None,
    xref_sink: list | None = None,
) -> int:
    """Export one volume's articles to JSON.

    ``xref_sink`` (optional): when a list is passed, EVERY xref this volume
    resolves — resolved AND unresolved — is appended to it as a flat record
    (``source``/``surface``/``target``/``status``/``resolved_to``).  The
    corpus-export orchestrator collects these across volumes and dumps a single
    ``xref_resolution.jsonl`` — a diffable resolution snapshot (unresolved
    targets are otherwise discarded and only survive as a count).

    ``body_override`` (article.id → body) is a test seam: when given,
    each article's body is taken from the map instead of ``article.body``.
    Used to run the
    full pipeline (transform → export) against an in-memory shadow
    body without writing to the DB.  Production callers pass nothing
    and behavior is unchanged.

    ``only_article_id`` is the single-article iteration seam: when
    set, only that one article's JSON is written.  ``tools/render_
    article.py`` uses this together with ``body_override`` to re-render
    a single article in ~5s after a transform-code change, vs the ~2min
    that ``rebuild_volume.py`` takes for a full per-volume rebuild.
    The volume-wide ``index.json`` is also skipped in that mode since
    the existing index already lists the article.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    def _body_for(article: Article) -> str:
        return body_override.get(article.id, "")

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

        # Cache plate wikitext to avoid re-fetching when ``_find_parent``
        # is called twice per plate (plate_map build + per-article loop).
        plate_wikitext_cache: dict[int, str] = {}

        def _plate_wikitext(plate):
            if plate.id in plate_wikitext_cache:
                return plate_wikitext_cache[plate.id]
            segs = (session.query(ArticleSegment)
                    .filter(ArticleSegment.article_id == plate.id)
                    .order_by(ArticleSegment.sequence_in_article).all())
            parts = []
            for seg in segs:
                pg = session.get(SourcePage, seg.source_page_id)
                if pg and pg.wikitext:
                    parts.append(pg.wikitext)
            wt = "\n".join(parts)
            plate_wikitext_cache[plate.id] = wt
            return wt

        def _find_parent(plate):
            """Find the parent article for a plate.

            Cascade:
              1. Raw-source signal from the plate's wikitext (``<section
                 begin="..."/>`` / ``{{rh|...}}`` / ``{{c|{{x-larger|...}}}}``).
                 This is the structural fix — EB1911 plates name their
                 parent explicitly in the page header.
              2. Exact / prefix title match (legacy behavior; kept for
                 plates whose title happens to equal an article title,
                 e.g. ``DOVE`` → ``DOVE (BIRD)``).
              3. Page proximity — nearest preceding non-plate whose
                 page range contains (or nearly contains) the plate's
                 page.  Handles the ~16 plates with no recognizable
                 signal (``PLATE (VOL. X, P. Y)`` orphans, etc.).
            """
            # 1. Raw-source signal.
            signal_parent = find_parent_by_signal(
                _plate_wikitext(plate), plate.page_start, non_plates)
            if signal_parent is not None:
                return signal_parent

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
        xref_counts: dict[int, tuple[int, int]] = {}

        for article in articles:
            if (only_article_id is not None
                    and article.id != only_article_id):
                continue
            xrefs = _xrefs_from_body(
                _body_for(article), article.id, link_index)

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

            xref_counts[article.id] = (
                len(xref_list),
                sum(1 for e in xref_list if e["status"] == "resolved"),
            )

            if xref_sink is not None:
                src = stable_id(article)
                for e in xref_list:
                    xref_sink.append({
                        "source": src,
                        "surface": e["surface_text"],
                        "target": e["normalized_target"],
                        "type": e["xref_type"],
                        "status": e["status"],
                        "resolved_to": e.get("target_filename"),
                        **({"section": e["target_section"]}
                           if e.get("target_section") else {}),
                    })

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

            # Resolve inline link markers (xrefs + EB9): the body-linking
            # half of the decorator.
            body = _link_xrefs_in_body(
                _body_for(article), xrefs, stable_id(article), session,
                global_title_to_filename,
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

            # Caption back-fill from ArticleImage (the `_patch_img`
            # sweeper) was deleted 2026-05-27.  The figure-family
            # producers now emit captions inline via the canonical
            # `Figure(image, caption, legend, attribution)` -> `render_figure`
            # pipeline; any caption a producer doesn't emit inline is by
            # definition an extractor bug to fix in place, not something
            # a downstream sweeper should paper over.  The sweeper was
            # also writing junk for ~33 of its 83 corpus invocations
            # (MediaWiki `alt=` params, partial `Fig` strings, etc.), so
            # deletion improved output on those.  See
            # `[[total-functions-not-cleanup-passes]]`.
            # (Title chop-up happens at source via the sole title
            # extractor `elements/_title.py:produce_title`.  No
            # downstream sweeper.  Stale DB rows from before the chop-up
            # fix will display the leading title-bold until re-detected.)

            # No clean_body: each element is responsible for emitting
            # clean output (the recursive table fold + emit_html_cell
            # consolidation made this possible).  Any remaining
            # stray-pipe artifact is a producer bug to fix at source,
            # not patch over downstream.
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
                # Panel = the article's resolved INTERNAL cross-references only.
                # Unresolved entries and external «XL» links never enter it — the EB11
                # web downstairs stays internal (external links live inline, as «XL»).
                "xrefs": [e for e in xref_list if e["status"] == "resolved"],
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

            # The viewer is a thin shell now: Python owns marker→HTML, the client just
            # inserts this and hydrates math / rebuilds the runtime scan href.  is_local=False
            # bakes production clean URLs (/article/{id}/{title}); scans render as a bare
            # `scans.html` anchor that fixScanHrefs rebuilds at load (the back param is
            # location.href — runtime-only, never bakeable).
            payload["rendered_html"] = render_article(
                payload, is_local=False, target="site")

            safe_filename = _safe_filename(article, article.title)
            article_json = json.dumps(payload, indent=2, ensure_ascii=False)

            (out_path / safe_filename).write_text(article_json, encoding="utf-8")

            exported += 1

        # Skip the volume-wide index rebuild when we're only writing a
        # single article — the existing index.json already lists it.
        if only_article_id is not None:
            return exported

        # Write index file for the viewer
        index = []
        for article in articles:
            xref_count, resolved_count = xref_counts[article.id]
            body = _body_for(article)
            # First ~10 words of the body for disambiguation in the index.
            # The body is a marker stream; markers_to_text is the ONE converter
            # to plain text — it strips every marker, including the
            # «TITLE:…«/TITLE» head (the title is the separate `title` field), so
            # the preview is body prose and never shows the title through the body.
            preview_text = markers_to_text(body)
            # First non-empty, non-caption line.  When an article opens with an
            # image whose caption sits in its own paragraph, that caption
            # shouldn't be the preview — e.g. BEE opens with "Fig. 1.—Honey-bee
            # (Apis mellifica)…", skipped so the real body text follows.
            _caption_re = re.compile(
                r"^\s*(?:Fig|Plate)s?\s*\.?\s*(?:\d+|[IVX]+)?\b", re.IGNORECASE)
            first_line = ""
            for ln in preview_text.split("\n"):
                if not ln.strip():
                    continue
                if _caption_re.match(ln):
                    continue
                first_line = ln
                break
            first_line = re.sub(r"  +", " ", first_line).strip()
            # Reach the identifying clause: drop a leading parenthetical (dates /
            # etymology / pronunciation) and its trailing punctuation so the
            # description opens on the defining appositive ("king of England,
            # surnamed the Conqueror") instead of "(1027–1087),".  Repeated for
            # stacked parens; the title itself is already gone (markers_to_text
            # strips the «TITLE» head).
            first_line = re.sub(r"^(?:\([^()]*\)[,;:.]?\s*)+", "", first_line).strip()
            words = first_line.split()
            if len(words) > 12:
                body_start = " ".join(words[:12]) + "…"
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