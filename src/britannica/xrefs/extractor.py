import re

from britannica.markers import iter_al_markers, iter_ln_markers
from britannica.xrefs.normalizer import normalize_xref_target


def _is_plausible_target(target: str) -> bool:
    """Reject targets that are clearly not article references."""
    if not target:
        return False
    # Reject single common words that result from broken markup or overcapture
    if target.lower() in (
        "a", "above", "also", "although", "an", "and", "at", "bel", "below",
        "but", "by", "dr", "emperor", "for", "founded", "further",
        "he", "her", "his", "in", "is", "it", "its",
        "not", "of", "on", "or", "s", "son", "spread",
        "the", "their", "these", "they", "this", "those", "to",
        "under", "was", "were", "which", "with",
    ):
        return False
    # Reject very short targets (1-2 chars) — almost always noise
    if len(target) <= 2:
        return False
    # Reject absurdly long targets (table content parsed as xrefs)
    if len(target) > 200:
        return False
    # Reject targets that start with common words (sentence fragments, not titles)
    if re.match(r"(?i)^(?:also|although|and|especially|for|further|particularly|separate|the)\b", target):
        return False
    # Reject bibliographic citations (contain numbers, volume refs, page refs)
    if re.search(r"\b(?:p\.|pp\.|vol\.|Ber\.|Journ\.|Proc\.|Hist\.|Dict\.|Biog\.|Gesch\.|Zeits\.|\d{4})", target):
        return False
    # Reject bibliographic-style references (author name + title)
    if re.search(r"'s\s+(Dict|Hist|Bibl|Life|Lives|Memoir)", target):
        return False
    # Reject targets with stray semicolons (broken markup)
    if ";" in target:
        return False
    # Reject Wikisource cross-project / language-prefix targets:
    # ``:sv:Antiqvarisk Tidskrift för Sverige`` (Swedish Wikisource),
    # ``:de:...``, ``:fr:...``, etc.  These are inter-project links,
    # not EB1911 article references.
    if re.match(r"^\s*:[a-z]{2,3}:", target, re.IGNORECASE):
        return False
    # Reject targets ending in a single uppercase letter (with or
    # without trailing period): ``See Rev. E``, ``See Miss A``,
    # ``See Sir J`` — the trailing letter is a person's first
    # initial, the surface text was truncated mid-name by the
    # extractor's pattern.  Single uppercase letter is never a real
    # article title.
    if re.search(r"\b[A-Z]\.?\s*$", target) and len(target) <= 12:
        return False
    # Reject legal-citation residue: ``R.S.C., O. xliii.`` shape —
    # comma-separated all-caps initialism followed by a period+lower-
    # roman fragment.  These get extracted from cell-table tabular
    # citations that the loose ``See X`` pattern grabs.
    if re.search(r"^[A-Z]\.[A-Z]\.[A-Z]\.,?\s*[A-Z]?\.\s*[ivxlcdm]+\b",
                 target, re.IGNORECASE):
        return False
    return True


def extract_xrefs(text: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen_by_type: set[tuple[str, str]] = set()   # (normalized, type)
    seen_targets: set[str] = set()               # normalized only — dedupe
                                                 # "see" or "see_also" lists
                                                 # whose target already
                                                 # appeared as a link xref.

    def _add(surface: str, target: str, xref_type: str, display: str,
             window: bool = False) -> None:
        normalized = normalize_xref_target(target)
        if not normalized:
            return
        # A see/see_also entry duplicating an existing link is redundant.
        if xref_type in ("see", "see_also") and normalized in seen_targets:
            return
        key = (normalized, xref_type)
        if key in seen_by_type:
            return
        seen_by_type.add(key)
        seen_targets.add(normalized)
        rec = {
            "surface_text": surface.strip(),
            "normalized_target": normalized,
            "xref_type": xref_type,
            "display": display,
        }
        if window:
            rec["window"] = True
        results.append(rec)

    # Link markers are implicit cross-references, read through THE «LN» reader
    # (`markers.iter_ln_markers`).  The display is the RECURSED slot, so the
    # `([^«]*)` display group that used to live here silently dropped every
    # marked-up cross-reference (`«SC»Parasitic Diseases«/SC»`): no xref, so
    # nothing bound a target, so the bake stripped the link to plain text.
    # The optional [kind] slot: `w` marks a producer-stamped (q.v.) WINDOW —
    # an UNASSERTED extent the resolver cuts against the title index (same
    # trusted link policy).
    for m in iter_ln_markers(text):
        target = m.target.strip()
        surface = text[m.start:m.end]
        if m.kind == "w":
            # A window bypasses the plausibility armor: it is DELIBERATELY a
            # raw clause span ("and Plato"); junk windows are filtered by the
            # resolver's index cuts, not by extraction heuristics.
            _add(surface, target, "link", m.display, window=True)
        elif m.kind in ("see", "see_also"):
            # A see-window: same bypass, untrusted policy (abstain-default).
            _add(surface, target, m.kind, m.display, window=True)
        elif _is_plausible_target(target):
            _add(surface, target, "link", m.display)

    # «AL» is the surviving [[Author:…]] marker — 5.4 resolves the contributor
    # SIGNOFFS and leaves the rest for us.  Its target names a PERSON, not an
    # article title, so it is its own kind: the resolver matches a surname
    # against EB's surname-first titles instead of running the article ladder.
    for m in iter_al_markers(text):
        target = m.target.strip()
        if _is_plausible_target(target):
            _add(text[m.start:m.end], target, "author", m.display)

    return results
