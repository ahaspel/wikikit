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
        self.word_fns: dict[str, set] = {}
        for e in arts:
            fn, title = e["filename"], e["title"]
            self.by_ws.setdefault(wordset(title), []).append((fn, title))
            wf = wordset_f(title)
            self.by_wsf.setdefault(wf, []).append((fn, title))
            self.fn_ws[fn] = wf
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
        """Articles whose folded word-set ⊇ the name's (name words ⊂ title)."""
        ws = wordset_f(name)
        if not ws:
            return []
        sets = [self.word_fns.get(w, set()) for w in ws]
        common = set.intersection(*sets) if all(sets) else set()
        return [(fn, self.title_by_fn[fn]) for fn in common if ws <= self.fn_ws[fn]]

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
