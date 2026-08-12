"""Fast RAW-SOURCE access for repeated audits.

NOT `britannica.export.corpus.load_corpus`, which loads EXPORTED payloads.
This yields raw wikitext rows from a pickle of the DB.  The two were both
called `load_corpus`, and at a call site `from _corpus_cache import
load_corpus` and `from britannica.export.corpus import load_corpus` are
indistinguishable while returning different things about different data.

The naive pattern (one segment query + one Article.get PER article) costs ~73k
round-trips over 36k articles — minutes per run.  Source is static
([[project_source_is_static]]), so we assemble every article's raw text ONCE in
a single bulk query and pickle it.  Subsequent runs load the pickle (seconds)
and optionally pre-filter to articles whose raw contains a token of interest —
e.g. a Ts audit only needs the few hundred Ts-bearing articles, not all 36k.

    from _corpus_cache import iter_raw_articles
    for aid, vol, pg0, raw in iter_raw_articles(contains="{{Ts"):
        ...

`contains` is case-insensitive substring pre-filter (str or tuple-of-str → any).
`refresh=True` rebuilds the pickle from the DB.
"""
from __future__ import annotations
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "tools" / "_scratch" / "corpus_raw.pkl"


def _build() -> list[tuple[int, int, int, str]]:
    """One bulk query → [(article_id, volume, page0, raw), …] in article order.

    Reads `Article.body` — the article whole, exactly as sliced from the clean
    volume stream.  This used to fetch the per-page segments and glue them with
    `""`, which was the FIFTH different reassembly in the codebase (`""`, `"
"`,
    `"

"`, `" "`-unless-image, and this one) — five answers because the cut
    destroyed the information needed to invert it.  Nothing is cut now
    ([[project_page_position_out_of_band]]).
    """
    sys.path.insert(0, str(ROOT / "src"))
    from britannica.db.session import SessionLocal
    from britannica.db.models import Article
    s = SessionLocal()
    rows = (
        s.query(Article.id, Article.volume, Article.page_start, Article.body)
        .filter(Article.article_type != "plate")
        .order_by(Article.id)
        .all()
    )
    s.close()
    return [(aid, vol, pg0, body or "") for aid, vol, pg0, body in rows]


def iter_raw_articles(contains=None, refresh: bool = False):
    """Yield (article_id, volume, page0, raw) for every non-plate article,
    optionally only those whose raw contains `contains` (str or tuple → any),
    case-insensitive.  Uses the on-disk pickle unless `refresh`."""
    if refresh or not CACHE.exists():
        data = _build()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        with open(CACHE, "rb") as f:
            data = pickle.load(f)
    if contains is None:
        yield from data
        return
    needles = (contains,) if isinstance(contains, str) else tuple(contains)
    needles = tuple(n.lower() for n in needles)
    for aid, vol, pg0, raw in data:
        low = raw.lower()
        if any(n in low for n in needles):
            yield aid, vol, pg0, raw
