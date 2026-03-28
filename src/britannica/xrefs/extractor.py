import re

from britannica.xrefs.normalizer import normalize_xref_target


def extract_xrefs(text: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []

    patterns = [
        ("see_also", re.compile(r"\bSee also ([A-Z][A-Z\s\-]+)\b")),
        ("see", re.compile(r"\bSee ([A-Z][A-Z\s\-]+)\b")),
    ]

    for xref_type, pattern in patterns:
        for match in pattern.finditer(text):
            surface = match.group(0).strip()
            target = match.group(1).strip()

            results.append(
                {
                    "surface_text": surface,
                    "normalized_target": normalize_xref_target(target),
                    "xref_type": xref_type,
                }
            )

    return results