"""Page markers: the SECOND and last place page position is handled.

The first is `preprocess.stream_with_keys`, which creates the keys.  Everything
between the two has no concept of a page marker
([[project_page_position_out_of_band]]).

A key is `{page, offset, sig}`.  The offset is a RAW offset into `Article.body`,
and render works on the transformed output, whose lengths differ (PHYLLOXERA: raw
14,790 -> walked 14,312 -> rendered 15,603), so it cannot be used directly.
Something must bridge raw-space to rendered-space, and that is this module.

The alternative — having every producer report its length deltas so offsets stay
valid — was rejected: it is threading page markers through the pipeline again
under another name, and it puts an accounting duty on producers that know nothing
about pages.  Bridging once, at the end, keeps that out of them entirely.

The bridge is CONTENT: markup changes, but visible letters and their order do
not.  Each key carries a SIGNATURE — the opening letters of its page as they
render — computed once at export, where the raw body already is.  Here we find
that signature in the rendered output, near where the key proportionally belongs.

The signature travels IN the key deliberately.  Computing it here would mean
handing this module the raw body, which means handing it to whoever calls
`render_article` — and one of those is the xref resolver, which has no business
knowing anything about pages.  With the signature in the key the payload is
self-sufficient and every caller just passes a dict along.

Failure is benign by construction: a page we cannot place falls back to its
proportional position, so a marker may sit a little out of place, but the text is
never touched.
"""
from __future__ import annotations

import re

SIG_LEN = 24
PREFIX = 600
PREFIX_MAX = 3000
MIN_SIG = 8
MAX_OCC = 32

_TAG_RE = re.compile(r"<[^>]*>")
_MARKER_RE = re.compile(r"«[^«»]*»")
_PLACEHOLDER_RE = re.compile(r"\x03ELEM:\d+\x03")
_ENTITY_RE = re.compile(r"&(?:[a-z]+|#\d+);", re.IGNORECASE)

_OPENERS = {"{{": "}}", "[[": "]]", "{|": "|}"}
_CLOSERS = {"}}", "]]", "|}"}


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def letter_map(html: str) -> tuple[str, list[int]]:
    """`(letters, char_index_of_each_letter)` for the TEXT of `html`.

    Only text outside tags is counted, so an insertion point derived from a
    letter index can never land inside a tag — which is the failure
    `epub/pack.py` has to clean up after when a marker is placed by other means.
    HTML entities are skipped whole: `&amp;` is one character to a reader, but
    its letters would otherwise read as "amp" and corrupt the match.
    """
    letters: list[str] = []
    at: list[int] = []
    i, n = 0, len(html)
    while i < n:
        ch = html[i]
        if ch == "<":
            m = _TAG_RE.match(html, i)
            i = m.end() if m else i + 1
            continue
        if ch == "&":
            m = _ENTITY_RE.match(html, i)
            if m:
                i = m.end()
                continue
        if ch.isascii() and ch.isalpha():
            letters.append(ch.lower())
            at.append(i)
        i += 1
    return "".join(letters), at


def _prefix_for(raw: str, limit: int = PREFIX) -> str:
    """A prefix of `raw` with any construct the cut left open closed off, so the
    walker bounds it instead of spilling a table's attributes into the text.

    `limit` is a parameter, not a constant, because `signature` widens the window
    when a page opens on content that renders few letters."""
    head = raw[:limit]
    stack: list[str] = []
    i, n = 0, len(head)
    while i < n:
        two = head[i:i + 2]
        if two in _OPENERS:
            stack.append(_OPENERS[two])
            i += 2
            continue
        if two in _CLOSERS:
            if stack and stack[-1] == two:
                stack.pop()
            i += 2
            continue
        i += 1
    return head + "".join(reversed(stack))


def _render_fragment(raw: str, volume: int) -> str:
    """`raw` source -> HTML, by the SAME path the article body takes."""
    from britannica.pipeline.stages.elements import process_elements
    from britannica.pipeline.stages.elements._context import ElementContext
    from britannica.render.article import RenderContext
    from britannica.render.inline import decode_inline
    marked = process_elements(_prefix_for(raw), ElementContext(volume=volume))
    ctx = RenderContext(volume=volume, scan_url="scans.html", unproofed_pages={})
    return decode_inline("«P»" + marked, escape=True, body_blocks=True, ctx=ctx)


def signature(body: str, offset: int, volume: int) -> str:
    """The first `SIG_LEN` rendered letters of the page beginning at `offset`.

    This is a KEY, so it must be computed by the same function that computes the
    value it will be looked up against.  It was not: the key came from an ad-hoc
    marker-strip over `process_elements` output, while `marker_positions` searches
    `letter_map` over finished HTML.  The two disagree wherever a marker's NAME
    carries letters the HTML puts in a tag — a page opening `<math>(l^{\\lambda_1}…`
    keyed on `mathllambdallambdaldotsa`, because `«MATH:` has no closing `»` for
    the `«[^«»]*»` sweep to match, while the render writes
    `<span class="tex-math">(l^{\\lambda_1}…` whose letters begin `llambda…`.  It
    could never match, so those pages fell back to a proportional guess and their
    markers landed mid-word ([[feedback_tune_dont_fork]]).

    So: render the page's opening the way the body is rendered, then read its
    letters with `letter_map` — the one extractor, on both sides.  "The first 24
    rendered letters of this page" then means literally the same thing at export
    and at render, and matches by construction rather than by coincidence.

    A page can open on content that renders few letters (a numeric table, an
    image).  Widen the window rather than give up: a signature shorter than
    `MIN_SIG` is unusable and drops the page straight to the fallback.
    """
    window = PREFIX
    while True:
        try:
            html = _render_fragment(body[offset:offset + window * 4], volume)
        except Exception:
            return ""
        letters, _at = letter_map(html)
        if len(letters) >= SIG_LEN or window >= PREFIX_MAX:
            return letters[:SIG_LEN]
        window *= 2


def locate(letters: str, sigs: list[str], expected: list[int]) -> list[int | None]:
    """Assign each page a letter index: monotonic, nearest to where it belongs.

    Global assignment rather than a greedy forward scan — with a scan, one page
    matching a later recurrence of its own text drags the floor forward and every
    page after it reports missing.  Here a mispicked page costs only itself.  A
    gap is priced above any possible deviation, so it is taken only when no
    monotonic candidate exists at all.
    """
    n = len(sigs)
    gap_penalty = float(len(letters) + 1)
    cand: list[list[int]] = []
    for sig in sigs:
        occ: list[int] = []
        if sig and len(sig) >= MIN_SIG:
            st = 0
            while len(occ) < MAX_OCC:
                j = letters.find(sig, st)
                if j < 0:
                    break
                occ.append(j)
                st = j + 1
        cand.append(occ)

    states: dict[int, float] = {-1: 0.0}
    paths: dict[int, list[int | None]] = {-1: []}
    for i in range(n):
        new: dict[int, float] = {}
        newp: dict[int, list[int | None]] = {}

        def offer(key: int, cost: float, path: list[int | None]) -> None:
            if key not in new or cost < new[key]:
                new[key] = cost
                newp[key] = path

        for last, cost in states.items():
            offer(last, cost + gap_penalty, paths[last] + [None])
            for pos in cand[i]:
                if pos < last:
                    continue
                offer(pos, cost + abs(pos - expected[i]), paths[last] + [pos])
        if not new:
            new = {k: v + gap_penalty for k, v in states.items()}
            newp = {k: paths[k] + [None] for k in states}
        if len(new) > 48:
            keep = sorted(new, key=lambda k: new[k])[:48]
            new = {k: new[k] for k in keep}
            newp = {k: newp[k] for k in keep}
        states, paths = new, newp
    best = min(states, key=lambda k: states[k])
    return paths[best]


def marker_positions(html: str, keys, body_span: int = 0) -> list[tuple[int, int]]:
    """`[(char_index_in_html, page_number)]` — where each page's marker goes.

    `keys` is the article's `[{page, offset, sig}]`, each carrying the signature
    computed at export.  Nothing else is needed: no raw body, no session, no
    volume — so `render_article` is a pure function of its payload and no caller
    has to be handed a render input it has no business holding.

    The first key is the page the article opens on and is placed structurally;
    the rest are located by their signatures.
    """
    if not keys:
        return []
    letters, at = letter_map(html)
    if not letters:
        return []

    ordered = sorted(keys, key=lambda k: k.get("offset", 0))
    pages = [(k.get("offset", 0), k.get("page")) for k in ordered]
    sigs = [k.get("sig") or "" for k in ordered]
    # Proportional prior: the key's share of the raw body estimates its share of
    # the rendered letters.  `body_span` is the raw length; without it we fall
    # back to spreading the keys evenly, which is weaker but still ordered.
    span = body_span or (ordered[-1].get("offset", 0) + 1)
    expected = [int(len(letters) * (off / max(1, span))) for off, _pg in pages]

    found = [0] + (locate(letters, sigs[1:], expected[1:]) if len(ordered) > 1 else [])
    return _place(pages, found, expected, letters, at)


def _place(ordered, found, expected, letters, at) -> list[tuple[int, int]]:
    # The next LOCATED position at or after each index — the ceiling a fallback
    # may not cross.  `locate` returns its finds already monotonic, so this is
    # non-decreasing and never sits behind the running floor.
    ceiling: list[int | None] = [None] * len(ordered)
    nxt: int | None = None
    for i in range(len(ordered) - 1, -1, -1):
        if found[i] is not None:
            nxt = found[i]
        ceiling[i] = nxt

    out: list[tuple[int, int]] = []
    floor = 0
    for i, ((_off, pg), idx) in enumerate(zip(ordered, found)):
        if i == 0:
            # The article's own first page: STRUCTURAL, at the body's start.
            # Not `at[0]`, which is the first LETTER — that would put the marker
            # after any opening punctuation (PHYLLOXERA's body begins "(Gr. …",
            # so the marker landed between the "(" and the "G").
            out.append((0, pg))
            continue
        if idx is not None:
            li = idx
        else:
            # A page we could not locate falls back to its proportional
            # position: the marker may sit a little out of place, but the TEXT
            # is untouched.
            #
            # The prior is only a prior, and a weak one — raw offsets do not map
            # linearly onto rendered letters (GYROSCOPE's page 773 was LOCATED
            # at letter 19,167 while the prior said 11,287).  So a fallback must
            # be clamped to the page ORDER, which is not a guess: page 774
            # cannot precede page 773.  Unclamped it did, in 30 of the longest
            # articles, rendering two adjacent margin numbers swapped.
            li = min(expected[i], len(letters) - 1)
            li = max(li, floor)
            if ceiling[i] is not None:
                li = min(li, ceiling[i])
        floor = li
        out.append((at[max(0, min(li, len(at) - 1))], pg))
    return out


def inject(html: str, keys, ctx, body_span: int = 0) -> str:
    """Insert the page markers into rendered `html`.

    THE SECOND AND LAST PLACE page position is handled.  Insertion is at a TEXT
    position — `letter_map` counts only text outside tags — so a marker cannot
    land inside a tag by construction, which is the failure `epub/pack.py:306`
    exists to clean up after.

    Inserted back-to-front so earlier indices stay valid.  Two markers CAN share
    a position — a fallback clamped to its predecessor lands exactly on it — and
    back-to-front insertion reverses whatever it inserts at one index, so the tie
    is broken by page DESCENDING: the later page goes in first and ends up on the
    right.  Without that, `spots` order survives the stable sort and the pair
    renders swapped, which is the very thing the clamp in `_place` prevents.
    """
    if not keys or not html:
        return html
    vol = getattr(ctx, "volume", "?")
    unproofed = getattr(ctx, "unproofed_pages", None) or {}
    epub = getattr(ctx, "epub_bundled", None)
    scan_url = getattr(ctx, "scan_url", "scans.html")

    spots = marker_positions(html, keys, body_span)
    # `spots` is in page order, so the index IS the tie-break.
    back_to_front = sorted(enumerate(spots), key=lambda t: (t[1][0], t[0]),
                           reverse=True)
    for _seq, (char_idx, page) in back_to_front:
        pg = str(page)
        cls = "page-marker unproofed" if pg in unproofed else "page-marker"
        if epub is not None:
            # EPUB drops scans, so the boundary stays as a non-linked indicator.
            tag = (f'<span class="{cls}" data-page="{pg}" data-vol="{vol}">'
                   f'</span>')
        else:
            title = (f"Volume {vol}, page {pg} (unproofed source) — click to view scan"
                     if pg in unproofed else
                     f"Volume {vol}, page {pg} — click to view scan")
            tag = (f'<a class="{cls}" data-page="{pg}" data-vol="{vol}" '
                   f'title="{title}" href="{scan_url}"></a>')
        html = html[:char_idx] + tag + html[char_idx:]
    return html
