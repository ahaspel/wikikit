from dataclasses import dataclass, field

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal
from britannica.pipeline.stages.title import produce_title
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


def _extract_template_content(text: str, template_name: str) -> str | None:
    """Return the first argument of a ``{{template_name|…}}`` invocation
    in ``text``, walking brace depth so nested templates stay intact.

    Returns None if the template isn't present.  A regex like
    ``{{x-larger\\|([^}]+)\\}\\}`` truncates at the first inner ``}``,
    which breaks for nested wrappers (``{{x-larger|{{uc|TITLE}}}}``).
    """
    needle = "{{" + template_name + "|"
    start = text.find(needle)
    if start < 0:
        return None
    i = start + len(needle)
    depth = 1  # we're already past the opening "{{"
    out = []
    while i < len(text):
        if text[i] == "{" and i + 1 < len(text) and text[i + 1] == "{":
            depth += 1
            out.append("{{")
            i += 2
        elif text[i] == "}" and i + 1 < len(text) and text[i + 1] == "}":
            depth -= 1
            if depth == 0:
                return "".join(out)
            out.append("}}")
            i += 2
        else:
            out.append(text[i])
            i += 1
    return None


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
    # Strip wiki bold/italic markers (vol 1 AMPHITHEATRE wraps the
    # title field in `«B»…«/B»` and the plate label in
    # `<small>«B»{{sc|Plate}} I.«/B»</small>`; we want neither leaking
    # into rendered titles).  Bold/italic arrive as «B»/«I» markers
    # because clean_pages converts wikitext quote runs upstream.
    text = re.sub(r"«/?[BI]»", "", text)
    return text.strip()


# Wikisource section ID corrections: typos and false article names.
# Key = section ID as it appears in the source, value = corrected title
# (or None to treat as a generic continuation, not a new article).
_SECTION_ID_FIXES = {
    "Algebrab": "ALGEBRA",
    "Algebrae": None,  # continuation of ALGEBRA
    "PHOLOLOGY": None,  # typo for PHILOLOGY — continuation
}


# ---------------------------------------------------------------------------
# Bold-delimited title extraction.
#
# Empirically (vol-1 prototype: 1275/1369 articles, 93% match against the
# current regex-based extractor; remaining 7% are *mechanical* differences
# from un-unwrapped wikilinks / {{sc|}} templates, plus a handful of cases
# where the new rule is strictly better), the EB1911 invariant is:
#   - The title begins at the first `«B»` on the heading line.
#   - The title ends at the LAST `«/B»` that belongs to it, where
#     "belongs" means: the gap between successive `«B»…«/B»` blocks is
#     short title-continuation glue (parens, brackets, ` and `, ` or `,
#     ` & `, comma, space).  Body content (years, prose) is not glue.
#
# This rule replaces the old uppercase-run regex + clean_first body
# slicing.  It naturally handles every known failure pattern:
#   - NITRIC ACID-class (annotation in title) — single `«B»…«/B»` then
#     prose, so the parenthetical falls into body, not title.
#   - ODO OF BAYEUX-class (inner footnote inside bold) — inner `«FN:…«/FN»`
#     is opaque content inside the bold span, doesn't break extraction.
#   - ROBESPIERRE-class (name particle outside bold) — whatever's between
#     the outer `«B»` and `«/B»` is the title; case doesn't matter.
# ---------------------------------------------------------------------------

_BOLD_DELIMITER_OPEN = "«B»"
_BOLD_DELIMITER_CLOSE = "«/B»"
# Glue must not look like body prose.  Reject anything with a year.
_GLUE_YEAR_RE = re.compile(r"\b1[6-9]\d{2}\b|\b20\d{2}\b|c\.\s*\d{3,4}")


def _looks_like_title_glue(text: str) -> bool:
    """Is `text` (the gap between «/B» and the next «B») plausibly a
    title-continuation, e.g. `, `, ` and `, ` (or Smith), `?"""
    if len(text) > 200:
        return False
    if _GLUE_YEAR_RE.search(text):
        return False
    # Strip markers AND unwrap inline templates (small-caps, sc, etc.)
    # so the length check measures content, not template wrapping —
    # `(originally {{small-caps|Schneider}}, then {{small-caps|Schnitter}})`
    # is short content but long with templates.
    stripped = re.sub(r"«/?[A-Z]+(?::[^«»]*)?»", "", text)
    for _ in range(4):
        before = stripped
        stripped = re.sub(r"\{\{[^{}|]+\|([^{}|]*)\}\}", r"\1", stripped)
        stripped = re.sub(r"\{\{[^{}|]+\}\}", "", stripped)
        if stripped == before:
            break
    stripped = stripped.strip()
    if len(stripped) > 60:
        return False
    # Reject sentence breaks (`x. Y...`) — those are body content.
    if re.search(r"[a-z]\.\s+[A-Z]", stripped):
        return False
    return True


def _extract_bold_delimited_title(text: str) -> tuple[str | None, str]:
    """Return (title_raw, body_raw).  `title_raw` spans from the first
    `«B»` (inclusive) to the matching final `«/B»` (inclusive), still
    carrying its internal markup (wikilinks, templates, refs) for the
    caller to clean.  `body_raw` is everything after that final `«/B»`,
    left-trimmed of trivial leading whitespace and punctuation.

    Returns (None, text) when `text` does not begin with a bold span.
    The bold must be at the START — a `«B»` mid-prose is a body
    reference (e.g. ``"He was educated at «B»Cambridge«/B»…"``), NOT a
    new-article heading."""
    # Allow only insignificant leading whitespace before the first «B».
    # If anything else precedes it, this section is a continuation
    # whose first line happens to contain bold text mid-prose.
    leading = re.match(r"^\s*", text)
    first_open = leading.end() if leading else 0
    if not text.startswith(_BOLD_DELIMITER_OPEN, first_open):
        return None, text

    pos = first_open
    last_close = -1
    while True:
        b_idx = text.find(_BOLD_DELIMITER_OPEN, pos)
        if b_idx < 0:
            break
        e_idx = text.find(_BOLD_DELIMITER_CLOSE, b_idx + len(_BOLD_DELIMITER_OPEN))
        if e_idx < 0:
            break  # unbalanced; stop where we are
        e_idx_end = e_idx + len(_BOLD_DELIMITER_CLOSE)
        if last_close >= 0:
            gap = text[last_close:b_idx]
            if not _looks_like_title_glue(gap):
                break
        last_close = e_idx_end
        pos = e_idx_end

    if last_close < 0:
        return None, text

    title_raw = text[first_open:last_close]
    body_raw = text[last_close:]
    # If the title contains an unbalanced `[` or `(`, the matching
    # closing bracket/paren is at the start of the body (a multi-bold
    # title like `«B»MARS, MLLE«/B» [«B»ANNE … BOUTET«/B»]` ends at
    # the second `«/B»`, before the closing `]`).  Pull the closer in.
    for opener, closer in (("[", "]"), ("(", ")")):
        opens = title_raw.count(opener)
        closes = title_raw.count(closer)
        if opens > closes:
            # Find the matching closer in body_raw and extend title.
            needed = opens - closes
            i = 0
            while needed > 0 and i < len(body_raw):
                if body_raw[i] == closer:
                    needed -= 1
                    i += 1
                    if needed == 0:
                        title_raw = title_raw + body_raw[:i]
                        body_raw = body_raw[i:]
                        break
                elif body_raw[i] == opener:
                    # Nested opener inside body, abort balancing.
                    break
                else:
                    i += 1
    # Recognition only: split the heading from the body. Comma-consumption
    # (and title cleaning) is the title producer's job — see title.py.
    return title_raw, body_raw




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


# Letter-article opener: section starts with a drop-cap template
# (`{{di|X}}` / `{{dropinitial|X}}`), optionally wrapped in `{{Serif|...}}`,
# `'''...'''` (becomes `«B»...«/B»` post-converter), or both.  The 26
# letter articles A–Z are the ONLY sections corpus-wide that match this
# pattern (empirically verified 26/26 across 50,647 sections in
# `tools/_scratch/scan_dropcap_letter_sections.py`), so a structural
# match alone is sufficient — no body-keyword check needed.
_LETTER_OPENER_RE = re.compile(
    r"^\s*(?:'''|«B»)?\s*"
    r"(?:\{\{\s*[Ss]erif\s*\|\s*)?"
    r"\{\{\s*(?:[Dd]rop\s*[Ii]nitial|[Dd]i)\s*\|"
)


def _first_template_arg(text: str) -> str | None:
    """Return the first positional argument of an open template.

    The caller positions ``text`` right after the opening ``{{name|``;
    we read until the matching ``|`` (depth-1) or ``}}`` (depth-0),
    counting nested ``{{...}}`` so a nested template doesn't end the
    arg early.  Returns the raw arg text or None on imbalance.
    """
    depth = 0
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if text[i:i+2] == "{{":
            depth += 1
            out.append("{{"); i += 2; continue
        if text[i:i+2] == "}}":
            if depth == 0:
                return "".join(out)
            depth -= 1
            out.append("}}"); i += 2; continue
        if ch == "|" and depth == 0:
            return "".join(out)
        out.append(ch); i += 1
    return None


def _detect_letter_article(sec_id: str, sec_text: str) -> str | None:
    """Letter-article handler (A, B, C, …, Z; 26 total in EB1911).

    Letter articles open with a drop-cap template instead of a bold
    heading.  Source uses six template shapes for this:
      * `{{dropinitial|X}}` / `{{di|X}}`
      * `{{Serif|{{di|X|5em}}}}`
      * `{{di|{{serif|J}}|4em}}`
      * `{{dropinitial|'''{{serif|K}}'''|6em}}`
      * `'''{{di|T}}'''` (becomes `«B»{{di|T}}«/B»` post quote-run conv)

    Match shape: drop-cap at section start with a single-letter arg
    (after unwrapping at most one level of `{{serif|X}}` wrapper).
    Returns the uppercased letter, else None.
    """
    m = _LETTER_OPENER_RE.match(sec_text)
    if not m:
        return None
    arg = _first_template_arg(sec_text[m.end():])
    if arg is None:
        return None
    # Unwrap a single layer of `{{name|X}}` (handles `{{serif|J}}`).
    arg = re.sub(r"\{\{[^{}|]+\|([^{}|]+)\}\}", r"\1", arg)
    # Strip residual markers, quotes, whitespace.
    arg = re.sub(r"«/?[A-Z]+»|'''|''", "", arg).strip()
    if len(arg) != 1 or not arg.isalpha():
        return None
    letter = arg.upper()
    # If the enclosing section is named (not `s1`/`s2`/…), letter must
    # match the section name — keeps us from misidentifying a
    # drop-cap that happens to appear inside another article's section.
    if not re.match(r"^s\d+$", sec_id) and letter != sec_id.upper():
        return None
    return letter


# Match a `{|` opener up to the end of line OR the next `</noinclude>`,
# whichever comes first — EB1911 pages often place `{|...` immediately
# before `</noinclude>` on the same line.
_NOINCLUDE_TABLE_OPEN = re.compile(
    r"(?:^|\n)\s*\{\|[^\n<]*"
)
# Require the `|}` to be on its own (preceded by whitespace/newline
# and not followed by another `}`) so we don't match the `|}}` pattern
# that closes an empty-arg template like `{{rh|…|}}`.
_NOINCLUDE_TABLE_CLOSE = re.compile(
    r"(?:^|\n)\s*\|\}(?!\})"
)


def _strip_noinclude_preserve_tables(text: str) -> str:
    """Strip <noinclude> blocks but preserve any `{|...` and `|}` markers.

    Many EB1911 wikisource pages place the table opener `{|...` in the
    page header <noinclude> and the closer `|}` in the footer <noinclude>
    so the page displays as a valid table on its own. Stripping the whole
    block leaves the middle rows orphaned — the balanced-table extractor
    in process_elements then pairs a `{|` on one page with a `|}` many
    pages later, swallowing all intermediate prose (this was silently
    eating the Climate / Fauna and Flora / Population sections of
    UNITED STATES, THE, and likely other long geography-heavy articles).
    """
    def _keep_tables(m: re.Match) -> str:
        block = m.group(0)
        kept: list[str] = []
        for open_m in _NOINCLUDE_TABLE_OPEN.finditer(block):
            kept.append(open_m.group(0).strip())
        if _NOINCLUDE_TABLE_CLOSE.search(block):
            kept.append("|}")
        return ("\n" + "\n".join(kept) + "\n") if kept else ""

    return _NOINCLUDE.sub(_keep_tables, text)


def _preprocess_wikitext(text: str) -> str:
    """Minimal preprocessing of raw wikitext for boundary detection.

    Strips <noinclude> blocks (preserving any table `{|`/`|}` wrappers),
    <section end> tags, and normalizes line endings. Preserves
    <section begin> tags and all other raw wikitext.
    """
    text = _strip_noinclude_preserve_tables(text)
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

    # If the prefix is just preserved noinclude table wrappers ({|/|}
    # plus whitespace) it belongs WITH the following section's body
    # (those `{|` lines wrap the rows that follow). Fold them into
    # section 0 and clear the prefix so tentative-candidate detection
    # downstream (which keys on `not prefix`) still fires.
    if prefix and sections and re.fullmatch(
        r"(?:\s*(?:\{\|[^\n]*|\|\}))+\s*", prefix
    ):
        sections[0] = (sections[0][0],
                       (prefix + "\n" + sections[0][1]).strip())
        prefix = ""

    # Pre-split: a single section may contain multiple articles if there
    # are bold headings mid-text.  Split on '''ALLCAPS patterns at
    # paragraph boundaries only (double newline).  Raw wikitext has
    # hard line breaks within paragraphs, so a single \n would create
    # false splits on bold words that happen to start a line.
    _BOLD_SPLIT = re.compile(
        r"\n\n(?=«B»[A-Z])"
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
            # Peel leading HTML comments — typographic hints like
            # `<!-- column 2 -->` precede the bold heading on
            # MARRYAT, FREDERICK (vol 17 leaf 776) and similar pages.
            # Without this peel, the `<!--` start matches the table-
            # markup pre-filter below and the whole line gets skipped,
            # losing the bold heading on it.
            stripped = re.sub(
                r"^(?:<!--[^>]*-->\s*)+",
                "", stripped,
            )
            if not stripped:
                continue
            # Re-check table opener in case peeled leading {{nop}}
            # revealed a `{|` table opener underneath.
            if stripped.startswith("{|"):
                _in_table += 1
                continue
            if re.match(r"^(<!--.*?-->|</?table\b|\|\}|\|-|\|(?!«B»)|</?tr|</?td|\[\[(?:File|Image):|\{\{)", stripped, re.IGNORECASE) or re.search(r"</table>\s*$", stripped, re.IGNORECASE):
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
        # Multi-line bold titles: when a name like
        #   '''ROBESPIERRE, MAXIMILIEN FRANÇOIS MARIE ISIDORE'''
        #   '''DE''' (1758-1794), French revolutionist, ...
        # has its bold span continuation on the next physical line, the
        # extractor would only see line 1.  Extend first_line while the
        # next line starts with `«B»` AND the current line ended with a
        # bold close (so we're chaining a multi-bold title, not stepping
        # into a body paragraph that happens to begin with bold).
        while (
            _first_line_idx is not None
            and _first_line_idx + 1 < len(_sec_lines)
            and first_line.rstrip().endswith("«/B»")
            and _sec_lines[_first_line_idx + 1].lstrip().startswith("«B»")
        ):
            _first_line_idx += 1
            first_line = first_line + "\n" + _sec_lines[_first_line_idx].strip()
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

        # ---- Special handler: single-letter "A/B/C/.../Z" articles ----
        # EB1911 has 24 letter articles (A-Z except J and K — confirmed
        # by source-grounded audit on 2026-05-16; T uses a bold-wrapped
        # variant that the bold-delimited rule catches naturally; the
        # other 23 use a `{{dropinitial|X}}` or `{{di|X}}` template at
        # section start, sometimes wrapped in `{{Serif|...}}` or with
        # the letter nested inside `'''{{serif|X}}'''`).  Identified
        # positively by `_detect_letter_article` (dropinitial-at-start
        # AND letter-article body keywords).  If matched, this section
        # is a letter article and we short-circuit the bold detection.
        _letter = _detect_letter_article(sec_id, sec_text)
        if _letter is not None:
            title = _letter
            # Strip the opening dropinitial/di template (with any
            # `«B»...«/B»` or `{{Serif|...}}` wrappers) from body.
            body = re.sub(
                r"^\s*«B»\s*\{\{[Dd]i[^}]*\}\}\s*«/B»\s*",
                "", sec_text, count=1,
            )
            body = re.sub(
                r"^\s*\{\{Serif\s*\|\s*\{\{[Dd]i[^}]*\}\}\s*\}\}\s*",
                "", body, count=1)
            body = re.sub(
                r"^\s*\{\{(?:[Dd]rop\s*[Ii]nitial|[Dd]i)[^{}]*"
                r"(?:\{\{[^{}]*\}\}[^{}]*)*\}\}\s*",
                "", body, count=1).strip()
            candidates.append(CandidateArticle(
                title=title, body=body, is_tentative=False,
                raw_sec_id=sec_id,
            ))
            continue

        # ---- Primary path: bold-delimited title extraction ----
        # The dominant EB1911 invariant: a new article's title begins at
        # the first `«B»` of the first content line and ends at the last
        # `«/B»` that belongs to it (continuation glue between successive
        # bold spans is short — parens, connectors, comma).  Empirically
        # this captures ~99.9% of articles cleanly and avoids the
        # destructive-template-strip bug that mangled bodies in the
        # legacy path.  Everything else is a continuation segment.
        _new_title_raw, _new_body_raw = _extract_bold_delimited_title(
            first_line_unwrapped)
        if _new_title_raw:
            _new_title, _new_body_raw = produce_title(
                _new_title_raw, _new_body_raw)
        else:
            _new_title = ""

        if _new_title and _has_valid_title_content(
                _normalize_title(_new_title)):
            title = _new_title
            body = _new_body_raw
            remaining_lines = _sec_lines[_first_line_idx + 1:]
            if remaining_lines:
                remaining = "\n".join(remaining_lines).strip()
                if body and remaining:
                    body = body + "\n" + remaining
                elif remaining:
                    body = remaining
            pre_heading = "\n".join(_sec_lines[:_heading_start_idx]).strip()
            if pre_heading:
                body = pre_heading + "\n\n" + body
            body = body.strip()
            candidates.append(CandidateArticle(
                title=title, body=body, is_tentative=False,
                raw_sec_id=sec_id,
            ))
            continue

        # Neither the bold nor the dropinitial signal fired.  Suppress the
        # old `_is_letter_article` and `is_named-no-bold` fallbacks (they
        # produced false-positive duplicate articles for continuation
        # segments like vol 1 p33 "A").  Fall through to the existing
        # logic below, which now handles only continuation cases.
        clean_first = first_line_unwrapped.replace("«B»", "").replace("«/B»", "")
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

        # A bold heading at the start is the definitive signal for a
        # new article.  Named sections without bold are continuations
        # of the previous article repeated across pages on Wikisource.
        # `clean_pages` converts source `'''TITLE'''` to `«B»TITLE«/B»`,
        # so we look for the marker form.
        has_bold_heading = first_line_unwrapped.startswith("«B»")
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
                bold_match = re.match(r"^«B»([^«]+)«/B»", first_line)
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
            elif is_named and len(sec_id) != 1:
                # `_detect_letter_article` (special handler, runs first)
                # already classified any single-letter section that's
                # legitimately about the letter.  If we reach here with
                # a single-letter sec_id, it's not a letter article —
                # don't use the letter as a title.
                title = sec_id.upper()
            else:
                title = None
        # Removed: `elif is_named and not candidates and not prefix and
        # _is_article_section_id(sec_id):`.  Used to fire a tentative
        # new article when a continuation page's first section had a
        # named `<section begin="X">` matching the article's name but
        # no bold heading (because the heading was on the previous
        # page).  Under the bold-required rule we adopted (see
        # `_extract_bold_delimited_title`), a section without a bold
        # heading is ALWAYS continuation — never a new article.  This
        # fallback was splitting multi-page articles (AIR-ENGINE 481-483,
        # AIRY 483-485, ALECSANDRI 578-579, ALSTRÖMER 801-802 on vol 1)
        # into shorter pieces.  The continuation branch below now
        # handles these correctly.
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
                r"^(?:«B»[^«]+«/B»[\s,.\-]*(?:and\s+|&\s+)?)+",
                first_line, re.IGNORECASE,
            )
            if bold_heading:
                body = first_line[bold_heading.end():].lstrip(" ,.")
            else:
                # Non-bold heading.  Re-run the heading regex against a
                # NON-destructively cleaned version of first_line — only
                # marker-strip and `{{uc|X}}` uppercasing (both content-
                # preserving), no template-stripping.  This keeps nested
                # templates like `{{nowrap|N{{tfrac|M}} m.}}` intact in
                # the sliced body so they reach transform_articles whole.
                first_for_body = first_line.replace(
                    "«B»", "").replace("«/B»", "")
                first_for_body = re.sub(
                    r"\{\{uc\|([^{}|]*)\}\}",
                    lambda m: m.group(1).upper(),
                    first_for_body, flags=re.IGNORECASE,
                )
                body_heading = re.match(
                    rf"^({_UC_CHAR}{_UC_RUN}"
                    rf"(?:"
                    rf"[\s,]+{_UC_CHAR}{_UC_RUN}"
                    rf"|\s+[IVX]+(?![A-Z])"
                    rf"|{_QUALIFIER}{_UC_CHAR}{_UC_RUN}"
                    rf")*)",
                    first_for_body,
                )
                if body_heading:
                    body = first_for_body[
                        body_heading.end():].lstrip(" ,.")
                else:
                    # Title was found via destructive template-strip
                    # (e.g. `{{sc|X}}` wrapping).  Last-resort: use the
                    # mangled slice and accept the damage — these cases
                    # are rare; if they show up in metrics, expand the
                    # set of content-preserving unwraps above.
                    body = clean_first[
                        len(heading_text):].lstrip(" ,.")
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
            stripped_first = re.sub(
                r"^«B»[^«]+«/B»\s*", "", first_line).lstrip(" ,.")
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
    # Split on bold headings at line/paragraph boundaries.  Source
    # `'''TEXT'''` is `«B»TEXT«/B»` after clean_pages.  Edge cases:
    #   - Author-link wrap: `«B»[[Author:John Keats|KEATS, JOHN]]«/B»`
    #   - Inline etymology: `«B»SEWING<ref>…</ref> MACHINES.«/B»`
    _BOLD_HEADING = re.compile(
        r"(?:^|\n\n)(\u00ABB\u00BB"
        r"(?:\[\[[^\]|]*\|)?"
        r"[A-Z\u00C0-\u00DE]"
        r"(?:[A-Z\u00C0-\u00DE'\u2018\u2019\-,. ]|<ref[^>]*>.*?</ref>)*"
        r"(?:\]\])?"
        r"\u00AB/B\u00BB)",
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
        stripped_bold = bold_marker.replace("«B»", "").replace("«/B»", "")
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

    # Strip formatting markers before heading detection.  After
    # clean_pages's `_convert_quote_runs`, bold and italic are «B»/«I»
    # markers (the raw `'''`/`''` forms no longer exist).
    clean = (line.replace("«B»", "").replace("«/B»", "")
                 .replace("«I»", "").replace("«/I»", ""))

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


_PLATE_FIELD_RE = re.compile(
    r"^\s*"
    r"(?:\{\{(?:smaller|larger)\|)?"      # optional outer size wrapper
    r"\{\{(?:sc|uc|small-caps)\|"          # {{sc| (or uc / small-caps)
    r"Plate"                              # the word "Plate"
    r"(?:"
    r"\s+([IVX]+)\.?\}\}"                 #   …I.}}    (roman inside the sc)
    r"|"
    r"\s*\}\}\s*([IVX]+)\.?"              #   }} II.   (roman outside the sc)
    r"|"
    r"\s*\}\}"                            #   }} alone — single-plate article
    r")",                                 #     (vol 2 ANTHROPOLOGY: no Roman)
    re.IGNORECASE,
)


# Non-anchored variant of ``_PLATE_FIELD_RE`` — matches a
# ``{{sc|Plate …}}`` / ``{{uc|Plate …}}`` token *anywhere* on the page,
# not just at the start of a heading-template field.  Used by
# ``_plate_label_from_content`` for the title suffix of heuristic-
# detected plates whose ``Plate N.`` label sits in a layout-table cell.
_PLATE_LABEL_ANY_RE = re.compile(
    r"\{\{(?:sc|uc|small-caps)\|"
    r"Plate"
    r"(?:"
    r"\s+([IVX]+)\s*\.?\s*\}\}"           #   …I.}}  /  …I}}
    r"|"
    r"\s*\.?\s*\}\}\s*([IVX]+)\.?"        #   }} II. /  .}} II
    r"|"
    r"\s*\.?\s*\}\}"                      #   }} alone /  .}}  (single-plate)
    r")",
    re.IGNORECASE,
)


def _plate_label_from_content(raw: str) -> str | None:
    """Return a plate's Roman numeral (or ``""`` for a bare ``Plate.``
    with no numeral) from a ``{{sc|Plate …}}`` / ``{{uc|Plate …}}``
    token appearing *anywhere* on the page.

    Used only as a title-suffix fallback for heuristic-detected plates
    (ROUND TOWERS, TAPESTRY, EGYPT, INDIA, …): their page heading is
    ``{{c|{{x-larger|TITLE}}}}`` rather than ``{{rh|…|{{sc|Plate N.}}}}``,
    so ``_extract_plate_number`` (heading-fields only — deliberately
    narrow, since it also gates plate *detection* via
    ``_heading_names_plate``) finds nothing.  The ``Plate N.`` label
    still exists, just inside a layout-table cell instead.  Since this
    only runs once a page is already known to be a plate, a stray
    ``{{sc|Plate}} III`` cross-reference is the worst it can pick up — a
    cosmetic title-suffix slip, never data loss."""
    m = _PLATE_LABEL_ANY_RE.search(raw)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").upper()


def _extract_plate_number(raw: str) -> str | None:
    """Return the Roman-numeral plate number from a page's heading
    template, or None if the page heading carries no `Plate N.` token.

    This is the same walk as `_heading_names_plate`; it returns the
    numeral instead of a bool so the boundary detector can compose a
    title like "DOG, PLATE I" and the slug derived from it stays
    deterministic across rebuilds (the plate number is the only
    field guaranteed to be unique per plate).

    Searches the whole raw page rather than only the
    ``<noinclude>…</noinclude>`` block: transcribers vary on whether
    the page-heading template lives inside the noinclude (DOG vol 8,
    SHAKESPEARE vol 24) or outside it (AEGEAN CIVILIZATION vol 1).
    """
    for tmpl_name in ("rh", "EB1911 Page Heading"):
        idx = raw.find("{{" + tmpl_name + "|")
        if idx < 0:
            continue
        fields: list[str] = []
        depth = 0
        current = ""
        for ch in raw[idx:]:
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
        for f in fields[1:]:
            # Strip cosmetic wrappers transcribers add around the
            # plate-label field (vol 1 AMPHITHEATRE:
            # `<small>'''{{sc|Plate}} I.'''</small>`); the regex below
            # only knows the bare and `{{(smaller|larger)|…}}` forms.
            cleaned = re.sub(r"</?(?:big|small|sub|sup)\b[^>]*>",
                             "", f, flags=re.IGNORECASE)
            cleaned = re.sub(r"«/?[BI]»", "", cleaned)
            m = _PLATE_FIELD_RE.match(cleaned)
            if m:
                # group(1)/group(2) are the Roman numeral when the
                # heading carries one; both are None for bare
                # `{{sc|Plate}}` (single-plate articles).  Empty
                # string in that case so the gate fires while the
                # title-composer can distinguish "PLATE I" from
                # "PLATE" with no number.
                return (m.group(1) or m.group(2) or "").upper()
    return None


def _heading_names_plate(raw: str) -> bool:
    """True iff the page's heading template carries an explicit
    `Plate N.` token in one of its named-header positions.

    EB1911 wikisource convention: every plate insert page has a page-
    heading template (`{{rh|…}}` or `{{EB1911 Page Heading|…}}`) where
    one of the side-header fields holds `{{sc|Plate I.}}` (or II/III/…).
    The article-name field holds the parent article (DOG, AMPHITHEATRE,
    &c.).  Examples:

        {{EB1911 Page Heading||DOG||{{sc|Plate I.}}}}      (right side)
        {{EB1911 Page Heading|{{sc|Plate II.}}|DOG|...|...}} (left side)
        {{rh||GRAPHIC ART|{{smaller|{{sc|Plate I.}}}}}}    (outside noinclude)

    Transcribers vary on whether the heading template sits inside the
    page's ``<noinclude>…</noinclude>`` block or outside it (AEGEAN
    plates put it outside, DOG/SHAKESPEARE put it inside), so the walk
    looks at the whole raw page.
    """
    return _extract_plate_number(raw) is not None


def _compose_plate_title(raw: str, volume: int, page_number: int) -> str:
    """Build the title for a plate-insert page: the parent article's
    name (from the page heading or, failing that, the ``<section
    begin="…"/>`` name) suffixed with the plate's ``PLATE N.`` label.

    Priority for the base name:
      1. ``{{x-larger|TITLE}}`` / ``{{larger|TITLE}}`` in the
         ``<noinclude>`` heading (``{{c|{{x-larger|ROUND TOWERS}}}}``).
      2. The first plain-text field of a ``{{rh|…}}`` /
         ``{{EB1911 Page Heading|…}}`` heading (the article-name slot).
      3. The ``<section begin="X"/>`` name (REGALIA's plate carries
         ``<section begin="Regalia"/>`` but a layout-template heading).
      4. ``PLATE (VOL. n, P. m)`` — last resort, no recognizable title.

    The plate number comes from the heading's ``{{sc|Plate N.}}``
    side-header (``_extract_plate_number``) or, when the label sits in a
    layout-table cell instead, from anywhere on the page
    (``_plate_label_from_content``).  A bare ``Plate.`` (no numeral)
    yields the ``, PLATE`` suffix so the plate's slug stays distinct
    from its parent article's."""
    plate_title: str | None = None
    header_match = re.search(r"<noinclude>(.*?)</noinclude>", raw, re.DOTALL)
    if header_match:
        hdr = header_match.group(1)
        # {{x-larger|TITLE}} / {{larger|TITLE}} — walk braces so a
        # nested {{uc|SHAKESPEARE}} inside {{x-larger|…}} doesn't
        # truncate the field at the inner `}`.
        inner = _extract_template_content(hdr, "x-larger")
        if inner is None:
            inner = _extract_template_content(hdr, "larger")
        if inner is not None:
            plate_title = _strip_templates(inner).rstrip(",.")
        else:
            # {{rh|…|TITLE|…}} / {{EB1911 Page Heading|…|TITLE|…}} —
            # split on top-level pipes (brace-depth aware) for fields.
            for tmpl_start in (hdr.find("{{rh|"),
                               hdr.find("{{EB1911 Page Heading|")):
                if tmpl_start < 0:
                    continue
                fields: list[str] = []
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
                for f in fields[1:]:
                    clean = _strip_templates(f).rstrip("}]")
                    if (clean and len(clean) > 2 and not clean.isdigit()
                            and not re.match(r"^Plate\b", clean)
                            and re.search(r"[A-Za-z]{3,}", clean)):
                        plate_title = clean
                        break
                if plate_title:
                    break
    if not plate_title:
        secs = _match_section_begin(raw)
        if secs:
            name = (secs[0][2] or "").strip()
            if (name and not re.fullmatch(r"s\d+", name, re.IGNORECASE)
                    and re.search(r"[A-Za-z]{3,}", name)):
                plate_title = name.upper()
    if not plate_title:
        plate_title = f"PLATE (VOL. {volume}, P. {page_number})"

    plate_num = _extract_plate_number(raw)
    if plate_num is None:
        plate_num = _plate_label_from_content(raw)
    if plate_num:
        plate_title = f"{plate_title}, PLATE {plate_num}"
    elif plate_num is not None:
        plate_title = f"{plate_title}, PLATE"
    return plate_title


def _strip_wiki_table_spans(text: str) -> str:
    """Drop every balanced ``{|…|}`` wiki-table span — together with the
    ``{{ts|…}}`` styling templates and ``[[Image:…]]`` cells inside it,
    however deeply nested.  ``{{…}}`` template blocks are skipped over
    so a ``{{Ts|vmi|}}``'s ``|}}`` isn't misread as a table closer.

    ``_is_plate_page`` measures the *narrative prose* left after the
    figure layout is removed.  The old non-greedy ``\\{\\|.*?\\|\\}`` strip
    stopped at the first ``|}`` — the innermost image-cell table — on
    the deeply-nested plate inserts (ROUND TOWERS, TAPESTRY, EGYPT,
    INDIA, …), leaving the rest of the layout's ``{{ts|…}}`` noise
    behind and pushing the prose-word count over threshold."""
    n = len(text)

    def _skip_template(start: int) -> int:
        depth, j = 1, start + 2
        while j < n - 1 and depth > 0:
            t = text[j:j + 2]
            if t == "{{":
                depth += 1
                j += 2
            elif t == "}}":
                depth -= 1
                j += 2
            else:
                j += 1
        if depth > 0:
            # Unbalanced ``{{`` (malformed source — e.g. CAPITAL's
            # ``{{EB1911 Fine Print|{{sc|Fig.}} 1.—…`` with no matching
            # ``}}`` before the cell ends).  Treat the ``{{`` as literal
            # and resume scanning, rather than gobbling to end-of-text.
            return start + 2
        return j

    out: list[str] = []
    i = 0
    while i < n:
        two = text[i:i + 2]
        if two == "{|":
            depth, j = 1, i + 2
            while j < n - 1 and depth > 0:
                t = text[j:j + 2]
                if t == "{{":
                    j = _skip_template(j)
                elif t == "{|":
                    depth += 1
                    j += 2
                elif t == "|}":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j                      # drop the whole table span
        elif two == "{{":
            j = _skip_template(i)
            out.append(text[i:j])       # keep top-level templates intact
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


_PROSE_CELL_RE = re.compile(
    r"(?:^|\n)\s*\|(?!\}|-)"
    r"(?:[A-Za-z][\w-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s|]+)\s*)*"   # |attrs| prefix
    r"\|?\s*"
    r"([^|\n][^\n]*)",                                               # cell content
)

# Caption / legend / credit lead-ins — a cell whose text begins with
# one of these is figure material, never running article prose.
_CAPTION_LEADIN_RE = re.compile(
    r"^(?:"
    r"(?:Fig|Plate)s?\.?\s*[\dIVXLCivxlc]"        # Fig. 7.—…  /  Plate II.
    r"|[\dIVXLCivxlc]+\s*[.,]?\s*[—–-]"           # 1.—THE SCEPTRES…  /  i.—…
    r"|\(?\s*[a-z]\s*\)"                           # (a) …  /  a) …
    r"|\(?\s*(?:Photo|From|After|By\s+permission|Copyright|Drawn|Redrawn|"
    r"Reproduced|Modified|Lent|Engraved)\b"
    r")",
    re.IGNORECASE,
)


def _has_prose_cell(text: str) -> bool:
    """True if some ``{|…|}`` cell holds a long run of running prose — a
    real article paragraph laid out in a table, not a caption / credit /
    title fragment.

    Vetoes the ``_is_plate_page`` heuristic on pages like vol 22
    POLLINATION p18, whose bird-/slug-pollination paragraphs sit inside
    a ``{|cellpadding=…|}`` figure grid: ``_strip_wiki_table_spans``
    (correctly, for a real plate) drops the whole table, so the prose-
    word count collapses to ~0 and the page looks like a plate.  Genuine
    plate cells are short or, at most, a verbose caption; the 40-word
    floor on text that *isn't* a caption / legend / credit lead-in only
    trips on a real article paragraph."""
    for m in _PROSE_CELL_RE.finditer(text):
        cell = m.group(1)
        # Pull out the image link, line-breaks, and (iteratively) any
        # styling-template wrappers so the leftover is the bare cell
        # text — then it can be tested for a caption lead-in.
        cell = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", " ", cell, flags=re.IGNORECASE)
        cell = re.sub(r"<br\s*/?>|<[^>]+>", " ", cell, flags=re.IGNORECASE)
        for _ in range(6):
            new = re.sub(r"\{\{[^{}]*\}\}", " ", cell)
            if new == cell:
                break
            cell = new
        cell = re.sub(r"«/?[BI]»|&[a-z]+;|&#\d+;", " ", cell, flags=re.IGNORECASE)
        cell = re.sub(r"^\s*[|!]+\s*", "", cell)            # leftover cell pipes
        # Strip leading punctuation residue (a stranded ``. 7.—`` after
        # the ``{{sc|Fig}}`` wrapper was removed) so the caption lead-in
        # test sees ``7.—Flemish Mitre…`` rather than ``. 7.—…``.
        cell = re.sub(r"^[\s.,;:—–\-]+", "", cell)
        cell = cell.strip()
        if not cell or _CAPTION_LEADIN_RE.match(cell):
            continue
        words = cell.split()
        if len(words) >= 40 and any(c.islower() for c in cell):
            return True
    return False


_RH_SIDE_PAGENUM_RE = re.compile(
    r"\{\{(?:rh|RunningHeader|EB1911\s+Page\s+Heading)\|", re.IGNORECASE)


def _rh_has_page_number_in_side_slot(text: str) -> bool:
    """True if the rh-shape page-heading template's left or right
    side-slot carries a printed page number.

    EB1911 plate inserts are unpaginated — the rh either is absent or
    carries no page number in its side slots (the centre slot holds
    the article title; side slots are empty, or hold ``{{sc|Plate I.}}``
    / a section keyword).  An article body page that happens to consist
    of just rh + a single big figure (genealogical tables, large maps,
    big diagrams) DOES carry its printed page number in the rh — that's
    how a reader looks it up.

    This is the raw-source distinguisher between "plate insert with one
    composite image" (UNIFORMS, NEUROPATHOLOGY, COLORADO map) and
    "body page with one big figure" (BOURBON / GUISE genealogies,
    EGYPT tools page, etc.).
    """
    m = _RH_SIDE_PAGENUM_RE.search(text)
    if not m:
        return False
    start = m.end()
    depth, i = 2, start
    while i < len(text) and depth > 0:
        if text[i:i+2] == "{{":
            depth += 2; i += 2
        elif text[i:i+2] == "}}":
            depth -= 2
            if depth == 0:
                break
            i += 2
        else:
            i += 1
    # Split args on top-level pipes.
    args, d, cur = [], 0, []
    for ch in text[start:i]:
        if ch == "{":
            d += 1; cur.append(ch)
        elif ch == "}":
            d -= 1; cur.append(ch)
        elif ch == "|" and d == 0:
            args.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    # Side slots: index 0 (left) and any slot at index >= 2 (right /
    # tail — covers 3-slot ``{{rh|L|M|R}}`` AND 4-slot
    # ``{{EB1911 Page Heading|L|M|R|tail}}``).  Centre slot (index 1)
    # holds the article title, never a page number.
    side_slots = ([args[0]] if args else []) + args[2:]
    page_num_re = re.compile(r"(?<![\w.])\d{2,}(?!\s*%)")
    for slot in side_slots:
        slot = re.sub(r"\{\{em\|[^}]*\}\}", "", slot)  # ``{{em|2.4}}`` is spacing
        if page_num_re.search(slot):
            return True
    return False


def _is_plate_page(text: str) -> bool:
    """Detect plate pages — mostly images with little prose.

    In raw wikitext, images are [[File:...]] or [[Image:...]].
    A true plate has minimal running prose (just captions and maybe a
    title). Pages that happen to have several images mid-article
    (e.g. vol 17 MAP p.641 with 4 figure images) are NOT plates —
    they're text pages with figures embedded.

    Single-image plate inserts (UNIFORMS, NEUROPATHOLOGY, COLORADO map
    etc.) need the structural rh-page-number check to distinguish them
    from body pages devoted to a single big figure (genealogy tables,
    large diagrams that span a full page).
    """
    stripped = text.strip()
    img_count = len(re.findall(r"\[\[(?:File|Image):", stripped, re.IGNORECASE))
    if img_count < 1:
        return False
    # An article paragraph laid out in a table cell → not a plate
    # (POLLINATION p18 et al.) — UNLESS the page also carries a
    # ``{{sc|Plate N.}}`` label, which an article continuation page
    # never does but a plate insert with a descriptive matter-paragraph
    # (PALAEONTOLOGY p633's "Materials for the Restoration of
    # Ichthyosaurs.—This plate illustrates…") does.  See _has_prose_cell.
    if _has_prose_cell(stripped) and _plate_label_from_content(stripped) is None:
        return False

    # Count non-image, non-table, non-template, non-caption words
    # (actual running prose).  Plate captions/matter live in
    # ``{|…|}`` cells (stripped) or ``{{center|…}}`` / ``{{sc|…}}`` /
    # ``{{EB1911 Fine Print|…}}`` wrappers (stripped here); whatever
    # plain text remains is the page's narrative prose, and a real plate
    # has almost none.
    prose = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", stripped, flags=re.IGNORECASE)
    prose = _strip_wiki_table_spans(prose)
    prose = re.sub(r"<table\b.*?</table>", "", prose, flags=re.DOTALL | re.IGNORECASE)
    for _ in range(8):
        _next = re.sub(r"\{\{[^{}]*\}\}", " ", prose)
        if _next == prose:
            break
        prose = _next
    # Stray ``Fig. N.—…`` captions not wrapped in a template.
    prose = re.sub(r"Figs?\.\s*[\dIVXLCivxlc]+[^.\n]*\.",
                   "", prose, flags=re.IGNORECASE)
    prose_words = len(prose.split())

    # Tight thresholds — genuine plates have very little narrative prose.
    if img_count >= 3:
        return prose_words <= 80
    if img_count >= 2:
        return prose_words <= 30
    # Single-image plate insert: ~7 such pages corpus-wide (UNIFORMS,
    # NEUROPATHOLOGY, COLORADO map, JERUSALEM map, POLAR REGIONS map).
    # Distinguish from a body page with one big figure (BOURBON/GUISE
    # genealogies, EGYPT tools page) by the rh — body pages carry the
    # printed page number in the rh's side slots; plate inserts don't.
    if prose_words > 5:
        return False
    return not _rh_has_page_number_in_side_slot(stripped)


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
                        # The segment body still has the raw `«B»…«/B»`,
                        # which the transform stage would re-render as
                        # `«B»…«/B»`, duplicating the article title in
                        # the body.
                        first = moved_segments[0]
                        stripped = re.sub(
                            r"^\s*«B»(?:\[\[[^\]]*\|)?([^«\]]+)(?:\]\])?«/B»[\s,.\-]*",
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


def _split_out_plates(pages: list) -> tuple[list[DetectedArticle], list]:
    """PASS 1 of boundary detection.

    Every EB1911 plate insert is a single, self-contained Wikisource
    page, so plate detection is a pure per-page predicate — no
    article-boundary state machine needed.  Pull each plate page into
    its own ``type='plate'`` article and hand the remaining (plate-free)
    pages to the article state machine, which then never has to reason
    about plates at all.

    A page is a plate when:
      • it carries a ``{{sc|Plate N.}}`` / ``{{uc|Plate N.}}`` /
        ``{{sc|Plate.}}`` label anywhere — in a page-heading side-header
        (``_heading_names_plate``) or in a layout-table cell
        (``_plate_label_from_content``).  An EB1911 plate insert always
        has one; an article page never does.  This is the authoritative
        signal — accepted regardless of anything else on the page; or
      • it has no such label but is image-heavy with negligible running
        prose (``_is_plate_page``) AND doesn't itself open a real
        (bold-titled) article — the ~20 label-less plate inserts (ALTAR,
        COSTUME, FORAMINIFERA, …).  A tiny stub with a couple of figures
        is still an article, not a plate."""
    plates: list[DetectedArticle] = []
    rest: list = []
    for page in pages:
        raw = (page.wikitext or "").strip()
        if not raw:
            continue
        text = _preprocess_wikitext(raw)
        if _heading_names_plate(raw) or _plate_label_from_content(raw) is not None:
            is_plate = True
        elif _is_plate_page(text):
            parsed = _parse_page_by_sections(text)
            if parsed is None:
                parsed = _split_on_bold_headings(text)
            has_real_heading = any(
                not c.is_tentative for c in parsed.candidates)
            prefix_bold = bool(re.match(
                r"«B»[A-ZÀ-Þ][A-ZÀ-Þ«»/IB’\s,.\-]+«/B»",
                parsed.prefix_text or ""))
            is_plate = not has_real_heading and not prefix_bold
        else:
            is_plate = False
        if is_plate:
            plates.append(DetectedArticle(
                title=_compose_plate_title(raw, page.volume, page.page_number),
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
            ))
        else:
            rest.append(page)
    return plates, rest


def detect_boundaries(volume: int) -> list[DetectedArticle]:
    """Detect article boundaries from raw wikitext.

    Reads SourcePages from the database but writes nothing.
    Returns a list of DetectedArticle with titles, page ranges, and
    raw wikitext segments.

    Two passes: ``_split_out_plates`` lifts every (self-contained)
    plate page out first; the streaming article state machine below then
    runs over the remaining pages and never sees a plate.
    """
    session = SessionLocal()

    try:
        all_pages = (
            session.query(SourcePage)
            .filter(SourcePage.volume == volume)
            .order_by(SourcePage.page_number)
            .all()
        )

        # PASS 1 — pull out every plate (each is one self-contained page).
        plate_articles, pages = _split_out_plates(all_pages)

        # PASS 2 — article boundaries over the remaining (plate-free) pages.
        articles: list[DetectedArticle] = []
        open_article: DetectedArticle | None = None

        for page in pages:
            # Read raw wikitext and preprocess minimally.
            # Corrections from data/corrections.json have already been
            # applied to `wikitext` during clean_pages.
            raw = (page.wikitext or "").strip()
            if not raw:
                continue
            text = _preprocess_wikitext(raw)

            # Try to parse article boundaries from this page.
            parsed = _parse_page_by_sections(text)
            if parsed is None:
                parsed = _split_on_bold_headings(text)

            # Prefix text belongs to the currently open article — unless
            # it starts with a bold ALL-CAPS heading, which signals a new
            # article placed before the first section marker on this page.
            # Only all-caps titles are treated as articles; mixed-case bold
            # text is a subsection heading within the current article.
            if parsed.prefix_text:
                _prefix_art_match = re.match(
                    r"\u00ABB\u00BB([A-Z\u00C0-\u00DE][A-Z\u00C0-\u00DE\u00AB\u00BB/IB\u2019\s,.\-]+)\u00AB/B\u00BB",
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
                # ``<section begin="Charles X."/>`` on a continuation
                # page would normally be absorbed as a subsection of the
                # currently open article by the Title-Case heuristic
                # below.  But if a real article with that section name
                # already exists and is closed — an interspersed
                # continuation page (CHARLES X., DICTIONARY,
                # HENRY VI./VII.) — the content belongs to THAT article:
                # route it there instead of absorbing.  (Plate pages are
                # lifted out by ``_split_out_plates`` before this loop
                # runs, so the old plate branch here is gone.)
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
                    if body_text:
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

        # Merge the PASS-1 plates back in, in page order.  When a plate
        # and an article share a page_start (a plate insert can fall on
        # the same printed page where an article continues), the article
        # — which "owns" that page range — sorts first.
        articles.extend(plate_articles)
        articles.sort(key=lambda a: (
            a.page_start, 0 if a.article_type == "article" else 1, a.title))

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
