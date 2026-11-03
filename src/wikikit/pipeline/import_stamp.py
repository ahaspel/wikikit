"""Are the imported source pages still what the import would store now?

A rebuild reuses `source_pages` by default — the fetched pages are static —
but the import also lays the book's supplied pages over them and its patches
on them (`source_pages.load_payloads`), and those change.  A rebuild that
reused pages imported before them built none of it: 433 patched pages and
three pages held for missing text, an hour's rebuild, nothing in it
(2026-11-03).

`write` records a fingerprint of everything the import reads, after a full
import; `check` compares it with the inputs as they stand, and fails — before
the rebuild touches anything — when they differ or nothing was recorded.

    uv run python -m wikikit.pipeline.import_stamp write|check
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from wikikit.corpora import current_corpus
from wikikit.source_pages import RAW_DIR, SUPPLIED_DIR


def _stamp_path() -> Path:
    return Path(current_corpus().derived("source_pages_import.json"))


def import_inputs() -> dict:
    """What the import reads: the supplied pages and the patches by content;
    the fetched pages by name, size and time (37k static files, refetched
    whole when they change)."""
    def whole(paths):
        h = hashlib.sha256()
        for p in paths:
            h.update(p.as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
        return h.hexdigest()[:16]

    book = current_corpus()
    patches = [book.data("patches.json")] if book.has_data("patches.json") else []
    supplied = sorted(Path(SUPPLIED_DIR).rglob("*.json")) if Path(SUPPLIED_DIR).exists() else []
    h = hashlib.sha256()
    for p in sorted(Path(RAW_DIR).rglob("*.json")):
        st = p.stat()
        h.update(f"{p.as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return {"patches": whole([Path(p) for p in patches]), "supplied": whole(supplied),
            "fetched": h.hexdigest()[:16]}


def main() -> int:
    what = sys.argv[1:2]
    if what == ["write"]:
        _stamp_path().parent.mkdir(parents=True, exist_ok=True)
        _stamp_path().write_text(json.dumps(import_inputs(), indent=1) + "\n", encoding="utf-8")
        print(f"  import stamped: {_stamp_path()}")
        return 0
    if what == ["check"]:
        if not _stamp_path().exists():
            print("  REFUSED: no record of what the imported source pages were made from "
                  "— rebuild with --reimport")
            return 1
        then = json.loads(_stamp_path().read_text(encoding="utf-8"))
        now = import_inputs()
        moved = [k for k in now if then.get(k) != now[k]]
        if moved:
            print(f"  REFUSED: the imported source pages predate the {', '.join(moved)} as they stand "
                  "— rebuild with --reimport")
            return 1
        print("  imported source pages are current (fetched, supplied, patches)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
