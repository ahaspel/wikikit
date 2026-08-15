import json
import re
from collections import Counter
from pathlib import Path

from sqlalchemy import func

from britannica.export.sections import detect_sections
from britannica.db.models import (
    Article, ArticleContributor, ArticleSegment,
    Contributor, ContributorInitials, SourcePage,
)
from britannica.db.session import SessionLocal
from britannica.export.pages import (
    _LEAF_OFFSET,
    _get_printed_pages,
    _get_scan_map,
    _leaf_for_ws,
    _load_printed_pages,
    _load_scan_map,
    _printed_page,
)
from britannica.markers import (iter_ln_markers, markers_to_text,
                                strip_marker_tokens, strip_title_markers)
from britannica.export.plate_parent import find_parent_by_signal
from britannica.pipeline.stages.elements._link import ln_marker
from britannica.render.article import render_article
from britannica.xrefs.normalizer import (NormalizedIndex,
                                        normalize_xref_target)


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


def _resolve_ln_markers(text: str, resolve) -> str:
    """Rewrite every 2-part `«LN[kind]:target|display«/LN»` through ``resolve``.

    ``resolve(kind, target, display) -> str`` supplies the replacement.  The
    marker is read through THE one reader (`markers.iter_ln_markers`); a marker
    with no close is left exactly as it stands — this is a resolver, and
    inventing a close would hide the producer bug that failed to write one.
    """
    out, i = [], 0
    for m in iter_ln_markers(text):
        out.append(text[i:m.start])
        out.append(resolve(m.kind, m.target, m.display))
        i = m.end
    out.append(text[i:])
    return "".join(out)


def _fold_name(t: str) -> str:
    """Letters and digits only, case-folded — two spellings of ONE name fold
    alike.  `Oyster-catcher`, `Oystercatcher` and `oystercatcher` all fold to
    `oystercatcher`."""
    return re.sub(r"[\W_]+", "", t or "", flags=re.UNICODE).casefold()


def build_title_index(articles) -> NormalizedIndex:
    """THE title → filename index.  One builder, so there is one index.

    The export built this and the post-export xref pass built its own "mirror"
    of it, kept in step by a comment — and they had already drifted: the mirror
    kept plates and skipped `article_sort_key`, so the two disagreed about which
    article owns a title wherever a plate shared one or the heap order differed.
    Both now call this.
    """
    index = NormalizedIndex()
    for a in sorted(articles, key=article_sort_key):
        if a.article_type == "plate":
            continue
        index.add(a.title, _safe_filename(a, a.title))
    return index


def swapped_link(target_text: str, display: str, title_to_filename):
    """`(target, shown, filename)` when the source filed this link BACKWARDS,
    else None — the caller keeps its own order.

    The source's two-positional link templates are NOT consistently ordered:
    `{{EB1911 article link|A|B}}` files the article second 2,725 times and first
    2,291; `{{1911link|A|B}}` second 661 and first 463.  Each producer wrap
    guesses one order, so each is wrong whenever the source used the other — and
    the reader gets the FILED TITLE in running prose ("according to Munzinger,
    Werner" where the author wrote "Werner Munzinger").  Both strings survive in
    the marker, so the order is recoverable HERE, where the title index exists.

    BOTH sides a real title, pointing at DIFFERENT articles: the producer's
    positional convention decides, and `{{1911link}}` is written both ways in the
    source (50 follow its target-first rule, 21 invert it).  Where one title
    EXTENDS the other, the extending one is the reference — `Mark` vs `Mark,
    Gospel of St`, `Bismarck` vs `Bismarck, Otto Eduard Leopold von` — so a
    citation of the Gospel stops landing on the evangelist.  15 links.  Where
    neither extends the other (`Dragon` vs `Draco`, `dress` vs `Costume`) nothing
    in the data adjudicates, and this abstains rather than guess: the producer's
    convention already gets most of those right.

    Otherwise swap only when the display is an exact article title, the target is
    not, AND the display is the LONGER string.  The length direction is the whole
    discriminator: `«LN:Spain#History|Spain»`, `«LN:Victor Cousin|Cousin»` and
    `«LN:Exodus, The|Exodus»` all satisfy the first two conditions and are already
    CORRECT — prose is terser than a filed title, so a display SHORTER than its
    target is prose doing its job.  1,601 such links are left alone; 514 across
    309 articles are corrected.

    When the two strings FOLD ALIKE there is no reference to recover — they spell
    one name two ways — and showing the target would print the WIKISOURCE page
    name over the words EB1911 set in type.  STILT's
    `{{1911link|Oystercatcher|Oyster-catcher}}` is the shape: arg1 is the wiki
    page, arg2 the printed spelling, and OUR titles come from EB1911, so the side
    that matches a filed title IS the printed side (`OYSTER-CATCHER`, vol 20
    p 462).  Length cannot discriminate here — the hyphen alone makes the printed
    spelling one character "longer".  So take the swap's TARGET, which is what
    makes the link resolve, and still show the filed spelling: 40 links across 28
    pairs, all EB1911 compounds (`Bag-pipe`, `Tread-mill`, `Ear-ring`) that were
    rendering as modern closed-up forms.
    """
    # AS WRITTEN, not folded.  This decides whether to replace what the READER
    # SEES, so it is the precision question, not the recall one: under the fold
    # `Menelek II.` and `Justinian I.` match the filed `MENELEK II` / `JUSTINIAN
    # I`, the swap fires, and the regnal number the page printed disappears.
    t_up, d_up = target_text.strip().upper(), display.strip().upper()
    fn_t = title_to_filename.get_as_written(target_text)
    fn_d = title_to_filename.get_as_written(display)

    if fn_t and fn_d and fn_t != fn_d:
        if d_up.startswith(t_up) and not t_up.startswith(d_up):
            return display, _shown_text(target_text, display), fn_d
        return None

    if fn_d and not fn_t and len(display) > len(target_text):
        return display, _shown_text(target_text, display), fn_d

    # Same name, two spellings, arriving the OTHER way round.  The swap above
    # only fires when the producer put the filed title in the DISPLAY, which is
    # what the target-first templates do (`{{1911link|Oystercatcher|
    # Oyster-catcher}}`).  The display-first ones hand the wiki page name
    # straight to the display and the filed title to the target
    # (`{{EB1911 article link|Bagpipe|Bag-pipe}}`), so nothing needs swapping and
    # the reader still gets `Bagpipe` where the page prints `Bag-pipe`.  Whoever
    # matches a filed title is EB1911's own spelling, whichever slot it landed
    # in — 28 links across 21 pairs reach it by this route.
    # Length is the discriminator here as it is above, and it is what keeps this
    # to the class actually in evidence: the modern page name CLOSES UP a
    # compound EB1911 sets with a hyphen, so it is always the shorter string.
    # Without it the rule also rewrites 203 links that merely differ in an
    # apostrophe or a comma (`Seven Years' War` / `Seven Years’ War`), where
    # nothing shows which spelling is the page's and no defect was ever found.
    if (fn_t and not fn_d and len(display) < len(target_text)
            and _fold_name(target_text) == _fold_name(display)):
        return target_text, target_text, fn_t
    return None


def _shown_text(target_text: str, display: str) -> str:
    """What the reader sees once the link is swapped: the target's own words,
    unless the two are one name spelled two ways — then EB1911's spelling."""
    if _fold_name(display) == _fold_name(target_text):
        return display
    return target_text


def _description_text(raw: str | None) -> str:
    """A stored contributor description → display text.

    The ONE conversion for both export routes.  A description is a marker stream
    like any other field, so it goes through `markers_to_text` — the sole
    marker→text converter — rather than a private strip.  The bespoke
    `«BIOLINK:…«/BIOLINK»` regex this replaces lived on ONE of the two routes,
    which is why the roster read clean while the per-article payload (and the
    download bundle built from it) shipped the raw marker.
    """
    return " ".join(markers_to_text(raw or "").split()).rstrip(".")


def _resolve_bio_articles(session, contrib_map: dict[str, dict]) -> None:
    """Add bio_article_filename to contributors with biographical articles."""
    # Build title -> filename lookup from all articles in DB
    # Deterministic first-wins per title (heap order must not pick homonyms).
    all_articles = sorted(session.query(Article).all(), key=article_sort_key)
    title_map: dict[str, str] = {}
    for a in all_articles:
        title_map.setdefault(a.title.upper(), _safe_filename(a, a.title))

    for entry in contrib_map.values():
        desc_raw = entry.get("description") or ""
        desc = desc_raw.lower()
        if "biographical article" not in desc:
            continue

        # If the source description contained an explicit
        # ``{{EB1911 article link|display|target}}`` template, the walker
        # produced an ordinary ``«LN:target|display«/LN»`` — the SAME marker the
        # article body carries, from the SAME producer.  Use the display text as
        # the primary lookup key — it bypasses the surname-inversion path for
        # peerage cases like St. Cyres → Iddesleigh.
        #
        # This read a private `«BIOLINK»` until the fork was removed, and it read
        # the arguments the wrong way round: BIOLINK was emitted display-first and
        # parsed target-first.  The corpus settles the template's order — for
        # `{{…|Luchaire, Denis J. Achille|Luchaire, Denis Jean Achille}}` only the
        # SECOND is an article title — so `«LN»`'s target-first form makes the
        # names below correct rather than merely lucky.  (The bug was invisible
        # because the loop tries both candidates.)
        # Read through THE «LN» reader.  A display can carry nested markers
        # — Kropotkin's is `«SC»Kropotkin«/SC», Prince P. A.` — and any
        # `([^«]*)«/LN»` stops dead at the first one, which is exactly how the
        # BIOLINK regex this replaces went blind.  The display is read out
        # through the ONE converter so nested markers become text, not residue.
        bio_m = next(iter_ln_markers(desc_raw), None)
        if bio_m is not None:
            link_target = bio_m.target.strip()
            link_display = (
                " ".join(markers_to_text(bio_m.display).split()).strip()
                or link_target
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

    # (The BIOLINK strip that used to sit here is gone: descriptions are converted
    # by `_description_text` at emit, on BOTH routes.  A strip here reached only
    # the roster.)


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
# A collision LOSER is preferentially re-slugged on its ACCENT-FOLD (BOGÓ ->
# "bogo" -> a real, forwarder-routable hash) rather than an opaque, un-routable
# "-N" counter; the numeric suffix is only the fallback when the fold ALSO
# collides.  Keyed by article id; consulted by stable_id ahead of base+suffix.
_STABLE_ID_OVERRIDE: dict[int, str] = {}


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


def _folded_base_stable_id(article) -> str:
    """A collision loser's id hashed on the ACCENT-FOLDED slug source (NFKD:
    'Bogó' -> 'bogo'), so it stays a real {vv}-{pppp}-{hash6} the forwarder can
    recompute — not an opaque, un-routable -N counter."""
    import hashlib
    import unicodedata
    raw = article.section_name or ""
    slug = _section_slug(unicodedata.normalize("NFKD", raw)) if raw else ""
    slug = slug or _section_slug(unicodedata.normalize("NFKD", article.title or ""))
    h = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:6]
    return f"{article.volume:02d}-{article.page_start:04d}-{h}"


def article_sort_key(article) -> tuple:
    """Total, content-derived order for ANY article collection whose consumer
    picks or tie-breaks by position (homonym resolution, plate binding, title
    maps, the export loop that writes index.json).  DB heap order changes with
    every parallel rebuild and row ids are sequence-assigned per rebuild —
    neither may leak into output (the 2026-07-24 rebuild adjudication caught
    xref targets, plate parents and bylines flipping between identical-code
    rebuilds).  The section slug is the stable_id's own hash source, so it
    separates same-titled same-page homonyms the way their URLs do."""
    return (article.volume, article.page_start, article.page_end or 0,
            article.title or "", _section_slug_for(article))


def register_stable_id_dedup(articles) -> int:
    """Make every article's stable_id UNIQUE across collisions.  A collision
    LOSER is re-slugged on its accent-fold (a real, forwarder-routable hash);
    only if that fold ALSO collides does it fall back to a numeric -N suffix.
    Call once over the FULL corpus before any stable_id / filename / «LN» baking.
    Returns how many articles were re-slugged."""
    from collections import defaultdict as _dd
    _STABLE_ID_SUFFIX.clear()
    _STABLE_ID_OVERRIDE.clear()
    by_base: dict[str, list] = _dd(list)
    for a in articles:
        by_base[_base_stable_id(a)].append(a)
    used = set(by_base)                       # every base id already in use
    n = 0
    for _base, arts in by_base.items():
        if len(arts) <= 1:
            continue
        counter = 2
        # Deterministic on CONTENT: (title, slug) — ids are per-rebuild
        # sequence values and must not decide who keeps the bare id.
        for a in sorted(arts, key=lambda x: ((x.title or ""),
                                             _section_slug_for(x), x.id))[1:]:
            alt = _folded_base_stable_id(a)
            if alt != _base and alt not in used:
                _STABLE_ID_OVERRIDE[a.id] = alt   # accent-fold: forwarder-routable
                used.add(alt)
            else:
                _STABLE_ID_SUFFIX[a.id] = f"-{counter}"
                counter += 1
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
    aid = getattr(article, "id", None)
    if aid in _STABLE_ID_OVERRIDE:            # collision loser re-slugged on its fold
        return _STABLE_ID_OVERRIDE[aid]
    return _base_stable_id(article) + _STABLE_ID_SUFFIX.get(aid, "")


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


def _xrefs_from_body(body, article_id, resolver, fn_to_id=None, self_fn=None):
    """The candidate-source half of the xref decorator: extract every
    reference from the body and resolve it through the shared ``LinkResolver``
    (fill + prose-fish — docs/xref_resolution_strategy.md), returning transient
    ``Xref`` values.  No DB read.

    ``resolver`` resolves in filename space; ``fn_to_id`` maps its picks back
    to DB ids and ``self_fn`` is THIS article's filename (the see-tier topic
    filter + the same-article `#section` form key on it).  Only the post-export
    resolve phase (6b5) constructs the resolver; ``resolver is None`` — a
    single-article look-render or a deferring export — means nothing to resolve
    against, so return no xrefs; the body's «LN» markers then strip to their
    display text downstream."""
    if resolver is None:
        return []
    from britannica.xrefs.extractor import extract_xrefs
    from britannica.xrefs.reference import Xref
    from britannica.link_resolver import prose_window
    fn_to_id = fn_to_id or {}
    xrefs = []
    for m in extract_xrefs(body):
        xr = Xref(
            article_id=article_id,
            surface_text=m["surface_text"],
            normalized_target=m["normalized_target"],
            xref_type=m["xref_type"],
        )
        trusted = m["xref_type"] in ("link", "qv")
        # The extractor already read the marker's display — no second parse of
        # the surface text.  Strip nested inline markers from it: the resolver
        # wants the plain name (`r. v. h.`), not `«SC»r. v. h.«/SC»`, whose
        # stray tokens would mistokenize as name parts.
        display = strip_marker_tokens(m.get("display", ""), "").strip() or None
        ruled = resolver.adjudicated(m["normalized_target"])
        if ruled is not None:
            # A hand ruling wins over every tier — but the self-reference rule
            # still applies on top of it (Absalom and Achitophel -> DRYDEN, JOHN
            # is a self-link inside the Dryden article, and must not render).
            fn = ruled or None
            section = None
            if fn == self_fn:
                fn = None
        elif m["xref_type"] == "author":
            # A PERSON, not an article title — its own tier.
            fn = resolver.resolve_person(
                m["normalized_target"], display, self_fn=self_fn,
                prose=prose_window(body, m["surface_text"]))
            section = None
        else:
            fn, section, cut = resolver.resolve_xref(
                m["normalized_target"], display,
                prose=prose_window(body, m["surface_text"]) if trusted else "",
                self_fn=self_fn, trusted=trusted,
                embedded=m["xref_type"] == "link",
                window=m.get("window", False))
            # The name-cut that bound — the bake shortens a window-stamp's
            # display to exactly these words (a full-length cut is a no-op).
            xr.matched_cut = cut
        xr.target_article_id = fn_to_id.get(fn) if fn else None
        xr.target_section = section
        xr.status = ("resolved" if xr.target_article_id is not None
                     else "unresolved")
        xrefs.append(xr)
    return xrefs


def xref_panel_entries(xrefs, session):
    """Panel rows for a set of resolved xrefs: normalized_target + status +,
    for a resolved one, OUR canonical target_filename/target_title (the panel is
    OUR index — it shows DESCARTES, RENÉ, not the source's phrasing).  One owner,
    shared by the in-export path and the post-export resolve phase (Phase F)."""
    out = []
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
                entry["target_filename"] = _safe_filename(target, target.title)
                entry["target_title"] = target.title
        out.append(entry)
    return out


def _link_xrefs_in_body(body, xrefs, self_stable_id, session,
                        global_title_to_filename):
    """The body-linking half of the xref decorator, lifted out of
    export_articles_to_json: resolve the 2-part «LN»/«EB9» PRODUCER markers
    to 3-part filename links FIRST, then wrap resolved qv/see prose in place.
    Order matters — see the comment on the resolve pass below."""
    # normalized_target → (filename, matched_cut) — the cut rides along so a
    # window-stamp's bake can shorten the display to the words that bound.
    link_targets: dict[str, tuple] = {}
    for xref in xrefs:
        if xref.target_article_id is not None and xref.normalized_target:
            target = session.get(Article, xref.target_article_id)
            if target:
                link_targets[xref.normalized_target.lower()] = (
                    _safe_filename(target, target.title),
                    getattr(xref, "matched_cut", None),
                )

    # Resolve the 2-part producer markers («LN:target|display», «EB9:…») BEFORE any
    # 3-part «LN:filename|target|display» exists.  `_resolve_link`'s 2-part regex would
    # otherwise re-match a freshly-written 3-part marker — capturing `target|display` as
    # its display group ([^«]* spans the inner `|`), missing the filename lookup, and
    # stripping to that — leaking the pipe (`PLATE|Plate`).  So: producer «LN» first,
    # then «EB9», then the prose wraps LAST; every 3-part marker is created strictly
    # after `_resolve_link` has finished.  (A regex guard forbidding `|` in the display
    # can't work — a legit 2-part display can hold a `|` from an inline `{{IMG:…|…}}`.)
    def _cut_span(display: str, cut: str):
        """Char span of the bound cut inside the stamped window — token-wise,
        case-folded, punctuation-tolerant (the cut is the NORMALIZED form).
        None when the tokens can't be located."""
        dtoks = list(re.finditer(r"\S+", display))
        cw = [f for f in (_fold_name(w) for w in cut.split()) if f]
        dw = [_fold_name(t.group(0)) for t in dtoks]
        k = len(cw)
        if not cw or k > len(dw):
            return None
        for i in range(len(dw) - k + 1):
            if dw[i:i + k] == cw:
                return dtoks[i].start(), dtoks[i + k - 1].end()
        return None

    _WINDOW_KINDS = ("w", "see", "see_also")

    def _resolve_link(kind, target_text, display) -> str:
        # Normalize before the lookup so a `#section` target collapses to the same
        # `ARTICLE: SECTION` key extract_xrefs stored in link_targets — a `#`-bearing
        # target would otherwise miss (the key is normalized, the raw target isn't).
        if kind not in _WINDOW_KINDS:
            swap = swapped_link(target_text, display, global_title_to_filename)
            if swap:
                return ln_marker(*swap)

        hit = link_targets.get(normalize_xref_target(target_text).lower())
        if hit:
            fn, cut = hit
            if kind in _WINDOW_KINDS and cut:
                # A window-stamp: the resolver bound a CUT of the window (a
                # suffix for (q.v.), any span for see) — link exactly those
                # words; the unchosen words return to prose.
                span = _cut_span(display, cut)
                if span:
                    a, b = span
                    return (display[:a]
                            + ln_marker(cut, display[a:b], fn)
                            + display[b:])
                return ln_marker(cut, display, fn)
            return ln_marker(target_text, display, fn)   # internal — the link target
        if kind in _WINDOW_KINDS:
            # An unresolved window-stamp strips WHOLE, never the WS fallback:
            # the window is OUR guess at a reference, not the author's page
            # name — an external link on it would assert something nobody said.
            return display
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
    # The `[kind]` slot is consumed HERE — the bake resolves and writes the
    # plain 3-part form (or strips), so no `[kind]` survives into a post-bake
    # body.  `w` = a producer-stamped (q.v.) window (unasserted extent).
    #
    # Found by SCAN, not by a span pattern.  The display slot is the RECURSED
    # one, so it can legitimately carry markers — `{{1911link|«I»organi
    # trum«/I»|Organi trum}}` prints its cross-reference in italics — and a
    # `([^«]*)` display simply fails to match those, leaving the link unresolved
    # and the marker to leak.  Scanning also retires the ordering hazard the
    # pattern needed guarding against: fields are counted, so a 3-part marker
    # cannot be re-read as a 2-part one with `target|display` as its display.
    body = _resolve_ln_markers(body, _resolve_link)

    def _resolve_eb9(m: re.Match) -> str:
        target_text, display = m.group(1), m.group(2)
        fn = global_title_to_filename.get(target_text)
        if fn:
            return ln_marker(target_text, display, fn)
        return display
    body = re.sub(
        r"«EB9:([^|]*)\|([^«]*)«/EB9»",
        _resolve_eb9, body,
    )

    # «AL» (a surviving [[Author:…]] reference) bakes exactly like a link once the
    # PERSON tier has resolved it; an unresolved one strips to its display text —
    # the person has no EB article, which is the common case.  Runs AFTER
    # `_resolve_link` for the same reason `_resolve_eb9` does: it writes 3-part
    # markers that the 2-part regex above must never re-scan.
    def _resolve_author(m: re.Match) -> str:
        target_text, display = m.group(1), m.group(2)
        hit = link_targets.get(normalize_xref_target(target_text).lower())
        if hit:
            return ln_marker(target_text, display, hit[0])
        return display
    # DOTALL + non-greedy display: an author signature's display carries nested
    # markers (`«SC»r. v. h.«/SC»`), so a `[^«]*` display slot stops at the first
    # nested `«` and leaves the «AL» unbaked — it then leaks through render,
    # which has no «AL» open/close substitution (unlike «LN»).  Mirrors 6b4's
    # `_AL_RE`, the shape that already spans these.
    body = re.sub(
        r"«AL:([^|»]*)\|(.*?)«/AL»",
        _resolve_author, body, flags=re.DOTALL,
    )

    # (The prose-scan wrap that ran LAST here is DELETED — J7 slice 3: every
    # qv/see reference is producer-stamped at its site, so its marker is baked
    # by `_resolve_link` above like any link; there is no prose left to re-find.
    # `xrefs`/`self_stable_id`/`session` feed `link_targets` at the top.)
    return body



def printed_page_keys(session, article) -> list[dict]:
    """The article's page keys — ``[{page, offset, sig}]``, ordinary article data.

    Each key carries its own SIGNATURE: the opening letters of that page as they
    will render.  Computing it here, once, where the raw body already is, is what
    keeps the raw body out of the render payload — and that matters, because a
    render input has to be handed to whoever calls `render_article`, and one of
    those is the xref resolver, which has no business knowing anything about
    pages.  With the signature travelling in the key, the payload is self-
    sufficient and every caller just passes a dict along, as it already does for
    `sections` and `xrefs`.

    The ws→printed rewrite used to run over the body's page tokens.  The body has
    none now, so the mapping lives on the KEYS — page numbers are handled here and
    at injection, nowhere else ([[project_page_position_out_of_band]]).

    The drop rule is unchanged and still deliberate: a ws page with no entry in
    printed_pages.json is a plate, with no printed number, so it gets NO key and
    therefore no marker — rather than walking back to the previous page and
    printing "p. 980" twice with plate content in between.

    Shared by the direct export and the DEFERRED render in
    ``tools/pipeline/resolve_xrefs_post.py``, which renders from the JSON on disk
    and so must rebuild these from the database rather than read them back.
    """
    from britannica.render.page_markers import signature
    pp = _get_printed_pages().get(str(article.volume), {})
    body = article.body or ""
    rows = (session.query(ArticleSegment.offset, SourcePage.page_number)
            .join(SourcePage, SourcePage.id == ArticleSegment.source_page_id)
            .filter(ArticleSegment.article_id == article.id)
            .order_by(ArticleSegment.sequence_in_article).all())
    keys: list[dict] = []
    for i, (off, ws) in enumerate(sorted(rows, key=lambda r: r[0])):
        direct = pp.get(str(ws))
        if direct is None:
            continue
        keys.append({
            "page": int(direct),
            "offset": int(off),
            # The FIRST key is the page the article opens on — placed at the
            # body's start structurally, so it needs no signature to be found.
            "sig": "" if i == 0 else signature(body, int(off), article.volume),
        })
    return keys


def export_articles_to_json(
    volume: int,
    out_dir: str | Path,
    body_override: dict[int, str] | None = None,
    only_article_id: int | None = None,
    link_index=None,
    xref_sink: list | None = None,
    defer_xrefs: bool = False,
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
        # article_sort_key, not the SQL ORDER BY alone: same-titled same-page
        # homonyms tie on every SQL key and would fall to heap order — which
        # this loop then bakes into index.json (and every LinkResolver
        # candidate list downstream inherits it).
        articles = sorted(
            session.query(Article).filter(Article.volume == volume).all(),
            key=article_sort_key,
        )

        # Global title → filename map for cross-volume soft-link
        # resolution (e.g. {{EB9link|Atom}} on a vol-17 article wants
        # to link to ATOM in vol 2).  Built once per export run;
        # deterministic first-wins (earliest article by content order).
        global_title_to_filename = build_title_index(session.query(Article).all())

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
            # A plate IS a single page, so its source wikitext is that page's —
            # reached from the plate's own page number rather than by hopping
            # through a segment row ([[project_page_position_out_of_band]]).
            pg = (session.query(SourcePage)
                  .filter(SourcePage.volume == plate.volume,
                          SourcePage.page_number == plate.page_start)
                  .first())
            wt = (pg.wikitext or "") if pg else ""
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

        for article in articles:
            if (only_article_id is not None
                    and article.id != only_article_id):
                continue
            # defer_xrefs (Phase-F): the whole xref+render tail moves to the
            # post-export resolve phase (6b4), which can see the kind index.
            # Here the body is written with its raw producer markers.
            xrefs = ([] if defer_xrefs else _xrefs_from_body(
                _body_for(article), article.id, link_index))

            xref_list = xref_panel_entries(xrefs, session)

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
            body = (_body_for(article) if defer_xrefs else _link_xrefs_in_body(
                _body_for(article), xrefs, stable_id(article), session,
                global_title_to_filename,
            ))

            # Convert PAGE markers from Wikisource to printed page numbers.
            # A ws page with no direct entry in printed_pages.json is
            # a plate — no printed number — so we drop its marker
            # entirely rather than walking back to the previous page
            # (which creates misleading duplicates like "p. 980" twice
            # with plate content between them).

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
                # Page keys — ordinary article data, like `sections`.  Each
                # carries its own signature, so `render_article` needs nothing
                # but this payload and no caller has to hand it a render input.
                "page_keys": printed_page_keys(session, article),
                # Raw body LENGTH (not the body): lets the injector turn a key's
                # offset into a proportional prior without carrying the text.
                "body_span": len(article.body or ""),
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
                # EMPTY by contract — Phase 6b4 (`resolve_contributors_post`) is the
                # sole writer of this field, and it patches all 37,226 articles.  This
                # used to populate it with a per-article ContributorInitials query
                # whose result was overwritten in every case: the resolver
                # consolidation moved binding out of the export and left the dead
                # population behind.  It cost 37,226 needless queries and, worse,
                # presented as a second writer — the description conversion was added
                # HERE and never reached the shipped payload, taking contributor_bio
                # leaks from 1,363 to 6,588 ([[feedback_dont_grow_catchalls]]).
                "contributors": [],
            }

            # The viewer is a thin shell now: Python owns marker→HTML, the client just
            # inserts this and hydrates math / rebuilds the runtime scan href.  Links bake
            # as clean URLs (/article/{id}) — one form, local and production; scans render
            # as a bare `scans.html` anchor that fixScanHrefs rebuilds at load (the back
            # param is location.href — runtime-only, never bakeable).
            # Rendered HTML is a pure function of the RESOLVED payload (decorated
            # body + panel), so under defer_xrefs the render moves to 6b4 too --
            # rendered exactly once, after resolution.
            if not defer_xrefs:
                payload["rendered_html"] = render_article(payload, target="site")

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

        # The contributor ROSTER is NOT built here.  Phase 6b4
        # (`resolve_contributors_post`) rebuilds `contributors.json` from the
        # final DB state — it resolves the bio articles and writes the file — so
        # everything that used to stand here was overwritten wholesale on every
        # run: a per-article Contributor/ContributorInitials query, a merge with
        # the previous volume's file, a second `_resolve_bio_articles`, and a
        # second description conversion.  Two writers of one output is how the
        # conversion came to be applied on the dead path only.

        return exported

    finally:
        session.close()