"""What was this artifact built FROM — one owner for the answer.

Every published artifact (site bundles, EPUB, MDX) is derived from two things:
the exported corpus and the code that transformed it.  When an artifact drifts,
it is because one of those moved and the artifact did not.

`corpus_stamp.py` already answers "is this corpus the output of a completed
rebuild, and has anything written to it since", and `deploy.sh` gates on it.  It
cannot answer THIS question, because the corpus is only half the input: the
sampler that shipped a build behind on 2026-09-18 sat on an UNCHANGED corpus and
differed only in code — the topic fix had landed after the deploy built it.  A
corpus stamp passes that case happily.

The MDX has carried both halves since it was written (`input_sha256`,
`export_code_sha256`); the EPUB carried neither.  Rather than grow a second
implementation of the same idea, the rule lives here and both formats call it
([[feedback_tune_dont_fork]]).

ROLLUPS ARE THE COMPARISON.  The per-item maps say WHICH file differs, which is
what you want once something is wrong; the rollup answers "is anything
different" in one comparison, which is what a gate wants.  Keep both: a gate
that is expensive to run is a gate that stops being run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/britannica"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rollup(items: dict[str, str]) -> str:
    """One digest over a name->digest map, order-independent."""
    return digest("\n".join(f"{k}:{v}" for k, v in sorted(items.items())).encode("utf-8"))


def source_files() -> dict[str, str]:
    """Every `.py` under `src/britannica`, repo-relative path -> sha256.

    Byte-for-byte the rule the MDX manifest has always used, so moving it here
    leaves that manifest's meaning unchanged.
    """
    return {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p.read_bytes())
            for p in sorted(SRC.rglob("*.py"))}


def payload_digest(article: dict) -> str:
    """One exported article -> sha256 of its canonical JSON.

    Sorted keys, so a re-export that only reorders keys is not a difference.
    """
    return digest(json.dumps(article, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def corpus_files(payloads: dict) -> dict[str, str]:
    """`{path_or_stem: article}` -> `{stem: sha256}`."""
    out = {}
    for key, article in payloads.items():
        stem = Path(str(key)).name
        if stem.endswith(".json"):
            stem = stem[:-5]
        out[stem] = payload_digest(article)
    return out


def rebuild_stamp() -> dict:
    """The corpus side, from the stamp `rebuild_all` already writes.

    Reusing it rather than re-hashing 37k payloads keeps ONE answer to "which
    corpus is this" — the same value `corpus_stamp.py --check` gates the deploy
    on — so an artifact and a deploy cannot disagree about what they mean by it.
    """
    path = ROOT / "data/derived/rebuild_stamp.json"
    if not path.is_file():
        return {}
    d = json.loads(path.read_text(encoding="utf-8"))
    return {k: d[k] for k in ("signature", "articles", "finished") if k in d}


def fingerprint(payloads: dict | None = None, *, detail: bool = False) -> dict:
    """The provenance block an artifact embeds.

    `detail` keeps the per-item maps (the MDX embeds them); without it you get
    the rollups alone, which is all a drift check needs and is three lines
    instead of forty thousand.
    """
    code = source_files()
    block: dict = {"code_rollup": _rollup(code), "code_file_count": len(code),
                   "rebuild_stamp": rebuild_stamp()}
    if payloads is not None:
        corpus = corpus_files(payloads)
        block["corpus_rollup"] = _rollup(corpus)
        block["article_count"] = len(corpus)
        if detail:
            block["corpus_sha256"] = corpus
    if detail:
        block["code_sha256"] = code
    return block
