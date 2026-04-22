from dataclasses import dataclass, field

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal
import re

# Raw wikitext section-begin tag.
def _match_section_begin(text):
    """Find all section-begin markers, handling both quoted and unquoted forms."""
    # Quoted: <section begin="Foo Bar" />
    # Unquoted: <section begin=Foo/>
    pattern = re.compile(
        r'<section\s+begin=(?:"([^"]+)"|([A-Za-z][^/>\s]*))\s*/?>', re.IGNORECASE)
    results = []
    for m in pattern.finditer(text):
        name = m.group(1) if m.group(1) is not None else m.group(2)
        results.append((m.start(), m.end(), name))
    return results

# Keep the old pattern for simple finditer compatibility
_SEC_MARKER = re.compile(r'<section\s+begin="([^"]+)"\s*/?>', re.IGNORECASE)

# Section-end tags — stripped during preprocessing.
_SEC_END = re.compile(r'<section\s+end=(?:"[^"]*"|[^/>\s]*)\s*/?>', re.IGNORECASE)

# <noinclude> blocks — stripped during preprocessing (page headers, quality tags).
_NOINCLUDE = re.compile(r"<noinclude>.*?</noinclude>", re.DOTALL | re.IGNORECASE)

# Generic Wikisource section IDs that are never real article titles.
_GENERIC_SEC_ID = re.compile(
    r"^(?:part|s|text|rpart|plate)\d*$", re.IGNORECASE
)


def _strip_templates(text: str) -> str:
    """Unwrap nested wiki template wrappers, keeping the innermost content.

    Handles patterns like {{mono|{{fs|108%|TITLE}}}} -> TITLE.
    Multi-param templates keep the last parameter.
    Also strips inline HTML formatting tags (<big>, <small>, <sub>, <sup>)
    used for drop-caps / size emphasis in headings — we don't want
    '<big>S</big>UCCINIC <big>A</big>CID' leaking into the title.
    """
    for _ in range(5):
        text = re.sub(r"\{\{[^{}|]*\|([^{}|]*)\}\}", r"\1", text)
        text = re.sub(r"\{\{[^{}]*\|([^{}|]*)\}\}", r"\1", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(
        r"</?(?:big|small|sub|sup|span|font)\b[^>]*>",
        "", text, flags=re.IGNORECASE,
    )
    return text.strip()


# Wikisource section ID corrections: typos and false article names.
# Key = section ID as it appears in the source, value = corrected title
# (or None to treat as a generic continuation, not a new article).
_SECTION_ID_FIXES = {
    "Algebrab": "ALGEBRA",
    "Algebrae": None,  # continuation of ALGEBRA
    "PHOLOLOGY": None,  # typo for PHILOLOGY — continuation
}


def _is_article_section_id(sec_id: str) -> bool:
    """Return True if a section ID looks like a real article title, not a
    generic Wikisource continuation marker (part1, s2, text1, etc.)."""
    if _GENERIC_SEC_ID.match(sec_id):
        return False
    # Trailing digits on an otherwise-valid name (Egypt2, Egypt3) are
    # Wikisource continuations, not separate articles.
    if re.search(r"\d+$", sec_id):
        return False
    # Single letters are handled separately — they are only valid as
    # letter-of-the-alphabet articles, detected via _is_letter_article.
    if len(sec_id) == 1:
        return False
    return True


def _is_letter_article(sec_id: str, sec_text: str) -> bool:
    """Return True if this section is a single-letter encyclopedia article
    (e.g. the article about the letter A, B, C, etc.).
    These have a single-letter section ID and text about the letter itself."""
    if len(sec_id) != 1 or not sec_id.isalpha():
        return False
    # The text should be about the letter — check for characteristic phrases
    lower = sec_text[:200].lower()
    return any(phrase in lower for phrase in [
        "letter", "alphabet", "symbol", "phoenician",
    ])


def _preprocess_wikitext(text: str) -> str:
    """Minimal preprocessing of raw wikitext for boundary detection.

    Strips <noinclude> blocks, <section end> tags, and normalizes line endings.
    Preserves <section begin> tags and all other raw wikitext.
    """
    text = _NOINCLUDE.sub("", text)
    text = _SEC_END.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


# ── Detection output types ─────────────────────────────────────────────


@dataclass
class SegmentInfo:
    """A text segment belonging to an article, with its source page."""
    source_page_id: int
    page_number: int
    sequence: int
    text: str


@dataclass
class DetectedArticle:
    """An article boundary detected from raw wikitext.  Pure data — no DB models."""
    title: str
    volume: int
    page_start: int
    page_end: int
    article_type: str  # "article" or "plate"
    segments: list[SegmentInfo] = field(default_factory=list)
    # Wikisource <section begin="X"> name from the article's first page.
    # Used as a tiebreaker in stable IDs when multiple articles share a
    # (volume, page_start).
    section_name: str = ""

    @property
    def body(self) -> str:
        """Reconstruct full body from segments."""
        parts = []
        for seg in sorted(self.segments, key=lambda s: s.sequence):
            text = (seg.text or "").strip()
            if not text:
                continue
            if parts:
                joiner = "\n\n" if re.match(r"\[\[(?:File|Image):", text, re.IGNORECASE) else " "
                parts.append(joiner)
            parts.append(text)
        return "".join(parts).strip()


# ── Per-page parsing helpers ───────────────────────────────────────────


@dataclass
class CandidateArticle:
    title: str
    body: str
    is_tentative: bool = False  # True if created from named section without bold
    # Original case of the <section begin="…"> name. Used to detect
    # Title Case subsection continuations (ALGAE's "Benthos", "Occurrence
    # in the rocks") vs ALL-CAPS new-article continuations.
    raw_sec_id: str = ""


@dataclass
class ParsedPage:
    prefix_text: str
    candidates: list[CandidateArticle]


def _parse_page_by_sections(text: str) -> ParsedPage | None:
    """Parse a page using section markers if present.

    Returns None if no section markers found (fall back to heuristic parsing).
    """
    raw_markers = _match_section_begin(text)
    if not raw_markers:
        return None

    # Split text into sections
    sections: list[tuple[str, str]] = []  # (section_id, section_text)
    for i, (mstart, mend, mname) in enumerate(raw_markers):
        start = mend
        end = raw_markers[i + 1][0] if i + 1 < len(raw_markers) else len(text)
        section_text = text[start:end].strip()
        sections.append((mname, section_text))

    # Text before the first marker is prefix (continuation of previous article)
    prefix = text[:raw_markers[0][0]].strip()

    # Pre-split: a single section may contain multiple articles if there
    # are bold headings mid-text.  Split on '''ALLCAPS patterns at
    # paragraph boundaries only (double newline).  Raw wikitext has
    # hard line breaks within paragraphs, so a single \n would create
    # false splits on bold words that happen to start a line.
    _BOLD_SPLIT = re.compile(
        r"\n\n(?='{3}[A-Z])"
    )
    expanded_sections = []
    for sec_id, sec_text in sections:
        # Named sections (not s1, s2, etc.) are kept whole — the bold
        # heading inside is formatting, not a new article boundary.
        is_named_sec = not re.match(r"^s\d+$", sec_id)
        if is_named_sec:
            expanded_sections.append((sec_id, sec_text.strip()))
        else:
            parts = _BOLD_SPLIT.split(sec_text)
            for j, part in enumerate(parts):
                # First part keeps the original section ID; subsequent parts
                # are anonymous sub-sections created by the split.
                sub_id = sec_id if j == 0 else f"s{900 + len(expanded_sections)}"
                expanded_sections.append((sub_id, part.strip()))

    candidates = []
    for sec_id, sec_text in expanded_sections:
        # Normalize Wikisource-style section-ID artifacts:
        #   - Trailing `_p1`/`_p2` = split-transclusion part markers
        #   - `_` as URL-style word separator (Jade_(bay))
        sec_id = re.sub(r"_p\d+$", "", sec_id, flags=re.IGNORECASE)
        sec_id = sec_id.replace("_", " ")

        # Apply section ID corrections (Wikisource typos, false splits)
        if sec_id in _SECTION_ID_FIXES:
            fix = _SECTION_ID_FIXES[sec_id]
            if fix is None:
                # Suppressed — treat as continuation, not a new article
                if candidates:
                    candidates[-1].body += "\n\n" + sec_text
                else:
                    prefix = (prefix + "\n\n" + sec_text).strip()
                continue
            sec_id = fix

        # Section IDs with / are sub-sections (e.g. Britain/Roman).
        # Use the part before / as the article name so sub-sections
        # merge into one article.
        if "/" in sec_id:
            sec_id = sec_id.split("/")[0]

        # Determine the article title
        # Named sections (not s1, s2, s3...) use the section ID as the title
        is_named = not re.match(r"^s\d+$", sec_id)

        # Strip link wrappers and bold markers for heading detection.
        # Some Wikisource headings are wrapped in [[link|'''TITLE''']] wrappers.
        # Skip leading non-content lines (HTML comments, tables, images,
        # templates) to find the actual first content line.
        first_line = ""
        _sec_lines = sec_text.split("\n")
        _first_line_idx = 0
        # Track wikitable state: `{|` opens a table, `|}` closes it.
        # Content between them (including caption lines that don't
        # start with `|`, e.g. figure captions) is table content and
        # must be skipped — otherwise the first caption line gets
        # mistaken for the article's heading (MILLIPEDE vol 18 p.492).
        _in_table = 0
        for _i, _line in enumerate(_sec_lines):
            stripped = _line.strip()
            if not stripped:
                continue
            if stripped.startswith("{|"):
                _in_table += 1
                continue
            if _in_table > 0:
                if stripped.startswith("|}"):
                    _in_table -= 1
                # Skip any line while inside a wikitable
                continue
            # Peel off leading invisible-output templates — they mask
            # real headings from the pre-filter below. Example: the
            # section for MORLEY (of Blackburn) starts with
            # `{{nop}}[[Author:…|'''MORLEY''' …]]`; without this peel
            # the `{{` prefix makes the whole line look like table
            # markup and it gets skipped.
            stripped = re.sub(
                r"^(?:\{\{(?:nop|clear|-)\}\}\s*)+",
                "", stripped, flags=re.IGNORECASE,
            )
            # Also peel `{{Section|Derby, Earls of}}` section anchors —
            # they produce no visible output but hide the bold heading
            # on the same line (DERBY, EARLS OF vol 8 p79).
            stripped = re.sub(
                r"^\{\{Section\|[^{}]*\}\}\s*",
                "", stripped, flags=re.IGNORECASE,
            )
            if not stripped:
                continue
            # Re-check table opener in case peeled leading {{nop}}
            # revealed a `{|` table opener underneath.
            if stripped.startswith("{|"):
                _in_table += 1
                continue
            if re.match(r"^(<!--.*?-->|</?table\b|\|\}|\|-|\|(?!''')|</?tr|</?td|\[\[(?:File|Image):|\{\{)", stripped, re.IGNORECASE) or re.search(r"</table>\s*$", stripped, re.IGNORECASE):
                continue
            first_line = stripped
            _first_line_idx = _i
            break
        # If first_line opens `[[Author:…` or `[[…|'''…'''` but the
        # closing `]]` is on a following line (COMTE, AUGUSTE vol 6 p838
        # splits the Author link across newlines), extend first_line
        # until the outer brackets balance. Track the original heading
        # start separately from the extended end so pre_heading /
        # remaining_lines still slice the body correctly.
        _heading_start_idx = _first_line_idx
        while (
            _first_line_idx is not None
            and first_line.count("[[") > first_line.count("]]")
            and _first_line_idx + 1 < len(_sec_lines)
        ):
            _first_line_idx += 1
            first_line = first_line + " " + _sec_lines[_first_line_idx].strip()
        first_line_unwrapped = re.sub(
            r"\[\[[^\]|]*\|(.*?)\]\]",
            r"\1", first_line,
        )
        # Strip invisible-output templates like {{nop}} at line start —
        # they're used for paragraph-break spacing but block the
        # `has_bold_heading` check below. Leaves '''…''' at line start
        # detectable. Example (vol 18 p.840):
        #   {{nop}}[[Author:John Morley|'''MORLEY''' [{{sc|of Blackburn}}]…]]
        first_line_unwrapped = re.sub(
            r"^\s*(?:\{\{(?:nop|clear|-)\}\}\s*)+",
            "", first_line_unwrapped, flags=re.IGNORECASE,
        )
        clean_first = first_line_unwrapped.replace("'''", "")
        # {{uc|Swift, Jonathan}} → SWIFT, JONATHAN (uppercase template —
        # SWIFT vol 26 p243 wraps the whole title this way, and without
        # uppercasing the unwrap leaves "Swift, Jonathan" which the
        # UC-only heading regex collapses to just "S").
        clean_first = re.sub(
            r"\{\{uc\|([^{}|]*)\}\}",
            lambda m: m.group(1).upper(),
            clean_first, flags=re.IGNORECASE,
        )
        # Unwrap {{sc|X}} → X and similar inline templates so their
        # content counts toward the heading.
        clean_first = re.sub(r"\{\{[^}]*\|([^}|]*)\}\}", r"\1", clean_first)
        clean_first = re.sub(r"\s+", " ", clean_first).strip()

        # Extract the title from the start of the line.  The pattern must
        # handle Mc/Mac/O'/d' prefixes which mix case (McCORMICK, O'BRIEN).
        # Each word starts with uppercase (or a known prefix + uppercase).
        # Subsequent words must be 2+ chars in general, with one
        # exception: a pure Roman numeral (`I`, `V`, `X`, `L`, `C`, `D`,
        # `M`) after a SPACE — for regnal numbers like ALEXANDER I,
        # GEORGE V. Single letters after a COMMA are usually initials
        # or chemical-formula starts (ACCIUS, LUCIUS, R... is the Latin
        # poet ACCIUS, LUCIUS followed by R. era), so we don't allow
        # them.
        # Uppercase letter classes used in EB1911 headings:
        #   A-Z, À-Þ (Latin-1 uppercase),
        #   Ā Ē Ī Ō Ū (Latin Extended-A with macrons, used in transliterations),
        #   Œ Ś (ligature, Sanskrit),
        #   Ḍ Ḥ Ṃ Ṇ Ṛ Ṣ Ṭ Ẓ (Latin Extended Additional, Arabic/Sanskrit
        #     transliterations — MANṢŪR, AMĪR, etc.),
        #   Æ is already in À-Þ (U+00C6).
        _UC = (r"A-Z\u00C0-\u00DE\u0100\u0112\u012A\u014C\u016A\u0152\u015A"
               r"\u1E0C\u1E24\u1E42\u1E46\u1E5A\u1E62\u1E6C\u1E92")
        _UC_CHAR = rf"[{_UC}]"
        _UC_RUN = rf"[{_UC}''\u2018\u2019\-]+"
        # Qualifier between surname and forename:
        #   "MAP (or Mapes), WALTER"
        #   "MORLEY [of Blackburn], JOHN MORLEY"
        #   "ASELLI [Asellius, or Asellio], GASPARO"
        # The bracket/paren content is preserved in the title.
        _QUALIFIER = r"\s*[\[\(][^\]\)]+[\]\)][,\s]+"
        heading_match = re.match(
            rf"^({_UC_CHAR}{_UC_RUN}"
            rf"(?:"
            rf"[\s,]+{_UC_CHAR}{_UC_RUN}"
            rf"|\s+[IVX]+(?![A-Z])"
            rf"|{_QUALIFIER}{_UC_CHAR}{_UC_RUN}"
            rf")*)",
            clean_first,
        )

        # A bold heading '''TITLE''' at the start is the definitive signal
        # for a new article.  Named sections without bold are continuations
        # of the previous article repeated across pages on Wikisource.
        has_bold_heading = first_line_unwrapped.startswith("'''")
        _is_tentative = False

        # Bold heading is the sole signal for a new article, whether
        # the section is named or anonymous.
        # Exception: a named section without bold that is the first content
        # on the page (no candidates yet, no prefix) is treated as a new
        # article — it's not a continuation if there's nothing to continue.
        _used_bold_fallback = False
        if has_bold_heading:
            heading_title = heading_match.group(1).strip().rstrip(",.") if heading_match else None
            if heading_title:
                # Normalize " , " → ", " (artifacts from stripping
                # bracketed alternate forms between surname and forename
                # in split bios like "ASELLI [alts], GASPARO").
                heading_title = re.sub(r"\s+,", ",", heading_title)
                heading_title = re.sub(r"\s+", " ", heading_title).strip()
            # If the regex couldn't extract the title, fall back to
            # extracting the full bold text (handles Mc/Mac/O'/d' prefixes
            # and extended Unicode that the heading regex doesn't cover).
            if heading_title is None or not _has_valid_title_content(_normalize_title(heading_title)):
                bold_match = re.match(r"^'''([^']+)'''", first_line)
                if bold_match:
                    fallback = bold_match.group(1)
                    # Unwrap Author wikilinks: [[Author:X|DISPLAY]] → DISPLAY.
                    # Also strip inline <ref>…</ref> (etymology footnotes
                    # that break a bold heading, e.g. '''SEWING<ref>…</ref> MACHINES''').
                    fallback = re.sub(
                        r"<ref[^>]*>.*?</ref>", "", fallback, flags=re.DOTALL)
                    fallback = re.sub(
                        r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", fallback)
                    fallback = _strip_templates(fallback).rstrip(",.")
                    heading_title = fallback
                    _used_bold_fallback = True
            # Prefer section ID when the heading match is a partial capture
            # (e.g. "TISIO" from "TISIO (or Tisi), BENVENUTO" — sec_id
            # "Tisio_Benvenuto" has more info). Only swap when sec_id is
            # LONGER than the heading — otherwise the heading is the
            # better title (e.g. "ASELLI, GASPARO" vs sec_id "Aselli").
            if (heading_title and is_named
                    and heading_title.upper() != sec_id.upper()
                    and sec_id.upper().startswith(heading_title.upper().split(",")[0].split()[0])
                    and len(sec_id) > len(heading_title)):
                title = sec_id.upper()
            elif heading_title and _has_valid_title_content(
                _normalize_title(heading_title)
            ):
                title = heading_title
            elif is_named and (len(sec_id) != 1 or _is_letter_article(sec_id, sec_text)):
                title = sec_id.upper()
            else:
                title = None
        elif _is_letter_article(sec_id, sec_text):
            # Single-letter article about the letter itself
            title = sec_id.upper()
        elif is_named and not candidates and not prefix and _is_article_section_id(sec_id):
            # First named section on the page, no bold — tentatively new article
            title = sec_id.upper()
            _is_tentative = True
        else:
            # No bold heading — check if this is a numbered continuation
            # of a candidate on this page (e.g. Japan01 → Japan).
            base_id = re.sub(r"\d+$", "", sec_id)
            matched_candidate = None
            if base_id:
                for c in reversed(candidates):
                    if c.title.upper() == base_id.upper():
                        matched_candidate = c
                        break
            if matched_candidate:
                matched_candidate.body += "\n\n" + sec_text
                continue
            # If there's already a candidate on this page, append this
            # section to it (not to prefix). This preserves PAGE ORDER
            # when multiple named sections follow the first candidate
            # (e.g. CLEMENT (POPES) page 502 has Clement VII then
            # Clement VIII, IX, X, XI, XII in order — all should flow
            # together in the containing article's body, not get
            # reordered by having VIII+ in prefix). Emit a «SEC:name»
            # marker before the appended content so the viewer can
            # render each subsection as a navigable heading.
            if candidates:
                if is_named and _is_article_section_id(sec_id):
                    candidates[-1].body += (
                        f"\n\n\u00abSEC:{sec_id}\u00ab/SEC\u00bb\n\n"
                        + sec_text
                    )
                else:
                    candidates[-1].body += "\n\n" + sec_text
                continue
            # Otherwise, continuation of previous article
            if prefix:
                prefix = prefix + "\n\n" + sec_text
            else:
                prefix = sec_text
            continue

        if not title:
            if prefix:
                prefix = prefix + "\n\n" + sec_text
            else:
                prefix = sec_text
            continue

        # Extract body — find where the heading ends in the ORIGINAL text
        # (which may have bold markers)
        if heading_match and not _used_bold_fallback:
            # Find the heading text in the original first line and skip past it
            heading_text = heading_match.group(0)
            # The original might have bold markers (''') around the heading.
            # Strip ALL bold groups at the start (handles multi-word titles
            # like '''PRAXIAS''' and '''ANDROSTHENES,''')
            bold_heading = re.match(
                r"^(?:'{3}[^']+'{3}[\s,.\-]*(?:and\s+|&\s+)?)+",
                first_line, re.IGNORECASE,
            )
            if bold_heading:
                body = first_line[bold_heading.end():].lstrip(" ,.")
            else:
                # Fall back: strip bold markers, find heading, take the rest
                body = clean_first[len(heading_text):].lstrip(" ,.")
            # Prepend any leading content (tables, images) that was skipped
            # during heading detection — it belongs to this article.
            pre_heading = "\n".join(_sec_lines[:_heading_start_idx]).strip()
            # Add remaining lines after the heading line
            remaining_lines = _sec_lines[_first_line_idx + 1:]
            if remaining_lines:
                remaining = "\n".join(remaining_lines).strip()
                if body and remaining:
                    body = body + "\n" + remaining
                elif remaining:
                    body = remaining
            if pre_heading:
                body = pre_heading + "\n\n" + body
            body = body.strip()
        elif has_bold_heading:
            # Bold heading present but regex couldn't parse it — strip
            # the bold text from the first line to get the body.
            stripped_first = re.sub(r"^'''[^']+'''\s*", "", first_line).lstrip(" ,.")
            remaining_lines = _sec_lines[_first_line_idx + 1:]
            if remaining_lines:
                remaining = "\n".join(remaining_lines).strip()
                if stripped_first and remaining:
                    body = stripped_first + "\n" + remaining
                elif remaining:
                    body = remaining
                else:
                    body = stripped_first
            else:
                body = stripped_first
            body = body.strip()
        else:
            body = "\n".join(_sec_lines[_heading_start_idx:]).strip()

        if title:
            candidates.append(CandidateArticle(
                title=title, body=body, is_tentative=_is_tentative,
                raw_sec_id=sec_id,
            ))

    return ParsedPage(prefix_text=prefix, candidates=candidates)


def _split_on_bold_headings(text: str) -> ParsedPage:
    """Split text with no section markers on bold headings.

    Returns a ParsedPage with prefix (text before first bold heading)
    and candidates for each bold heading found.
    """
    # Split on bold headings at line/paragraph boundaries.
    # In raw wikitext, bold is '''TEXT'''. Edge cases handled:
    #   - Author-link wrap: '''[[Author:John Keats|KEATS, JOHN]]'''
    #   - Inline etymology: '''SEWING<ref>…</ref> MACHINES.'''
    _BOLD_HEADING = re.compile(
        r"(?:^|\n\n)('{3}"
        r"(?:\[\[[^\]|]*\|)?"
        r"[A-Z\u00C0-\u00DE]"
        r"(?:[A-Z\u00C0-\u00DE'\u2018\u2019\-,. ]|<ref[^>]*>.*?</ref>)*"
        r"(?:\]\])?"
        r"'{3})",
        re.DOTALL,
    )

    parts = _BOLD_HEADING.split(text)

    if len(parts) <= 1:
        # No bold headings found — entire text is continuation
        return ParsedPage(prefix_text=text, candidates=[])

    prefix = parts[0].strip()
    candidates = []

    # parts alternates: text, bold-match, text, bold-match, text, ...
    i = 1
    while i < len(parts):
        bold_marker = parts[i]
        body_after = parts[i + 1].strip() if i + 1 < len(parts) else ""
        # Extract title from the bold marker, stripping template wrappers.
        # Unwrap wikilinks ([[Author:X|Y]] → Y) and strip inline refs.
        stripped_bold = bold_marker.replace("'''", "")
        stripped_bold = re.sub(
            r"<ref[^>]*>.*?</ref>", "", stripped_bold, flags=re.DOTALL)
        stripped_bold = re.sub(
            r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", stripped_bold)
        clean = _strip_templates(stripped_bold)
        title = clean.rstrip(",.")
        if title:
            body_text = body_after.lstrip(" ,.")
            candidates.append(CandidateArticle(title=title, body=body_text))
        i += 2

    return ParsedPage(prefix_text=prefix, candidates=candidates)


def _is_heading(line: str) -> bool:
    title, _ = _extract_heading(line)
    return title is not None


def _normalize_title(title: str) -> str:
    """Strip parentheticals, trailing periods, and spacing artifacts."""
    # Strip all parenthetical content (etymologies, dates, alternate names)
    title = re.sub(r"\s*\([^)]*\)", "", title)
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    # Clean comma artifacts from parenthetical removal (e.g. "SMITH, , JOHN")
    title = re.sub(r",\s*,", ",", title).strip(", ")
    # Strip trailing phrase after comma if it's:
    # - mixed-case (descriptor like "Greek", "Grand Master")
    # - a 2-letter fragment not in the valid title list (formula like "CH")
    if "," in title:
        before, _, after = title.rpartition(",")
        after = after.strip()
        if after and not after.isupper():
            title = before.strip()
        elif after and len(after) == 2 and after not in _VALID_TWO_LETTER:
            title = before.strip()
    # Strip trailing period (encyclopedia formatting convention)
    title = re.sub(r"\.$", "", title)
    return title


# Matches pure Roman numerals (II, IV) and numbered section headings (IV. TOPIC)
_ROMAN_NUMERAL = re.compile(r"^[IVXLCDM]+\.?(\s|$)")

# Two-letter article titles that actually exist in the encyclopedia.
# Other 2-letter combinations (CH, RO, OF, etc.) are fragments.
_VALID_TWO_LETTER = frozenset({
    "AA", "AB", "AD", "AE", "AI", "AL", "AM", "AN", "AR", "AS", "AT",
    "AX", "AY",
})


def _has_valid_title_content(title: str) -> bool:
    """Require at least one run of 2+ consecutive uppercase letters.

    Also rejects:
    - chemical formulas (middle-dot, arrow, or digits mixed with letters)
    - pure Roman numerals (section numbers like II, III, IV)
    - two-letter titles not in the known allowlist
    """
    if "\u00b7" in title or "\u2192" in title:
        return False
    # Reject digits mixed with letters (chemical formulas like CH3, C6H5)
    # but allow standalone numbers (dates like 1812 in "WAR OF 1812")
    if re.search(r"[A-Za-z]\d|\d[A-Za-z]", title):
        return False
    if title and title[0].isdigit():
        return False
    # Single letter titles (A, B, C) exist but are too ambiguous —
    # they conflict with contributor initials on front matter pages.
    # These 26 articles will be handled as special cases later.
    if _ROMAN_NUMERAL.match(title):
        return False
    # Reject numbered section headings (ORDER I, PART II, CLASS IV, etc.)
    if re.match(
        r"^(?:ORDER|PART|SECTION|CLASS|BOOK|CHAPTER|DIVISION|GROUP|SERIES|PERIOD|GRADE|LEGION|BRIGADE|FAMILY|TRIBE|GENUS|SUBORDER|SUBFAMILY)\s+[IVXLCDM]+\.?$",
        title,
    ):
        return False
    # Reject unknown 2-letter titles (common source of fragments)
    if len(title) == 2 and title not in _VALID_TWO_LETTER:
        return False
    return bool(re.search(r"[A-Z\u00C0-\u00DE]{2,}", title))


def _extract_heading(line: str) -> tuple[str | None, str]:
    """Extract an all-caps heading from the start of a line.

    Used by _parse_page_by_sections for anonymous sections only.
    Returns (title, remainder) or (None, line) if no heading found.
    """
    line = line.strip()
    if not line:
        return None, ""

    # Strip formatting markers before heading detection
    # In raw wikitext: bold ('''), italic (''), wiki markup
    clean = line.replace("'''", "").replace("''", "")

    # Match all-caps word(s) at the start, optionally with parenthetical and comma-name
    m = re.match(
        r"^([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019.\-]+"
        r"(?:[\s]+[A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019.\-]+)*"
        r"(?:\s+\([^)]*\))?"
        r"(?:,\s*[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF''\u2019\-]+(?:\s+[A-Z\u00C0-\u00DE][A-Za-z\u00C0-\u00FF''\u2019\-]+)*)?"
        r")",
        clean,
    )
    if not m:
        return None, line

    raw_title = m.group(0).strip().rstrip(",.")
    # Strip parentheticals from title
    title = re.sub(r"\s*\([^)]*\)", "", raw_title).strip()
    title = re.sub(r"\.$", "", title)

    if not title or len(title) > 255:
        return None, line

    # Get remainder from the clean line
    remainder = clean[m.end():].lstrip(" ,.")
    return title, remainder


def _split_plate_sections(text: str) -> list[tuple[str | None, str]]:
    """Split a plate page into sections by all-caps headings.

    Returns list of (title, body) tuples. If no headings are found,
    returns one section with no title.
    """
    lines = text.split("\n")
    sections: list[tuple[str | None, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this is a section heading (all-caps, short, not a marker)
        if (stripped.upper() == stripped
                and not stripped.startswith("[[File:")
                and not stripped.startswith("[[Image:")
                and not stripped.startswith(("{|", "|-", "|}"))
                and 2 < len(stripped) <= 60
                and any(c.isalpha() for c in stripped)
                and not any(c.isdigit() for c in stripped)):
            # New section — save the previous one
            heading = stripped.rstrip(".")
            if heading != current_title:
                if current_lines:
                    sections.append((current_title, "\n\n".join(current_lines)))
                    current_lines = []
                current_title = heading
        else:
            current_lines.append(stripped)

    # Save the last section
    if current_lines or current_title:
        sections.append((current_title, "\n\n".join(current_lines)))

    # If no sections were created, return the whole text as one section
    if not sections:
        sections.append((None, text.strip()))

    # Merge initial untitled section into the first titled section
    if len(sections) > 1 and sections[0][0] is None and sections[1][0] is not None:
        combined_body = sections[0][1] + "\n\n" + sections[1][1] if sections[0][1] else sections[1][1]
        sections = [(sections[1][0], combined_body)] + sections[2:]

    # Merge sections with the same title
    merged: dict[str | None, list[str]] = {}
    order: list[str | None] = []
    for title, body in sections:
        if title not in merged:
            merged[title] = []
            order.append(title)
        if body:
            merged[title].append(body)

    return [(title, "\n\n".join(merged[title])) for title in order if merged[title]]


def _is_plate_page(text: str) -> bool:
    """Detect plate pages — mostly images with little prose.

    In raw wikitext, images are [[File:...]] or [[Image:...]].
    A true plate has minimal running prose (just captions and maybe a
    title). Pages that happen to have several images mid-article
    (e.g. vol 17 MAP p.641 with 4 figure images) are NOT plates —
    they're text pages with figures embedded.
    """
    stripped = text.strip()
    img_count = len(re.findall(r"\[\[(?:File|Image):", stripped, re.IGNORECASE))
    if img_count < 2:
        return False

    # Count non-image, non-table, non-caption words (actual running prose)
    prose = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", stripped, flags=re.IGNORECASE)
    prose = re.sub(r"\{\|.*?\|\}", "", prose, flags=re.DOTALL)
    prose = re.sub(r"<table\b.*?</table>", "", prose, flags=re.DOTALL | re.IGNORECASE)
    # Strip caption-style content (Fig. N.—…, numbered lists)
    prose = re.sub(r"(?:\{\{(?:small-caps|sc)\|[^}]*\}\}|Fig\.)\s*\d+[^.\n]*\.",
                   "", prose, flags=re.IGNORECASE)
    prose_words = len(prose.split())

    # Tight thresholds — genuine plates have very little narrative prose.
    if img_count >= 3:
        return prose_words <= 80
    return prose_words <= 30


def _parse_page(text: str) -> ParsedPage:
    # Split into paragraph blocks (separated by blank lines), preserving structure.
    raw_lines = text.splitlines()

    # Collapse into non-empty lines, preserving blank-line boundaries as "\n\n".
    lines: list[str] = []
    prev_blank = False
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            prev_blank = True
            continue
        if prev_blank and lines:
            # Insert a paragraph-break marker before this line
            lines.append("")
        lines.append(stripped)
        prev_blank = False

    # Filter to only non-empty lines for heading detection
    content_lines = [l for l in lines if l]

    if not content_lines:
        return ParsedPage(prefix_text="", candidates=[])

    first_heading_index: int | None = None
    in_table = False
    for i, line in enumerate(lines):
        if not line:
            continue
        if line.startswith("{|"):
            in_table = True
        if in_table:
            if line.startswith("|}"):
                in_table = False
            continue
        title, _ = _extract_heading(line)
        if title is not None:
            first_heading_index = i
            break

    if first_heading_index is None:
        return ParsedPage(
            prefix_text=_join_lines(lines),
            candidates=[],
        )

    prefix_lines = lines[:first_heading_index]
    article_lines = lines[first_heading_index:]

    candidates: list[CandidateArticle] = []
    current_title: str | None = None
    current_body_lines: list[str] = []
    in_table = False

    for line in article_lines:
        if not line:
            # Blank line = paragraph break within body
            current_body_lines.append(line)
            continue

        # Skip heading detection inside table blocks
        if line.startswith("{|"):
            in_table = True
        if in_table:
            if current_title is not None:
                current_body_lines.append(line)
            if line.startswith("|}"):
                in_table = False
            continue

        title, remainder = _extract_heading(line)

        if title is not None:
            if current_title is not None:
                candidates.append(
                    CandidateArticle(
                        title=current_title,
                        body=_join_lines(current_body_lines),
                    )
                )

            current_title = title
            current_body_lines = []

            if remainder:
                current_body_lines.append(remainder)
        else:
            if current_title is not None:
                current_body_lines.append(line)

    if current_title is not None:
        candidates.append(
            CandidateArticle(
                title=current_title,
                body=_join_lines(current_body_lines),
            )
        )

    return ParsedPage(
        prefix_text=_join_lines(prefix_lines),
        candidates=candidates,
    )


def _join_lines(lines: list[str]) -> str:
    """Join lines, treating empty strings as paragraph-break markers.

    Table blocks ({|...|}  in raw wikitext) are preserved with their
    internal newlines.
    """
    paragraphs: list[list[str]] = [[]]
    in_table = False

    for line in lines:
        if not line:
            if not in_table and paragraphs[-1]:
                paragraphs.append([])
            continue

        if line.startswith("{|"):
            in_table = True
        if in_table:
            paragraphs[-1].append(line)
            if line.startswith("|}"):
                in_table = False
            continue

        paragraphs[-1].append(line)

    result_parts = []
    for p in paragraphs:
        if not p:
            continue
        # If this paragraph contains a table, join with newlines
        if any(l.startswith("{|") for l in p):
            result_parts.append("\n".join(p))
        else:
            result_parts.append(" ".join(p))

    return "\n\n".join(result_parts).strip()


def _extract_heading_title(raw: str) -> str | None:
    """Extract the article title from a page heading template.

    Page headings have the form:
        {{EB1911 Page Heading|LEFT|TITLE|RIGHT|PAGE}}
        {{rh|LEFT|TITLE|RIGHT}}

    The title fields contain the first and/or last article on the page.
    We look for the field that looks most like an article title (all-caps,
    not a page number, not too short).
    """
    m = re.search(
        r"\{\{(?:EB1911 Page Heading|rh)\|([^}]+)\}\}", raw[:500])
    if not m:
        return None
    fields = m.group(1).split("|")
    # Strip templates and whitespace from each field
    candidates = []
    for f in fields:
        f = re.sub(r"\{\{[^{}]*\}\}", "", f).strip()
        f = re.sub(r"&[a-z]+;", " ", f).strip()
        if not f or f.isdigit() or len(f) < 3:
            continue
        # Must be mostly uppercase (article titles are ALL CAPS)
        if f.upper() == f or f.replace(".", "").replace(",", "").upper() == f.replace(".", "").replace(",", ""):
            candidates.append(f.rstrip(".,"))
    if not candidates:
        return None
    # Return the longest candidate (most likely to be the article title)
    return max(candidates, key=len)


def _heading_matches(heading_title: str, article_title: str) -> bool:
    """Check if a page heading title matches the current article title.

    Handles partial matches: the heading might show a shortened or
    slightly different form of the article title.
    """
    h = heading_title.upper().strip()
    a = article_title.upper().strip()
    # Exact match
    if h == a:
        return True
    # One contains the other (handles abbreviations like "ROOSEVELT" matching
    # "ROOSEVELT, THEODORE")
    if h in a or a in h:
        return True
    # First word match (handles "ROORKEE" vs "ROORKEE, INDIA")
    h_first = h.split(",")[0].split("(")[0].strip()
    a_first = a.split(",")[0].split("(")[0].strip()
    if h_first == a_first and len(h_first) > 3:
        return True
    return False


def _fix_swallowed_pages(articles: list[DetectedArticle],
                         pages: list) -> list[DetectedArticle]:
    """Fix articles that swallowed pages belonging to a later article.

    When consecutive pages have no section markers but their page heading
    matches a later detected article, move those pages from the swallower
    to the correct article.
    """
    # Build a map of article titles to articles (for lookup)
    title_map = {}
    for a in articles:
        title_map[a.title.upper()] = a

    # Build page heading map
    heading_map = {}  # page_number -> heading title
    for page in pages:
        raw = (page.wikitext or "").strip()
        if not raw:
            continue
        m = re.search(r'Page Heading\|[^|]*\|([^|]+)\|', raw[:300])
        if m:
            heading_map[page.page_number] = m.group(1).strip().upper().rstrip(",.")

    # For each article, check if trailing pages have headings matching
    # a different article that starts later
    for art in articles:
        if art.article_type != "article":
            continue
        if len(art.segments) < 2:
            continue

        # Find trailing segments whose heading doesn't match this article
        # and DOES match a later article.  Only consider pages that have
        # NO section markers — pages with markers are correctly placed
        # by the main detection logic.
        pages_with_markers = set()
        for page in pages:
            raw = (page.wikitext or "").strip()
            if re.search(r'<section\s+begin=', raw):
                pages_with_markers.add(page.page_number)

        mismatch_start = None
        for i, seg in enumerate(art.segments):
            if seg.page_number in pages_with_markers:
                mismatch_start = None  # reset — can't split at/past a marked page
                continue
            heading = heading_map.get(seg.page_number, "")
            if not heading:
                continue
            matches_self = (
                heading in art.title.upper() or
                art.title.upper() in heading or
                heading.split(",")[0] == art.title.upper().split(",")[0]
            )
            if not matches_self:
                # Does the heading match any later article?
                target = None
                for later in articles:
                    if later.page_start > art.page_start and later.article_type == "article":
                        later_upper = later.title.upper()
                        if (heading in later_upper or later_upper in heading or
                                heading.split(",")[0] == later_upper.split(",")[0]):
                            target = later
                            break
                if target and mismatch_start is None:
                    mismatch_start = i

        if mismatch_start is not None:
            # Move segments from mismatch_start onward to the target article
            moved_segments = art.segments[mismatch_start:]
            art.segments = art.segments[:mismatch_start]
            art.page_end = art.segments[-1].page_number if art.segments else art.page_start

            # Find the target article and prepend the moved segments
            heading = heading_map.get(moved_segments[0].page_number, "")
            for target in articles:
                if target.page_start > art.page_start and target.article_type == "article":
                    target_upper = target.title.upper()
                    if (heading in target_upper or target_upper in heading or
                            heading.split(",")[0] == target_upper.split(",")[0]):
                        # Strip redundant bold title from the first moved
                        # segment — when a page has the target's bold
                        # heading (e.g. `'''[[Author:…|ROOSEVELT, THEODORE]]'''`)
                        # but no <section> marker, _split_on_bold_headings
                        # couldn't parse it (wiki-link inside bold).
                        # The segment body still has the raw `'''…'''`,
                        # which the transform stage renders as `«B»…«/B»`,
                        # duplicating the article title in the body.
                        first = moved_segments[0]
                        stripped = re.sub(
                            r"^\s*'{3}(?:\[\[[^\]]*\|)?([^'\]]+)(?:\]\])?'{3}[\s,.\-]*",
                            "", first.text or "",
                        )
                        if stripped != (first.text or ""):
                            first.text = stripped.lstrip()
                        # Renumber sequences
                        offset = len(moved_segments)
                        for seg in target.segments:
                            seg.sequence += offset
                        for j, seg in enumerate(moved_segments):
                            seg.sequence = j + 1
                        target.segments = moved_segments + target.segments
                        target.page_start = moved_segments[0].page_number
                        break

    return articles


# ── Detection (pure) ──────────────────────────────────────────────────


def detect_boundaries(volume: int) -> list[DetectedArticle]:
    """Detect article boundaries from raw wikitext.

    Reads SourcePages from the database but writes nothing.
    Returns a list of DetectedArticle with titles, page ranges, and
    raw wikitext segments.
    """
    session = SessionLocal()

    try:
        pages = (
            session.query(SourcePage)
            .filter(SourcePage.volume == volume)
            .order_by(SourcePage.page_number)
            .all()
        )

        articles: list[DetectedArticle] = []
        open_article: DetectedArticle | None = None

        for page in pages:
            # Read raw wikitext and preprocess minimally.
            raw = (page.wikitext or "").strip()
            if not raw:
                continue
            text = _preprocess_wikitext(raw)

            # Try to parse article boundaries from this page.
            parsed = _parse_page_by_sections(text)
            if parsed is None:
                parsed = _split_on_bold_headings(text)

            # If the page has no article boundaries, check whether it's
            # a plate page (full-page image layout) or a continuation.
            # Plate pages occupy a full page, start no articles, and are
            # mostly images — they should not be folded into the open
            # article's body text.
            if not parsed.candidates and _is_plate_page(text):
                # Extract plate title from page header templates
                plate_title = None
                header_match = re.search(
                    r"<noinclude>(.*?)</noinclude>", raw, re.DOTALL)
                if header_match:
                    hdr = header_match.group(1)
                    # {{x-larger|TITLE}} or {{larger|TITLE}} (may nest {{uc|...}})
                    t = re.search(r"\{\{x-larger\|([^}]+)\}\}", hdr)
                    if not t:
                        t = re.search(r"\{\{larger\|([^}]+)\}\}", hdr)
                    if t:
                        plate_title = _strip_templates(t.group(1)).rstrip(",.")
                    else:
                        # {{rh|...|TITLE|...}} or {{EB1911 Page Heading|...|TITLE|...}}
                        # Split on top-level pipes (respecting brace depth) to find fields.
                        for tmpl_start in [
                            hdr.find("{{rh|"),
                            hdr.find("{{EB1911 Page Heading|"),
                        ]:
                            if tmpl_start < 0:
                                continue
                            # Walk from the first | after template name
                            fields = []
                            depth = 0
                            current = ""
                            for ch in hdr[tmpl_start:]:
                                if ch == "{":
                                    depth += 1
                                    current += ch
                                elif ch == "}":
                                    depth -= 1
                                    if depth <= 0:
                                        break
                                    current += ch
                                elif ch == "|" and depth <= 2:
                                    fields.append(current)
                                    current = ""
                                else:
                                    current += ch
                            if current:
                                fields.append(current)
                            # fields[0] = template name, fields[1] = first arg, etc.
                            # For rh: fields ~ [rh, left, TITLE, right]
                            # For Page Heading: fields ~ [PH, plate-label, TITLE, ...]
                            # Find the best title candidate among fields[1:]
                            for f in fields[1:]:
                                clean = _strip_templates(f).rstrip("}]")
                                if (clean and len(clean) > 2
                                        and not clean.isdigit()
                                        and not re.match(r"^Plate\b", clean)
                                        and re.search(r"[A-Za-z]{3,}", clean)):
                                    plate_title = clean
                                    break
                            if plate_title:
                                break
                if not plate_title:
                    plate_title = f"PLATE (VOL. {page.volume}, P. {page.page_number})"

                plate = DetectedArticle(
                    title=plate_title,
                    volume=page.volume,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    article_type="plate",
                    segments=[SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=1,
                        text=text,
                    )],
                )
                articles.append(plate)

                # Don't update open_article — let it continue past the plate
                continue

            # Prefix text belongs to the currently open article — unless
            # it starts with a bold ALL-CAPS heading, which signals a new
            # article placed before the first section marker on this page.
            # Only all-caps titles are treated as articles; mixed-case bold
            # text is a subsection heading within the current article.
            if parsed.prefix_text:
                _prefix_art_match = re.match(
                    r"'''([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE''\u2019\s,.\-]+)'''",
                    parsed.prefix_text,
                )
                if (_prefix_art_match and parsed.candidates
                        and _has_valid_title_content(
                            _normalize_title(_prefix_art_match.group(1).strip().rstrip(",.")))):
                    # Split the prefix: text before bold heading → open article,
                    # bold heading onward → new candidate prepended to the list.
                    prefix_parsed = _split_on_bold_headings(parsed.prefix_text)
                    if prefix_parsed.prefix_text and open_article is not None:
                        next_seq = len(open_article.segments) + 1
                        open_article.segments.append(SegmentInfo(
                            source_page_id=page.id,
                            page_number=page.page_number,
                            sequence=next_seq,
                            text=prefix_parsed.prefix_text,
                        ))
                        open_article.page_end = page.page_number
                    parsed.candidates = prefix_parsed.candidates + parsed.candidates
                elif open_article is not None:
                    next_seq = len(open_article.segments) + 1
                    open_article.segments.append(SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=next_seq,
                        text=parsed.prefix_text,
                    ))
                    open_article.page_end = page.page_number

            # If there are no headings on the page, the whole page is continuation.
            if not parsed.candidates:
                if parsed.prefix_text and open_article is not None:
                    open_article.page_end = page.page_number
                continue

            # Process headings found on the page.
            # Running-head titles from {{EB1911 Page Heading|...}} —
            # used below to disambiguate tentative candidates.
            # Template shape: {{EB1911 Page Heading|L#|LTITLE|RTITLE|R#}}
            _raw_head = (page.wikitext or "")[:400]
            _hm = re.search(
                r"Page Heading\|[^|]*\|([^|]*)\|([^|]*)\|", _raw_head)
            _heading_titles: set[str] = set()
            if _hm:
                for t in (_hm.group(1), _hm.group(2)):
                    t = t.strip().upper().rstrip(",.")
                    if t:
                        _heading_titles.add(t)

            for candidate in parsed.candidates:
                body_text = (candidate.body or "").strip()

                # Cross-article section redirect.  A Title Case
                # ``<section begin="Regalia"/>`` on a continuation
                # page would normally be absorbed as a subsection of
                # the currently open article (REGENERATION OF LOST
                # PARTS in our worked example) by the Title-Case
                # heuristic below.  But if a real article with that
                # section name ALREADY exists (REGALIA was detected on
                # the prior page and is now closed), the content
                # belongs to THAT article — typically a plate or
                # continuation page interspersed with the open
                # article's page range.  Route instead of absorb.
                _redirect = None
                if candidate.is_tentative and candidate.raw_sec_id:
                    _cand_sec = candidate.raw_sec_id.casefold()
                    for _a in articles:
                        if (_a.section_name
                                and _a.section_name.casefold() == _cand_sec
                                and _a is not open_article):
                            _redirect = _a
                            break
                if _redirect is not None:
                    # Plate detection for redirected sections:
                    # ``_is_plate_page`` is too strict here (REGALIA's
                    # plate captions are verbose enough to exceed its
                    # prose-word thresholds).  Instead look for the
                    # explicit ``{{sc|Plate}} N.`` / ``PLATE N`` title
                    # that every Wikisource EB1911 plate page carries.
                    _looks_like_plate = body_text and (
                        _is_plate_page(body_text)
                        or bool(re.search(
                            r"\{\{(?:sc|uc)\|Plate\}\}\s*[IVX]+",
                            body_text, re.IGNORECASE))
                        or bool(re.search(
                            r"\{\{(?:sc|uc)\|Plate\s+[IVX]+[.,]?\}\}",
                            body_text, re.IGNORECASE))
                    )
                    if _looks_like_plate:
                        # Plate page (mostly images + captions) — create
                        # a dedicated plate-type article owned by the
                        # redirect target, matching the normal plate
                        # path at line 1194.  Without this, the plate's
                        # raw wikitable markup (image grids, caption
                        # cells) gets dumped into the parent article's
                        # prose body and renders as broken tables.
                        articles.append(DetectedArticle(
                            title=_redirect.title,
                            volume=page.volume,
                            page_start=page.page_number,
                            page_end=page.page_number,
                            article_type="plate",
                            segments=[SegmentInfo(
                                source_page_id=page.id,
                                page_number=page.page_number,
                                sequence=1,
                                text=body_text,
                            )],
                        ))
                    elif body_text:
                        _next_seq = len(_redirect.segments) + 1
                        _redirect.segments.append(SegmentInfo(
                            source_page_id=page.id,
                            page_number=page.page_number,
                            sequence=_next_seq,
                            text=body_text,
                        ))
                        _redirect.page_end = page.page_number
                    continue

                # Wikisource repeats <section begin="X"> on continuation pages.
                # If the currently open article has the same title, this is
                # continuation — append rather than creating a duplicate.
                exact_match = (
                    open_article is not None
                    and open_article.title == candidate.title
                )
                _open_base = re.sub(r"\d+$", "", open_article.title) if open_article else ""
                _cand_base = re.sub(r"\d+$", "", candidate.title)
                fuzzy_match = (
                    open_article is not None
                    and candidate.is_tentative
                    and (
                        _open_base.startswith(_cand_base)
                        or _cand_base.startswith(_open_base)
                    )
                    and _cand_base
                )
                # A tentative candidate (named section without a bold
                # heading) is a subsection of the currently open article
                # when the page's running head (from {{Page Heading|…}})
                # matches the open article's title AND does NOT match
                # the candidate's own title. Pages that transition
                # between two real articles carry BOTH titles in the
                # heading ({left | right}); in that case the candidate
                # is a legitimate new article, not a subsection, so we
                # must not absorb it.
                _open_root = open_article.title.upper().split(",")[0] if open_article else ""
                _cand_root = candidate.title.upper().split(",")[0]
                _heading_roots = {h.split(",")[0] for h in _heading_titles}
                heading_says_continuation = (
                    candidate.is_tentative
                    and open_article is not None
                    and _open_root in _heading_roots
                    and _cand_root not in _heading_roots
                )
                # First-word fallback: if the tentative candidate and
                # the open article share their first word (e.g.
                # "CLEMENT (POPES)" vs "CLEMENT VI" or "CLEMENT XII"),
                # treat the candidate as a subsection. Real article
                # transitions always have different first words
                # (ROORKEE → ROOSEVELT, THEODORE). Applies even when
                # a running header names a LATER article on the page —
                # that header belongs to the *next* article, not the
                # tentative continuation.
                _open_firstword = _open_root.split()[0] if _open_root else ""
                _cand_firstword = _cand_root.split()[0] if _cand_root else ""
                firstword_says_continuation = (
                    candidate.is_tentative
                    and open_article is not None
                    and _open_firstword
                    and _open_firstword == _cand_firstword
                )
                # Title Case <section begin="Foo"/> names (those with any
                # lowercase letter) are usually subsection continuations
                # by Wikisource convention — EB1911 real article titles
                # are ALL CAPS. ALGAE's "Benthos" continuation and
                # "Occurrence in the rocks" get carved off as spurious
                # articles without this guard.
                #
                # Exception: biographical articles follow "Surname,
                # Firstname" (or "Surname, Firstname (Qualifier)") form,
                # which has lowercase letters but is always a real
                # article — Angelico/Fra, Comte/Auguste, Cervantes, etc.
                _sec_id = candidate.raw_sec_id or ""
                _is_bio_section = bool(re.match(
                    r"^[A-Z][a-zA-ZÀ-ÿ'\- ]+,\s+[A-Z]",
                    _sec_id,
                ))
                section_name_says_continuation = (
                    candidate.is_tentative
                    and open_article is not None
                    and _sec_id
                    and _sec_id != _sec_id.upper()
                    and any(c.islower() for c in _sec_id)
                    and not _is_bio_section
                )
                if body_text and candidate.is_tentative and (
                    exact_match or fuzzy_match
                    or heading_says_continuation
                    or firstword_says_continuation
                    or section_name_says_continuation
                ):
                    next_seq = len(open_article.segments) + 1
                    # If this is a subsection absorption (name differs
                    # from the open article), emit a «SEC:name» marker
                    # so the viewer can render it as a navigable heading
                    # and xrefs can link to it as #section-<slug>.
                    # Skip pure Wikisource continuations (same name as
                    # open article, or same name as an already-emitted
                    # SEC marker earlier in the article — Wikisource
                    # repeats `<section begin="Clement XII"/>` on each
                    # continuation page).
                    _sec_name = candidate.title.strip()
                    _already_seen = False
                    if _sec_name:
                        # Normalize by stripping trailing "(qualifier)" so
                        # a bare "CLEMENT VII" on a continuation page is
                        # recognized as a dup of an earlier
                        # "Clement VII (pope)" / "Clement VII (Antipope)".
                        def _norm_sec(n: str) -> str:
                            n = re.sub(r"\s*\([^)]*\)\s*$", "", n).strip()
                            return n.upper()
                        _cand_norm = _norm_sec(_sec_name)
                        _existing = "\n".join(s.text or "" for s in open_article.segments)
                        for _m in re.finditer(
                            r"\u00abSEC:([^\u00ab]+)\u00ab/SEC\u00bb", _existing
                        ):
                            if _norm_sec(_m.group(1)) == _cand_norm:
                                _already_seen = True
                                break
                    if (not exact_match
                            and not fuzzy_match
                            and _sec_name
                            and not _already_seen):
                        marker_body = (
                            f"\u00abSEC:{candidate.title.strip()}\u00ab/SEC\u00bb"
                            "\n\n" + body_text
                        )
                    else:
                        marker_body = body_text
                    open_article.segments.append(SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=next_seq,
                        text=marker_body,
                    ))
                    open_article.page_end = page.page_number
                    continue

                # New article
                detected = DetectedArticle(
                    title=candidate.title,
                    volume=page.volume,
                    page_start=page.page_number,
                    page_end=page.page_number,
                    article_type="article",
                    section_name=candidate.raw_sec_id or "",
                )
                if body_text:
                    detected.segments.append(SegmentInfo(
                        source_page_id=page.id,
                        page_number=page.page_number,
                        sequence=1,
                        text=body_text,
                    ))
                articles.append(detected)
                open_article = detected

        # Post-process: fix pages whose heading indicates they belong to
        # a different article.  Walk through detected articles and check
        # each page's heading.  If a run of consecutive pages has a heading
        # that matches a LATER article (by section marker), move those pages.
        articles = _fix_swallowed_pages(articles, pages)

        return articles

    finally:
        session.close()


# ── Persistence ────────────────────────────────────────────────────────


def persist_articles(detected: list[DetectedArticle]) -> int:
    """Create Article and ArticleSegment records from detected boundaries."""
    session = SessionLocal()
    try:
        for det in detected:
            article = Article(
                title=det.title,
                volume=det.volume,
                page_start=det.page_start,
                page_end=det.page_end,
                body=det.body,
                article_type=det.article_type if det.article_type == "plate" else None,
                section_name=det.section_name or None,
            )
            session.add(article)
            session.flush()

            for seg in det.segments:
                session.add(ArticleSegment(
                    article_id=article.id,
                    source_page_id=seg.source_page_id,
                    sequence_in_article=seg.sequence,
                    segment_text=(seg.text or "").strip(),
                ))

        session.commit()
        return len(detected)
    finally:
        session.close()
