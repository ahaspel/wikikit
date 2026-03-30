import unicodedata


def normalize_unicode(text: str) -> str:
    # NFC (not NFKC) to preserve Unicode subscripts/superscripts (₂, ³, etc.)
    # NFKC would decompose them back to regular digits
    return unicodedata.normalize("NFC", text)