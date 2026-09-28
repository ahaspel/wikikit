"""Wikisource title index — the existence-check behind external `«XL»` links.

Loaded once from the enwikisource all-titles dump
(``data/external/enwikisource-latest-all-titles.gz``).  Before the wikilink
resolver emits an outbound link to a Wikisource page, it verifies the constructed
target against this set — so we never mint a link to a page that doesn't exist
(the user's "what guarantee do these resolve?" — this is the guarantee).

Only the namespaces our links target are loaded: main (0 — works and their
subpages) and Portal (100).  The dump's bulk is ns 104 (3.7M scan Pages), skipped.
Interwiki targets (Wikipedia/Wiktionary/Wikidata) are NOT here — they can't be
verified against a Wikisource dump, so the resolver strips them rather than guess.
"""
from __future__ import annotations

import csv
import functools
import gzip
from pathlib import Path

_DUMP = Path("data/external/enwikisource-latest-all-titles.gz")

# namespace number → wikilink prefix, for the namespaces external links target.
_NS_PREFIX = {0: "", 100: "Portal"}


@functools.lru_cache(maxsize=1)
def _titles() -> frozenset[str]:
    """Valid WS page titles (underscored, namespace-prefixed) in the linked
    namespaces — built once and cached for the run."""
    if not _DUMP.exists():
        # No dump present (it's a gitignored ~20 MB download) — degrade gracefully:
        # no external verification, so every external link strips to display text.
        # Acquire with:  curl -L -o data/external/enwikisource-latest-all-titles.gz \
        #   https://dumps.wikimedia.org/enwikisource/latest/enwikisource-latest-all-titles.gz
        return frozenset()
    out: set[str] = set()
    csv.field_size_limit(10 ** 7)
    with gzip.open(_DUMP, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        next(reader, None)  # header: page_namespace, page_title
        for row in reader:
            if len(row) < 2:
                continue
            try:
                ns = int(row[0])
            except ValueError:
                continue
            prefix = _NS_PREFIX.get(ns)
            if prefix is None:
                continue
            out.add(f"{prefix}:{row[1]}" if prefix else row[1])
    return frozenset(out)


def is_ws_page(target: str) -> bool:
    """True if ``target`` (a wikilink target, spaces or underscores) is a real
    Wikisource page in a namespace we link into."""
    return target.strip().replace(" ", "_") in _titles()
