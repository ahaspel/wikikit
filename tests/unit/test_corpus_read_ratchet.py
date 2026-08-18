"""The corpus has ONE reader, and it refuses to skip.

`export.corpus.load_corpus` is total: it applies `NON_ARTICLE` itself and RAISES
on a payload it cannot read, because "a file missing them is a FAILURE, not a
silent skip (that is how an article with no `id` used to drop out of a phase
unnoticed)".  Every other module that wanted the corpus wrote the read itself —
`glob("data/derived/articles/*.json")`, a hand-spelled exclusion list, and

    try:  d = json.loads(...)
    except Exception:  continue

— which is the same three lines twelve times, and the last one is a lie: an
article the tool could not read becomes an article the tool reports nothing
about.  For a leak finder that is a false CLEAN; for `export_fingerprint`, the
tool a rebuild is adjudicated against, it is worse — an article missing from BOTH
fingerprints is neither "changed" nor "disappeared", it is invisible
([[feedback_audit_against_source]]).

None of it was losing anything the day it was written, and none of it was losing
anything the day this test was: 37,226 files, 0 failures.  That is exactly the
state `snapshot_corpus` was in for weeks before BOGÓ collided with BOG and it
began dropping an article per capture in silence.  The defect is not the loss —
it is that the instrument cannot tell you.

So the rule is structural rather than remembered: read the corpus through its
one reader.  A new tool that globs the directory fails here.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The reader itself — the only place that may touch the directory directly.
OWNERS = {"src/britannica/export/corpus.py"}

_GLOB_RE = re.compile(
    r"""glob\s*\(\s*["'][^"']*articles[^"']*\*\.json"""     # glob("…articles/*.json")
    r"""|ARTICLES_DIR\s*\.\s*glob"""                        # ARTICLES_DIR.glob(…)
    r"""|ARTS\s*\.\s*glob""")                               # ARTS.glob(…)


def _hits() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for pat in ("src/**/*.py", "tools/**/*.py"):
        for f in ROOT.glob(pat):
            if "__pycache__" in str(f) or "_scratch" in str(f):
                continue
            rel = str(f.relative_to(ROOT)).replace("\\", "/")
            if rel in OWNERS:
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8",
                                                 errors="replace").splitlines(), 1):
                if _GLOB_RE.search(line):
                    out.setdefault(rel, []).append(i)
    return out


def test_corpus_is_read_through_its_one_reader():
    strays = _hits()
    assert not strays, (
        "these read the article corpus directly instead of through "
        "`export.corpus.load_corpus`:\n"
        + "\n".join(f"  {f}: lines {lines}" for f, lines in sorted(strays.items()))
        + "\nload_corpus applies NON_ARTICLE and RAISES on an unreadable payload; "
          "a hand-rolled read skips it and reports nothing, which is how an "
          "instrument comes to under-report without anyone noticing.")


def test_the_reader_still_refuses_to_skip():
    """Guard on the guard: if `load_corpus` ever became lenient, the ratchet
    above would be enforcing a rule that no longer buys anything."""
    from britannica.export.corpus import load_corpus
    import inspect
    src = inspect.getsource(load_corpus)
    assert "failures.append" in src, "load_corpus no longer records failures"
    assert "raise" in src or "CorpusLoadError" in src, \
        "load_corpus no longer raises on failure — the skip is back"
