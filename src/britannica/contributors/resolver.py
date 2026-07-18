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
import unicodedata
from collections import defaultdict

_TITLE_RE = re.compile(
    r"^(?:(?:Prof(?:essor)?\.?|Dr\.?|Mr\.?|Mrs\.?|Miss|Sir|Rev(?:erend)?\.?|The)\s+)+",
    re.IGNORECASE,
)


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


def _score_first(input_first: str, cand_first: str) -> int:
    """Score whether `input_first` names the same person as `cand_first`:
    2 = full-name match ("Donald"/"Donald"), 1 = initial↔spelled-out
    ("D."/"Donald"), 0 = no match.  Shared by ContributorResolver and
    ContributorIndex."""
    inp = input_first.rstrip(".").lower()
    cand = cand_first.rstrip(".").lower()
    if not inp or not cand:
        return 0
    if inp == cand:
        return 2
    if len(inp) == 1 and cand.startswith(inp):
        return 1
    if len(cand) == 1 and inp.startswith(cand):
        return 1
    return 0


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
        return _score_first(input_first, cand_first)

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


def _name_core_tokens(name: str) -> list[str]:
    """The 'First Middle Last' token list of a name — honorifics, a trailing
    ``(…)`` qualifier, and a trailing ``, CREDENTIALS`` / ``; ORDER`` tail all
    removed, and each token stripped of edge punctuation — so surname
    extraction and scoring see the person, not the packaging ("Louis Bell,
    Ph.D" → ['Louis', 'Bell']; "Edward Cuthbert Butler; O.S.B" → [..., 'Butler'])."""
    n = _strip_trailing_paren(_strip_title(_normalise_spaces(name or "")))
    n = re.split(r"[;,]", n, 1)[0]
    toks = [t.strip(".,;") for t in n.split(" ")]
    return [t for t in toks if t]


def _norm_initials(initials: str) -> str:
    """Asterisk-preserving initials key.  Lazy import keeps resolver.py free of
    the extract-contributors dependency (and its import cycle)."""
    if not initials:
        return ""
    from britannica.pipeline.stages.extract_contributors import _normalize_initials
    return _normalize_initials(initials)


def _fold(s: str) -> str:
    """Diacritic-folded lowercase, with the Scottish patronymic prefix normalized
    (McLachlan / MacLachlan / M'Lachlan all → 'mclachlan') so a surname's spelling
    variants across sources compare equal — a signer's footer 'McLachlan' matches
    the front matter's 'M'Lachlan'.  The ≥3-letter-root guard keeps short
    look-alikes intact (Mace, Macy, Mack are not prefixed names)."""
    t = "".join(
        c for c in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(c)
    ).lower()
    return re.sub(r"^m(?:['’‘]|ac|c)([a-z]{3,})", r"mc\1", t)


class ContributorIndex:
    """The single owner of ``(name, initials) → contributor id`` resolution
    ([[project_contributor_resolver_consolidation]]).

    Built from records ``(id, full_name, [initials])``; resolves over two
    indexes — an ASTERISK-PRESERVING initials map (``V. C.`` and ``V. C.*`` are
    distinct people) and diacritic-folded surname buckets scored on first/middle
    names (via the shared ``_score_first``).

    It NEVER guesses ([[feedback_contributor_zero_false_positives]]): anything
    short of a confident, unique determination returns None; there is no
    abstain/guess knob.  A unique initials owner is trusted whenever its SURNAME
    agrees with the entry's (diacritic-folded) — same person despite first-name
    drift (Harry/Henry, ``W.``/William, Müller/Muller).  Only a surname MISMATCH
    signals a dropped-mark collision (Bell/Bénédite, Muir/Muther) where the
    initials are corrupt; then the name resolves the true owner — and if the
    name resolves nowhere, the initials owner stands (spelling drift,
    McNaught/M'Naught).
    """

    def __init__(self, records) -> None:
        self._by_id: dict[int, str] = {}
        self._by_initials: dict[str, list[int]] = defaultdict(list)
        self._by_surname: dict[str, list[int]] = defaultdict(list)
        self._core: dict[int, list[str]] = {}
        for cid, full_name, initials in records:
            self._by_id[cid] = full_name
            core = _name_core_tokens(full_name)
            self._core[cid] = core
            if core:
                self._by_surname[_fold(core[-1])].append(cid)
            for ini in initials:
                key = _norm_initials(ini)
                if key:
                    self._by_initials[key].append(cid)

    def _surname_of(self, cid: int) -> str:
        core = self._core.get(cid) or []
        return _fold(core[-1]) if core else ""

    def _match_count(self, core: list[str], cid: int) -> int:
        """How many of the entry's name tokens (first + each middle + surname)
        candidate ``cid`` matches — the tie-break between an initials-owner and a
        same-surname name candidate when their surnames disagree."""
        cc = self._core.get(cid) or []
        if not cc or not core:
            return -1
        n = 1 if _score_first(core[0], cc[0]) > 0 else 0
        cand_mid = cc[1:-1]
        n += sum(1 for im in core[1:-1]
                 if any(_score_first(im, cm) > 0 for cm in cand_mid))
        if _fold(core[-1]) == _fold(cc[-1]):
            n += 1
        return n

    def _best(self, core: list[str], ids: list[int]) -> int | None:
        """The unique best-scoring id among `ids` for the entry's core tokens —
        first name must be compatible (initial↔spelled-out counts); ties and
        no-first-match return None (never a guess)."""
        scored: list[tuple[int, int, int]] = []
        for cid in ids:
            cc = self._core.get(cid) or []
            if not cc:
                continue
            first_score = _score_first(core[0], cc[0])
            if first_score == 0:
                continue
            cand_middles = cc[1:-1]
            middle_score = sum(
                1 for im in core[1:-1]
                if any(_score_first(im, cm) > 0 for cm in cand_middles)
            )
            scored.append((first_score, middle_score, cid))
        if not scored:
            return None
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        if len(scored) == 1 or (scored[0][0], scored[0][1]) > (scored[1][0], scored[1][1]):
            return scored[0][2]
        return None

    def _resolve_name(self, core: list[str]) -> int | None:
        """Confident unique contributor for the entry's core tokens, by surname
        bucket + first/middle score; None when unknown or ambiguous."""
        if not core:
            return None
        return self._best(core, self._by_surname.get(_fold(core[-1]), []))

    def resolve(self, name: str | None = None,
                initials: str | None = None) -> int | None:
        """Resolve ``(name, initials)`` to a contributor id, or None."""
        init_ids = (self._by_initials.get(_norm_initials(initials), [])
                    if initials else [])
        core = _name_core_tokens(name) if name else []

        if init_ids:
            if len(init_ids) == 1:
                owner = init_ids[0]
                if not core:
                    return owner
                # The initials point to `owner`, but a name can share a surname
                # with a DIFFERENT person, and the true person can be stored under
                # an OCR-typo'd surname.  So bind whichever of the initials-owner
                # or a name-resolved candidate matches the entry's full name on
                # MORE tokens (first + each middle + surname); the initials-owner
                # keeps ties, as the default.  Name-first, over-trusting neither
                # signal — the surname alone is NOT enough (initials-overdependence
                # is how we misclassify):
                #  · SAVONAROLA signs Linda Mary Villari's initials but names her
                #    son "Luigi Villari" — same surname, yet Luigi matches
                #    first+surname (2) vs Linda's surname-only (1) → the NAME wins.
                #  · SCHUBERT names "William Henry Hadow" (first+middle+surname, 3)
                #    but signs Howell's "W. H. H." (first+middle, 2) → NAME wins.
                #  · HOBBES names "George Croom Robertson" but the philosopher is
                #    stored under the typo "Roberston": the owner matches
                #    first+MIDDLE Croom (2), the bucket's George *Scott* Robertson
                #    first+surname (2) — a tie, so the owner (same person, typo
                #    surname) stands, not the wrong George Robertson.
                named = self._resolve_name(core)
                if (named is not None and named != owner
                        and self._match_count(core, named)
                        > self._match_count(core, owner)):
                    return named
                return owner
            # Several owners at this initials key → the entry name must land
            # uniquely on one of them, else abstain.
            return self._best(core, init_ids) if core else None
        # No initials signal → confident name resolution, or None.
        return self._resolve_name(core)
