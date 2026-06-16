"""Super-walker — volume-level article boundary detection.

A volume is a closed interval on article boundaries (its first byte is an
article's start, its last byte is an article's end; no article straddles a
volume boundary).  So article detection is: lay out the whole volume's pages
as one stream and cut at each article-opening heading.  A continuation page
(no new heading) just flows into the current article.  No per-page parsing,
no continuation-merge.

This module does **boundary detection ONLY** — it turns pages into articles.
It does not clean titles, extract elements, or do anything within an article.
(Architecture: see memory project_title_as_element.md.)

An article starts at a *title block* — a heading the classifier calls a title,
sitting at a block start (section open, blank line, or table close), past any
lead illustration.  Three shapes:
  * `«B»TITLE«/B»` opening a section (most articles, incl. figure-led)
  * `«B»TITLE«/B»` after a blank line / table close inside a section that holds
    several articles (sections are NOT 1:1 with articles)
  * `{{dropinitial|L}}` + prose, no bold heading (the single-letter articles)
Continuations (section re-emits with no opening heading) and subsection
headings (title-ish but not at a block start, or rejected by the classifier)
are not cuts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from britannica.db.models import SourcePage
from britannica.db.session import SessionLocal
from britannica.pipeline.stages.detect_boundaries import _split_out_plates
from britannica.pipeline.stages.elements._title import _letter_from_dropcap
from britannica.pipeline.stages.preprocess import make_stream, preprocess
from britannica.volumes import article_ws_range

_SECTION_BEGIN = re.compile(r'<section\s+begin\s*=\s*"?([^">]*)"?\s*/?>')
# An article heading: «B»…«/B», optionally wrapped in an [[Author:…|…]] link.
# Inner italic spans are tolerated; the closer is «/B».  `\s*` after the link
# pipe: the «B» can sit on the NEXT line (`[[Author:…|\n«B»STAWELL…«/B»]]`).
_HEADING = re.compile(r"(?:\[\[[^\]|]*\|\s*)?«B»((?:[^«]|«/?I»)*?)«/B»")
_PAGE = re.compile(r"\x01PAGE:(\d+)\x01")

# Block starts inside a section, after the open: a blank line, a page break, or
# a closed wikitable / HTML table (the previous article's trailing figure).
_BLOCK_BOUNDARY = re.compile(r"\n\n|\x01PAGE:\d+\x01|(?:\|\}|</table>)[ \t]*\n")
# Leading whitespace / table-remnant pipes / nbsp to skip.
_WS = re.compile(r"(?:\s|&nbsp;|\xa0|\|)+")
# Lead layout that may sit before a heading — the article's own opening
# illustration / fine-print frame / drop-cap / column comment, or page-level
# transclusion chrome (`<noinclude>` header/footer, `<section …>` tags) — all
# skipped to REACH the heading on RAW source.  Recognized-and-skipped, never
# stripped: what comes in goes out (their producers consume them downstream).
_LEAD = [
    re.compile(r"<noinclude>.*?</noinclude>", re.DOTALL | re.I),  # page chrome
    re.compile(r"<section\s+(?:begin|end)\b[^>]*?/?>", re.I),     # transclusion tag
    re.compile(r"<!--.*?-->", re.DOTALL),                       # HTML comment
    re.compile(r"<br\s*/?>", re.I),                            # line break
    re.compile(r"\{\|.*?\n\s*\|\}", re.DOTALL),                 # wikitable
    re.compile(r"\{\|.*?\|\}", re.DOTALL),                      # wikitable (inline close)
    re.compile(r"<table[^>]*>.*?</table>", re.DOTALL | re.I),   # HTML table
    re.compile(r"\[\[(?:File|Image):(?:[^\[\]]|\[\[[^\]]*\]\])*\]\]",
               re.I),                                          # image
    re.compile(r"\{\{(?:[^{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})*\}\}"),
                                                               # template (≤3 deep)
]

# Strict Roman numeral, so real words made of Roman letters (CIVIL, DILL, VILL)
# aren't mistaken for section numbers; only well-formed numerals (II, IV) match.
_STRICT_ROMAN = re.compile(
    r"M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\.?$")
# Numbered structural headings (ORDER I, PART II …) — not article titles.
_NUMBERED = re.compile(
    r"^(?:ORDER|PART|SECTION|CLASS|BOOK|CHAPTER|DIVISION|GROUP|SERIES|PERIOD"
    r"|GRADE|LEGION|BRIGADE|FAMILY|TRIBE|GENUS|SUBORDER|SUBFAMILY)"
    r"\s+[IVXLCDM]+\b")
# A Title-case word (Cap then lowercase): the signature of a taxonomy subsection
# heading ("A. Rhachitomi", "Sub-class. TRILOBITA"), never an all-caps headword.
_TITLECASE_WORD = re.compile(r"^[A-ZÀ-Þ][a-zà-ÿ][a-zà-ÿ-]*$")


@dataclass
class WalkedArticle:
    """One detected article: the byte offset where it starts in the volume
    stream and the page it starts on.  NO produced title — the title is the
    leading heading inside the article's span, for the title producer to make
    later.  ``raw_heading`` is the unprocessed matched heading (for the letter
    articles, just the letter), kept only for diffing against the current
    pipeline."""
    start: int
    page_start: int
    raw_heading: str


def _first_word_caps(t: str) -> bool:
    """True if the title's first word is uppercase-dominant — the headword
    signature.  Counting upper ≥ lower (rather than all-upper) keeps the
    lowercase-prefixed surnames (McPHERSON, MacVEAGH) and single-letter words
    (X RAYS, I.O.U.), and transliteration capitals (Ī, Ū, Ṣ) via
    ``str.isupper``; it still rejects title-case openers like 'Sub-class …'."""
    t = t.lstrip("ʿʼ'\"([{ \t.-")
    first = re.split(r"[\s,]", t, 1)[0]
    up = sum(c.isupper() for c in first)
    lo = sum(c.islower() for c in first)
    return up >= 1 and up >= lo


def _has_titlecase_word(t: str) -> bool:
    """True if any word is Title-case (Cap then lowercase) — taxonomy subsection
    headings ('A. Rhachitomi', 'Sub-class. TRILOBITA').  Mc/Mac surnames don't
    match (uppercase resumes after the prefix); all-caps headwords don't."""
    for w in re.split(r"[\s,]+", t):
        w = re.sub(r"^[^0-9A-Za-zÀ-ÿ]+|[^0-9A-Za-zÀ-ÿ]+$", "", w)
        if _TITLECASE_WORD.match(w):
            return True
    return False


# Read a block-opening heading down to its plain headword text — the classifier's
# eyes, recognition only.  «I» italics, a footnote, a link, a styler template, a
# disambiguator paren all come off; `uc`/`sc` content is folded to the CAPITALS it
# renders as, so the caps test reads true case.  NOT title production — produce_title
# owns that, downstream; here we only look enough to classify.
_HI = re.compile(r"«/?I»")
_HREF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^/]*/\s*>", re.DOTALL)
_HLINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")
_HABBR = re.compile(r"\{\{\s*abbr\s*\|([^{}|]*)\|[^{}]*\}\}", re.I)
_HCAPS = re.compile(r"\{\{\s*(?:uc|sc|asc|small[\s-]?caps?)\s*\|([^{}|]*)\}\}", re.I)
_HTMPL3 = re.compile(r"\{\{[^{}|]+\|[^{}]*\|([^{}|]*)\}\}")
_HTMPL2 = re.compile(r"\{\{[^{}|]+\|([^{}|]*)\}\}")
_HTMPL0 = re.compile(r"\{\{[^{}]*\}\}")
_HTAG = re.compile(r"<[^>]+>")
_HPAREN = re.compile(r"\s*\([^)]*\)")


def _heading_text(raw_heading: str) -> str:
    """The plain headword text of a raw «B» heading, for classification only."""
    t = _HI.sub("", raw_heading)
    t = _HREF.sub("", t)
    t = _HLINK.sub(r"\1", t)
    t = _HABBR.sub(r"\1", t)
    t = _HCAPS.sub(lambda m: m.group(1).upper(), t)
    for _ in range(8):
        before = t
        t = _HTMPL3.sub(r"\1", t)
        t = _HTMPL2.sub(r"\1", t)
        t = _HTMPL0.sub("", t)
        if t == before:
            break
    t = _HTAG.sub("", t)
    t = _HPAREN.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" ,.;:")


def _is_title(raw_heading: str) -> bool:
    """The one classification: is this block-opening heading an article title?
    One question in two parts — is the headword caps-set PROSE (the EB1911 title
    convention), and NOT a numeral or formula (which wear the same bold at the
    same block starts)?  Everything else fell out of those two."""
    t = _heading_text(raw_heading)
    if len(t) < 2:                                   # empty / lone char (dropcap)
        return False
    if (t[0].isdigit() or "·" in t or "→" in t       # not prose: a numeral /
            or re.search(r"[A-Za-z]\d|\d[A-Za-z]", t)  #   formula (CH3, C6H5) /
            or _STRICT_ROMAN.match(t)                #   roman (II, IV) /
            or _NUMBERED.match(t)):                  #   section marker (ORDER I)
        return False
    if _has_titlecase_word(t):                       # Title-case = subsection
        return False                                 #   (A. Rhachitomi, Sub-class.)
    return len(t) == 2 or _first_word_caps(t)        # caps-prose headword


def _heading_at(body: str, pos: int):
    """Skip lead layout from ``pos``; return the `«B»` heading match there
    (positions in ``body``), or None if the next content isn't a heading."""
    prev = -1
    while pos != prev:
        prev = pos
        m = _WS.match(body, pos)
        if m:
            pos = m.end()
        for pat in _LEAD:
            m = pat.match(body, pos)
            if m:
                pos = m.end()
                break
    return _HEADING.match(body, pos)


def _page_before(stream: str, pos: int) -> int:
    mk = stream.rfind("\x01PAGE:", 0, pos)
    if mk < 0:
        return 0
    pm = _PAGE.match(stream, mk)
    return int(pm.group(1)) if pm else 0


def _volume_pages(session, volume: int) -> list:
    """The volume's SourcePages constrained to its article-leaf range
    (``volumes.ARTICLE_WS_RANGE``): front matter and back matter never enter the
    gather, so step 1 is article/plate ONLY — not "gather every page and let the
    front matter fall off as the unclaimed lead before the first heading".  A
    volume with no recorded range (e.g. vol 29) admits every page."""
    q = session.query(SourcePage).filter(SourcePage.volume == volume)
    rng = article_ws_range(volume)
    if rng is not None:
        q = q.filter(SourcePage.page_number >= rng[0],
                     SourcePage.page_number <= rng[1])
    return q.order_by(SourcePage.page_number).all()


def volume_stream(volume: int) -> str:
    """The clean, frozen continuous stream for ``volume`` — the single input
    to boundary detection AND the element walker.

    Plate-free pages are assembled (``make_stream``) and run through the
    whole-volume ``preprocess`` (source cleaning + page-transition healing),
    so every boundary and every article body is sliced from clean source with
    cross-page tables/hyphens/sentences already healed at the seam.  Section
    tags survive (detect consumes ``<section begin>``); the ``\\x01PAGE:N\\x01``
    markers survive as page-number bookkeeping.
    """
    session = SessionLocal()
    try:
        all_pages = _volume_pages(session, volume)
        # Plates are lifted first; article detection runs over the plate-free
        # pages.  Front matter never reaches here — `_volume_pages` constrained
        # the gather to the article-leaf range.
        _plates, pages = _split_out_plates(all_pages)
        return preprocess(make_stream(pages), volume)
    finally:
        session.close()


def super_walk(volume: int) -> list[WalkedArticle]:
    """Emit one article per title block across the volume.

    For each `<section begin>` section: scan every block start (section open,
    each blank line, each table close) for a title heading; each is an article.
    Plus the single-letter drop-cap case.  Continuations (no opening heading)
    and subsection headings (rejected by `_is_title`) fall out for free."""
    stream = volume_stream(volume)
    tags = list(_SECTION_BEGIN.finditer(stream))
    out: list[WalkedArticle] = []
    for i, tag in enumerate(tags):
        sec_id = (tag.group(1) or "").strip() or "s1"
        seg_start = tag.end()
        seg_end = tags[i + 1].start() if i + 1 < len(tags) else len(stream)
        body = stream[seg_start:seg_end]

        seen: set[int] = set()
        # Single-letter article: a drop-cap opener (any of the source's six
        # template shapes), not a bold heading — the title producer's own
        # structural detector (sole owner of drop-cap letter extraction).
        letter = _letter_from_dropcap(body.lstrip())
        if letter:
            seen.add(0)
            out.append(WalkedArticle(
                tag.start(), _page_before(stream, tag.start()), letter))

        starts = [0] + [m.end() for m in _BLOCK_BOUNDARY.finditer(body)]
        for pos in starts:
            m = _heading_at(body, pos)
            if m is None or m.start() in seen or not _is_title(m.group(1)):
                continue
            seen.add(m.start())
            gpos = seg_start + m.start()
            out.append(WalkedArticle(
                gpos, _page_before(stream, gpos), m.group(1)))

    out.sort(key=lambda a: a.start)
    # A boundary SET has no duplicates: a heading can fall under two overlapping
    # Wikisource <section begin> tags, and the per-section `seen` set only dedups
    # within a section.  Collapse same-position boundaries here.
    deduped: list[WalkedArticle] = []
    seen_pos: set[int] = set()
    for a in out:
        if a.start in seen_pos:
            continue
        seen_pos.add(a.start)
        deduped.append(a)
    return deduped
