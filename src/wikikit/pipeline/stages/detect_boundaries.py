from dataclasses import dataclass, field

from wikikit.db.models import Article, ArticleSegment
from wikikit.db.session import SessionLocal
from wikikit.corpora import current_corpus
from wikikit.wikitext import PAGE_HEAD_RE, page_head_fields
import re
from wikikit.util.strings import until_stable

# Bold-delimited title span-finding (the «B»-run heading) is owned by the
# sole title extractor, ``elements/_title.py:_title_span``.  The former
# local ``_extract_bold_delimited_title`` (+ its ``_looks_like_title_glue``
# /``_GLUE_YEAR_RE`` glue helpers) was a second copy of that walk and has
# been deleted — ``_parse_page_by_sections`` now calls ``_title_span``
# directly.


# ── Detection output types ─────────────────────────────────────────────


@dataclass
class SegmentInfo:
    """Where a source page BEGINS inside its article's body — a page key.

    No text.  The article's body is one slice of the clean volume stream, held
    whole on `DetectedArticle.body`; this records only which page starts where.
    """
    source_page_id: int
    page_number: int
    sequence: int
    offset: int          # into DetectedArticle.body


@dataclass
class DetectedArticle:
    """An article boundary detected from raw wikitext.  Pure data — no DB models."""
    title: str
    volume: int
    page_start: int
    page_end: int
    article_type: str  # "article" or "plate"
    # The article's text: ONE slice of the clean volume stream, stored as sliced
    # and never edited.  It used to be a @property that rebuilt the body from
    # per-page segments, joining them with `" "` — or `"\n\n"` when the next
    # piece happened to start with a File link.  That heuristic existed only
    # because the cut threw away the separator it was guessing at; three other
    # call sites guessed differently (`""`, `"\n"`, `"\n\n"`).  Nothing is cut
    # now, so nothing has to be guessed.
    body: str = ""
    segments: list[SegmentInfo] = field(default_factory=list)   # page KEYS
    # Wikisource <section begin="X"> name from the article's first page.
    # Used as a tiebreaker in stable IDs when multiple articles share a
    # (volume, page_start).
    section_name: str = ""


# ── Per-page parsing helpers ───────────────────────────────────────────


# `_normalize_title` + `_VALID_TWO_LETTER` retired with `clean_title`: detection no
# longer flattens a heading to classify it — `eb1911/boundaries._heading_text` reads the
# headword for the is-title test, and `produce_title` produces the title itself.


# ── Detection (pure) ──────────────────────────────────────────────────


# Running-head templates that carry the printed folio (case/whitespace tolerant).
# Inside a side slot: {{em}}/{{gap}} are SPACING (strip); the rest are display
# wrappers (reduce to their text); what's left is the folio.
_PH_SPACING = re.compile(r"\{\{\s*(?:em|gap|nbsp)\b[^{}]*\}\}", re.I)
_PH_SPEC = re.compile(r"\{\{\s*(?:size|fs)\s*\|([^{}]*)\}\}", re.I)   # leads w/ a spec arg
_PH_PLAIN = re.compile(
    r"\{\{\s*(?:x-larger|larger|smaller|x-smaller|xx-larger|uc|sc|asc|sm)"
    r"(?:\s+block)?\s*\|([^{}]*)\}\}", re.I)
_PH_DIGITS = re.compile(r"(?<![\w.])\d+(?!\s*%)")
# A slot that IS a folio, not one that merely mentions a number.
_PH_BARE_FOLIO = re.compile(r"^\s*\d+\s*\.?\s*$")
# A side slot that is PURELY a strict Roman numeral is a front-matter folio
# (preface/index pp. viii, x, xii) — paginated, just not in Arabic.
_PH_ROMAN = re.compile(
    r"^\s*(?=[ivxlcdm])m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})"
    r"\s*\.?\s*$", re.I)


def _ph_reduce_once(slot: str) -> str:
    """One reduction pass over a running-head side slot."""
    slot = _PH_SPACING.sub(" ", slot)
    slot = _PH_SPEC.sub(lambda m: m.group(1).split("|")[-1], slot)
    return _PH_PLAIN.sub(lambda m: m.group(1).replace("|", " "), slot)


def _ph_reduce(slot: str) -> str:
    """Collapse a side slot to its displayed text: strip spacing templates,
    reduce size/style wrappers to their content (``{{size|xl|125}}``→``125``,
    ``{{x-larger|544| }}``→``544``), so the bare folio is left."""
    return until_stable(slot, _ph_reduce_once)


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
    m = PAGE_HEAD_RE.search(raw)
    if not m:
        return None
    args = page_head_fields(raw)
    side = ([args[0]] if args else []) + args[2:]       # left + right; skip centre
    reduced = [_ph_reduce(s).strip() for s in side]

    # A DIGIT anywhere in the side slots beats a Roman numeral in any of them.
    # The head is not always three slots: vol 5 p957 is
    # `{{EB1911 Page Heading|{{x-smaller|FRANCE]}}|CHARLES VIII.|X.|921}}`, where
    # `X.` is the VOLUME and 921 the folio — taking the first slot that looked
    # folio-shaped returned "X.".  Arabic is the main matter's folio and Roman
    # only the front matter's, so Arabic wins wherever both appear.
    for red in reduced:
        d = _PH_DIGITS.search(red)
        if d:
            return d.group(0)
    # Still nothing: the transcriber put the number in the CENTRE slot, which
    # normally holds the title (vol 9 p643 centres `{{size|xl|{{em|8.3}}611}}`,
    # vol 12 p226 `{{size|xl|209}}`).  The centre must reduce to a BARE number to
    # be read as a folio — vol 29's index pages centre the instruction
    # "…read the instructions given on Page 1.", and searching that for a digit
    # returns 1 as the folio of every index page.
    for slot in args[1:2]:
        red = _ph_reduce(slot).strip()
        if _PH_BARE_FOLIO.match(red):
            return _PH_DIGITS.search(red).group(0)
    for red in reduced:
        if _PH_ROMAN.match(red):
            return red
    return None


def _split_out_plates(pages: list) -> tuple[list[DetectedArticle], list]:
    """PASS 1 of boundary detection.

    A plate insert is a single, self-contained Wikisource page, so plate
    detection is a per-page test on the raw leaf — and it is the BOOK's test
    (``Corpus.is_plate``; EB1911's is eb1911/plates.py: an unpaginated
    leaf that carries an image).  Pull each plate into its own ``type='plate'``
    article, titled by the book (``Corpus.plate_title``), and hand the rest to
    article detection, which then never has to reason about plates at all.  A
    book that names no plate test has no plates: every page is text.

    The plate's RENDERED body, by contrast, IS preprocessed (as a one-leaf
    stream: corrections + quote-runs + entity-decode), so a plate follows the
    exact same content path as an article — only its recognition is separate."""
    from wikikit.pipeline.stages.preprocess import stream_with_keys
    book = current_corpus()
    plates: list[DetectedArticle] = []
    rest: list = []
    for page in pages:
        raw = (page.wikitext or "").strip()
        if not raw:
            continue
        if book.is_plate is None or not book.is_plate(raw):
            rest.append(page)
            continue
        # A plate IS a page, so its body is that page's clean text and it has
        # exactly one key, at offset 0.  `stream_with_keys` on the single page
        # gives both — and gives them with no page token in the text, same as
        # every article body.
        body, _pkeys, _skeys = stream_with_keys([page], page.volume)
        plates.append(DetectedArticle(
            title=book.plate_title(raw, page.volume, page.page_number),
            volume=page.volume,
            page_start=page.page_number,
            page_end=page.page_number,
            article_type="plate",
            body=body,
            segments=[SegmentInfo(
                source_page_id=page.id,
                page_number=page.page_number,
                sequence=0,
                offset=0,
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
    from wikikit.db.models import ArticleContributor
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
    re-detect call ``wipe_articles(volume)`` first, as the CLI
    `detect-boundaries` does.
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

            # Page KEYS, not text.  `Article.body` above holds the article whole,
            # exactly as sliced from the clean volume stream; these say only which
            # page starts where inside it.  The `.strip()` that used to be applied
            # to each piece here is gone with them — it was destroying the seam
            # newline that `make_stream` had deliberately put in.
            for seg in det.segments:
                session.add(ArticleSegment(
                    article_id=article.id,
                    source_page_id=seg.source_page_id,
                    sequence_in_article=seg.sequence,
                    offset=seg.offset,
                ))

        session.commit()
        return len(detected)
    finally:
        session.close()
