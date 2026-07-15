"""TITLE producer.

A detected article's leading heading IS its title.  ``produce_title`` carves the
title↔body cut and returns the raw heading span; the TITLE element producer strips
the headword↔body joint and recurses the span into the «TITLE» marker;
``decode_title`` reduces that one marker to the plain field.  One produced title,
two views — the rendered heading and the plain field — both descending from the
single recursion, so they cannot diverge.  (Replaced the flat legacy
``clean_title``, now deleted; the detection classifier's own headword read lives
in ``super_walker._heading_text``.)
"""
from __future__ import annotations

import re

# Shared with `_is_connective_gap` (the title-run extender, below): small-caps →
# caps and the inline-marker strip, to read a heading gap's plain text.
_SC = re.compile(r"\{\{(?:sc|asc|small[\s\-]?caps?)\|([^{}|]*)\}\}", re.I)
_MARK = re.compile(r"«/?(?:B|I|SC)»")


# A bold heading span, optionally wrapped in an [[Author:…|…]] link — consume the
# link's closing `]]` (after «/B») so clean_title's link-stripper can fire.
# `\s*` after the pipe: the «B» can sit on the next line (STAWELL).
_BOLD = re.compile(r"(?:\[\[[^\]|]*\|\s*)?«B».*?«/B»(?:\s*\]\])?", re.DOTALL)

# Letter-article opener: the article opens with a drop-cap template
# (`{{di|X}}` / `{{dropinitial|X}}`), optionally wrapped in `{{Serif|…}}`,
# `'''…'''` (becomes `«B»…«/B»` post quote-run conversion), or both.  The 26
# letter articles A–Z are the only sections corpus-wide that match this shape.
# Six source spellings are documented (see ``_letter_from_dropcap``).
_LETTER_OPENER_RE = re.compile(
    r"^\s*(?:'''|«B»)?\s*"
    r"(?:\{\{\s*[Ss]erif\s*\|\s*)?"
    r"\{\{\s*(?:[Dd]rop\s*[Ii]nitial|[Dd]i)\s*\|"
)
# [[Author:…|inner]] / [[Portal:…|inner]] link wrapper — unwrapped to its inner
# heading in the title span so the walk recurses the bold run into the «TITLE»
# node instead of mangling the link (HOLLAR, WENZEL or WENCESLAUS).
_AUTHORLINK = re.compile(
    r"\[\[(?:Author|Portal):[^\]|]*\|(.*?)\]\]", re.DOTALL | re.IGNORECASE)

# The trailing JOINT comma EB1911 leaves at the end of the bold headword
# (``«B»ARTEMIS,«/B»``, ``«B»{{uc|Colenso,}}«/B»``) — the inline separator to the
# definition.  Stripped on the RAW span in the TITLE producer, BEFORE the walk
# recurses it, so neither the rendered marker (DISPLAY) nor the decoded field
# (PLAIN) ever carries it: one removal on the shared input, both views clean.
# The trailing joint EB1911 leaves at the end of the bold headword, found behind
# whatever run of close-syntax the recursion is about to collapse (template
# ``}}``, style markers ``«/B»``/``«/I»``/…, a stray link ``]]``, whitespace), at
# any nesting.  Quasi-illicit: a producer trimming its own inner — the marked
# exception titles force, since the joint is content-shaped but isn't content.
_TITLE_CLOSE = r"(?:\}\}|\]\]|«/[A-Za-z]+»|\s)*"
# COMMA / SEMICOLON / COLON: always a joint — no title legitimately ends in one.
# (Internal ones, ``CUSANUS, NICOLAUS``, are followed by content, not close-
# syntax, so untouched.)
_TITLE_JOINT_COMMA = re.compile(r"[,;:]\s*(?=" + _TITLE_CLOSE + r"$)")
# PERIOD: a terminator / regnal dot (``ABBAS II.``, ``FREDERICK I.``,
# ``AUTOGRAPHS.``, ``…1812.``) — UNLESS the headword's terminal token is a LONE,
# NON-REGNAL letter, where the dot is an initial or the tail of an initialism
# (``ALBERT F.``, ``I.O.U.``, ``U.S.A.``).  We capture that token together with
# whatever close-syntax sits between/around it and the dot (so the dot is caught
# inside OR outside the bold), and judge group 1.  (Multi-letter abbreviation
# ``ST.`` is the residual casualty — a frozen list later, or an accepted loss.)
_TITLE_TERM_PERIOD = re.compile(
    r"(\w+)(" + _TITLE_CLOSE + r")\.(" + _TITLE_CLOSE + r")$", re.UNICODE)
# A lone trailing I/V/X is a regnal numeral, not an initial; L/C/D/M never appear
# as a monarch's numeral, so a lone trailing C./D. is an initial and keeps its dot.
_REGNAL_LETTER = frozenset("IVX")
# Abbreviations that legitimately END a title with a real dot.  The corpus has
# exactly one — ``ST`` (Saint), in the inverted ``FRANCIS OF ASSISI, ST.`` form;
# ``STE`` (Sainte) rides along for the same reason though it doesn't occur today.
# (Leading/internal ``ST.`` — ST. PETERSBURG — is never touched: the strip is
# trailing-only.)  Everything else short before a dot is a real word — ``OF``,
# ``THE``, ``LAW`` — and a genuine terminator.
_TITLE_ABBR = frozenset({"ST", "STE"})


def strip_title_joint(span: str) -> str:
    """Strip the trailing joint off a raw title span, BEFORE the walk recurses it,
    so neither the rendered marker (DISPLAY) nor the decoded field (PLAIN) carries
    it.  The comma is always a joint; the period is a terminator/regnal dot except
    after a lone non-regnal letter (initial / initialism) or a known abbreviation
    (``ST.``), where the dot belongs to the name."""
    span = _TITLE_JOINT_COMMA.sub("", span)
    m = _TITLE_TERM_PERIOD.search(span)
    if m:
        tok = m.group(1).upper()
        keep = (len(tok) == 1 and tok not in _REGNAL_LETTER) or tok in _TITLE_ABBR
        if not keep:
            span = span[:m.start()] + m.group(1) + m.group(2) + m.group(3)
    return span


# ── Plain-title decode: the field is the WALKED «TITLE» marker stripped to text,
# not a parallel re-parse of the raw heading.  The heading is already recursed
# into markers, so the field is just those markers stripped (recursion =
# recognition) — making field-vs-heading divergence impossible by construction.
_FN_MARK = re.compile(r"«FN(?:\[[^\]]*\])?:.*?«/FN»", re.DOTALL)
_LN_MARK = re.compile(r"«(?:LN|XL):(?:[^|»]*\|)?(.*?)«/(?:LN|XL)»", re.DOTALL)
_UPPER_SPAN = re.compile(
    r"«SPAN\[[^\]]*text-transform:uppercase[^\]]*\]»(.*?)«/SPAN»", re.DOTALL)
_SC_MARK = re.compile(r"«SC»(.*?)«/SC»", re.DOTALL)
_SPAN_MARK = re.compile(r"«/?SPAN(?:\[[^\]]*\])?»")
_STYLE_MARK = re.compile(r"«/?(?:B|I|SC|U|SUP|SUB)»")
_HTML_TAG = re.compile(r"<[^>]+>")


def decode_title(marker: str) -> str:
    """Plain title from the walked «TITLE» marker content: drop the footnote, take
    a link's display text, render small-caps and ``{{uc}}`` (text-transform:
    uppercase) as CAPS, then drop the remaining span/style wrappers (keeping their
    inner text).  Replaces ``clean_title``'s raw-heading regex re-parse — the same
    plain text, but sourced from the one recursion, not a parallel second one."""
    t = _FN_MARK.sub("", marker)                          # footnote → drop
    t = _LN_MARK.sub(r"\1", t)                            # link → its display text
    t = _UPPER_SPAN.sub(lambda m: m.group(1).upper(), t)  # {{uc}} span → CAPS
    t = _SC_MARK.sub(lambda m: m.group(1).upper(), t)     # small-caps → CAPS
    t = _SPAN_MARK.sub("", t)                             # other spans → inner
    t = _STYLE_MARK.sub("", t)                            # B/I/U/SUP/SUB → strip
    t = _HTML_TAG.sub("", t)                              # stray raw tags (<big>…)
    return re.sub(r"\s+", " ", t).strip()


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


def _letter_from_dropcap(opening: str) -> str | None:
    """The letter of a letter-article (A, B, C, …, Z; 26 in EB1911), or None.

    Letter articles open with a drop-cap template instead of a bold heading.
    Source uses six template shapes for this:
      * ``{{dropinitial|X}}`` / ``{{di|X}}``
      * ``{{Serif|{{di|X|5em}}}}``
      * ``{{di|{{serif|J}}|4em}}``
      * ``{{dropinitial|'''{{serif|K}}'''|6em}}``
      * ``'''{{di|T}}'''`` (becomes ``«B»{{di|T}}«/B»`` post quote-run conv)

    Match shape: drop-cap at the opening with a single-letter first arg (after
    unwrapping at most one level of ``{{serif|X}}`` wrapper).  Returns the
    uppercased letter, else None.  ``produce_title`` runs on an already-bounded
    article opening, so there is no section-name to cross-check — the structural
    drop-cap-with-single-letter-arg parse alone is the signal.
    """
    m = _LETTER_OPENER_RE.match(opening)
    if not m:
        return None
    arg = _first_template_arg(opening[m.end():])
    if arg is None:
        return None
    # Unwrap a single layer of `{{name|X}}` (handles `{{serif|J}}`).
    arg = re.sub(r"\{\{[^{}|]+\|([^{}|]+)\}\}", r"\1", arg)
    # Strip residual markers, quotes, whitespace.
    arg = re.sub(r"«/?[A-Z]+»|'''|''", "", arg).strip()
    if len(arg) != 1 or not arg.isalpha():
        return None
    return arg.upper()


def _is_connective_gap(gap: str) -> bool:
    """True if the gap between two bold spans is heading CONNECTIVE (a
    parenthetical alt-name / `,` / `and`·`or` / surname particle) rather than the
    descriptive body.  Keeps the title run going across same-line forename/joint
    bolds (BELLARMINE …, ROBERTO …; ABANA … and PHARPAR) but stops at the body."""
    cleaned = _MARK.sub("", _SC.sub(r"\1", gap)).strip()
    if len(cleaned) > 70:                      # a clause, not a connective
        return False
    if re.search(r"\(\s*[cbfl]?\.?\s*\d", gap):  # (1542– / (c. 1036 / (b. … = body date
        return False
    if not re.search(r"[(),;]|\b(?:and|or|surnamed|né|née|nee|called|alias)\b",
                     gap, re.I):
        return False
    return True


def _title_span(opening: str) -> tuple[str, str]:
    """Split a raw article opening into (title_span, rest).  The title is the
    heading RUN: the first `«B»` through the LAST `«B»` reachable across
    connective gaps; it stops where the descriptive body begins."""
    s = opening.lstrip()
    lead = len(opening) - len(s)
    m = _BOLD.match(s)
    if not m:
        return "", opening
    end = m.end()
    while True:
        nb = _BOLD.search(s, end)
        if not nb or not _is_connective_gap(s[end:nb.start()]):
            break
        end = nb.end()
    # If the last title-bold sat inside an open parenthetical (AMYNTAS II.
    # (or «B»III.«/B»)), the span stops at «/B» before the `)` — pull it in.
    span = opening[:lead + end]
    if span.count("(") > span.count(")"):
        cm = re.match(r"[^«(]*?\)", opening[lead + end:])
        if cm:
            end += cm.end()
    return opening[:lead + end], opening[lead + end:]


def _letter_title_span(opening: str) -> tuple[str, str] | None:
    """Carve a letter-article's leading drop-cap construct off the body — the
    drop-cap analogue of ``_title_span``.  Returns ``(title_span, rest)``, or
    None when OPENING is not a letter article.  The span is «TITLE»-stamped like
    a bold heading; the drop-cap walks to its bare letter (``_content`` drops the
    size arg), so ``decode_title`` yields the plain letter for the field."""
    if _letter_from_dropcap(opening) is None:
        return None
    s = opening.lstrip()
    lead = len(opening) - len(s)
    i = 0
    # Optional bold wrapper around the drop-cap (`'''{{di|T}}'''` →
    # `«B»{{di|T}}«/B»` post quote-run); consume its matching close after.
    close = ""
    if s.startswith("'''"):
        close, i = "'''", 3
    elif s.startswith("«B»"):
        close, i = "«/B»", len("«B»")
    while i < len(s) and s[i].isspace():
        i += 1
    if s[i:i + 2] != "{{":
        return None
    # Balance the leading {{…}} template(s) — a {{Serif|{{di|…}}}} wrapper or a
    # bare {{di|…}} — to depth 0; that IS the drop-cap construct.
    depth = 0
    while i < len(s):
        if s[i:i + 2] == "{{":
            depth += 1
            i += 2
        elif s[i:i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                break
        else:
            i += 1
    if depth != 0:
        return None
    if close:
        j = i
        while j < len(s) and s[j].isspace():
            j += 1
        if s[j:j + len(close)] == close:
            i = j + len(close)
    return opening[:lead + i], opening[lead + i:]


# Leading transclusion chrome the walker carries into a first-segment opening
# (B3): a `<section …>` tag or a running header (`{{rh}}`) — skipped before the
# heading is sought (it is not the title).  A scan of vols 1-3 (4825 articles)
# shows only these two reach here; the former `<noinclude>` / `{{EB1911 Page
# Heading}}` clauses matched 0 (both are stripped upstream in preprocess) and were
# dropped.
_LEAD_CHROME = re.compile(
    r"^\s*(?:<section\b[^>]*>"
    r"|\{\{\s*rh\b[^{}]*\}\})+",
    re.DOTALL | re.IGNORECASE)


def produce_title(opening: str, section_name: str = "") -> tuple[str, str]:
    """Produce ``(body, title_raw_span)`` from a raw article OPENING (the text
    beginning at the heading, as the walker hands it over).  The producer owns
    the title↔body cut — the walker only set the boundary.

    ``section_name`` is the article's ``«section begin="…"»`` id (already on
    ``Article.section_name``); used ONLY to recover the fuller title when the
    bold heading is a partial capture.  WHICH bold headers are titles is
    classification — the boundary detector's job — so we only extract.

    ``title_raw_span`` is the raw heading span (markers/footnote intact) the walk
    recurses into the «TITLE» node — a bold run, or a letter article's drop-cap
    construct; "" only in the rare body-opens-at-prose fallback, where there is
    nothing to carve.  The plain field is decoded from the WALKED node in
    ``walk_article``, never re-parsed here."""
    # The opening carries the leading «PAGE» leaf marker (super_detect stamps it
    # on segment 0, AHEAD of the heading), which blocks the bold-heading match.
    # Step over it for the title search, but KEEP it — it carries the page number
    # the body needs — by re-prepending it to whatever body we return.
    pm = re.match("\x01PAGE:[0-9]+\x01", opening)
    page = pm.group(0) if pm else ""
    opening = _LEAD_CHROME.sub("", opening[len(page):])
    span, rest = _title_span(opening)
    if span:
        # The plain field is decoded from the WALKED «TITLE» marker in
        # walk_article (recursion = recognition — the ONE title source); the
        # partial-capture section recovery moved there with it.  produce_title
        # carves the cut and hands back the raw span (author-link unwrapped); the
        # freight-strip runs in the TITLE element producer, on the recursed marker
        # — the joint comma can hide inside `{{uc|Colenso,}}` until the walk
        # collapses it, so it isn't visible to a strip here, pre-walk.
        return page + rest.lstrip(" \t,."), _AUTHORLINK.sub(r"\1", span)
    # Letter articles open with a drop-cap, not a bold heading.  Carve the
    # drop-cap construct as the title span (all six documented shapes) so the
    # letter rides the «TITLE» node exactly like a bold heading — one decider.
    lc = _letter_title_span(opening)
    if lc is not None:
        lspan, lrest = lc
        return page + lrest.lstrip(), lspan
    # No bold heading and no drop-cap.  The body opens at content (CHESS "once
    # known as …"), so there is NOTHING here to extract: the first line is prose,
    # not a title.  Return no span and let the caller keep the authoritative
    # detected title — produce_title must never fabricate a title from body text.
    return page + opening, ""
