"""Single source of truth for transcription-typo corrections.

`data/corrections.json` contains per-volume literal `{from, to}` text
replacements applied to source-page wikitext before downstream pipeline
stages see it.  Three stages need these corrections applied:

* `prepare_wikitext` — corrects `page.wikitext` consumed by
  detect_boundaries, transform_articles, etc.
* `extract_contributors` — reads `raw_text` from JSON files directly
  (bypassing the SourcePage.wikitext path) when scanning footer
  initials, so it must re-apply corrections to its input.
* `build_contributor_table` (Tools) — same shape as
  extract_contributors; reads raw JSON and needs corrections.

Without a shared utility each stage either duplicates the logic (the
prior state — three byte-identical copies of `_apply_corrections`)
or silently no-ops corrections on its path.

Format of `data/corrections.json`:

  {
    "1:493": [{"from": "...", "to": "..."}, ...],
    "6:680": [...]
  }

Keys are `"{volume}:{page}"` strings.  Lookup is by volume prefix; the
page number is informational (helps a human locate the source typo)
and doesn't affect application order.
"""

from __future__ import annotations

import json

_cache: dict | None = None


def load_corrections() -> dict:
    """The book's `corrections.json`, loaded once.

    A book that does not DECLARE corrections has none — the DNB, for one,
    which previously shared this path and would have been handed EB1911's.  A
    book that declares them and has lost the file fails: this used to return
    `{}` for an absent file, which made "corrections silently not applied" look
    exactly like "no corrections needed"."""
    global _cache
    if _cache is not None:
        return _cache
    from wikikit.corpora import current_corpus
    book = current_corpus()
    if not book.has_data("corrections.json"):
        _cache = {}
        return _cache
    with book.data("corrections.json").open(encoding="utf-8") as f:
        _cache = json.load(f)
    return _cache


def apply_corrections(text: str, volume: int) -> str:
    """Apply every `corrections.json` entry whose key begins with
    `"{volume}:"` to `text`.  Each entry is a literal `str.replace`,
    so applying twice is a no-op."""
    corrs = load_corrections()
    vol_prefix = f"{volume}:"
    for key, entries in corrs.items():
        if not key.startswith(vol_prefix):
            continue
        if not isinstance(entries, list):
            continue
        for c in entries:
            if isinstance(c, dict) and "from" in c and "to" in c:
                text = text.replace(c["from"], c["to"])
    return text


def reset_cache() -> None:
    """Test-only: clear the cache so a changed corrections file is
    re-read within a single process."""
    global _cache
    _cache = None
