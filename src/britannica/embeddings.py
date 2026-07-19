"""Lead-text embeddings for topic-link disambiguation (the fisher's semantic score).

The topic fisher scores a candidate article by ``cosine(its lead embedding, the
bucket-path embedding)`` — semantic *membership* in the category, which reads
aboutness rather than word-overlap, so a river beats the department that merely
mentions a river and the length-normalization kills the verbosity bias that sank
the word-overlap scoreboard (docs/topic_resolver_redesign.md, stage 3).

fastembed runs BAAI/bge-small-en-v1.5 (ONNX, ~50 MB, local, no key,
deterministic).  Each article lead is embedded once and cached to
``data/derived/lead_embeddings.npz`` keyed by filename; bucket-path strings are
embedded on demand (there are far fewer distinct paths than articles, they vary
per run, and proximity-weighting builds them from parts anyway).

Vectors are L2-normalized at build time and on the query side, so ``cosine`` is a
plain dot product.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_CACHE = Path("data/derived/lead_embeddings.npz")
_ARTS_DIR = Path("data/derived/articles")
_INDEX = _ARTS_DIR / "index.json"

# Strip the producer's marker SYNTAX from a lead before embedding, keeping the
# payload text: «TITLE:X»/«I:x» -> "X"/"x", bare « » dropped.  The kind rung reads
# the raw lead (it parses the markers); embeddings want clean prose.
_MARK_TAG = re.compile(r"«[^»]*?:")


def _clean_lead(text: str) -> str:
    return _MARK_TAG.sub(" ", text).replace("«", " ").replace("»", " ").strip()


def _l2(mat: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalize so cosine collapses to a dot product."""
    n = np.linalg.norm(mat, axis=-1, keepdims=True)
    return mat / np.clip(n, 1e-12, None)


class LeadEmbeddings:
    """filename -> unit lead-vector, plus on-demand text embedding.

    Built once (``build_cache``) and reloaded (``load``).  The model is only
    instantiated when a *query* string needs embedding, so scoring a run off the
    cache never touches ONNX.
    """

    def __init__(self, vectors: dict[str, np.ndarray]):
        self._vec = vectors
        self._model = None

    # -- query ----------------------------------------------------------------
    def vector_of(self, filename: str):
        return self._vec.get(filename)

    def _embedder(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=_MODEL_NAME)
        return self._model

    def embed_text(self, text: str) -> np.ndarray:
        vec = np.asarray(next(iter(self._embedder().embed([text or " "]))),
                         dtype=np.float32)
        return vec / max(float(np.linalg.norm(vec)), 1e-12)

    def embed_weighted(self, parts: list[tuple[str, float]]) -> np.ndarray:
        """Unit vector of a weight-averaged bundle of strings.

        The bucket path is proximity-weighted here: the immediate parent
        discriminates far better than the top category, so the leaf/near
        segments carry more weight.  Each segment is embedded, unit-normalized,
        scaled by its weight, summed, and renormalized.
        """
        texts = [t or " " for t, _ in parts]
        ws = np.asarray([w for _, w in parts], dtype=np.float32)
        vecs = _l2(np.asarray(list(self._embedder().embed(texts)), dtype=np.float32))
        combo = (vecs * ws[:, None]).sum(axis=0)
        return combo / max(float(np.linalg.norm(combo)), 1e-12)

    def cosine(self, filename: str, query_vec: np.ndarray) -> float:
        v = self._vec.get(filename)
        return -1.0 if v is None else float(v @ query_vec)

    # -- persistence ----------------------------------------------------------
    @classmethod
    def load(cls, path: Path = _CACHE) -> "LeadEmbeddings":
        d = np.load(path, allow_pickle=False)
        return cls(dict(zip(d["filenames"].tolist(), d["vectors"])))

    @staticmethod
    def exists(path: Path = _CACHE) -> bool:
        return path.exists()


def _iter_leads(limit: int | None = None):
    """(filename, lead-text) for every real article, in index order.

    Falls back to the title when the lead is empty so no article embeds to the
    blank vector.
    """
    from britannica.xrefs.disambiguation import body_opening
    index = json.loads(_INDEX.read_text(encoding="utf-8"))
    n = 0
    for e in index:
        if e.get("article_type") != "article":
            continue
        fn = e["filename"]
        try:
            body = json.loads((_ARTS_DIR / fn).read_text(encoding="utf-8")).get("body", "")
        except Exception:
            body = ""
        yield fn, (_clean_lead(body_opening(body)) or e["title"])
        n += 1
        if limit and n >= limit:
            return


def build_cache(path: Path = _CACHE, batch: int = 256, limit: int | None = None) -> int:
    """Embed every article lead and write the vector cache.  Returns the count."""
    from fastembed import TextEmbedding
    fns, leads = [], []
    for fn, lead in _iter_leads(limit):
        fns.append(fn)
        leads.append(lead or " ")
    model = TextEmbedding(model_name=_MODEL_NAME)
    mat = _l2(np.asarray(list(model.embed(leads, batch_size=batch)), dtype=np.float32))
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, filenames=np.array(fns), vectors=mat)
    return len(fns)


if __name__ == "__main__":
    count = build_cache()
    print(f"embedded {count} leads -> {_CACHE}")
