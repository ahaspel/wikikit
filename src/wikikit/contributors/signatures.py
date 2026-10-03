"""A book's signature templates: the template a transcriber writes at an
article's foot, and the initials the printed page shows there.

Some books are signed through one template per contributor — the DNB's
`{{DNB AHG}}` prints "A. H. G." — and what each prints cannot be worked out
from its name (`{{DNB JTt}}` prints "J. T-t.", `{{DNB FMO'D}}` "F. M. O'D.").
So the book declares the table (``signatures.json``: template name ->
``{"printed": initials}``), and a template found in it IS a signature: the
walker's CONTRIBUTOR_FOOTER, rendered as the page prints it.  A book with no
table has none of these, and its own signature forms are untouched.

Names match as Wikisource matches them: underscores are spaces, runs of
space are one; case is kept — `{{DNB JT-t}}` and `{{DNB JT-T}}` are two
templates printing two different signatures.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from wikikit.corpora import current_corpus

_BARE = re.compile(r"\{\{\s*([^{}|]+?)\s*\}\}\Z")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


@lru_cache(maxsize=None)
def _table(book_key: str) -> dict[str, str]:
    book = current_corpus()
    if not book.has_data("signatures.json"):
        return {}
    raw = json.loads(book.data("signatures.json").read_text(encoding="utf-8"))
    return {_norm(name): entry["printed"] for name, entry in raw.items() if entry.get("printed")}


def printed_signature(raw: str) -> str | None:
    """The initials a bare signature template prints (`{{DNB AHG}}` ->
    "A. H. G."), or None when ``raw`` is not one of the book's signatures."""
    m = _BARE.match(raw.strip())
    if not m:
        return None
    return _table(current_corpus().key).get(_norm(m.group(1)))
