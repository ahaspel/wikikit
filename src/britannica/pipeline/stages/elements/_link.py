"""Link wraps — the `«LN:target|display»` family, folded into the peel/recurse/wrap mechanism.

Every link emits `«LN:target|display»`; they differ only in how the TARGET is parsed from the raw
(display-first vs target-first vs `#fragment` vs `Author:` vs the `1911 Enc…/` prefix strip). The
DISPLAY is the recursed slot: `_link_display` peels it (the PEEL side of the mechanism), the
classifier decomposes it to child nodes, and the wrap here parses the target from `raw` and wraps
the substituted `body`. So the seven old producers are seven `_PR_WRAP` rows on one shared peel —
no bespoke producer functions, no `_classify_link_composite`.

The seven rows share ONE wrap, `_link_wrap`, and differ only in a `_LinkForm`:

    slots     which characters of the raw are the target and the display
    resolve   the target's own transform, and what to show when the source
              named no display
    emit      `«LN»` for six of them, `«AL»` for author links

There are exactly TWO ways to read the slots — a `{{template|…}}`'s positionals
and a `[[bracket|…]]`'s pipe — and before this they were written out three and
four times, plus a third copy inside `_link_display` that had to be kept in step
by hand ("mirrors each wrap's own display parse").  The copies drifted where it
mattered: `subpage_target` was called from three rows under three different
surrounding rules, the display-first and target-first rows disagreed about which
positional is the target (so the export has to un-guess it in `swapped_link`),
and an argument-less template returned the empty string from the three template
rows — discarding the recursed display — where the four bracket rows returned it
as prose.  One skeleton means one answer to each of those.
"""

from __future__ import annotations

import re

from britannica.markers import MARKER_TOKEN_RE


def _split_top_pipes(s: str) -> list[str]:
    """Split on `|` at bracket depth 0 — so a nested `{{sc|X}}`'s inner pipe does not
    shear the slot list (the fraction/dual-line slot-split, shared shape)."""
    parts: list[str] = []
    depth = last = i = 0
    n = len(s)
    while i < n:
        two = s[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            i += 2
            continue
        if two in ("}}", "]]"):
            if depth:
                depth -= 1
            i += 2
            continue
        if depth == 0 and s[i] == "|":
            parts.append(s[last:i])
            last = i + 1
        i += 1
    parts.append(s[last:])
    return parts


def _link_args(raw: str) -> str:
    """The inner args of a `{{…link…}}` template (delimiters peeled) — the wrap's
    target/positional parse reads this (`inner` carries the classified DISPLAY, not the args)."""
    return re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))


def _positionals(raw: str) -> list[str]:
    """A `{{…link…}}`'s positional args — the name dropped, `key=value` dropped."""
    parts = [p.strip() for p in _split_top_pipes(_link_args(raw))]
    return [p for p in parts[1:] if "=" not in p and p]


def _slots_bracket(raw: str) -> tuple[str, str | None]:
    """`[[Target|Display]]` — display is None when the source piped none."""
    body = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target, _sep, display = body.partition("|")
    return target.strip(), (display.strip() or None)


_MARKUP_SLOT = re.compile(r"\{\{|«")


def _slots_target_first(raw: str) -> tuple[str, str | None]:
    """`{{lkpl|Target|Display}}` — target is the first positional.

    Unless that slot carries MARKUP, in which case the two are written
    backwards.  A Wikisource page name is plain text and cannot hold `{{sc}}` or
    italics, so a marked-up slot is what the PAGE PRINTS:
    `{{1911link|«I»organi trum«/I»|Organi trum}}` sets its cross-reference in
    italics and files the article as `Organi trum`.  Reading it the template's
    usual way shows the reader the page name, capital and all, and drops the
    italics on the floor — the markup goes into the target, which is flattened.
    21 links, and NO link anywhere in the corpus carries markup in the other
    slot, so the tell is unambiguous.
    """
    pos = _positionals(raw)
    if not pos:
        return "", None
    if len(pos) == 1:
        return pos[0], None
    if _MARKUP_SLOT.search(pos[0]) and not _MARKUP_SLOT.search(pos[1]):
        return pos[1], pos[0]
    return pos[0], pos[1]


def _slots_display_first(raw: str) -> tuple[str, str | None]:
    """`{{EB1911 article link|Display|Target}}` — the other convention.  A lone
    positional is BOTH: the source named one string and meant it to be shown and
    followed."""
    pos = _positionals(raw)
    if not pos:
        return "", None
    if len(pos) == 1:
        return pos[0], pos[0]
    return pos[1], pos[0]


def _link_display(raw: str, label: str) -> str:
    """The DISPLAY slot for `label`'s link form — the one arg the mechanism recurses (the PEEL
    side).  Reads the SAME slots the wrap reads, so the classified display cannot drift from
    what the wrap emits; where the source named no display, a template form falls back to the
    target (that string is both) and a bracket form to nothing."""
    form = _LINK_FORMS[label]
    target, display = form.slots(raw)
    if display is not None:
        return display
    return target if form.peel_target_when_bare else ""


def _strip_link_prefix(t: str) -> str:
    """Drop a single leading `word:` namespace/interwiki prefix (w:/Portal:/…); the
    colon must be followed by a non-space, so the section colon form `Europe: History`
    is NOT treated as a prefix.  Mirrors the resolver's normalizer strip."""
    i = t.find(":")
    if 0 < i < len(t) - 1 and " " not in t[:i] and not t[i + 1].isspace():
        return t[i + 1:].strip()
    return t


def _ln(target: str, disp: str, ctx) -> str:
    """Emit `«LN:target|disp«/LN»` with the TARGET flattened to a plain name.

    The DISPLAY is already the recursed slot; the target is parsed flat from the
    raw, so a target that carries markup (`{{sc|Samuel}}, Books`) went into the
    marker verbatim — and its inner `|` then collided with the «LN» field
    delimiter downstream, dropping the display.  Give the target the SAME
    recursion the display gets (walk it, so `{{sc}}`→`«SC»`), then flatten to
    text because a `|`-delimited field must be flat: `{{sc|Samuel}}, Books` →
    `Samuel, Books`.  Fast path when there's nothing to recurse (the common case:
    a plain name or a `#section` fragment)."""
    if "{{" in target or "«" in target or "<" in target:
        from britannica.pipeline.stages.elements import process_elements
        from britannica.markers import markers_to_text
        target = markers_to_text(process_elements(target, ctx)).strip()
    return ln_marker(target, disp)


_SUBPAGE_ORDINAL = re.compile(r"^\d+[\s_]*")


def subpage_target(target: str) -> str:
    """A Wikisource subpage path → OUR ``ARTICLE#Section`` form.

    Wikisource paginates a long article into numbered subpages —
    ``Egypt/2_Ancient_Egypt``, ``Egypt/3_History#Mahommedan``, ``Japan/04 Art``.
    The middle segment's NUMBER is their pagination, meaningless here; what
    survives the crossing is the article and the section name.

        /Egypt/3_History#Mahommedan  ->  Egypt#Mahommedan     (an explicit
        Egypt/2_Ancient_Egypt        ->  Egypt#Ancient Egypt   fragment wins;
        Rome/History                 ->  Rome#History          else the last
        Japan/04 Art                 ->  Japan#Art             path segment)

    `resolve_section` then does the rest, and its fallback is what makes this
    safe: a section that exists is linked; one that doesn't degrades to the whole
    article, so the reader never gets an anchor that lands nowhere.  Keeping the
    raw path instead resolves to nothing at all — it is not an article, not a
    slug, not anything in our namespace — while still LOOKING like a precise
    reference.

    A target can carry MARKUP — SUDAN's `{{11link|{{sc|Dongola}}: «I»Mudiria«/I»|
    Dongola (province)}}` reaches here with the italics already marked — and a
    CLOSE marker's slash is not a path separator.  Splitting the raw string turned
    `«/I»` into `«#I»`, which then failed the 3-part «LN» opener grammar (its
    fields are `[^|«]*`), collapsed the marker to its 2-part reading, and put the
    filename in the href with a raw pipe in the anchor text.  So the path work
    happens on a string whose markers are held aside.
    """
    t = (target or "").strip().lstrip("/")
    held: list[str] = []

    def _hold(m: re.Match) -> str:
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x01"

    def _release(s: str) -> str:
        return re.sub(r"\x00(\d+)\x01", lambda m: held[int(m.group(1))], s)

    t = MARKER_TOKEN_RE.sub(_hold, t)
    path, _, frag = t.partition("#")
    segs = [s for s in path.split("/") if s]
    if not segs:
        return _release(t)
    article = segs[0].replace("_", " ").strip()
    anchor = frag if frag else (segs[-1] if len(segs) > 1 else "")
    anchor = _SUBPAGE_ORDINAL.sub("", anchor.replace("_", " ")).strip()
    return _release(f"{article}#{anchor}" if anchor else article)


def ln_marker(target: str, display: str, filename: str | None = None) -> str:
    """THE `«LN»` emitter — the only place the marker's field order is written.

    Two forms, one owner: the producer's 2-part `«LN:target|display«/LN»`, and the
    export's 3-part `«LN:filename|target|display«/LN»` once resolution knows which
    article the target is.

    The 3-part form had NO owner: `export/article_json.py` spelled it out by hand in
    five separate f-strings (`_resolve_link` ×3, `_resolve_eb9`, `_resolve_author`),
    each independently deciding which value went in which slot — and a sixth was
    added on 2026-08-13 before anyone noticed.  Six hand-written copies of one
    grammar is how the target/display order drifts, and drift in THIS grammar is
    what prints a filed catalogue title into running prose.
    """
    head = f"{filename}|{target}" if filename is not None else target
    return f"«LN:{head}|{display}«/LN»"


# ── The seven link wraps (rows in `_PR_WRAP`; `body` = the substituted display) ─────────────
_ROMAN_LABEL = re.compile(r"^[IVXLC]+\.")
_WORK_PREFIX = re.compile(r"^1911\s+[Ee]ncyclop[^/]*/", re.IGNORECASE)
_AUTHOR_PREFIX = re.compile(r"^\s*Author:\s*", re.IGNORECASE)


# Each resolver answers the two questions the skeleton cannot: what the target
# becomes, and what to show when the source named no display.  Returning an
# empty target means "this is not a link" — the skeleton emits the display as
# prose, which is the only sane thing to do with a reference to nowhere.
def _resolve_article(target, display, body):
    """A subpage target whose display is a roman numeral is a SECTION LABEL —
    `IV. History` names a part of the page it sits on, not another article."""
    if "/" in target:
        if display and _ROMAN_LABEL.match(display):
            return "", body
        # The subpage path becomes OUR section form; it used to be discarded
        # entirely in favour of the display arg, which sent the reader to the top
        # of a 110-page article when the source had named one of its sections.
        return subpage_target(target), body
    return target, body


def _resolve_target_first(target, display, body):
    """A no-display `{{lkpl|Egypt/3 History}}` printed the raw Wikisource path to
    the reader in 23 places.  The piped form already shows the article name;
    match it."""
    if "/" not in target:
        return target, body
    folded = subpage_target(target)
    return folded, (body if display is not None else folded.partition("#")[0])


def _resolve_intra(target, display, body):
    return ("#" + target if target else ""), body


def _resolve_selfref(target, display, body):
    """Strip the `1911 Encyclopædia Britannica/` prefix, KEEP the `#Section`
    fragment.  A bare ref to the work (no `/Article`) points at no article."""
    shown = body if display is not None else ""
    rest = _WORK_PREFIX.sub("", target)
    if rest == target:                      # a reference to the work itself
        return "", (shown or target)
    # `rest` may still carry a Wikisource SUBPAGE — `Egypt/3 History#Mahommedan`.
    # Fold it into our `ARTICLE#Section` form; a plain `Article#Section` passes
    # through unchanged.
    folded = subpage_target(rest) if "/" in rest else rest.strip()
    article = folded.partition("#")[0].strip()
    if not article:
        return "", shown
    return folded, (shown or article)


def _resolve_author(target, display, body):
    """Carried through the walk NEUTRALLY as `«AL:name|display»`.  The
    signature-vs-reference decision is DEFERRED to 6b4, where the finished roster
    resolves it ([[project_roster_from_author_links]]): a display that is a known
    contributor's initials becomes the bare-initials signoff, otherwise an `«LN»`
    xref.  The walk no longer needs a roster, and render + binding become one
    roster-driven decision."""
    name = _AUTHOR_PREFIX.sub("", target).strip()
    return name, (body or name)


def _resolve_fragment(target, display, body):
    section = target.lstrip("#").strip()
    return ("#" + section if section else ""), (body if display is not None
                                                else section)


def _resolve_wikilink(target, display, body):
    """With no display, show the bare name (`w:`/`Portal:` prefix stripped as
    noise); the target keeps its prefix for the resolver."""
    return target, (body if display is not None else _strip_link_prefix(target))


def _al(target: str, disp: str, ctx) -> str:
    return f"«AL:{target}|{disp}«/AL»"


class _LinkForm:
    """One link form: where its slots are, what its target becomes, how it emits.

    `peel_target_when_bare` is the PEEL side of the same fact — a template form
    with one positional means that string as both target and display, so the
    display slot to recurse is the target; a bracket form with no pipe has no
    display slot at all.
    """

    __slots__ = ("slots", "resolve", "emit", "peel_target_when_bare")

    def __init__(self, slots, resolve, emit=None, peel_target_when_bare=False):
        self.slots = slots
        self.resolve = resolve
        self.emit = emit or _ln
        self.peel_target_when_bare = peel_target_when_bare


_LINK_FORMS = {
    "EB1911_ARTICLE_LINK": _LinkForm(_slots_display_first, _resolve_article,
                                     peel_target_when_bare=True),
    "TARGET_FIRST_LINK":   _LinkForm(_slots_target_first, _resolve_target_first,
                                     peel_target_when_bare=True),
    "INTRA_ARTICLE_LINK":  _LinkForm(_slots_target_first, _resolve_intra,
                                     peel_target_when_bare=True),
    "EB1911_SELFREF":      _LinkForm(_slots_bracket, _resolve_selfref),
    "AUTHOR_LINK":         _LinkForm(_slots_bracket, _resolve_author, _al),
    "FRAGMENT_LINK":       _LinkForm(_slots_bracket, _resolve_fragment),
    "WIKILINK":            _LinkForm(_slots_bracket, _resolve_wikilink),
}


def _link_wrap(label: str):
    """THE link wrap.  `body` is the substituted (already recursed) display."""
    form = _LINK_FORMS[label]

    def wrap(raw, body, ctx):
        target_raw, display_raw = form.slots(raw)
        target, disp = form.resolve(target_raw, display_raw, body.strip())
        if not target:
            return disp
        return form.emit(target, disp, ctx)

    wrap.__name__ = f"_wrap_{label.lower()}"
    return wrap


# The classifier's set of link labels IS the set of forms — one list, so a form
# cannot be added without the classifier routing it.
_LINK_LABELS = frozenset(_LINK_FORMS)


_wrap_article_link = _link_wrap("EB1911_ARTICLE_LINK")
_wrap_target_first = _link_wrap("TARGET_FIRST_LINK")
_wrap_selfref = _link_wrap("EB1911_SELFREF")
_wrap_author_link = _link_wrap("AUTHOR_LINK")
_wrap_fragment_link = _link_wrap("FRAGMENT_LINK")
_wrap_intra_link = _link_wrap("INTRA_ARTICLE_LINK")
_wrap_wikilink = _link_wrap("WIKILINK")
