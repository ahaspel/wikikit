import unicodedata


# Print/transcription artifacts that should be normalized to their modern
# ASCII or Latin-1 equivalents. The source text uses these for visual
# reasons (fullwidth forms in chem equations, ligature glyphs for
# avoirdupois units) that no longer serve any purpose once we control
# our own typography.
_PRINT_ARTIFACTS: dict[str, str] = {
    "＝": "=",       # FULLWIDTH EQUALS SIGN
    "＋": "+",       # FULLWIDTH PLUS SIGN
    "－": "-",       # FULLWIDTH HYPHEN-MINUS
    "＜": "<",       # FULLWIDTH LESS-THAN SIGN
    "＞": ">",       # FULLWIDTH GREATER-THAN SIGN
    "℔": "lb",      # L B BAR SYMBOL (avoirdupois pound)
    "℥": "oz",      # OUNCE SIGN
    "℈": "scruple", # SCRUPLE
    "✕": "×",  # MULTIPLICATION X (dingbat) → MULTIPLICATION SIGN
}

_PRINT_ARTIFACTS_TABLE = str.maketrans(
    {k: v for k, v in _PRINT_ARTIFACTS.items() if len(k) == 1 and len(v) == 1}
)


def normalize_unicode(text: str) -> str:
    # NFC (not NFKC) to preserve Unicode subscripts/superscripts (₂, ³, etc.)
    # NFKC would decompose them back to regular digits
    return unicodedata.normalize("NFC", text)


def replace_print_artifacts(text: str) -> str:
    """Normalize rare/obscure glyphs that were typographic choices in the
    1911 source to modern ASCII or Latin-1 equivalents.

    Targets the codepoints identified in audit_rare_unicode.py as safe
    substitutions — ones where replacement carries no loss of meaning
    and gains font portability, copy-paste, and search indexability.
    """
    if not text:
        return text
    # Fast path: table-based single-char replacements
    text = text.translate(_PRINT_ARTIFACTS_TABLE)
    # Multi-char replacements (℔→lb, ℥→oz, ℈→scruple)
    for src, dst in _PRINT_ARTIFACTS.items():
        if len(src) == 1 and len(dst) > 1 and src in text:
            text = text.replace(src, dst)
    return text