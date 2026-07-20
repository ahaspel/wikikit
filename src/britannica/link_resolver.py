"""The sole link resolver: a NAME → EB article.  Shared by the classified-TOC
topic population AND the inline-xref resolution (Phase 6b4) — one resolver, no
forks (docs/xref_resolution_strategy.md, [[project_resolver_consolidation]]).

It does three separable things, each its own method so a caller can use some or
all:
  1. ``resolve_section``  — section links (`§` / `#` / a bare section-title), an
     essentially separate pass;
  2. ``candidates``       — propose the tightest non-empty candidate bag (FILL);
  3. ``fish``             — pick one from a bag (the fisher).
``resolve`` orchestrates all three with the topic policy (kind gates, feature-kind
broadening); the recall primitives themselves live in ``NameIndex``, so these are
thin policies over shared primitives, not forks.

Callers differ only in the disambiguation context: topics pass ``want`` (a bucket
kind), ``ctx`` (bucket words) and ``path`` (the bucket trail); xrefs pass ``prose``
(the text around the reference) and leave ``want`` empty — the kind gates are then
inert (``want`` falsy → ``_kind_ok`` True, ``_BROADEN_KINDS`` never matches).

Lifted from ``populate_classified_toc.build_resolver``; ``resolve`` is behaviour-
preserving (topic path byte-identical), with ``prose`` threaded into the fisher.
"""
import json
import re
import unicodedata
from pathlib import Path

from britannica.export.sections import match_section
from britannica.xrefs.normalizer import normalize_xref_target
from britannica.xrefs.scoring import find_fuzzy_match
from britannica.xrefs.disambiguation import (
    body_opening, kind_qualifies, lead_kind, pick_by_kind)
from britannica.embeddings import LeadEmbeddings, build_cache
from britannica.topic_fisher import Fisher
from britannica.name_index import NameIndex, content, wordset_f

ARTICLES_INDEX = Path("data/derived/articles/index.json")
ARTS_DIR = ARTICLES_INDEX.parent
SECTION_INDEX = Path("data/derived/classified_section_index.json")
CLASSIFIED_TOC = Path("data/derived/classified_toc.json")

# The trusted-xref ladder runs the fill rungs in two tiers over BOTH the target
# and the display name: every full-containment rung on either name before any
# partial-match recovery rung — a tight display match beats a loose target one.
_XREF_TIGHT = ("exact", "alt", "fold", "subset", "superset")
_XREF_LOOSE = ("firstword", "fuzzy")

# A2 broadening (reach past the exact collision to a leading-token candidate) is
# safe ONLY where the bucket wants a PHYSICAL FEATURE filed as a variant of the
# bare name — Zürich → ZÜRICH, LAKE OF.  Bucket-only; inert for xrefs (no want).
_BROADEN_KINDS = {"lake", "river", "mountain", "island"}
# A peerage topic ('Bristol, 2nd earl of') must land on a biographical title, not
# the bare toponym; a person/tribe bucket never binds a clear place article.
_PEERAGE = re.compile(
    r",\s*(?:\d+(?:st|nd|rd|th)\s+)?(?:earl|baron|viscount|marquess|marquis|duke|"
    r"duchess|countess|count|comte|comtesse|lord|lady|baronet|prince|princess|"
    r"landgrave|margrave)\b", re.I)
_PICK_PLACE = {"town", "city", "division", "lake", "river", "mountain", "island"}
_WANT_PLACE = {"division", "town", "lake", "river", "mountain", "island"}
_OR_SPLIT = re.compile(r"\s+or\s+", re.I)
_PAREN_RE = re.compile(r"\s*\(([^)]*)\)")
_SECT_RE = re.compile(r"^(.*?)[\s,(]*§+\s*(.+?)[\s).]*$")
# ``BIBLE (KING JAMES)/EXODUS: 4:27`` — a scripture ref whose target is the EB1911
# entry for that book (``EXODUS`` / ``EXODUS, BOOK OF`` / ``EXODUS, THE``).
_BIBLE_REF_RE = re.compile(r"^BIBLE\b.*?/([A-Z][A-Z\s]+?)(?:\s*[:#]|$)", re.IGNORECASE)


def _art_norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return " ".join(s.split())


def load_section_index() -> dict[str, list]:
    """Unique section titles -> [filename, slug, article_title].  Cached;
    built once from the article files' `sections` when missing."""
    if SECTION_INDEX.exists():
        try:
            return json.loads(SECTION_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    from collections import defaultdict
    from britannica.export.corpus import load_corpus
    bucket: dict[str, set] = defaultdict(set)
    # Total load: a silently skipped article used to shrink this index for the
    # WHOLE run — fewer resolvable section links, no signal that it happened.
    payloads, _ = load_corpus(ARTS_DIR, require=())
    for fn, d in payloads.items():
        for s in d.get("sections") or []:
            if isinstance(s, dict) and (s.get("title") or "").strip():
                bucket[_art_norm(s["title"])].add(
                    (fn.name, s.get("slug", ""), d.get("title", "")))
    uniq = {k: list(next(iter(v))) for k, v in bucket.items() if len(v) == 1}
    try:
        SECTION_INDEX.write_text(json.dumps(uniq, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception:
        pass
    return uniq


def load_topic_map() -> dict[str, set]:
    """filename -> {top-level category names} off the classified TOC — the
    see-tier's source-topic filter.  Empty when the TOC isn't built yet (every
    see/cf reference then abstains, the untrusted tier's safe default)."""
    try:
        d = json.loads(CLASSIFIED_TOC.read_text(encoding="utf-8"))
    except Exception:
        return {}
    tm: dict[str, set] = {}

    def _walk(cat, node):
        for art in node.get("articles", []):
            fn = art.get("filename")
            if fn:
                tm.setdefault(fn, set()).add(cat)
        for ch in node.get("children", []):
            _walk(cat, ch)

    for cat in d.get("categories", []):
        for sub in cat.get("subsections", []):
            _walk(cat["name"], sub)
    return tm


def _clean_prose(t: str) -> str:
    """Marker stream -> plain prose: links to their display, markers/page
    stamps out, whitespace collapsed.  What the fisher should embed."""
    t = re.sub(r"«LN:(?:[^|]*\|)*([^«]*)«/LN»", r"\1", t)
    t = re.sub(r"«[^»]*»", "", t)
    t = re.sub(r"\x01PAGE:\d+\x01", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def prose_window(body: str, surface: str, radius: int = 140) -> str:
    """The prose around one reference — the xref path's disambiguation
    context (docs/xref_resolution_strategy.md: context = PROSE, not bucket)."""
    i = body.find(surface)
    if i < 0:
        return _clean_prose(surface)
    return _clean_prose(body[max(0, i - radius): i + len(surface) + radius])


def _topic_alternates(raw: str) -> list:
    """A name's own alternate spellings: 'Agaiambo or Agaumbu' -> both;
    'Afars (Danakil)' -> 'Afars' and 'Danakil'.  Recall only."""
    m = _PAREN_RE.search(raw)
    outs = [_PAREN_RE.sub("", raw).strip()]
    if m:
        outs.append(m.group(1).strip())
    for part in outs[:]:
        if _OR_SPLIT.search(part):
            outs.extend(p.strip() for p in _OR_SPLIT.split(part))
    seen, uniq = set(), []
    for o in outs:
        if o and o.lower() not in seen:
            seen.add(o.lower())
            uniq.append(o)
    return uniq


class LinkResolver:
    """Build once over the exported corpus; resolve many names."""

    def __init__(self, aliases: bool = False):
        article_index = json.loads(ARTICLES_INDEX.read_text(encoding="utf-8"))
        arts = [e for e in article_index if e.get("article_type") == "article"]
        # The shared FILL substrate — word-set / fold / subset / first-word / fuzzy.
        self.idx = NameIndex(arts)
        # Alias overlay (title aliases + section + vol29 index): opt-in, so the
        # topic path stays byte-identical.  The xref path enables it — the reach
        # the retired resolver.py cascade had, kept by the sole resolver.
        if aliases:
            self._overlay_aliases()
        self.title_by_fn = self.idx.title_by_fn
        self.section_index = load_section_index()
        # "RUSSIAN LITERATURE" keyed by last word -> [(prefix, fn, title)], so an
        # "X § Literature" ref can find a DEDICATED "{demonym} Literature" article.
        self.tail_arts: dict[str, list] = {}
        for e in article_index:
            if e.get("article_type") != "article":
                continue
            parts = e["title"].split()
            if len(parts) >= 2:
                self.tail_arts.setdefault(_art_norm(parts[-1]), []).append(
                    (_art_norm(" ".join(parts[:-1])), e["filename"], e["title"]))
        self._sec_cache: dict[str, list] = {}
        self._open_cache: dict[str, str] = {}
        self._topic_map: dict[str, set] | None = None    # lazy: see-tier only
        # Embeddings for the fisher's semantic rung (built once, cached; regenerable).
        try:
            _emb = LeadEmbeddings.load()
        except Exception:
            build_cache()
            _emb = LeadEmbeddings.load()
        self.fisher = Fisher(_emb, self._opening)

    def _overlay_aliases(self):
        """Merge the alias / section-alias / vol29-index-alias maps: each alias
        inherits its canonical title's article (recall only)."""
        from britannica.xrefs.alias_table import (
            build_alias_map, build_section_alias_map, build_vol29_index_aliases)
        merged: dict[str, str] = {}
        for m in (build_alias_map(), build_section_alias_map(),
                  build_vol29_index_aliases()):
            merged.update(m)
        for alias, canonical in merged.items():
            cands = self.idx.by_norm.get(normalize_xref_target(canonical))
            if cands:
                self.idx.add_alias(alias, cands[0][0])

    # -- small readers -------------------------------------------------------
    def _article_sections(self, fn: str) -> list:
        if fn not in self._sec_cache:
            try:
                d = json.loads((ARTS_DIR / fn).read_text(encoding="utf-8"))
                self._sec_cache[fn] = [s for s in d.get("sections") or []
                                       if isinstance(s, dict)]
            except Exception:
                self._sec_cache[fn] = []
        return self._sec_cache[fn]

    def _opening(self, fn: str) -> str:
        if fn not in self._open_cache:
            try:
                d = json.loads((ARTS_DIR / fn).read_text(encoding="utf-8"))
                self._open_cache[fn] = body_opening(d.get("body", ""))
            except Exception:
                self._open_cache[fn] = ""
        return self._open_cache[fn]

    def _resolve_plain(self, raw: str):
        """name -> (filename, title): exact title, then find_fuzzy_match.  None
        for an unresolved name OR an undisambiguated collision."""
        n = normalize_xref_target(raw)
        cands = self.idx.by_norm.get(n)
        if cands:
            return cands[0] if len(cands) == 1 else None
        fn = self.idx.fuzzy(raw)
        return (fn, self.title_by_fn[fn]) if fn else None

    def _res(self, fn, disambig=None):
        title = self.title_by_fn[fn]
        d = {"target": title, "display": title, "filename": fn}
        if disambig:
            d["disambig"] = disambig
        return d

    # -- folded-in xref target forms (Bible ref, colon-suffix) --------------
    def _try_bible(self, name: str):
        """`BIBLE (KING JAMES)/EXODUS: …` -> the EB entry for that book."""
        m = _BIBLE_REF_RE.match(normalize_xref_target(name))
        if not m:
            return None
        book = m.group(1).strip().upper()
        for variant in (book, f"{book}, BOOK OF", f"{book}, THE"):
            cands = self.idx.by_norm.get(normalize_xref_target(variant))
            if cands:
                return self._res(cands[0][0], "bible")
        return None

    def _resolve_colon(self, name: str, self_fn: str = None):
        """Normalized section-suffix form ``ARTICLE: SECTION`` (a `#section` target
        that normalize_xref_target rewrote to `: `).  Empty base = a same-article
        section (the xref caller supplies ``self_fn``)."""
        target = normalize_xref_target(name)
        if ": " not in target:
            return None
        base, _, suffix = target.rpartition(": ")
        base, suffix = base.strip(), suffix.strip()
        if not suffix:
            return None
        if not base:
            return ({"target": name, "display": suffix.title(),
                     "filename": self_fn, "anchor": suffix} if self_fn else None)
        xm = self._resolve_plain(base)
        if not xm:
            return None
        xfn, xdisp = xm
        sec = match_section(self._article_sections(xfn), suffix, aggressive=True)
        if sec:
            sl, t = sec.get("slug", ""), sec.get("title", "")
            tc = re.sub(r"^[ivxlc]+\.\s*", "", t, flags=re.I).strip("—. ")
            return {"target": f"{xdisp}#{sl}", "display": f"{xdisp} — {tc}",
                    "filename": xfn, "anchor": sl}
        return None

    # -- 1. sections (a separate pass) --------------------------------------
    def _resolve_sect(self, raw: str):
        """`X § Y` -> a DEDICATED `{demonym} Y` article (Russia § Lit -> RUSSIAN
        LITERATURE), else Y as a SECTION of X.  None -> fall back to the bare X."""
        m = _SECT_RE.match(raw)
        if not m:
            return None
        x = m.group(1).strip().rstrip(",(").strip()
        y = m.group(2).strip()
        xn, yn = _art_norm(x), _art_norm(y)
        if not (xn and yn):
            return None
        for pre, fn, title in self.tail_arts.get(yn, []):     # a dedicated article
            if pre and (pre.startswith(xn) or xn.startswith(pre)
                        or (len(pre) >= 4 and len(xn) >= 4 and pre[:4] == xn[:4])):
                return {"target": title, "display": title, "filename": fn}
        xm = self._resolve_plain(x)                           # else a section OF X
        if xm:
            xfn, xdisp = xm
            sec = match_section(self._article_sections(xfn), y, aggressive=True)
            if sec:
                sl, t = sec.get("slug", ""), sec.get("title", "")
                tc = re.sub(r"^[ivxlc]+\.\s*", "", t, flags=re.I).strip("—. ")
                return {"target": f"{xdisp}#{sl}", "display": f"{xdisp} — {tc}",
                        "filename": xfn, "anchor": sl}
        return None

    def _section_post(self, raw: str, ctx: set):
        """The post-fill section forms: a raw ``#anchor``, then the name IS a
        unique section-title whose article's words are present in ``ctx``."""
        if "#" in raw:
            base, anchor = raw.split("#", 1)
            bm = self._resolve_plain(base)
            if bm:
                bfn, bdisp = bm
                return {"target": raw, "display": anchor.strip() or bdisp,
                        "filename": bfn, "anchor": anchor.strip()}
        si = self.section_index.get(_art_norm(raw))
        if si:
            sfile, sslug, sart = si
            artwords = [w for w in _art_norm(sart).split() if len(w) >= 4]
            if any(w in ctx for w in artwords):
                return {"target": f"{sart}#{sslug}", "display": sart,
                        "filename": sfile, "anchor": sslug}
        return None

    def resolve_section(self, raw: str, ctx: set = None):
        """The whole section pass for a standalone caller: `§` form first, then the
        `#` / bare section-title forms.  (``resolve`` splits these around the fill —
        `§` before, the rest after — so a real article title wins over a same-named
        section; a section-only caller doesn't care about that straddle.)"""
        if "§" in raw:
            sr = self._resolve_sect(raw)
            if sr:
                return sr
        return self._section_post(raw, ctx or set())

    # -- 2. propose candidates (FILL) ---------------------------------------
    def candidates(self, name: str, *, superset: bool = False):
        """The tightest non-empty candidate bag from the fill ladder, with the rung
        that produced it: exact → alt → fold → subset → [superset] → first-word →
        fuzzy.  Pure recall (no picking, no gates).

        ``superset`` (title ⊂ link, reverse containment) is OFF by default: it wins
        on verbose work-title xrefs (UNITED STATES DECLARATION OF INDEPENDENCE ->
        the Declaration) but is poison for topics, where the contained title is a
        red herring (a given name / half a compound: Custard Apple -> APPLE).  The
        xref path turns it on; the topic path leaves it off."""
        bag = self.idx.exact(name)
        if bag:
            return bag, "exact"
        for alt in _topic_alternates(name):
            bag = self.idx.exact(alt)
            if bag:
                return bag, "alt"
        bag = self.idx.fold_match(name)
        if bag:
            return bag, "fold"
        bag = self.idx.subset(name)
        if bag:
            return bag, "subset"
        if superset:
            bag = self.idx.superset(name)              # title ⊂ link (reverse)
            if bag:
                return bag, "superset"
        bag = self.idx.firstword(name)
        if bag:
            return bag, "firstword"
        fn = self.idx.fuzzy(name)
        if fn:
            return [(fn, self.title_by_fn[fn])], "fuzzy"
        return [], "none"

    # -- 3. fish (pick one) --------------------------------------------------
    def fish(self, name, cands, *, path=None, prose=None, want_kind=None,
             trusted=True):
        """Pick one candidate from a bag — the bucket path (topics) or the reference
        prose (xrefs) as context.  Returns (fn, title, method), or (None, None,
        "abstain") for an untrusted cue whose prose clears no candidate."""
        return self.fisher.fish(name, cands, path or [], want_kind, prose=prose,
                                trusted=trusted)

    # -- article-xref orchestrator (Phase 6b5) -------------------------------
    def _topics_of(self, fn):
        if self._topic_map is None:
            self._topic_map = load_topic_map()
        return self._topic_map.get(fn)

    def resolve_see(self, target: str, display: str = None, *,
                    self_fn: str = None):
        """The UNTRUSTED tier (see / see also / cf): a real see points WITHIN
        the source article's own subject, so filter fill candidates to those
        sharing a source topic category and ABSTAIN when none do (bibliographic
        authors / verb-usage `see` land in other topics or none).  The shared
        category doubles as the fisher's bucket for any that qualify.  Only a
        SUBSTANTIAL tight match may bind — exact/alt/fold, or a subset covering
        ≥2 content words; a single-word surname/firstword match is a red
        herring (a wrong same-named person who may share the topic).
        Returns a filename or None."""
        src_cats = self._topics_of(self_fn) if self_fn else None
        if not src_cats:
            return None
        for name in (n for n in (target, display) if n):
            bag, tag = self.candidates(name)       # superset off — component noise
            substantial = tag in ("exact", "alt", "fold") or (
                tag == "subset" and len(content(wordset_f(name))) >= 2)
            if not substantial:
                continue
            match = [c for c in bag
                     if (self._topics_of(c[0]) or set()) & src_cats]
            if len(match) == 1:
                return match[0][0]
            if len(match) >= 2:
                fn, _, _ = self.fish(name, match, path=sorted(src_cats))
                if fn:
                    return fn
        return None

    def _xref_pass(self, name, prose, self_fn, rungs):
        """One tier of the trusted ladder over one name: the folded-in target
        forms (Bible ref, colon-suffix), then the fill bag IF its rung is in
        this tier, fished on the reference's prose.  -> (fn, section) | None."""
        b = self._try_bible(name)
        if b:
            return b["filename"], None
        c = self._resolve_colon(name, self_fn=self_fn)
        if c:
            return c["filename"], c.get("anchor")
        bag, tag = self.candidates(name, superset=True)
        if not bag or tag not in rungs:
            return None
        if len(bag) > 1 and self_fn:
            # An article never links to ITSELF while another candidate shares
            # the name (the old resolver's self-reference rule).
            bag = [c for c in bag if c[0] != self_fn] or bag
        if len(bag) == 1:
            return bag[0][0], None
        fn, _, _ = self.fish(name, bag, prose=prose)
        return (fn, None) if fn else None

    def resolve_xref(self, target: str, display: str = None, *,
                     prose: str = "", self_fn: str = None,
                     trusted: bool = True):
        """One inline article reference -> ``(filename, section)`` (both None
        when nothing binds).  ``target`` is the extractor's normalized target;
        ``display`` the marker's display text when it differs.  TRUSTED cues
        (link / q.v.) always pick — the author declared the reference real, so
        a weak fish still beats no link; untrusted cues (see / cf) go through
        ``resolve_see`` and abstain by default."""
        if display and normalize_xref_target(display) == \
                normalize_xref_target(target):
            display = None
        if not trusted:
            fn = self.resolve_see(target, display, self_fn=self_fn)
            return (fn, None) if fn else (None, None)
        names = [n for n in (target, display) if n]
        # Prefer the MORE-SPECIFIC name first: the side carrying more content
        # words identifies the reference better (target `SAY` vs display
        # `Léon Say`).  Stable sort → target still leads on a tie.
        names.sort(key=lambda n: -len(content(wordset_f(n))))
        for rungs in (_XREF_TIGHT, _XREF_LOOSE):
            for name in names:
                r = self._xref_pass(name, prose, self_fn, rungs)
                if r:
                    return r
        return None, None

    # -- orchestrator --------------------------------------------------------
    def resolve(self, raw_name: str, want=(), ctx: set = None, path=None,
                prose=None):
        """NAME -> article dict (or an unresolved dict).  Stages: (1) `§` sections,
        (2) exact word-set FILL, (3) FISH a collision by context — the bucket
        (topics) or the reference ``prose`` (xrefs) — / kind / embedding, (4)
        LOOSEN empty bags (recall, then fish), gated so a typed bucket never binds
        a wrong-kind article; (5) post-fill section forms."""
        want_kind = want[0] if want else None
        path = path or []
        ctx = ctx or set()

        def _kind_ok(fn):
            # Reject only where reliable: ethnic/nature never bind a place; a
            # peerage topic must land on a biographical title.  Inert for xrefs
            # (want_kind None → True).
            if not want_kind or want_kind in _WANT_PLACE:
                return True
            if want_kind in ("ethnic", "nature"):
                return lead_kind(self._opening(fn)) not in _PICK_PLACE
            if _PEERAGE.search(raw_name):
                t = self.title_by_fn.get(fn, "")
                return ("," in t) or bool(_PEERAGE.search(t))
            return True

        def _fish_gated(cands, tag):
            fn, _, m = self.fish(raw_name, cands, path=path, prose=prose,
                                 want_kind=want_kind)
            return self._res(fn, tag + ":" + m) if (fn and _kind_ok(fn)) else None

        # 1. sections (`§` — before fill, so an explicit section ref wins)
        if "§" in raw_name:
            sr = self._resolve_sect(raw_name)
            if sr:
                return sr
        # 2. fill — exact word-set
        hits = self.idx.exact(raw_name)
        # 2a. feature-kind broadening (a Rivers/Lakes bucket wants ZUG, LAKE OF).
        if want_kind in _BROADEN_KINDS and not (
                len(hits) == 1 and kind_qualifies(
                    lead_kind(self._opening(hits[0][0])), want_kind)):
            pick = pick_by_kind(self.idx.subset(raw_name), want_kind, self._opening)
            if pick is not None:
                return self._res(pick, "broaden")
        if len(hits) == 1:
            return self._res(hits[0][0], "unique")
        if len(hits) >= 2:                                    # collision -> fish
            fn, _, m = self.fish(raw_name, hits, path=path, prose=prose,
                                 want_kind=want_kind)
            if fn:
                return self._res(fn, "fish:" + m)
        # 3b. folded-in xref target forms — reached only on an empty exact bag;
        #     inert for topics (no BIBLE/ shapes, no resolving colon-suffixes).
        b = self._try_bible(raw_name)
        if b:
            return b
        c = self._resolve_colon(raw_name)
        if c:
            return c
        # 4. loosen empty bags (recall only, tightest first, then fish; kind-gated)
        for alt in _topic_alternates(raw_name):               # name-side or/paren alts
            h = self.idx.exact(alt)
            if len(h) == 1 and _kind_ok(h[0][0]):
                return self._res(h[0][0], "loosen:alt")
            if len(h) >= 2:
                r = _fish_gated(h, "loosen:alt-fish")
                if r:
                    return r
        h = self.idx.fold_match(raw_name)                     # diacritic fold
        if len(h) == 1 and _kind_ok(h[0][0]):
            return self._res(h[0][0], "loosen:fold")
        if len(h) >= 2:
            r = _fish_gated(h, "loosen:fold-fish")
            if r:
                return r
        h = self.idx.subset(raw_name)                         # subset (name ⊂ title; content)
        if h:
            r = _fish_gated(h, "loosen:subset")
            if r:
                return r
        h = self.idx.firstword(raw_name)                      # any title w/ first word
        if len(h) == 1 and _kind_ok(h[0][0]):
            return self._res(h[0][0], "loosen:firstword")
        if len(h) >= 2:
            r = _fish_gated(h, "loosen:firstword-fish")
            if r:
                return r
        fn = self.idx.fuzzy(raw_name)
        if fn and _kind_ok(fn):
            return self._res(fn, "loosen:fuzzy")
        # 5. post-fill section forms (`#anchor`, or the name IS a section title)
        post = self._section_post(raw_name, ctx)
        if post:
            return post
        return {"target": raw_name, "display": raw_name}


def build_resolver():
    """Back-compat factory: the classified-TOC caller expects a callable
    ``resolve(name, want, ctx, path)``.  Returns the bound method."""
    return LinkResolver().resolve
