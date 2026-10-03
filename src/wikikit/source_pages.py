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

from wikikit.corrections import apply_corrections
from wikikit.util.loading import PartialLoadError, unreadable

def raw_dir() -> Path:
    """Where the selected book's fetched pages live.

    ONE owner for the layout.  A caller that builds `data/raw/<x>/vol_NN` itself
    is a second answer to the same question, which is what the dup-constants
    ratchet caught when the fetcher grew its own copy.
    """
    from wikikit.corpora import current_corpus
    return Path("data/raw") / current_corpus().raw_dir


def volume_dir(volume: int, base: Path | str | None = None) -> Path:
    """The directory holding one volume's pages, under ``base`` or the book's own."""
    return (Path(base) if base is not None else raw_dir()) / f"vol_{volume:02d}"


def page_filename(volume: int, page: int) -> str:
    """The filename one fetched page is stored under."""
    return f"vol{volume:02d}-page{page:04d}.json"


RAW_DIR = raw_dir()
#: Pages the BOOK supplies — transcribed from its scans (`wikikit.scan`) where
#: Wikisource has no page or only an unproofread one.  Committed in the book's
#: repository, in the same file shape as a fetched page, so they enter by the
#: same door: this loader, and the importer that reads through it.
SUPPLIED_DIR = Path("data/supplied")
_VOL_DIR_RE = re.compile(r"^vol_(\d+)$")
_PAGE_FILE_RE = re.compile(r"^vol(\d+)-page(\d+)\.json$")
_LEVEL_RE = re.compile(r'pagequality level="(\d)"')
_NOINCLUDE_RE = re.compile(r"<noinclude>.*?</noinclude>", re.S)


def page_level(raw_text: str) -> int | None:
    """A page's proofreading level as Wikisource records it (0 without text,
    1 not proofread, 2 problematic, 3 proofread, 4 validated), or None."""
    m = _LEVEL_RE.search(raw_text or "")
    return int(m.group(1)) if m else None


def prefer(fetched: dict | None, supplied: dict | None) -> dict | None:
    """Which page a book reads when it has both (user, 2026-10-02): a supplied
    transcription replaces a fetched page that is missing, empty, or not
    proofread (level 0 or 1); a proofread fetched page always stands."""
    if supplied is None:
        return fetched
    if fetched is None:
        return supplied
    text = fetched.get("raw_text") or ""
    level = page_level(text)
    if not _NOINCLUDE_RE.sub("", text).strip() or (level is not None and level <= 1):
        return supplied
    return fetched


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


def _scan_dir(base: Path, volume: int | None, want: set[int] | None,
              failures: list[tuple[Path, str]], required: bool) -> dict[tuple[int, int], tuple[Path, dict]]:
    """Every readable page file under one base, keyed (volume, page).  A file
    that will not parse, carries no ``raw_text``, is not a page at all, or whose
    in-file ``volume``/``page_number`` disagree with its filename is a FAILURE.
    The filename/field cross-check catches the one corruption a parse test
    cannot: a page filed under the wrong number, which would put its text in
    the wrong article's source."""
    out: dict[tuple[int, int], tuple[Path, dict]] = {}
    if volume is not None:
        vol_dirs = [volume_dir(volume, base)]
        if not vol_dirs[0].is_dir():
            if required:
                failures.append((vol_dirs[0], "volume directory does not exist"))
            return out
    else:
        vol_dirs = sorted(d for d in base.iterdir()
                          if d.is_dir() and _VOL_DIR_RE.match(d.name)) if base.is_dir() else []
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
            out[(vol_from_dir, pg)] = (f, d)
    return out


def load_payloads(volume: int | None = None, *,
                  pages: Iterable[int] | None = None,
                  raw_dir: Path | str = RAW_DIR,
                  supplied_dir: Path | str = SUPPLIED_DIR,
                  strict: bool = True) -> tuple[list[tuple[Path, dict]], list[tuple[Path, str]]]:
    """The page files a book reads, in (volume, page) order → ``([(path, payload)],
    failures)``: its fetched pages with its supplied pages laid over them by
    `prefer`.  Payloads are as filed — corrections are `load_pages`' business —
    which is what the importer stores.  A book with no fetched pages at all
    (every page supplied) has no fetched volume directory, and that is not a
    failure; a book with neither is."""
    raw_dir, supplied_dir = Path(raw_dir), Path(supplied_dir)
    want = set(pages) if pages is not None else None
    failures: list[tuple[Path, str]] = []
    supplied = _scan_dir(supplied_dir, volume, want, failures, required=False)
    fetched = _scan_dir(raw_dir, volume, want, failures, required=not supplied)
    out = []
    for key in sorted(fetched.keys() | supplied.keys()):
        f, s = fetched.get(key), supplied.get(key)
        chosen = prefer(f[1] if f else None, s[1] if s else None)
        out.append(s if s and chosen is s[1] else f)

    # A page you ASKED FOR and did not get is a failure, not a shorter result.
    # The callers this replaces all wrote `if p.exists():` around a named page —
    # `build_preface` over vol 1 ws10-23, `build_toc` over the vol 29 index range —
    # so a page that vanished produced a preface with a hole in it and no
    # complaint.  Enumeration has no such expectation and stays silent.
    if want is not None:
        got = {p for (_, p) in fetched.keys() | supplied.keys()}
        for pg in sorted(want - got):
            failures.append((volume_dir(volume, raw_dir) / page_filename(volume, pg),
                             "requested page is missing"))

    if failures and strict:
        raise SourceLoadError(failures)
    return out, failures


def load_pages(volume: int | None = None, *,
               pages: Iterable[int] | None = None,
               raw_dir: Path | str = RAW_DIR,
               supplied_dir: Path | str = SUPPLIED_DIR,
               strict: bool = True) -> tuple[list[SourcePage], list[tuple[Path, str]]]:
    """Load the book's pages in (volume, page) order → ``(pages, failures)``,
    corrections applied (`load_payloads` for which page is read).  ``volume``
    limits to one volume, ``pages`` to page numbers within it; failures are
    collected, reported with their reasons, and (``strict``) raised."""
    payloads, failures = load_payloads(volume, pages=pages, raw_dir=raw_dir,
                                       supplied_dir=supplied_dir, strict=strict)
    return [SourcePage(d["volume"], d["page_number"], path,
                       apply_corrections(d["raw_text"], d["volume"]))
            for path, d in payloads], failures


def volume_text(volume: int, *, sep: str = "\n") -> str:
    """Every page of one volume joined in page order.  For scans that only need to
    ask "does this string occur anywhere in the volume" — note that the join makes
    a construct opened on one page closable on any later one, so it cannot answer
    a balance question ([[feedback_measure_at_decision_site]])."""
    return sep.join(p.text for p in load_pages(volume)[0])
