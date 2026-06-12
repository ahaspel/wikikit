from dataclasses import dataclass, field

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal
from britannica.pipeline.stages.elements._title import (
    _letter_from_dropcap,
    _title_span,
    clean_title,
)
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


def _title_plaintext(text: str) -> str:
    """Project a TITLE field to plain text — the title path's own minimal need, NOT a
    general template sweeper.

    UNWRAPS styling-template wrappers keeping their content ({{mono|{{fs|108%|TITLE}}}}
    → TITLE, last param), and drops the drop-cap / size HTML tags (<big>S</big>UCCINIC →
    SUCCINIC) and «B»/«I» markers that mustn't leak into a rendered title.

    It does NOT silently delete unhandled templates: the old `_strip_templates` sweeper's
    content-destroying `{{…}}`→'' pass is gone, so a raw title-furniture template stays
    visible rather than vanishing.  (Honest title recursion via the TITLE element is a
    later step; this is the scoped field projection the boundary pass needs now.)"""
    for _ in range(5):
        text = re.sub(r"\{\{[^{}|]*\|([^{}|]*)\}\}", r"\1", text)
        text = re.sub(r"\{\{[^{}]*\|([^{}|]*)\}\}", r"\1", text)
    text = re.sub(
        r"</?(?:big|small|sub|sup|span|font)\b[^>]*>",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"«/?[BI]»", "", text)
    return text.strip()


# Wikisource section ID corrections: typos and false article names.
# Key = section ID as it appears in the source, value = corrected title
# (or None to treat as a generic continuation, not a new article).


# Bold-delimited title span-finding (the «B»-run heading) is owned by the
# sole title extractor, ``elements/_title.py:_title_span``.  The former
# local ``_extract_bold_delimited_title`` (+ its ``_looks_like_title_glue``
# /``_GLUE_YEAR_RE`` glue helpers) was a second copy of that walk and has
# been deleted — ``_parse_page_by_sections`` now calls ``_title_span``
# directly.






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
    # Raw title span (markers/footnote intact) carried for the title
    # producer to transform into the display title; "" for paths that
    # don't carve one.
    title_raw: str = ""

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

# Two-letter article titles that actually exist in the encyclopedia.
# Other 2-letter combinations (CH, RO, OF, etc.) are fragments.
_VALID_TWO_LETTER = frozenset({
    "AA", "AB", "AD", "AE", "AI", "AL", "AM", "AN", "AR", "AS", "AT",
    "AX", "AY",
})




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
            plate_title = _title_plaintext(inner).rstrip(",.")
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
                    clean = _title_plaintext(f).rstrip("}]")
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
            # ONE heading recognizer: ask the walk's OWN detector whether this
            # leaf carries a real article heading (→ article page, not a plate),
            # instead of the legacy per-page parser.  Lazy import breaks the
            # super_walker↔detect_boundaries cycle (super_walker imports
            # _split_out_plates for volume_stream).
            from britannica.pipeline.stages.super_walker import (
                has_article_heading)
            has_real_heading = has_article_heading(text)
            _markers = _match_section_begin(text)
            prefix_text = (text[:_markers[0][0]] if _markers else text).strip()
            prefix_bold = bool(re.match(
                r"«B»[A-ZÀ-Þ][A-ZÀ-Þ«»/IB’\s,.\-]+«/B»", prefix_text))
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


# Legacy per-page detect_boundaries(volume) — superseded by
# super_detect_boundaries (super_walker → article slices).
# Deleted 2026-05-27 per "one detect_boundaries function in the
# codebase".  Remaining helpers in this file are utilities still
# used by super_detect / super_walker / cli; the wholesale dead-
# helper cleanup is a separate pass.

def wipe_articles(volume: int) -> int:
    """Delete every Article in ``volume`` and all FK-dependent rows
    (ArticleSegment, ArticleContributor, CrossReference).
    Returns the count of articles deleted.

    SourcePages are kept (they're owned by the import stage).  Callers
    that want a fully-deterministic re-detect call this before
    ``persist_articles``.

    X-refs that target one of these articles from ANOTHER volume are
    UNLINKED (target_article_id ← None, status ← "unresolved") rather
    than deleted, so the originating volume's resolution work survives.
    """
    from britannica.db.models import (
        ArticleContributor, CrossReference)
    session = SessionLocal()
    try:
        art_ids = [a[0] for a in session.query(Article.id).filter(
            Article.volume == volume).all()]
        if not art_ids:
            return 0
        session.query(CrossReference).filter(
            CrossReference.target_article_id.in_(art_ids)
        ).update({"target_article_id": None,
                  "status": "unresolved"},
                 synchronize_session=False)
        session.query(CrossReference).filter(
            CrossReference.article_id.in_(art_ids)
        ).delete(synchronize_session=False)
        session.query(ArticleContributor).filter(
            ArticleContributor.article_id.in_(art_ids)
        ).delete(synchronize_session=False)
        session.query(ArticleSegment).filter(
            ArticleSegment.article_id.in_(art_ids)
        ).delete(synchronize_session=False)
        session.query(Article).filter(
            Article.id.in_(art_ids)
        ).delete(synchronize_session=False)
        session.commit()
        return len(art_ids)
    finally:
        session.close()


def persist_articles(detected: list[DetectedArticle]) -> int:
    """Create Article and ArticleSegment records from detected boundaries.

    Pure insertion — no implicit wipe.  Callers that want a full
    re-detect call ``wipe_articles(volume)`` first.  Both the CLI
    `detect-boundaries` and `tools/pipeline/rebuild_volume.py` do
    exactly that.
    """
    session = SessionLocal()
    try:
        for det in detected:
            # MOVE 2: detection no longer carries a title — `det.title`/`det.title_raw`
            # are "" (title rides unstripped in segment 0).  The title is produced in
            # exactly one place, `transform_articles.preprocess_article`/`walk_article`,
            # which writes `Article.title`/`title_raw`/`title_display`.  `title` is
            # NOT NULL, so the empty string is the placeholder until the transform runs.
            article = Article(
                title=det.title,
                volume=det.volume,
                page_start=det.page_start,
                page_end=det.page_end,
                body=det.body,
                article_type=det.article_type if det.article_type == "plate" else None,
                section_name=det.section_name or None,
                title_raw=det.title_raw or None,
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
