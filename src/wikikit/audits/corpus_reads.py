"""Each corpus has ONE reader — the instrument that finds a module reading it
directly.

`export.corpus.load_corpus` reads the EXPORTED articles; `source_pages.load_pages`
reads the RAW wikisource pages.  Both are total: they apply their own exclusion
rule and RAISE on a payload they cannot read.  Fifteen modules once wrote the
exported read themselves and nine the raw read — a glob, a hand-spelled
exclusion list, and `except Exception: continue`, which turns a page the tool
could not read into a page it reports nothing about.

DETECTION IS FILE-LEVEL ON PURPOSE.  Matching the path and the `glob(` on ONE
LINE let `ART = "data/derived/articles"` two lines up defeat it.  A module that
NAMES a corpus directory and ENUMERATES a directory is presumed to be reading
that corpus, whatever the spelling.  The honest exceptions — readers that go
direct and state their own coverage — are each repository's ledger
(tests/ledgers/corpus_reads.json): collection label -> {file: reason}.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# A collection: (label, what names it, its one reader).  `derived("articles"…)`
# since wikikit step 3: the book owns its output root, so a reader NAMES the
# directory through it.
COLLECTIONS = (
    ("exported articles",
     re.compile(r"data/derived/articles|ARTICLES_DIR|derived\(\s*[\"']articles[\"']"),
     "export.corpus.load_corpus"),
    ("raw source pages", re.compile(r"data/raw/wikisource|RAW_DIR"),
     "source_pages.load_pages"),
)

# This module NAMES both directories and ENUMERATES — its own source is the
# very shape it hunts — so it leaves itself out, as the recursion audit does.
SELF = Path(__file__).resolve()

ENUMERATES = re.compile(r"\.glob\(|glob\.glob\(|os\.listdir\(|\.iterdir\(|os\.scandir\(")


def code_only(src: str) -> str:
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


def sources(root: Path):
    for pat in ("src/**/*.py", "tools/**/*.py"):
        for f in root.glob(pat):
            if "__pycache__" in str(f) or "_scratch" in str(f) or f.resolve() == SELF:
                continue
            yield (str(f.relative_to(root)).replace("\\", "/"),
                   code_only(f.read_text(encoding="utf-8", errors="replace")))


def strays(root: Path, names, allowed) -> dict[str, int]:
    return {rel: len(ENUMERATES.findall(src))
            for rel, src in sources(root)
            if rel not in allowed and names.search(src) and ENUMERATES.search(src)}


def problems(root: Path, ledger) -> list[str]:
    """One message per collection read outside its reader and its ledger."""
    out = []
    for label, names, reader in COLLECTIONS:
        found = strays(root, names, ledger.get(label, {}))
        if found:
            out.append(
                "%s — these name the %s directory AND enumerate a directory, so "
                "they are presumed to read it outside `%s`:\n%s"
                % (label, label, reader,
                   "\n".join("  %s (%d call(s))" % (f, n)
                             for f, n in sorted(found.items()))))
    return out


def stale(root: Path, ledger) -> list[str]:
    """Ledger entries whose file is gone or no longer names its directory."""
    out = []
    for label, names, _reader in COLLECTIONS:
        out += [f"{label}: {g}" for g in ghosts(root, names, ledger.get(label, {}))]
    return out


def ghosts(root: Path, names, allowed) -> list[str]:
    out = []
    for rel in allowed:
        f = root / rel
        if not f.exists():
            out.append(f"{rel} (file is gone)")
        elif not names.search(f.read_text(encoding="utf-8", errors="replace")):
            out.append(f"{rel} (no longer names the directory)")
    return out
