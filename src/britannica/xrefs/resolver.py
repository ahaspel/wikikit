"""Exact + fuzzy xref resolvers, collision-aware — and THE one resolution
index + cascade every caller routes through.

When two or more articles share the same title (580 such titles in
the corpus — ZÜRICH canton vs ZÜRICH city, ABBAS I shah vs pasha,
ABERDEEN Scotland vs South Dakota, …), picking the first one we find
misroutes every xref to one of the candidates arbitrarily.  This
module's `disambiguate_among` applies three rules in order:

  1. Self-reference filter.  If the linking article is itself a
     candidate, drop it.  Handles the common case where article A
     mentions its same-named sibling A′ (canton linking to city).

  2. Display disambiguator.  Link wikitext like
     `{{1911link|Zürich|Zürich (city)}}` carries `(city)` in the
     display text.  If exactly one remaining candidate's body
     opening matches the disambiguator (literal or via the small
     synonym set), pick it.  Heuristic by design — baseline is
     random dict-last-write silent-pick, so 60%+ right is a win.

  3. Fallback.  Return the first remaining candidate.  Preserves
     current silent-pick behavior for truly ambiguous cases so no
     existing (lucky) link regresses.

The kind vocabulary and the body-opening matcher live in
`britannica.xrefs.disambiguation` — one owner, shared with the vol29
classified-TOC resolver, which feeds the same matcher a wanted-kind
read off the index structure (bucket / category) instead of a display
parenthetical.

`build_index` / `resolve` (relocated here from
pipeline/stages/resolve_xrefs.py, which now re-exports them) are the single
corpus-wide resolver — pipeline, single-pass export, vol29 topic, reader's
guide, and the contributor linkers all route through them.  Domain differences
become parameters, not forks.  [[project_resolver_consolidation]]
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from britannica.db.models import Article, CrossReference
from britannica.xrefs.alias_table import (
    build_alias_map,
    build_section_alias_map,
    build_vol29_index_aliases,
)
from britannica.xrefs.disambiguation import (
    body_opening, hint_kind, matches_disambiguator, pick_by_kind,
)
from britannica.xrefs.normalizer import normalize_xref_target
from britannica.xrefs.scoring import find_fuzzy_match


_LN_DISPLAY_RE = re.compile(r"«LN:[^|]*\|([^«]*)«/LN»")
_PAREN_DISAMBIG_RE = re.compile(r"\(([^)]+)\)")


def _display_disambiguator(xref: CrossReference) -> str | None:
    """The parenthesized disambiguator on EITHER side of the marker -- the
    source writes it in the target (`[[Zürich (city)|Zürich]]`, ~85% of them) OR
    the display -- so scan the whole surface plus the normalized target.  Returns
    the parenthetical text lowercased, or None for a bare date/numeral."""
    for text in (xref.surface_text or "", xref.normalized_target or ""):
        for dm in _PAREN_DISAMBIG_RE.finditer(text):
            word = dm.group(1).strip().lower()
            if word and re.search(r"[a-z]", word) and not re.fullmatch(
                    r"q\.?\s*v\.?", word):
                return word
    return None


def disambiguate_among(
    xref: CrossReference, candidates: list[Article], body_of=None, kinds_of=None
) -> int | None:
    """Apply self-reference + display-disambiguator + fallback rules
    to pick one candidate's id.  Returns None only when the candidate
    list is empty (callers should handle that upstream)."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].id

    # Rule 1: drop the linking article if it's in the set.
    remaining = [c for c in candidates if c.id != xref.article_id]
    if not remaining:
        # All candidates are the linking article (shouldn't happen,
        # but guard against empty result).
        remaining = candidates
    if len(remaining) == 1:
        return remaining[0].id

    # Rule 2: kind disambiguator (from either marker side).
    disambiguator = _display_disambiguator(xref)
    if disambiguator:
        def _open(c):
            return body_opening(body_of(c) if body_of else (c.body or ""))
        # 2a: the sharp path -- map the hint to a wanted-kind and let pick_by_kind
        # settle it by the candidates' own leads (Zürich (city) -> the town-lead
        # article, never mis-firing on the canton's "capital").
        want = hint_kind(disambiguator)
        if want:
            pick = pick_by_kind([(c, c.title) for c in remaining], want, _open,
                                kinds_of=kinds_of)
            if pick is not None:
                return pick.id
        # 2b: legacy whole-opening word-grep, kept as a fallback until the
        # post-export kind index subsumes it (step C after F).
        matched = [c for c in remaining
                   if matches_disambiguator(disambiguator, _open(c))]
        if len(matched) == 1:
            return matched[0].id

    # Rule 3: salience fallback.  A bare collision (or one whose hint didn't
    # resolve) has no kind signal, so pick the most PROMINENT article -- the
    # longest body, the main subject over a same-named stub -- instead of
    # dict-order first-wins.  `max` keeps the first candidate on a length tie, so
    # it stays deterministic and degrades to the old first-wins when bodies match.
    return max(
        remaining,
        key=lambda c: len(body_of(c) if body_of else (c.body or "")),
    ).id


def resolve_xref_fuzzy(
    xref: CrossReference, title_map: dict[str, int], aggressive: bool = False
) -> int | None:
    target = normalize_xref_target(xref.normalized_target or "")  # 1b: canonical (was .strip().upper())
    return find_fuzzy_match(target, title_map, aggressive=aggressive)


# ---------------------------------------------------------------------------
# The one resolution index + cascade.  Relocated (byte-identical logic) from
# pipeline/stages/resolve_xrefs.py, which now re-exports these under their
# historical names.  Parameterization + folding in the other resolvers happens
# in later steps of [[project_resolver_consolidation]].
# ---------------------------------------------------------------------------

_DISAMBIG_CACHE_FILE = Path("data/derived/xref_disambiguation_cache.json")
_PERSON_VARIANT_THRESHOLD = 3

# ``BIBLE (KING JAMES)/EXODUS: 4:27`` style scripture references.
# The target carries the structural form
# ``BIBLE...../<BOOK_NAME>[#chapter:verse | : chapter:verse]``;
# the right article is the EB1911 entry for that book.  Most books
# follow predictable title shapes (``EXODUS, BOOK OF`` / ``EXODUS,
# THE`` / ``DEUTERONOMY``) so this is a pure rule, no LLM needed.
_BIBLE_REF_RE = re.compile(
    r"^BIBLE\b.*?/([A-Z][A-Z\s]+?)(?:\s*[:#]|$)",
    re.IGNORECASE,
)


def _try_bible_handler(
    target: str, title_to_articles: dict[str, list[Article]]
) -> int | None:
    """Resolve ``BIBLE (KING JAMES)/EXODUS: 4:27`` form to the article
    about that book, by trying common title variants in order.

    Returns the resolved article id, or None if the book name doesn't
    correspond to any title variant we recognise.
    """
    m = _BIBLE_REF_RE.match(target)
    if not m:
        return None
    book = m.group(1).strip().upper()
    for variant in (book, f"{book}, BOOK OF", f"{book}, THE"):
        candidates = title_to_articles.get(variant)
        if candidates:
            return candidates[0].id
    return None


def _build_ambiguity_state(all_articles: list[Article]) -> tuple[
    set[str], dict[str, int], dict
]:
    """Return ``(ambiguous_titles, stable_id_to_article_id, cache)``.

    Ambiguous title := exists as standalone article AND has 3+
    ``TITLE, FirstName`` person-variant articles.  References to bare
    ``SMITH`` then go through the LLM disambiguation cache rather than
    auto-linking to the generic surname article.

    The cache file (``data/derived/xref_disambiguation_cache.json``)
    is populated by ``tools/xrefs/disambiguate_xrefs.py``.  When it
    exists, ambiguous candidates resolve to the cached choice;
    otherwise they're marked ``status='ambiguous'`` and the
    disambiguator picks them up on the next run.
    """
    # Lazy import: article_json imports `resolve` from here (via the
    # resolve_xrefs re-export) inside a function, so importing stable_id at
    # module load would close the cycle.  Deferring keeps load acyclic.
    from britannica.export.article_json import stable_id

    person_variants: dict[str, int] = defaultdict(int)
    standalone_titles: set[str] = set()
    for a in all_articles:
        if a.article_type != "article" or not a.title:
            continue
        n = normalize_xref_target(a.title)
        if not n:
            continue
        if "," in n:
            surname = n.partition(",")[0].strip()
            if surname:
                person_variants[surname] += 1
        else:
            standalone_titles.add(n)

    ambiguous_titles = {
        surname for surname, count in person_variants.items()
        if surname in standalone_titles
        and count >= _PERSON_VARIANT_THRESHOLD
    }

    stable_id_to_article_id: dict[str, int] = {}
    for a in all_articles:
        if a.article_type != "article":
            continue
        try:
            stable_id_to_article_id[stable_id(a)] = a.id
        except Exception:
            continue

    cache: dict = {}
    if _DISAMBIG_CACHE_FILE.exists():
        try:
            cache = json.loads(
                _DISAMBIG_CACHE_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            cache = {}

    return ambiguous_titles, stable_id_to_article_id, cache


def _try_disambig_cache(
    xref: CrossReference,
    source_article: Article,
    cache: dict,
    stable_id_to_article_id: dict[str, int],
) -> int | None:
    """Look up an ambiguous xref in the disambiguation cache.

    Returns the chosen ``target_article_id`` if the cache has an
    entry with a non-null ``chosen`` whose stable_id maps to a
    current article.  Returns ``None`` for cache miss OR for cached
    null (LLM said no candidate fits).  The caller distinguishes
    those cases via the cache key membership check.
    """
    from britannica.export.article_json import stable_id  # lazy: see above

    sid = stable_id(source_article)
    key = f"{sid}::{xref.surface_text}::{xref.normalized_target}"
    entry = cache.get(key)
    if not entry:
        return None
    chosen = entry.get("chosen")
    if not chosen:
        return None
    return stable_id_to_article_id.get(chosen)


class ResolutionIndex:
    """Everything ``resolve`` needs, built once over the corpus.

    Lifted out of the old ``resolve_xrefs_all`` so the same resolution can run
    in-memory inside the single-pass export (which has no stored
    CrossReference to read) — `[[feedback_export_owns_assembly]]`.
    """

    __slots__ = (
        "title_to_articles", "section_lookup", "title_map",
        "ambiguous_titles", "stable_id_to_article_id", "disambig_cache",
        "articles_by_id", "body_of", "kinds_of",
    )

    def __init__(self, title_to_articles, section_lookup, title_map,
                 ambiguous_titles, stable_id_to_article_id, disambig_cache,
                 articles_by_id, body_of, kinds_of=None):
        self.title_to_articles = title_to_articles
        self.section_lookup = section_lookup
        self.title_map = title_map
        self.ambiguous_titles = ambiguous_titles
        self.stable_id_to_article_id = stable_id_to_article_id
        self.disambig_cache = disambig_cache
        self.articles_by_id = articles_by_id
        self.body_of = body_of
        # candidate → its kinds from the post-export kind index (C-full); a
        # no-op returning ∅ when the index isn't built yet (in-DB resolve).
        self.kinds_of = kinds_of if kinds_of is not None else (
            lambda _c: frozenset())


def build_core_maps(items, *, value_of):
    """The shared core of every name→article index: a collision-keeping
    ``normalize_xref_target(title) → [candidate]`` map, plus a first-wins
    ``… → value_of(candidate)`` map for the fuzzy matcher.

    ``candidate`` is opaque — a DB ``Article`` (the xref/pipeline path) or an
    ``(filename, title)`` tuple (the tool-side topic / reader's-guide / linker
    paths that read the exported ``index.json``).  So every caller shares ONE
    keying + collision bucketing and differs only in what it hangs off each
    bucket (aliases/ambiguity vs tail-arts/section) and its domain cascade steps.
    """
    by_norm: dict[str, list] = {}
    tmap: dict = {}
    for title, cand in items:
        k = normalize_xref_target(title or "")
        if not k:
            continue
        by_norm.setdefault(k, []).append(cand)
        tmap.setdefault(k, value_of(cand))
    return by_norm, tmap


def build_index(all_articles: list[Article], corpus=None) -> ResolutionIndex:
    """Build the corpus-wide resolution maps once.

    Collision-aware ``title → list[Article]`` (via the shared ``build_core_maps``)
    plus alias / section / vol29 overlays, a plain ``title → id`` map for fuzzy,
    the ambiguity state + LLM disambiguation cache, and an id→article lookup.
    """
    # Collision-aware canonical map, via the shared core builder: title → [Article].
    title_to_articles, _ = build_core_maps(
        ((a.title, a) for a in all_articles if a.article_type != "plate"),
        value_of=lambda a: a.id,
    )

    # Aliases: don't overwrite canonical titles; inherit candidate lists so
    # disambiguation applies uniformly.  The alias tables are keyed raw .upper(),
    # so normalize them on the way in to share the canonical key space (1b).
    alias_map = build_alias_map()
    for alias, canonical in alias_map.items():
        a_n, c_n = normalize_xref_target(alias), normalize_xref_target(canonical)
        if a_n not in title_to_articles and c_n in title_to_articles:
            title_to_articles[a_n] = title_to_articles[c_n]

    section_map = build_section_alias_map()
    section_lookup: dict[str, str] = {}
    for alias, canonical in section_map.items():
        a_n, c_n = normalize_xref_target(alias), normalize_xref_target(canonical)
        if a_n not in title_to_articles and c_n in title_to_articles:
            title_to_articles[a_n] = title_to_articles[c_n]
            section_lookup[a_n] = a_n

    vol29_map = build_vol29_index_aliases()
    for alias, canonical in vol29_map.items():
        a_n, c_n = normalize_xref_target(alias), normalize_xref_target(canonical)
        if a_n not in title_to_articles and c_n in title_to_articles:
            title_to_articles[a_n] = title_to_articles[c_n]

    # Fuzzy needs a plain title→id map (first candidate for collisions).
    title_map: dict[str, int] = {
        k: v[0].id for k, v in title_to_articles.items()
    }

    ambiguous_titles, stable_id_to_article_id, disambig_cache = (
        _build_ambiguity_state(all_articles)
    )
    articles_by_id = {a.id: a for a in all_articles}

    # The collider tie-break reads a candidate's body opening.  Source it
    # from the in-memory corpus when supplied — so article.body is never
    # read for the full-corpus build — falling back to the DB body only for
    # a candidate absent from a partial (per-volume) corpus.
    if corpus is not None:
        def body_of(c):
            return corpus[c.id] if c.id in corpus else (c.body or "")
    else:
        def body_of(c):
            return c.body or ""

    # C-full: the post-export kind index (filename → [kinds]).  Keyed on the
    # stable_ids already resolved for the ambiguity state, so no extra hashing.
    # Absent during the in-DB build (before Phase 6b3 writes it); present in the
    # Phase-6b4 post-export resolve, where it lets the disambiguator pick a
    # candidate by its topic bucket when its own opening's lead is silent or
    # misleading.  [[project_resolver_consolidation]]
    kinds_by_id: dict[int, frozenset] = {}
    kind_index_file = Path("data/derived/kind_index.json")
    if kind_index_file.exists():
        try:
            raw_kinds = json.loads(kind_index_file.read_text(encoding="utf-8"))
        except Exception:
            raw_kinds = {}
        for sid, aid in stable_id_to_article_id.items():
            ks = raw_kinds.get(f"{sid}.json")
            if ks:
                kinds_by_id[aid] = frozenset(ks)

    def kinds_of(c):
        return kinds_by_id.get(c.id, frozenset())

    return ResolutionIndex(
        title_to_articles, section_lookup, title_map,
        ambiguous_titles, stable_id_to_article_id, disambig_cache,
        articles_by_id, body_of, kinds_of,
    )


def resolve(
    xref: CrossReference, idx: ResolutionIndex, *, aggressive: bool = False
) -> tuple[int | None, str | None]:
    """The resolution ladder for one xref, with no DB writes.

    Returns ``(target_article_id, target_section)`` — both ``None`` when
    nothing resolves.

    ``aggressive`` is the per-caller precision knob
    ([[project_resolver_consolidation]]): False (default) for inline article
    xrefs — a wrong link is a visible error the reader clicks; True for the
    classified-TOC / recall callers — an occasional miss is fine and an
    OCR-repaired hit is a win.  It gates only the fuzzy OCR-edit-distance pass;
    exact/alias/section/collision picking are decisive either way.
    """
    target = normalize_xref_target(xref.normalized_target or "")  # 1b: canonical (was .strip().upper())
    target_article_id: int | None = None
    section: str | None = None

    # 0. Ambiguous-title routing via the LLM disambiguation cache.
    if target in idx.ambiguous_titles:
        source = idx.articles_by_id.get(xref.article_id)
        cached_target = (
            _try_disambig_cache(
                xref, source, idx.disambig_cache,
                idx.stable_id_to_article_id,
            )
            if source is not None else None
        )
        if cached_target is not None:
            return cached_target, None
        # else: cache miss → fall through to normal resolution.

    # 1. Exact title / alias / section-alias (collision-aware).
    candidates = idx.title_to_articles.get(target)
    if candidates:
        target_article_id = disambiguate_among(
            xref, candidates, idx.body_of, idx.kinds_of)
        if target in idx.section_lookup:
            section = idx.section_lookup[target]

    # 1b. Bible-reference handler.
    if target_article_id is None:
        target_article_id = _try_bible_handler(target, idx.title_to_articles)

    # 2. Section-suffix form: "EUROPE: HISTORY" -> (EUROPE, HISTORY).  A bare
    # ": HISTORY" (a same-article `[[#History]]` normalized, empty base) resolves to
    # THIS article, section History — the source the context-free producer couldn't
    # name; the resolver fills it from xref.article_id.
    if target_article_id is None and ": " in target:
        base, _, suffix = target.rpartition(": ")
        base = base.strip()
        suffix = suffix.strip()
        if not base and suffix:
            target_article_id = xref.article_id
            section = suffix
        else:
            base_candidates = idx.title_to_articles.get(base)
            if base_candidates and suffix:
                target_article_id = disambiguate_among(
                    xref, base_candidates, idx.body_of, idx.kinds_of)
                section = suffix

    # 3. Fuzzy matching (aggressive gates the OCR edit-distance pass).
    if target_article_id is None:
        target_article_id = resolve_xref_fuzzy(
            xref, idx.title_map, aggressive=aggressive)

    # 4. LLM-resolved unresolveds (last-resort).
    if target_article_id is None and idx.disambig_cache:
        source = idx.articles_by_id.get(xref.article_id)
        if source is not None:
            cached_target = _try_disambig_cache(
                xref, source, idx.disambig_cache,
                idx.stable_id_to_article_id,
            )
            if cached_target is not None:
                target_article_id = cached_target

    return target_article_id, section
