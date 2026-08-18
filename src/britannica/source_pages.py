"""The RAW source as a collection — ONE loader, ONE error posture, corrections applied.

The output side got this first: `export.corpus.load_corpus` is the single total,
loud reader of `data/derived/articles`, and every phase goes through it.  The
INPUT side — `data/raw/wikisource/vol_NN/volNN-pagePPPP.json` — was still in the
state the exported corpus used to be in: nine live modules spelled the walk
themselves, in four different postures (`except Exception: continue`,
`except (OSError, json.JSONDecodeError): continue`, no guard at all), across two
different globs (`*.json` vs `vol*-page*.json`).

Two of those are production (`xrefs.alias_table`, `contributors.link_frontmatter`);
the rest are audits, where a swallowed page is a false CLEAN — the tool reports
nothing about a page it could not read, and its count still looks right
([[feedback_sweepers_hide_bugs]], [[feedback_honesty_surface_failures]]).

CORRECTIONS ARE PART OF READING.  `data/corrections.json` is how we fix a
transcription typo ([[feedback_corrections_json]]) — so corrected text IS our
source, and text without them is something no stage should consume.  `corrections.py`
already names the disease in its own docstring: three separate stages "must
re-apply" them, and a stage that forgets "silently no-ops corrections on its
path".  A reader every caller shares is how that stops being something to
remember ([[feedback_dissolve_dont_fix]]).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, NamedTuple

from britannica.corrections import apply_corrections
from britannica.util.loading import PartialLoadError, unreadable

RAW_DIR = Path("data/raw/wikisource")
_VOL_DIR_RE = re.compile(r"^vol_(\d+)$")
_PAGE_FILE_RE = re.compile(r"^vol(\d+)-page(\d+)\.json$")


class SourceLoadError(PartialLoadError):
    """One or more raw source pages could not be loaded."""

    def __init__(self, failures: list[tuple[Path, str]]):
        super().__init__(failures, noun="raw source page",
                         refusing="report over a partial source")


class SourcePage(NamedTuple):
    """One transcribed page.  ``text`` has corrections applied; ``path`` is kept so
    a finding can name the file a human has to open."""
    volume: int
    page: int
    path: Path
    text: str


def load_pages(volume: int | None = None, *,
               pages: Iterable[int] | None = None,
               raw_dir: Path | str = RAW_DIR,
               strict: bool = True) -> tuple[list[SourcePage], list[tuple[Path, str]]]:
    """Load raw pages in (volume, page) order → ``(pages, failures)``.

    ``volume`` limits to one volume, ``pages`` to specific page numbers within it.
    A file that will not parse, carries no ``raw_text``, is not a page at all, or
    whose in-file ``volume``/``page_number`` disagree with its filename is a
    FAILURE — collected, reported with its reason, and (``strict``) raised.  The
    filename/field cross-check is free here and catches the one corruption a
    parse test cannot: a page filed under the wrong number, which would put its
    text in the wrong article's source.
    """
    raw_dir = Path(raw_dir)
    want = set(pages) if pages is not None else None
    out: list[SourcePage] = []
    failures: list[tuple[Path, str]] = []

    if volume is not None:
        vol_dirs = [raw_dir / f"vol_{volume:02d}"]
        if not vol_dirs[0].is_dir():
            failures.append((vol_dirs[0], "volume directory does not exist"))
            vol_dirs = []
    else:
        vol_dirs = sorted(d for d in raw_dir.iterdir()
                          if d.is_dir() and _VOL_DIR_RE.match(d.name))

    for vd in vol_dirs:
        vol_from_dir = int(_VOL_DIR_RE.match(vd.name).group(1))
        for f in sorted(vd.glob("*.json")):
            m = _PAGE_FILE_RE.match(f.name)
            if not m:
                failures.append((f, "not a source-page filename"))
                continue
            pg = int(m.group(2))
            if want is not None and pg not in want:
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                failures.append((f, unreadable(e)))
                continue
            if not isinstance(d, dict) or "raw_text" not in d:
                failures.append((f, "no raw_text field"))
                continue
            if d.get("volume") != vol_from_dir or d.get("page_number") != pg:
                failures.append((f, f"filename says vol {vol_from_dir} page {pg}, "
                                    f"file says vol {d.get('volume')} page "
                                    f"{d.get('page_number')}"))
                continue
            out.append(SourcePage(vol_from_dir, pg, f,
                                  apply_corrections(d["raw_text"], vol_from_dir)))

    # A page you ASKED FOR and did not get is a failure, not a shorter result.
    # The callers this replaces all wrote `if p.exists():` around a named page —
    # `build_preface` over vol 1 ws10-23, `build_toc` over the vol 29 index range —
    # so a page that vanished produced a preface with a hole in it and no
    # complaint.  Enumeration has no such expectation and stays silent.
    if want is not None:
        got = {p.page for p in out}
        for pg in sorted(want - got):
            failures.append((raw_dir / ("vol_%02d" % volume) /
                             ("vol%02d-page%04d.json" % (volume, pg)),
                             "requested page is missing"))

    if failures and strict:
        raise SourceLoadError(failures)
    return out, failures


def volume_text(volume: int, *, sep: str = "\n") -> str:
    """Every page of one volume joined in page order.  For scans that only need to
    ask "does this string occur anywhere in the volume" — note that the join makes
    a construct opened on one page closable on any later one, so it cannot answer
    a balance question ([[feedback_measure_at_decision_site]])."""
    return sep.join(p.text for p in load_pages(volume)[0])
