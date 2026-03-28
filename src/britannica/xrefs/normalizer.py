import re


def normalize_xref_target(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text.upper()