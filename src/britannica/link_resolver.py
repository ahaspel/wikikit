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
from britannica.name_index import NameIndex

ARTICLES_INDEX = Path("data/derived/articles/index.json")
ARTS_DIR = ARTICLES_INDEX.parent
SECTION_INDEX = Path("data/derived/classified_section_index.json")

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
    bucket: dict[str, set] = defaultdict(set)
    for fn in Path("data/derived/articles").glob("*.json"):
        try:
            d = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
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

    def __init__(self):
        article_index = json.loads(ARTICLES_INDEX.read_text(encoding="utf-8"))
        arts = [e for e in article_index if e.get("article_type") == "article"]
        # The shared FILL substrate — word-set / fold / subset / first-word / fuzzy.
        self.idx = NameIndex(arts)
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
        # Embeddings for the fisher's semantic rung (built once, cached; regenerable).
        try:
            _emb = LeadEmbeddings.load()
        except Exception:
            build_cache()
            _emb = LeadEmbeddings.load()
        self.fisher = Fisher(_emb, self._opening)

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
    def candidates(self, name: str):
        """The tightest non-empty candidate bag from the fill ladder, with the rung
        that produced it: exact → alt → fold → subset → first-word → fuzzy.  Pure
        recall (no picking, no gates)."""
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
        bag = self.idx.firstword(name)
        if bag:
            return bag, "firstword"
        fn = self.idx.fuzzy(name)
        if fn:
            return [(fn, self.title_by_fn[fn])], "fuzzy"
        return [], "none"

    # -- 3. fish (pick one) --------------------------------------------------
    def fish(self, name, cands, *, path=None, prose=None, want_kind=None):
        """Pick one candidate from a bag — the bucket path (topics) or the
        reference prose (xrefs) as context.  Returns (fn, title, method)."""
        return self.fisher.fish(name, cands, path or [], want_kind, prose=prose)

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
        h = self.idx.subset(raw_name)                         # subset (name ⊂ title)
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
