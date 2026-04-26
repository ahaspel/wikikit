"""Resolve a free-text person name to a canonical EB11 contributor.

Shared across surfaces that need to identify contributor mentions in
free text (the article viewer's footer-credit lookup, the Reader's
Guide's "by <name>" citations, etc.).

The resolver takes a flat list of canonical full names (as stored in
data/derived/articles/contributors.json) and builds a lookup index
that tolerates common variants: missing middle names, initials vs
spelled-out first names, honorifics (Dr., Prof., Miss, Sir, Rev.),
and ambiguous last-name-only forms. It deliberately does NOT guess
when the DB is ambiguous and the input gives no disambiguating
signal — callers get None rather than a wrong match.
"""

from __future__ import annotations

import re

_TITLE_RE = re.compile(
    r"^(?:(?:Prof(?:essor)?\.?|Dr\.?|Mr\.?|Mrs\.?|Miss|Sir|Rev(?:erend)?\.?|The)\s+)+",
    re.IGNORECASE,
)
_INITIAL_RE = re.compile(r"^([A-Za-z])\.?$")


def _strip_title(name: str) -> str:
    return _TITLE_RE.sub("", name).strip()


def _strip_trailing_paren(name: str) -> str:
    """Drop a trailing "(…)" disambiguator — DB names like
    "Miss Mary Bateson (1865–1906)" or "Lady Broome (Mary Anne Broome)"
    carry lifespan or maiden-name qualifiers that won't appear in Guide
    references. Stripping them lets the lookup still match.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _normalise_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parts(name: str) -> list[str]:
    """Tokenise a name, dropping honorifics and trailing parens."""
    return _strip_title(
        _strip_trailing_paren(_normalise_spaces(name))
    ).split(" ")


def _first_letter(token: str) -> str | None:
    m = _INITIAL_RE.match(token)
    if m:
        return m.group(1).upper()
    if token and token[0].isalpha():
        return token[0].upper()
    return None


class ContributorResolver:
    """Given a list of canonical contributor full names, resolve free
    text names to the matching canonical form (or None)."""

    def __init__(self, full_names: list[str]) -> None:
        self._canonical: list[str] = []
        # exact-match key (lowercased, stripped of honorifics/spaces)
        self._by_exact: dict[str, str] = {}
        # last_name_lower -> list of canonical full names
        self._by_last: dict[str, list[str]] = {}

        for raw in full_names:
            if not raw or not isinstance(raw, str):
                continue
            canonical = raw.strip()
            self._canonical.append(canonical)
            # Exact-match keys for both the raw form and the stripped
            # form (honorifics + trailing paren removed) so inputs can
            # match either way.
            key_raw = _strip_title(canonical).lower()
            key_clean = _strip_trailing_paren(key_raw).lower()
            self._by_exact.setdefault(key_raw, canonical)
            self._by_exact.setdefault(key_clean, canonical)
            parts = _parts(canonical)
            if not parts:
                continue
            last = parts[-1].lower()
            self._by_last.setdefault(last, []).append(canonical)

    @staticmethod
    def _first_token_matches(input_first: str, cand_first: str) -> int:
        """Score the likelihood that `input_first` refers to `cand_first`.

        2 = full-name match ("Donald" vs "Donald").
        1 = initial match ("D." vs "Donald", or "Donald" vs "D.").
        0 = no match.
        """
        inp = input_first.rstrip(".").lower()
        cand = cand_first.rstrip(".").lower()
        if not inp or not cand:
            return 0
        if inp == cand:
            return 2
        # Initial vs spelled-out
        if len(inp) == 1 and cand.startswith(inp):
            return 1
        if len(cand) == 1 and inp.startswith(cand):
            return 1
        return 0

    def resolve(self, text: str) -> str | None:
        """Return the canonical full name for `text`, or None."""
        if not text:
            return None
        stripped = _strip_title(_normalise_spaces(text))
        if not stripped:
            return None

        # Strategy 1: exact case-insensitive match after stripping titles.
        key = stripped.lower()
        if key in self._by_exact:
            return self._by_exact[key]

        parts = stripped.split(" ")

        # Strategy 2: single-word input — rely on unique last-name match.
        if len(parts) == 1:
            bucket = self._by_last.get(parts[0].lower(), [])
            if len(bucket) == 1:
                return bucket[0]
            return None

        last = parts[-1].lower()
        bucket = self._by_last.get(last, [])
        if not bucket:
            return None
        if len(bucket) == 1:
            return bucket[0]

        input_first = parts[0]
        input_middles = [p for p in parts[1:-1]]

        # Strategy 3: score each last-name candidate by first-name and
        # middle-initial compatibility, then pick the uniquely best one.
        scored: list[tuple[int, int, str]] = []
        for cand in bucket:
            cand_parts = _parts(cand)
            if len(cand_parts) < 2:
                continue
            first_score = self._first_token_matches(input_first, cand_parts[0])
            # If the input's first token is clearly a different name from
            # the candidate's first (e.g. "Donald" vs "Duncan"), skip.
            if first_score == 0:
                continue
            # Middle-name overlap: count input middles that appear in
            # candidate's middle tokens (initial-compatible).
            middle_score = 0
            cand_middles = cand_parts[1:-1]
            for im in input_middles:
                for cm in cand_middles:
                    if self._first_token_matches(im, cm) > 0:
                        middle_score += 1
                        break
            scored.append((first_score, middle_score, cand))

        if not scored:
            return None
        # Prefer full-name match over initial match; then higher middle score.
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        best = scored[0]
        if len(scored) == 1:
            return best[2]
        # Tie-break: unique best means the #2 slot is strictly worse on
        # (first_score, middle_score). If the top two are tied, we can't
        # disambiguate honestly.
        if (best[0], best[1]) > (scored[1][0], scored[1][1]):
            return best[2]
        return None


def make_resolver_from_json(contributors: list[dict]) -> ContributorResolver:
    """Build a resolver from the data/derived/articles/contributors.json
    shape (list of {full_name, initials, ...})."""
    names = [
        c.get("full_name") for c in contributors
        if c.get("full_name")
    ]
    return ContributorResolver([n for n in names if isinstance(n, str)])
