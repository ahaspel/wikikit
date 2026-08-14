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