"""Fast corpus access for repeated audits.

The naive pattern (one segment query + one Article.get PER article) costs ~73k
round-trips over 36k articles — minutes per run.  Source is static
([[project_source_is_static]]), so we assemble every article's raw text ONCE in
a single bulk query and pickle it.  Subsequent runs load the pickle (seconds)
and optionally pre-filter to articles whose raw contains a token of interest —
e.g. a Ts audit only needs the few hundred Ts-bearing articles, not all 36k.

    from _corpus_cache import load_corpus
    for aid, vol, pg0, raw in load_corpus(contains="{{Ts"):
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
    """One bulk query → [(article_id, volume, page0, raw), …] in article order."""
    sys.path.insert(0, str(ROOT / "src"))
    from britannica.db.session import SessionLocal
    from britannica.db.models import Article, ArticleSegment, SourcePage
    s = SessionLocal()
    rows = (
        s.query(
            ArticleSegment.article_id,
            ArticleSegment.segment_text,
            SourcePage.page_number,
            Article.volume,
        )
        .join(SourcePage, ArticleSegment.source_page_id == SourcePage.id)
        .join(Article, ArticleSegment.article_id == Article.id)
        .filter(Article.article_type != "plate")
        .order_by(ArticleSegment.article_id, ArticleSegment.sequence_in_article)
        .all()
    )
    s.close()
    # Group consecutive rows by article_id (already ordered), assemble raw.
    out: list[tuple[int, int, int, str]] = []
    cur_id = None
    vol = page0 = None
    parts: list[str] = []
    for aid, seg_text, pg, v in rows:
        if aid != cur_id:
            if cur_id is not None:
                out.append((cur_id, vol, page0, "".join(parts)))
            cur_id, vol, page0, parts = aid, v, pg, []
        parts.append(seg_text or "")
    if cur_id is not None:
        out.append((cur_id, vol, page0, "".join(parts)))
    return out


def load_corpus(contains=None, refresh: bool = False):
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
