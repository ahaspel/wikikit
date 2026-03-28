import unicodedata


def normalize_unicode(text: str) -> str:
    return unicodedata.normalize("NFKC", text)