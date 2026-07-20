"""The topic fisher — pick the right article from a collision bag.

Stage 3 of the topic resolver (docs/topic_resolver_redesign.md).  Fill has already
put a handful of same-name articles in the bag; exactly one belongs in the bucket,
and the bag is tiny so we can afford real work.  A cascade of rising cost, each
rung validated:

  1. kind (is-a): `lead_kind` + synonym map.  Sharp and free, but *incomplete*
     (vocabulary gaps, silent on same-kind bags).  Cross-kind only.
  2. embedding-cosine: cosine(article-lead vector, proximity-weighted bucket-path
     vector).  Semantic *membership* — reads aboutness, kills the verbosity bias.
  3. LLM judge: the arbiter for kind/embedding disagreement or a thin cosine
     margin.  Build-time, via a disambiguation cache (empty until an arbiter pass
     fills it); embedding carries the pick meanwhile, and every case it arbitrates
     is recorded in `pending`.

Policy: where kind and embedding AGREE, trust it (two orthogonal signals).  A
cached LLM verdict overrides.  Otherwise embedding leads and the case is flagged.
The fisher ALWAYS picks — the answer is in the bag; a dead tie falls to salience.
"""
from __future__ import annotations

from britannica.topic_geo import geo_filter
from britannica.topic_subject import field_score, field_terms
from britannica.xrefs.disambiguation import pick_by_kind

# Proximity weights for a bucket path taken leaf-first: the immediate parent
# discriminates far better than the top category, so the near segments dominate.
_PATH_WEIGHTS = (3.0, 2.0, 1.3, 1.0, 0.8, 0.6)

# Cosine gap below which the top two candidates are "too close" for embedding to
# separate on its own -> defer (to kind if it spoke, else flag for the LLM).
_MARGIN = 0.02

# Absolute prose-cosine below which an UNTRUSTED cue (see/cf) ABSTAINS: the text
# around the reference doesn't point at any candidate, so it probably isn't a real
# link.  Trusted cues (link/q.v.) never abstain — the target IS declared real.
_ABSTAIN_GATE = 0.45

# Place/nature kinds where the bucket is DEFINITIONAL: a "Towns" bucket wants the
# town, a "Rivers" bucket the river.  When pick_by_kind uniquely separates such a
# bag, that pick is authoritative and WINS over embedding — embedding only ever
# arbitrates a SAME-kind bag (two towns), which is where kind falls silent.  The
# soft kinds (PERSON and the biography fields) stay on the agree/embedding/LLM
# policy, since a category like "Art > Biographies" doesn't settle which same-name
# person is meant.
_HARD_KINDS = {"division", "town", "lake", "river", "mountain", "island",
               "ethnic", "nature"}


def collision_key(topic: str, cands, path) -> str:
    """Stable signature for the LLM disambiguation cache: the topic, its leaf
    bucket, and the candidate file set (which already encodes the name match)."""
    leaf = path[-1] if path else ""
    fns = ",".join(sorted(fn for fn, _ in cands))
    return f"{topic}␟{leaf}␟{fns}"


class Fisher:
    """Holds the shared indices; `fish` resolves one collision bag."""

    def __init__(self, embeddings, opening, salience=None, llm_cache=None):
        self._emb = embeddings          # LeadEmbeddings
        self._opening = opening         # fn -> raw lead text (for the kind rung)
        self._salience = salience or (lambda fn: 0)
        self._llm = llm_cache or {}     # collision_key -> chosen fn (arbiter verdicts)
        self.pending: list[dict] = []   # collisions the LLM should arbitrate
        self.stats: dict[str, int] = {}
        self._path_cache: dict[tuple, object] = {}   # distinct paths are few; embed once

    def _path_vec(self, path):
        """Unit vector of the proximity-weighted path (root->leaf input).

        Memoized by the path tuple — thousands of topics share a few hundred
        distinct buckets, and each miss is one ONNX embed.
        """
        key = tuple(path)
        vec = self._path_cache.get(key)
        if vec is None:
            leaf_first = list(reversed(path)) or [""]
            parts = list(zip(leaf_first, _PATH_WEIGHTS))
            vec = self._emb.embed_weighted(parts)
            self._path_cache[key] = vec
        return vec

    def _tally(self, method):
        self.stats[method] = self.stats.get(method, 0) + 1

    def fish(self, topic: str, cands, path, want_kind, prose=None, trusted=True):
        """topic: raw name; cands: [(fn, title)]; path: [root, ..., leaf];
        want_kind: str|None.  Returns (fn, title, method) — or (None, None,
        "abstain") when ``trusted`` is False and the prose doesn't point at any
        candidate.

        The disambiguation context is a BUCKET PATH by default (topics).  Pass
        ``prose`` — the text surrounding a reference — instead, and the fisher keys
        its embedding rung on that prose and skips the bucket-only geo/field rung.
        ``trusted``: True for declared cues (link/q.v.) — always pick; False for the
        noisy tier (see/cf) — abstain below the cosine gate, and no free len==1 pass
        (docs/xref_resolution_strategy.md)."""
        title_of = dict(cands)
        if len(cands) == 1 and trusted:
            fn = cands[0][0]
            self._tally("unique")
            return fn, title_of[fn], "unique"

        # 0. bucket context — the bucket names the discriminating attribute and
        #    the lead states it: a DIRECT fact, so it precedes kind/embedding.
        #    (a) geography — country/region/nationality; (b) field — the person's
        #    profession vs the bucket subject.  Either can settle it alone, and
        #    they compose (a French bucket then narrowed to the painter).
        #    Bucket-only: skipped when the context is prose (xrefs have no bucket).
        if prose is None:
            winners, gstat = geo_filter(path, topic, cands, self._opening)
            pool = winners if (gstat in ("pick", "narrow") and winners) else cands
            if gstat in ("pick", "narrow") and len(pool) == 1:
                fn = pool[0][0]
                self._tally("geo")
                return fn, title_of[fn], "geo"
            fterms = field_terms(path)
            if fterms and len(pool) > 1:
                fs = {fn: field_score(self._opening(fn), fterms) for fn, _ in pool}
                best = max(fs.values())
                if best > 0:
                    fwin = [ft for ft in pool if fs[ft[0]] == best]
                    if len(fwin) == 1:
                        fn = fwin[0][0]
                        self._tally("field")
                        return fn, title_of[fn], "field"
                    pool = fwin
            if len(pool) < len(cands):
                cands = pool
                title_of = dict(cands)

        # 1. kind — a unique qualifier of the wanted kind.
        kpick = pick_by_kind(cands, want_kind, self._opening) if want_kind else None

        # 1a. hard place/nature kind: a unique kind pick is definitional -> it wins.
        if kpick is not None and want_kind in _HARD_KINDS:
            self._tally("kind-hard")
            return kpick, title_of[kpick], "kind"

        # 2. embedding — cosine against the weighted bucket path, or the reference
        #    prose when there is no bucket (xrefs).
        q = self._emb.embed_text(prose) if prose is not None else self._path_vec(path)
        scored = sorted(cands, key=lambda ft: self._emb.cosine(ft[0], q), reverse=True)
        epick = scored[0][0]
        top = self._emb.cosine(scored[0][0], q)
        second = self._emb.cosine(scored[1][0], q) if len(scored) > 1 else -1.0

        # Untrusted cue (see/cf): abstain if the prose doesn't clearly point at a
        # candidate — a failed resolution IS the filter for the noisy tier.
        if not trusted and top < _ABSTAIN_GATE:
            self._tally("abstain")
            return None, None, "abstain"

        # 3. the LLM arbiter's verdict wins if we have one.
        key = collision_key(topic, cands, path)
        if key in self._llm:
            fn = self._llm[key]
            if fn in title_of:
                self._tally("llm")
                return fn, title_of[fn], "llm"

        # 4. policy.
        if kpick is not None and kpick == epick:          # two orthogonal signals agree
            self._tally("agree")
            return kpick, title_of[kpick], "agree"
        if kpick is not None and (top - second) < _MARGIN:  # embedding can't separate; kind spoke
            self._tally("kind")
            return kpick, title_of[kpick], "kind"

        # embedding leads (or kind abstained) — flag the case for the arbiter.
        self.pending.append({
            "key": key, "topic": topic, "path": list(path),
            "want_kind": want_kind,
            "candidates": [{"fn": fn, "title": t} for fn, t in cands],
            "kind_pick": kpick, "embedding_pick": epick,
            "cosines": {fn: round(self._emb.cosine(fn, q), 4) for fn, _ in cands},
        })
        # dead tie on cosine -> salience break; else embedding.
        if top - second <= 0:
            best = max(cands, key=lambda ft: self._salience(ft[0]))
            self._tally("salience")
            return best[0], best[1], "salience"
        self._tally("embedding")
        return epick, title_of[epick], "embedding"
