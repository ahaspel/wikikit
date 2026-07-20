from dataclasses import dataclass, field

from britannica.db.models import Article, ArticleSegment
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


# `_normalize_title` + `_VALID_TWO_LETTER` retired with `clean_title`: detection no
# longer flattens a heading to classify it — `super_walker._heading_text` reads the
# headword for the is-title test, and `produce_title` produces the title itself.


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


# ── Detection (pure) ──────────────────────────────────────────────────


# Running-head templates that carry the printed folio (case/whitespace tolerant).
_PAGEHEAD = re.compile(
    r"\{\{\s*(?:rh|running\s*header|eb1911\s+page\s+heading)\s*\|", re.I)
# Inside a side slot: {{em}}/{{gap}} are SPACING (strip); the rest are display
# wrappers (reduce to their text); what's left is the folio.
_PH_SPACING = re.compile(r"\{\{\s*(?:em|gap|nbsp)\b[^{}]*\}\}", re.I)
_PH_SPEC = re.compile(r"\{\{\s*(?:size|fs)\s*\|([^{}]*)\}\}", re.I)   # leads w/ a spec arg
_PH_PLAIN = re.compile(
    r"\{\{\s*(?:x-larger|larger|smaller|x-smaller|xx-larger|uc|sc|asc|sm)"
    r"(?:\s+block)?\s*\|([^{}]*)\}\}", re.I)
_PH_DIGITS = re.compile(r"(?<![\w.])\d+(?!\s*%)")
# A side slot that is PURELY a strict Roman numeral is a front-matter folio
# (preface/index pp. viii, x, xii) — paginated, just not in Arabic.
_PH_ROMAN = re.compile(
    r"^\s*(?=[ivxlcdm])m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})"
    r"\s*\.?\s*$", re.I)
# A plate's content IS an image — bracket form OR the layout-template forms.
_PLATE_IMAGE = re.compile(
    r"\[\[(?:File|Image):|\{\{\s*(?:Css image|raw image|FIS?|framed image)\b",
    re.I)
_FRONT_TITLE_TMPL = re.compile(
    r"\{\{\s*eb1911\s+(?:half\s+)?title\s+page|\{\{\s*eb1911\s+\w+\s+copyright"
    r"|\{\{\s*eb1911\s+contributor\s+table", re.I)
_FRONT_BANNER = re.compile(r"ELEVENTH\s+EDITION", re.I)


def _ph_slots(inner: str) -> list[str]:
    """Split a template's inner on top-level pipes (brace/bracket-depth aware)."""
    parts, depth, cur = [], 0, []
    for ch in inner:
        if ch in "{[":
            depth += 1; cur.append(ch)
        elif ch in "}]":
            depth -= 1; cur.append(ch)
        elif ch == "|" and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _ph_reduce(slot: str) -> str:
    """Collapse a side slot to its displayed text: strip spacing templates,
    reduce size/style wrappers to their content (``{{size|xl|125}}``→``125``,
    ``{{x-larger|544| }}``→``544``), so the bare folio is left."""
    for _ in range(6):
        before = slot
        slot = _PH_SPACING.sub(" ", slot)
        slot = _PH_SPEC.sub(lambda m: m.group(1).split("|")[-1], slot)
        slot = _PH_PLAIN.sub(lambda m: m.group(1).replace("|", " "), slot)
        if slot == before:
            break
    return slot


def folio_of(raw: str) -> str | None:
    """The printed page (folio) number from the leaf's running-head template,
    or ``None`` if the leaf is unpaginated.

    EB1911 is wholly paginated: every article body page prints its folio in the
    running head — Arabic in the main matter, Roman in the front matter — even a
    page that is one big figure or table.  The head is one of ``{{rh}}`` /
    ``{{RunningHeader}}`` / ``{{running header}}`` / ``{{EB1911 Page Heading}}``;
    its left/right side slots hold the folio (the centre holds the article
    title), possibly inside size/style wrappers.  An unpaginated leaf — no such
    number — is a plate insert (or a blank)."""
    m = _PAGEHEAD.search(raw)
    if not m:
        return None
    i, depth = m.start() + 2, 1
    while i < len(raw) and depth:
        if raw[i:i + 2] == "{{":
            depth += 1; i += 2
        elif raw[i:i + 2] == "}}":
            depth -= 1; i += 2
        else:
            i += 1
    args = _ph_slots(raw[m.start() + 2:i - 2])[1:]      # drop the template name
    side = ([args[0]] if args else []) + args[2:]       # left + right; skip centre
    for slot in side:
        red = _ph_reduce(slot).strip()
        d = _PH_DIGITS.search(red)
        if d:
            return d.group(0)
        if _PH_ROMAN.match(red):
            return red
    return None


def _is_front_matter_title(raw: str) -> bool:
    """A volume front page: a title-page template, or the ENCYCLOPÆDIA
    BRITANNICA / ELEVENTH EDITION banner standing BEFORE any image.  (A title
    page leads with the banner; a plate carries the same words only in an
    engraved credit AFTER its image — so position, not the substring, decides.)"""
    if _FRONT_TITLE_TMPL.search(raw):
        return True
    m = _FRONT_BANNER.search(raw)
    if not m:
        return False
    img = _PLATE_IMAGE.search(raw)
    return img is None or m.start() < img.start()


def _is_plate(raw: str) -> bool:
    """A plate is an UNPAGINATED leaf that carries an image — its content IS the
    image.  A paginated leaf is article body; a text-only unpaginated leaf is
    front matter; a banner title page is front matter.  No prose heuristics."""
    return (folio_of(raw) is None
            and bool(_PLATE_IMAGE.search(raw))
            and not _is_front_matter_title(raw))


def _split_out_plates(pages: list) -> tuple[list[DetectedArticle], list]:
    """PASS 1 of boundary detection.

    Every EB1911 plate insert is a single, self-contained Wikisource page, so
    plate detection is a pure per-page STRUCTURAL test (`_is_plate`): an
    unpaginated leaf that carries an image.  No prose word-counts, no preprocess
    — it reads the raw leaf.  Pull each plate into its own ``type='plate'``
    article and hand the rest to the article state machine, which then never has
    to reason about plates at all.

    The plate's RENDERED body, by contrast, IS preprocessed (as a one-leaf
    stream: corrections + quote-runs + entity-decode), so a plate follows the
    exact same content path as an article — only its recognition is separate."""
    from britannica.pipeline.stages.preprocess import make_stream, preprocess
    plates: list[DetectedArticle] = []
    rest: list = []
    for page in pages:
        raw = (page.wikitext or "").strip()
        if not raw:
            continue
        if not _is_plate(raw):
            rest.append(page)
            continue
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
                text=preprocess(make_stream([page]), page.volume),
            )],
        ))
    return plates, rest


def wipe_articles(volume: int) -> int:
    """Delete every Article in ``volume`` and all FK-dependent rows
    (ArticleSegment, ArticleContributor).
    Returns the count of articles deleted.

    SourcePages are kept (they're owned by the import stage).  Callers
    that want a fully-deterministic re-detect call this before
    ``persist_articles``.
    """
    from britannica.db.models import ArticleContributor
    session = SessionLocal()
    try:
        art_ids = [a[0] for a in session.query(Article.id).filter(
            Article.volume == volume).all()]
        if not art_ids:
            return 0
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
            # MOVE 2: detection no longer carries a title — `det.title` is ""
            # (title rides unstripped in segment 0).  The title is produced in
            # exactly one place, `transform_articles.preprocess_article`/`walk_article`,
            # which writes `Article.title`.  `title` is NOT NULL, so the empty
            # string is the placeholder until the transform runs.
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
