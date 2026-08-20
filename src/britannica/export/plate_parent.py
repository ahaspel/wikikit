"""Extract the parent article name from a plate page's raw wikitext.

EB1911 plate pages carry the parent article's name as an explicit
raw-source marker in the page header.  This is structurally cleaner
than inferring the parent from the plate's title (``X, PLATE I``) or
from page proximity — the source is telling us directly which article
the plate illustrates.

Three patterns cover ~96% of plates:

* ``<section begin="ArticleName" />`` — explicit XML section attr,
  the highest-fidelity signal.
* ``{{rh|left|MIDDLE|right}}`` and its other spellings (``{{RunningHeader}}``,
  ``{{running header}}``, ``{{EB1911 Page Heading}}``; the name set lives in
  ``wikitext.PAGE_HEAD_RE``) — running-head template, with the
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
from britannica.wikitext import (first_template_body, iter_template_bodies,
                                 page_head_fields, split_top_pipes,
                                 template_end)


_SECTION_BEGIN_RE = re.compile(
    r'<section\s+begin\s*=\s*"([^"]+)"\s*/?>', re.IGNORECASE)
# Commons plate images follow "1911 Britannica - <Article> - <caption>.ext";
# the embedded article name is more specific than the running head, which is
# page furniture (the AURORA plates' head says "Aurora" — five same-page
# articles carry that title — while every image names AURORA POLARIS).
_IMG_ARTICLE_RE = re.compile(
    r'\[\[File:1911 Britannica - ([^\[\]|]+?) - ', re.IGNORECASE)
_C_OPEN_RE = re.compile(r"\{\{\s*c\s*\|", re.IGNORECASE)
_XLARGER_OPEN_RE = re.compile(r"\{\{\s*x-larger\s*\|", re.IGNORECASE)
# An INNERMOST template — no braces inside it — for the projection pass
# and for the letters test.
_INNERMOST_TEMPLATE_RE = re.compile(r"\{\{([^{}]*)\}\}")


def _c_xlarger_title(wikitext: str) -> "str | None":
    """The title inside `{{c|{{x-larger|TITLE}}}}` — read by walking braces.

    This was one regex naming both templates and one level of nesting, so it saw
    the pairing it spelled and nothing else: an `{{x-larger|{{uc|TITLE}}}}` inner
    came back truncated at the first inner brace, and any extra wrapper missed
    entirely ([[feedback_recursion_is_recognition]]).  Balanced reads instead, so
    the centred title can nest as deep as it likes.

    EVERY `{{c|…}}` is tried, not just the first: a page centres several things
    and the titled one is not always first — checking only the first missed the
    title on 25 pages when this was written.
    """
    for _off, centred in iter_template_bodies(wikitext, _C_OPEN_RE):
        body = centred.strip()
        m = _XLARGER_OPEN_RE.match(body)
        if not m:
            continue                       # this `{{c|…}}` centres something else
        end = template_end(body, 0)
        if end is None or end != len(body):
            continue                       # x-larger must BE the centred content
        return body[m.end():end - 2]
    return None

# Bogus section-attribute values that some plates use as a placeholder
# instead of the article name.  Skip them.
_PLACEHOLDER_RE = re.compile(r"^(?:S\d+|PLATE\d+|\d+)$", re.IGNORECASE)


def _strip_formatting(s: str) -> str:
    """Unwrap formatting templates by taking the last pipe-separated
    arg.  Handles ``{{x-larger|EMBROIDERY}}`` (1 arg → EMBROIDERY) and
    ``{{fs|180%|BOOKBINDING}}`` (2 args → BOOKBINDING).

    Unwraps until there is nothing left to unwrap.  This was `for _ in range(5)`
    around a hand-rolled brace walk — a claim that no plate title is wrapped more
    than five deep, which nothing checked and which failed by returning the sixth
    wrapper still in place, as a title.  The loop needs no cap because each turn
    strictly SHORTENS the string: it terminates on the data, not on a number
    ([[feedback_total_functions_not_cleanup_passes]]).
    """
    s = s.strip()
    s = re.sub(r"'+", "", s)  # strip wikitext italics
    while True:
        m = re.match(r"^\{\{[\w\-]+\s*\|", s)
        if not m:
            break
        end = template_end(s, 0)
        if end is None or s[end:].strip():
            break                 # never closes, or doesn't wrap the whole slot
        args = split_top_pipes(s[m.end():end - 2])
        if not args:
            break
        unwrapped = args[-1].strip()
        if unwrapped == s:        # no progress — stop rather than spin
            break
        s = unwrapped

    # A head may STYLE PARTS of a name rather than wrap the whole slot:
    # `{{x-larger|HALIFAX,}} {{fs|110%|1ST}} {{x-larger|MARQUESS}}`.  The
    # whole-slot unwrap above cannot touch those, and the field then came out
    # with its braces intact — unusable as a title, so 24 plates' running heads
    # were a signal we held and could not read.  Unwrapping each INNERMOST
    # template to its last argument, repeatedly, reads it as "HALIFAX, 1ST
    # MARQUESS".  Terminates on the data: every pass removes a template.
    while True:
        projected = _INNERMOST_TEMPLATE_RE.sub(
            lambda mm: mm.group(1).rsplit("|", 1)[-1], s)
        if projected == s:
            return s.strip()
        s = projected


def _parse_rh_middle(wikitext: str) -> str | None:
    """Pull the middle slot of the first running-head template.

    The head's name set and its slot split are the lexicon's
    (`wikitext.page_head_fields`); this module's own `_RH_RE` knew `{{rh}}`,
    `{{RunningHeader}}` and `{{EB1911 Page Heading}}` but not `{{running
    header}}`, the spelling that cost vol 18's MEDAL plate its number one module
    over ([[feedback_tune_dont_fork]]).
    """
    args = page_head_fields(wikitext)
    if len(args) < 3:
        return None
    middle = _strip_formatting(args[1].strip())
    # Some heads centre the FOLIO rather than the title
    # (`{{size|xl|{{em|8.3}}611}}`), which unwraps to `{{EM|8.3}}611` — no name in
    # it.  The test is LETTERS OUTSIDE the braces, not "any braces": a headword
    # may carry an embedded styler and still be a name
    # (`BUCKINGHAM, {{SMALLER|1ST}} DUKE OF` normalises to BUCKINGHAM), and
    # rejecting those threw away five real plate signals.
    if not re.search(r"[A-Za-z]", _INNERMOST_TEMPLATE_RE.sub("", middle)):
        return None
    return middle


def extract_signals(wikitext: str) -> list[str]:
    """Return ordered candidate parent-article names from a plate's
    raw wikitext.  Ordered ``c`` → ``section`` → image-name → ``rh``
    (``c`` is most consistently the parent article name across the
    corpus; the Commons image names carry the article name verbatim;
    ``rh`` sometimes carries a section/topic label instead, as in the
    AEGEAN CIVILIZATION plates — and for homonym pages only the page
    WORD, as in AURORA)."""
    candidates: list[str] = []

    inner = _c_xlarger_title(wikitext)
    if inner:
        v = _strip_formatting(inner).upper().strip()
        if v and not _PLACEHOLDER_RE.match(v):
            candidates.append(v)

    m = _SECTION_BEGIN_RE.search(wikitext)
    if m:
        v = m.group(1).strip().upper()
        if v and not _PLACEHOLDER_RE.match(v):
            candidates.append(v)

    for m in _IMG_ARTICLE_RE.finditer(wikitext):
        v = m.group(1).strip().upper()
        if v and not _PLACEHOLDER_RE.match(v) and v not in candidates:
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
