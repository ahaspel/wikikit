"""Name → candidate EB articles: the shared FILL substrate.

Every resolution concern (topic links, article xrefs, …) resolves a NAME to an EB
article, and they all recall candidates the same way — word-set (tight) →
diacritic-fold → subset → first-word (liberal), with a fuzzy last resort.  This is
that recall engine, and ONLY recall: it returns candidate bags, it never picks.
Picking (the fisher) and concern-specific policy — bucket vs prose, kind gates,
always-pick vs abstain — live in the caller.

Lifted verbatim from ``populate_classified_toc.build_resolver`` so the topic path
stays byte-identical (the tokenizers and per-rung candidate logic are unchanged).
See docs/xref_resolution_strategy.md.
"""
import re
import unicodedata

from britannica.xrefs.normalizer import normalize_xref_target
from britannica.xrefs.resolver import build_core_maps
from britannica.xrefs.scoring import find_fuzzy_match

_TOK = re.compile(r"[^\W_]+", re.UNICODE)


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def wordset(s: str) -> frozenset:
    return frozenset(w.upper() for w in _TOK.findall(s))


def wordset_f(s: str) -> frozenset:          # diacritic-folded
    return frozenset(w.upper() for w in _TOK.findall(fold(s)))


# Furniture: honorifics / suffixes / structural words that are never article
# identity, stripped from BOTH sides of a containment test.  Excludes surname-risky
# words on purpose (Lord/Lady, particles de/von) — those can BE the name.
_FURNITURE = frozenset({
    "OF", "THE", "AND",
    "SIR", "ESQ", "BART", "HON", "DR", "MR", "MRS", "MME", "MLLE", "MISS",
    "REV", "PROF", "ST", "SAINT", "JR", "SR", "MADAME",
})


def content(ws: frozenset) -> frozenset:
    """Word-set minus the FURNITURE — single-letter initials ('J.'/'C.'), honorifics
    ('Sir', 'Esq.', 'Bart.'), and structural words ('of', 'the').  None of these are
    article identity, so word-set CONTAINMENT must ignore them (else the letter
    articles tight-match every initial, 'Sir' matches every knight, etc.)."""
    return frozenset(w for w in ws if len(w) > 1 and w not in _FURNITURE)


class NameIndex:
    """Recall-only index over article (filename, title) rows.  Build once; each
    method returns a candidate bag ``[(filename, title), …]`` for one rung."""

    def __init__(self, arts):
        """arts: iterable of dicts carrying ``filename`` and ``title``."""
        arts = list(arts)
        self.title_by_fn: dict[str, str] = {e["filename"]: e["title"] for e in arts}
        self.by_ws: dict[frozenset, list] = {}
        self.by_wsf: dict[frozenset, list] = {}
        self.fn_ws: dict[str, frozenset] = {}
        self.fn_cws: dict[str, frozenset] = {}     # content (multi-letter) words
        self.word_fns: dict[str, set] = {}
        for e in arts:
            fn, title = e["filename"], e["title"]
            self.by_ws.setdefault(wordset(title), []).append((fn, title))
            wf = wordset_f(title)
            self.by_wsf.setdefault(wf, []).append((fn, title))
            self.fn_ws[fn] = wf
            self.fn_cws[fn] = content(wf)
            for w in wf:
                self.word_fns.setdefault(w, set()).add(fn)
        # Shared name→fn map for the fuzzy rung + section lookup (first-wins by norm).
        self.by_norm, self.tmap = build_core_maps(
            ((e["title"], (e["filename"], e["title"])) for e in arts),
            value_of=lambda ft: ft[0])

    # ── candidate rungs, tightest → liberal.  Each returns [(fn, title)]. ──
    def exact(self, name: str) -> list:
        return self.by_ws.get(wordset(name), [])

    def exact_ws(self, ws: frozenset) -> list:
        return self.by_ws.get(ws, [])

    def fold_match(self, name: str) -> list:
        return self.by_wsf.get(wordset_f(name), [])

    def subset(self, name: str) -> list:
        """Articles whose word-set ⊇ the name's CONTENT words (name ⊂ title,
        initials ignored: 'Fletcher, Alice C.' -> {FLETCHER, ALICE} ⊆ FLETCHER,
        ALICE CUNNINGHAM)."""
        ws = content(wordset_f(name))
        if not ws:
            return []
        sets = [self.word_fns.get(w, set()) for w in ws]
        common = set.intersection(*sets) if all(sets) else set()
        return [(fn, self.title_by_fn[fn]) for fn in common if ws <= self.fn_ws[fn]]

    def superset(self, name: str) -> list:
        """Titles whose folded word-set ⊆ the name's (title ⊂ link) — the reverse
        of ``subset``.  Returns the LONGEST such title(s) (most link words covered =
        most specific): `UNITED STATES DECLARATION OF INDEPENDENCE` -> `INDEPENDENCE,
        DECLARATION OF`.  Ties come back together for the fisher."""
        ws = content(wordset_f(name))
        if not ws:
            return []
        fns = set()
        for w in ws:
            fns |= self.word_fns.get(w, set())
        cands = [fn for fn in fns if self.fn_cws[fn] and self.fn_cws[fn] <= ws]
        if not cands:
            return []
        best = max(len(self.fn_cws[fn]) for fn in cands)
        return [(fn, self.title_by_fn[fn]) for fn in cands
                if len(self.fn_cws[fn]) == best]

    def firstword(self, name: str) -> list:
        """Any article whose title contains the name's FIRST (folded) word."""
        fw = _TOK.findall(fold(name))
        if not fw:
            return []
        hh = self.word_fns.get(fw[0].upper())
        return [(f, self.title_by_fn[f]) for f in hh] if hh else []

    def fuzzy(self, name: str):
        """Edit-distance last resort → a filename or None."""
        return find_fuzzy_match(normalize_xref_target(name), self.tmap, aggressive=True)

    def add_alias(self, alias_title: str, canonical_fn: str) -> None:
        """Index ``alias_title`` as an alternate name for ``canonical_fn`` — recall
        only, and never shadowing a real title's word-set / fold / normalized key
        (a collision with an existing title keeps the title)."""
        canonical_title = self.title_by_fn.get(canonical_fn)
        if canonical_title is None:
            return
        cand = [(canonical_fn, canonical_title)]
        ws = wordset(alias_title)
        if ws and ws not in self.by_ws:
            self.by_ws[ws] = cand
        wsf = wordset_f(alias_title)
        if wsf and wsf not in self.by_wsf:
            self.by_wsf[wsf] = cand
        for w in wsf:
            self.word_fns.setdefault(w, set()).add(canonical_fn)
        n = normalize_xref_target(alias_title)
        if n and n not in self.by_norm:
            self.by_norm[n] = cand
            self.tmap.setdefault(n, canonical_fn)
