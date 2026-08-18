"""Each corpus has ONE reader, and neither of them skips.

Two collections, one rule.  `export.corpus.load_corpus` reads the EXPORTED
articles; `source_pages.load_pages` reads the RAW wikisource pages.  Both are
total: they apply their own exclusion rule and RAISE on a payload they cannot
read, because "a file missing them is a FAILURE, not a silent skip (that is how
an article with no `id` used to drop out of a phase unnoticed)".

Fifteen modules wrote the exported read themselves and nine wrote the raw read —
a glob, a hand-spelled exclusion list, and `except Exception: continue` — and
that last line is a lie: a page the tool could not read becomes a page the tool
reports nothing about.  For a leak finder that is a false CLEAN; for
`export_fingerprint`, the tool a rebuild is adjudicated against, an article
missing from BOTH fingerprints is neither "changed" nor "disappeared" — it is
invisible ([[feedback_audit_against_source]]).

The raw side had a second failure the exported side does not: FOUR postures
across nine modules (`except Exception: continue`, `except (OSError,
json.JSONDecodeError): continue`, `if p.exists():`, and no guard at all), plus
two different globs, plus a corrections rule that three of them had to remember
to re-apply by hand and the rest silently skipped ([[feedback_dissolve_dont_fix]]).

DETECTION IS FILE-LEVEL ON PURPOSE.  The first version of this test matched the
path and the `glob(` on ONE LINE, so `ART = "data/derived/articles"` two lines
up defeated it — and it passed while `export_fingerprint`, both phase-7 gates and
the search indexer read the directory directly.  The ratchet had exactly the
weakness it was written to catch.  A module that NAMES a corpus directory and
ENUMERATES a directory is presumed to be reading that corpus, whatever the
spelling.

Each ledger below is the honest exception: a few readers do go direct, and each
states its own coverage instead — which is the rule this arc actually enforces.
Reading through the one reader is how a tool gets that for free; a tool that opts
out has to provide it itself, in the open, with the reason written down.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# file (repo-relative) -> why it reads the directory itself, and how it stays honest.
EXPORTED_DIRECT = {
    "src/britannica/export/corpus.py":
        "IS the reader",
    "tools/diagnostics/export_fingerprint.py":
        "hashes 37k payloads in a ProcessPoolExecutor over PATHS; carries each "
        "failure back with its reason and ABORTS rather than emit a partial "
        "fingerprint",
    "tools/diagnostics/mangled_markers.py":
        "parallel scan; an unreadable payload is returned as an 'unreadable' "
        "FINDING, not skipped",
    "tools/diagnostics/output_leaks.py":
        "'a corrupt JSON is a finding' — recorded as a row, not skipped",
    "tools/diagnostics/link_census.py":
        "samples N stems and compares against production; prints "
        "'(fetch failures N)' so a dropped pair is visible",
    "tools/pipeline/index_search_ec2.py":
        "streams files to Meilisearch; prints 'Found N' and 'Indexed N'",
    "tools/diagnostics/baseline_article_bodies.py":
        "copies the directory as a backup; counts what it wrote",
    "tools/viewer/build_stamp.py":
        "hashes files for a build stamp — never reads a payload",
    "src/britannica/epub/build.py":
        "enumerates article STEMS for the EPUB (`_STEM_RE` excludes the "
        "non-articles); reads no payload here — the build reports its own counts",
    "src/britannica/export/download.py":
        "reads the corpus through load_corpus; its globs are over the download "
        "OUTPUT dir, not the article dir",
    "tools/pipeline/download_images.py":
        "reads the corpus through load_corpus; its `iterdir` counts files in the "
        "IMAGE dir",
}

RAW_DIRECT = {
    "src/britannica/source_pages.py":
        "IS the reader",
    "src/britannica/xrefs/alias_table.py":
        "reads through load_pages; the surviving mention is a `RAW_DIR / 'vol_29'` "
        "existence guard, which enumerates nothing",
    "tools/pipeline/rebuild_volume.py":
        "names `vol_NN/` only to pass it to the IMPORT stage as `--indir`; the "
        "import is the reader that loads those pages into the DB",
}

# A collection is (label, what names it, its one reader, the allow-ledger).
COLLECTIONS = (
    ("exported articles", re.compile(r"data/derived/articles|ARTICLES_DIR"),
     "export.corpus.load_corpus", EXPORTED_DIRECT),
    ("raw source pages", re.compile(r"data/raw/wikisource|RAW_DIR"),
     "source_pages.load_pages", RAW_DIRECT),
)

_ENUMERATES = re.compile(r"\.glob\(|glob\.glob\(|os\.listdir\(|\.iterdir\(|os\.scandir\(")


def _code_only(src: str) -> str:
    """`src` with comments and docstrings blanked, everything else intact.

    The detector must read CODE, not prose: `source_pages.py`'s docstring
    explains itself by contrast with `data/derived/articles`, and matching that
    sentence accused the raw reader of reading the exported corpus.  String
    LITERALS stay — `Path("data/derived/articles")` is the very thing being
    looked for — so only comments and free-standing strings go.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    lines = src.split("\n")
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for i in range(node.lineno - 1, (node.end_lineno or node.lineno)):
                lines[i] = ""
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in lines)


def _sources():
    for pat in ("src/**/*.py", "tools/**/*.py"):
        for f in ROOT.glob(pat):
            if "__pycache__" in str(f) or "_scratch" in str(f):
                continue
            yield (str(f.relative_to(ROOT)).replace("\\", "/"),
                   _code_only(f.read_text(encoding="utf-8", errors="replace")))


def _strays(names, ledger):
    return {rel: len(_ENUMERATES.findall(src))
            for rel, src in _sources()
            if rel not in ledger and names.search(src) and _ENUMERATES.search(src)}


def test_each_corpus_is_read_through_its_one_reader():
    problems = []
    for label, names, reader, ledger in COLLECTIONS:
        strays = _strays(names, ledger)
        if strays:
            problems.append(
                "%s — these name the %s directory AND enumerate a directory, so "
                "they are presumed to read it outside `%s`:\n%s"
                % (label, label, reader,
                   "\n".join("  %s (%d call(s))" % (f, n)
                             for f, n in sorted(strays.items()))))
    assert not problems, (
        "\n\n".join(problems) +
        "\n\nEither read through the one reader — which applies the exclusion rule "
        "and RAISES on an unreadable payload — or add the file to that "
        "collection's ledger with the reason it can be trusted to state its own "
        "coverage.")


def test_both_readers_still_refuse_to_skip():
    """Guard on the guard: if either loader ever became lenient, the ratchet above
    would be enforcing a rule that no longer buys anything."""
    import inspect

    from britannica.export.corpus import load_corpus
    from britannica.source_pages import load_pages

    for fn, name in ((load_corpus, "load_corpus"), (load_pages, "load_pages")):
        src = inspect.getsource(fn)
        assert "failures.append" in src, "%s no longer records failures" % name
        assert "raise" in src, "%s no longer raises on failure — the skip is back" % name


def test_the_raw_reader_applies_corrections():
    """Corrections are part of READING, not something each caller re-applies.
    `corrections.py` names three stages that "must re-apply" them and warns that a
    stage which forgets "silently no-ops corrections on its path" — the reader is
    what makes that unforgettable ([[feedback_corrections_json]])."""
    import inspect

    from britannica.source_pages import load_pages
    assert "apply_corrections" in inspect.getsource(load_pages)


def test_no_ledger_carries_ghosts():
    """A ledger entry for a file that no longer reads the directory is a stale
    claim — the same rot as a doc asserting a net that does not run."""
    ghosts = []
    for label, names, _reader, ledger in COLLECTIONS:
        for rel in ledger:
            f = ROOT / rel
            if not f.exists():
                ghosts.append("%s: %s (file is gone)" % (label, rel))
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            if not names.search(src):
                ghosts.append("%s: %s (no longer names the directory)" % (label, rel))
    assert not ghosts, "stale ledger entries:\n  " + "\n  ".join(ghosts)
