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
    return text.upper().replace("Æ", "AE")