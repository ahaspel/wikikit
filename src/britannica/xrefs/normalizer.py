import re


def normalize_xref_target(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    # Section links can arrive in two forms:
    #   "Europe#History"  (wiki anchor syntax)
    #   "Europe: History" (editorial colon form)
    # Normalize both to "ARTICLE: SECTION" so they collapse to one entry.
    text = re.sub(r"\s*#\s*", ": ", text)
    return text.upper()