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

import bisect
import re
from dataclasses import dataclass

from britannica.db.models import SourcePage
from britannica.db.session import SessionLocal
from britannica.pipeline.stages.detect_boundaries import _split_out_plates
from britannica.pipeline.stages.elements._title import _letter_from_dropcap
from britannica.pipeline.stages.preprocess import stream_with_keys
from britannica.volumes import article_ws_range
from britannica.util.strings import HTML_TAG_RE

_SECTION_BEGIN = re.compile(r'<section\s+begin\s*=\s*"?([^">]*)"?\s*/?>')
# An article heading: «B»…«/B», optionally wrapped in an [[Author:…|…]] link.
# Inner italic spans are tolerated; the closer is «/B».  `\s*` after the link
# pipe: the «B» can sit on the NEXT line (`[[Author:…|\n«B»STAWELL…«/B»]]`).
_HEADING = re.compile(r"(?:\[\[[^\]|]*\|\s*)?«B»((?:[^«]|«/?I»)*?)«/B»")
# Block starts inside a section, after the open: a blank line, or a closed
# wikitable / HTML table (the previous article's trailing figure).
#
# A PAGE BREAK is also a block start — an article heading often sits at the top
# of a page with no blank line before it, and the seam is a single `\n`, so
# without it those articles are never detected at all.  It used to be the third
# alternative here (`\x01PAGE:\d+\x01`); the token is gone from the stream, so
# `super_walk` folds the page-key offsets into `starts` instead.  Same
# boundaries, taken from data rather than from a marker sitting in the text.
_BLOCK_BOUNDARY = re.compile(r"\n\n|(?:\|\}|</table>)[ \t]*\n")
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
_HTAG = HTML_TAG_RE
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


def _page_before(page_keys: list[tuple[int, int]], pos: int) -> int:
    """The page `pos` sits on — the last key at or before it.

    Was a backwards scan for the nearest `\\x01PAGE:` token.  The token is gone
    from the stream (page position is carried as KEYS now,
    [[project_page_position_out_of_band]]), so this is a bisect over data that is
    already computed — same answer, and it cannot be fooled by a token that a
    producer moved or ate.
    """
    if not page_keys:
        return 0
    i = bisect.bisect_right(page_keys, (pos, float("inf"))) - 1
    return page_keys[i][1] if i >= 0 else 0


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


def volume_stream(
    volume: int,
) -> tuple[str, list[tuple[int, int]], list[tuple[int, str]]]:
    """The clean, frozen stream for ``volume`` plus its KEYS — the single input
    to boundary detection AND the element walker.

    Returns ``(stream, page_keys, section_keys)``.  The stream carries NO
    positional chrome: no ``\\x01PAGE:N\\x01`` markers and no ``<section …>``
    tags.  Both were only ever annotations about WHERE things are, and both are
    now keys — ``[(offset, page_number)]`` and ``[(offset, section_name)]``
    ([[project_page_position_out_of_band]]).

    That makes the stream FINAL: nothing downstream edits it, so every offset
    stays valid, and no recognizer can trip over a marker that isn't content.
    Page-split words are still NOT rejoined here — they reconstruct downstream in
    the split-word producer.
    """
    session = SessionLocal()
    try:
        all_pages = _volume_pages(session, volume)
        # Plates are lifted first; article detection runs over the plate-free
        # pages.  Front matter never reaches here — `_volume_pages` constrained
        # the gather to the article-leaf range.
        _plates, pages = _split_out_plates(all_pages)
        return stream_with_keys(pages, volume)
    finally:
        session.close()


def super_walk(volume: int) -> list[WalkedArticle]:
    """Emit one article per title block across the volume.

    For each `<section begin>` section: scan every block start (section open,
    each blank line, each table close) for a title heading; each is an article.
    Plus the single-letter drop-cap case.  Continuations (no opening heading)
    and subsection headings (rejected by `_is_title`) fall out for free."""
    stream, page_keys, section_keys = volume_stream(volume)
    out: list[WalkedArticle] = []
    for i, (sec_off, sec_name) in enumerate(section_keys):
        sec_id = sec_name or "s1"
        # The tag itself is no longer in the stream — its position IS the key, so
        # the section runs from this key to the next.
        seg_start = sec_off
        seg_end = section_keys[i + 1][0] if i + 1 < len(section_keys) else len(stream)
        body = stream[seg_start:seg_end]

        seen: set[int] = set()
        # Single-letter article: a drop-cap opener (any of the source's six
        # template shapes), not a bold heading — the title producer's own
        # structural detector (sole owner of drop-cap letter extraction).
        letter = _letter_from_dropcap(body.lstrip())
        if letter:
            seen.add(0)
            out.append(WalkedArticle(
                seg_start, _page_before(page_keys, seg_start), letter))

        # Block starts: the section open, each blank line / table close, AND each
        # page break inside this section (see `_BLOCK_BOUNDARY`).
        starts = sorted({0}
                        | {m.end() for m in _BLOCK_BOUNDARY.finditer(body)}
                        | {off - seg_start for off, _pg in page_keys
                           if seg_start <= off < seg_end})
        for pos in starts:
            m = _heading_at(body, pos)
            if m is None or m.start() in seen or not _is_title(m.group(1)):
                continue
            seen.add(m.start())
            gpos = seg_start + m.start()
            out.append(WalkedArticle(
                gpos, _page_before(page_keys, gpos), m.group(1)))

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
