import re


def normalize_xref_target(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    # Interwiki / namespace prefix (w:/wikt:/Portal:/…): strip a single leading token +
    # colon with NO space after (distinct from the "Europe: History" section colon
    # below) so the bare name resolves against our corpus.  "Resolve links, not source
    # fidelity" — maximize internal matches.
    text = re.sub(r"^[^\s:]+:(?=\S)", "", text)
    # Section links can arrive in two forms:
    #   "Europe#History"  (wiki anchor syntax)
    #   "Europe: History" (editorial colon form)
    # Normalize both to "ARTICLE: SECTION" so they collapse to one entry.
    text = re.sub(r"\s*#\s*", ": ", text)
    # Fold the Æ ligature to AE — a pure canonicalization (unambiguous in
    # English), so "Ægean"/"Aegean" and "Encyclopædia"/"Encyclopaedia" resolve
    # as one entry.  Was a bespoke pre-step in the Reader's Guide title
    # resolver, now shared by every caller ([[project_resolver_consolidation]]).
    return _fold_typography(text.upper().replace("Æ", "AE"))


# Typography the two sides of a reference spell differently while meaning one
# name.  395 link targets missed a filed title on nothing else: `O'Neill` for
# `O’NEILL`, `Napoleon I.` for `NAPOLEON I`, `Constitution of Athens` for
# `“CONSTITUTION OF ATHENS”`, `Sea Power` for `SEA-POWER`, `Aemilia, Via` for
# `AEMILIA VIA`.  Those links resolved to nothing and rendered as flat text.
#
# Applied to the FINISHED key, so it can only ever merge — anything that matched
# before still matches, because both sides get the same further reduction.  It
# introduces no ambiguity either: across all 35,638 filed titles this collapses
# ZERO pairs of distinct articles onto one key.
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'",
                              "´": "'", "`": "'"})
_QUOTES_RE = re.compile(r"[“”\"]")
_DASHES_RE = re.compile(r"[-‐‑‒–—]")
_TRAILING_DOTS_RE = re.compile(r"\.+\s*$")


class NormalizedIndex:
    """A title index whose key IS the normalized form, on BOTH sides.

    Normalizing at the call site is a rule every lookup has to remember, and the
    export's title map did not: it was keyed by `title.upper()` while the xref
    map beside it was keyed by `normalize_xref_target`, so the same string was a
    filed title to one and unknown to the other.  `{{EB9link|Napoleon I.}}` found
    nothing, and `swapped_link` could not recognise `Queen Anne's Bounty` as a
    title, so neither the argument-order recovery nor the spelling fix ever ran
    on a typographic variant.

    Keeping the normalization INSIDE means a raw lookup is not expressible.

    Two questions are asked of this index and they want different answers, so
    both are named here rather than left to each caller's `.upper()`:

    ``get`` — "what does this reference DENOTE?"  Recall: the typography fold
    applies, so `Napoleon I.` finds `NAPOLEON I`.

    ``get_as_written`` — "is this string, AS PRINTED, a filed title?"  Precision:
    no fold, because the answer decides whether to replace what the reader sees.
    Under the fold it says yes to `Menelek II.` and `Justinian I.`, and the swap
    then shows `Menelek` and `Justinian` — dropping a regnal number the page set
    in type.  A resolver may guess widely about where a link POINTS; nothing may
    guess about what it SAYS.
    """

    __slots__ = ("_by_key", "_by_exact")

    def __init__(self, pairs=()):
        self._by_key, self._by_exact = {}, {}
        for key, value in pairs:
            self.add(key, value)

    def add(self, key: str, value) -> None:
        """First writer wins — callers order their input to make that stable."""
        self._by_key.setdefault(normalize_xref_target(key), value)
        self._by_exact.setdefault(key.strip().upper(), value)

    def get(self, key: str, default=None):
        return self._by_key.get(normalize_xref_target(key), default)

    def get_as_written(self, key: str, default=None):
        return self._by_exact.get((key or "").strip().upper(), default)

    def __contains__(self, key: str) -> bool:
        return normalize_xref_target(key) in self._by_key

    def __len__(self) -> int:
        return len(self._by_key)


def _fold_typography(key: str) -> str:
    key = key.translate(_APOSTROPHES)
    key = _QUOTES_RE.sub("", key)
    # A hyphen and a comma are word SEPARATION, not identity: EB1911 files
    # `SEA-POWER` and `AEMILIA VIA` where the reference writes `Sea Power` and
    # `Aemilia, Via`.
    key = _DASHES_RE.sub(" ", key)
    key = key.replace(",", " ")
    # `Napoleon I.` is `NAPOLEON I` — the stop belongs to the sentence.
    key = _TRAILING_DOTS_RE.sub("", key)
    return re.sub(r"\s+", " ", key).strip()