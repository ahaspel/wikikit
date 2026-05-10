"""Canonical-name resolution for contributor records.

Multi-source contributor data (vol 29 master Index, per-volume front
matter contributor tables, article-footer initials) routinely carries
the same author under spelling variants — punctuation differences
("Viscount St Cyres" vs "Viscount St. Cyres"), honorific prefixes
("Owen Charles Whitehouse" vs "Rev. Owen Charles Whitehouse"), curly
vs straight apostrophes ("O'Neill" vs "O'Neill"), or outright spelling
drift ("Edgcumbe" vs "Edgecumbe").  Without canonicalization each
variant becomes its own DB row at extract time.

`canonical_name()` is the single chokepoint for this:

  1. Apply Unicode normalization (NFKC + smart-quote folding +
     whitespace collapse) to handle punctuation/encoding variants
     deterministically.
  2. Look the normalized form up in `data/contributor_aliases.json`,
     a hand-vetted variant→canonical map for cases that aren't pure
     punctuation (spelling drift, prefix presence, etc.).
  3. Return the canonical name when matched, or the normalized name
     otherwise.

The JSON file lives in source-data, not the DB, so dedup decisions
survive `rebuild_all.sh`'s Phase 1 truncate.  Add new variants to the
JSON; never DB-mutate.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

_ALIASES_FILE = Path("data/contributor_aliases.json")
_aliases_cache: dict[str, str] | None = None


def _load_aliases() -> dict[str, str]:
    """Load and invert the aliases file: returns variant→canonical map.

    Both keys and values are pre-normalized (via `normalize`) so that
    lookups don't need to redo normalization on the alias keys.
    """
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache
    if not _ALIASES_FILE.exists():
        _aliases_cache = {}
        return _aliases_cache
    raw = json.loads(_ALIASES_FILE.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for canonical, variants in raw.get("aliases", {}).items():
        canonical_n = normalize(canonical)
        for v in variants:
            out[normalize(v)] = canonical_n
        # Identity entry — looking up the canonical itself returns itself.
        out[canonical_n] = canonical_n
    _aliases_cache = out
    return out


def normalize(name: str) -> str:
    """Normalize a name for matching and storage.

    - NFKC composes/decomposes Unicode forms consistently.
    - Smart quotes (U+2019, U+2018, U+201C, U+201D) fold to ASCII
      ' and ", so `Elizabeth O'Neill` and `Elizabeth O'Neill` collapse.
    - Backtick (U+0060) folds to apostrophe.
    - Whitespace collapsed to single spaces, trimmed.

    Reversible only in spirit — output is what the DB stores.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = (s.replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("`", "'"))
    s = " ".join(s.split())
    return s


def canonical_name(name: str) -> str:
    """Return the canonical form of `name`.

    First normalizes, then consults the variant→canonical map from
    `data/contributor_aliases.json`.  Names not listed as variants
    are returned in their normalized form unchanged.
    """
    if not name:
        return name
    aliases = _load_aliases()
    n = normalize(name)
    return aliases.get(n, n)


def reset_cache() -> None:
    """Test-only: clear the aliases cache so changes to the JSON
    file take effect within a single process."""
    global _aliases_cache
    _aliases_cache = None
