"""Extract the parent article name from a plate page's raw wikitext.

EB1911 plate pages carry the parent article's name as an explicit
raw-source marker in the page header.  This is structurally cleaner
than inferring the parent from the plate's title (``X, PLATE I``) or
from page proximity — the source is telling us directly which article
the plate illustrates.

Three patterns cover ~96% of plates:

* ``<section begin="ArticleName" />`` — explicit XML section attr,
  the highest-fidelity signal.
* ``{{rh|left|MIDDLE|right}}`` / ``{{RunningHeader|...}}`` /
  ``{{EB1911 Page Heading|...}}`` — running-head template, with the
  article name in the middle slot (often wrapped in ``{{x-larger|...}}``
  or ``{{fs|180%|...}}``).
* ``{{c|{{x-larger|ArticleName}}}}`` — centered larger title,
  typically paired with the section-begin marker.

The remaining ~4% of plates have no recognizable signal; for those the
caller falls back to the existing exact/prefix/proximity logic in
``_find_parent``.
"""
from __future__ import annotations

import re


_SECTION_BEGIN_RE = re.compile(
    r'<section\s+begin\s*=\s*"([^"]+)"\s*/?>', re.IGNORECASE)
_RH_RE = re.compile(
    r'\{\{(?:rh|RunningHeader|EB1911\s+Page\s+Heading)\|', re.IGNORECASE)
_C_XLARGER_RE = re.compile(
    r'\{\{c\|\s*\{\{x-larger\|([^}]+)\}\}\s*\}\}', re.IGNORECASE)

# Bogus section-attribute values that some plates use as a placeholder
# instead of the article name.  Skip them.
_PLACEHOLDER_RE = re.compile(r"^(?:S\d+|PLATE\d+|\d+)$", re.IGNORECASE)


def _balanced_args(text: str) -> list[str]:
    """Split a template body on ``|`` at brace-depth 0, preserving
    nested ``{{...}}``."""
    args: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth -= 1
            cur.append(ch)
        elif ch == "|" and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))  # trailing slot, even if empty
    return args


def _strip_formatting(s: str) -> str:
    """Unwrap formatting templates by taking the last pipe-separated
    arg.  Handles ``{{x-larger|EMBROIDERY}}`` (1 arg → EMBROIDERY) and
    ``{{fs|180%|BOOKBINDING}}`` (2 args → BOOKBINDING)."""
    s = s.strip()
    s = re.sub(r"'+", "", s)  # strip wikitext italics
    for _ in range(5):  # depth cap
        m = re.match(r"^\{\{[\w\-]+\s*\|", s)
        if not m:
            break
        body_start = m.end()
        depth, i = 2, body_start
        while i < len(s) and depth > 0:
            if s[i:i+2] == "{{":
                depth += 2
                i += 2
            elif s[i:i+2] == "}}":
                depth -= 2
                if depth == 0:
                    break
                i += 2
            else:
                i += 1
        if i >= len(s) or depth != 0:
            break
        if s[i+2:].strip():
            break  # template doesn't wrap the whole slot
        args = _balanced_args(s[body_start:i])
        if not args:
            break
        s = args[-1].strip()
    return s


def _parse_rh_middle(wikitext: str) -> str | None:
    """Pull the middle slot of the first running-head template."""
    m = _RH_RE.search(wikitext)
    if not m:
        return None
    start = m.end()
    depth, i = 2, start
    while i < len(wikitext) and depth > 0:
        if wikitext[i:i+2] == "{{":
            depth += 2
            i += 2
            continue
        if wikitext[i:i+2] == "}}":
            depth -= 2
            if depth == 0:
                break
            i += 2
            continue
        i += 1
    args = _balanced_args(wikitext[start:i])
    if len(args) < 3:
        return None
    return _strip_formatting(args[1].strip())


def extract_signals(wikitext: str) -> list[str]:
    """Return ordered candidate parent-article names from a plate's
    raw wikitext.  Ordered ``c`` → ``section`` → ``rh`` (``c`` is
    most consistently the parent article name across the corpus;
    ``rh`` sometimes carries a section/topic label instead, as in the
    AEGEAN CIVILIZATION plates)."""
    candidates: list[str] = []

    m = _C_XLARGER_RE.search(wikitext)
    if m:
        v = _strip_formatting(m.group(1)).upper().strip()
        if v and not _PLACEHOLDER_RE.match(v):
            candidates.append(v)

    m = _SECTION_BEGIN_RE.search(wikitext)
    if m:
        v = m.group(1).strip().upper()
        if v and not _PLACEHOLDER_RE.match(v):
            candidates.append(v)

    rh = _parse_rh_middle(wikitext)
    if rh:
        v = rh.upper().strip()
        if v and not _PLACEHOLDER_RE.match(v) and v not in candidates:
            candidates.append(v)

    # Deduplicate preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def normalize_variants(name: str) -> list[str]:
    """Return variant spellings to try when an exact-title lookup
    fails.  Handles plural/singular (ENAMELS↔ENAMEL), hyphenation
    (WOODCARVING↔WOOD-CARVING), and abbreviation (MANUSCRIPTS↔MSS)."""
    name = name.strip()
    out: list[str] = [name]
    if name.endswith("ES"):
        out.append(name[:-2])
    if name.endswith("S"):
        out.append(name[:-1])
    out.append(name + "S")
    if " " in name:
        out.append(name.replace(" ", "-"))
    if "-" in name:
        out.append(name.replace("-", " "))
        out.append(name.replace("-", ""))
    # Insert a hyphen at each candidate split point — covers
    # WOODCARVING → WOOD-CARVING without us having to know the split.
    if " " not in name and "-" not in name and len(name) >= 6:
        for i in range(3, len(name) - 2):
            out.append(f"{name[:i]}-{name[i:]}")
    out.append(name.replace("MANUSCRIPTS", "MSS"))
    out.append(name.replace("MSS", "MANUSCRIPTS"))
    # Strip trailing punctuation (SHIPBUILDING. → SHIPBUILDING).
    stripped = re.sub(r"[\.,;:—\-\s]+$", "", name)
    if stripped != name:
        out.append(stripped)
    # Take the prefix before an em-dash, " (", or comma — useful for
    # signals like ``SCULPTURE—FRENCH`` or ``CLIMATE AND CLIMATOLOGY``
    # subdivision names that aren't real article names by themselves.
    for sep in ("—", " (", ","):
        if sep in name:
            out.append(name.split(sep)[0].strip())

    # Dedupe.
    seen, dedup = set(), []
    for v in out:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            dedup.append(v)
    return dedup


def find_parent_by_signal(wikitext: str, plate_page: int,
                          non_plates) -> object | None:
    """Try each extracted signal × each normalization variant against
    the volume's non-plate articles; return the first match, preferring
    one whose page range contains the plate's page.

    ``non_plates`` is a list of Article objects in the same volume.
    Returns the matching Article or None.
    """
    if not wikitext:
        return None
    candidates = extract_signals(wikitext)
    if not candidates:
        return None

    by_title: dict[str, list] = {}
    for a in non_plates:
        by_title.setdefault(a.title.upper(), []).append(a)

    for cand in candidates:
        for variant in normalize_variants(cand):
            matches = by_title.get(variant.upper())
            if not matches:
                # Also try title-starts-with-variant (variant is a
                # short form: ALHAMBRA → ALHAMBRA, THE).
                matches = [a for a in non_plates
                           if a.title.upper().startswith(variant.upper() + ",")
                           or a.title.upper().startswith(variant.upper() + " (")
                           or a.title.upper().startswith(variant.upper() + " ")]
            if not matches:
                continue
            # Prefer one whose page range contains the plate's page.
            covering = [a for a in matches
                        if a.page_start <= plate_page <= a.page_end]
            if covering:
                return covering[0]
            # No coverage: nearest by page distance.
            return min(matches,
                       key=lambda a: abs(a.page_start - plate_page))
    return None
