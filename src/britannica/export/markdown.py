"""Marker stream → GitHub-Flavored Markdown — the agent/download emitter.

The article ``body`` is a marker stream — a SERIALIZED TREE, in paired,
uniquely-named delimiters that cannot be confused with content.  This reads that
tree the way :mod:`britannica.render.inline` does for HTML, and for the same
reason: the walk already did the recognition, so a consumer recovers the structure
by scanning the delimiters, never by matching across them.

Two shapes, both honest, exactly as ``decode_inline`` uses them:

  * where a marker's translation is CONTEXT-FREE (``«I»`` → ``*`` whatever its
    depth or parent), an independent open→text / close→text substitution IS a tree
    walk — position cannot change the answer, so position need not be known;
  * where it is NOT (a link's target lives in the opener but Markdown needs it at
    the close; a GFM table needs a separator row sized to the column count; a
    footnote hoists out of position), ``_sub_balanced`` scans for the DEPTH-matched
    close and recurses into the inner.

What is outlawed here is the lazy span — ``«X:…«/X»`` with ``[\\s\\S]*?`` — which
stops at the FIRST close rather than the matching one and fails silently.  This
module was 14 of them, written two days before the Python inline decoder existed
and modelled on ``markers_to_text``, a STRIPPER: for stripping, a mis-paired span
loses a few words and nothing announces it; for TRANSLATING, the same mis-pair
emits wrong text inside a wrong construct.  It shipped 8,140 raw markers across
560 articles, including a ``_OUTLINE_RE`` written for ``«OUTLINE:`` — a form that
has never existed — which therefore matched nothing for five weeks while 151
articles shipped a raw ``«OUTLINE»`` in the download bundle.

Totality is now grounded in the OUTPUT, not in a manifest: anything without a rule
survives visibly and `tools/diagnostics/output_leaks.py` counts it
([[project_leak_audit_reframe]]).  The old claim of being "TOTAL by construction"
over ``RENDERED_GUILLEMET_MARKER_NAMES`` was true and worthless — that constant
omitted the very names this file had no rule for.

Policy per marker family:
  * headings   «SEC:slug|name» → ``## name``   ; «SH:slug»…«/SH» → ``### …``
  * emphasis   «I»→``*…*``  «B»→``**…**``  «STK»→``~~…~~``  «SS»/«SR»→``<sub>``/``<sup>``
  * presentation (SHED to content)  «SC» «CTR» «DIV[…]» «SPAN[…]» «FL» «FR»
                 «MIRROR» and the size family «SM»/«LG»/«XS»/«XXS»/«XXL»/«FS»/«LH»
  * links      «LN:target|display«/LN» / «XL:url|display«/XL» → ``[display](target)``
  * footnotes  «FN[name]?:body«/FN» → ``[^n]`` inline + a collected notes block
  * math       «MATH:…«/MATH» → ``$…$`` (display → ``$$…$$``) ; «EQN» → ``$$``
  * images     {{IMG:file|meta|caption}} → ``![caption](file)`` reference
  * lists      «OL[type:…]»/«UL» with nested «LI» items → a Markdown list
                 (`1.` / `-`), keeping the source's own nesting
  * tables     «TABLE[…]»…«/TABLE»  →  a de-spanned GFM table (see _table_to_gfm)
  * structural drop (carried in the record's own fields, not the prose):
                 «TITLE» (the title field), «ANCHOR» (a bare nav target),
                 the «PAGE» stream markers.
"""
from __future__ import annotations

import re

from britannica.markers import strip_marker_tokens as strip_markers


# ── the balanced scan — the ONE way this module crosses a marker span ─────────

def _balanced_end(text: str, j: int, open_re: re.Pattern, close: str) -> int:
    """Index just past the DEPTH-matched ``close`` for an opener ending at ``j``.

    -1 when the span is unbalanced.  Callers leave unbalanced markers RAW rather
    than guessing a close: a visible marker is a reported leak, a guessed close is
    silently wrong text ([[feedback_honesty_surface_failures]]).
    """
    depth = 1
    while depth:
        nxt = open_re.search(text, j)
        nc = text.find(close, j)
        if nc < 0:
            return -1
        if nxt is not None and nxt.start() < nc:
            depth, j = depth + 1, nxt.end()
        else:
            depth, j = depth - 1, nc + len(close)
    return j


def _sub_balanced(text: str, open_re: re.Pattern, close: str, render) -> str:
    """Replace every balanced ``open_re``…``close`` span with ``render(m, inner)``."""
    out, i = [], 0
    while True:
        m = open_re.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        end = _balanced_end(text, m.end(), open_re, close)
        if end < 0:                       # unbalanced — carry it out raw, visibly
            out.append(text[i:m.end()])
            i = m.end()
            continue
        out.append(text[i:m.start()])
        out.append(render(m, text[m.end():end - len(close)]))
        i = end


# ── openers (an opener is matched; a close is a literal token) ────────────────

_TITLE_OPEN = re.compile(r"«TITLE:")
_FN_OPEN = re.compile(r"«FN(?:\[([^\]]*)\])?:")
_MATH_OPEN = re.compile(r"«MATH(\[[^\]]*\])?:")
_EQN_OPEN = re.compile(r"«EQN:[^»]*»")
_SH_OPEN = re.compile(r"«SH:[^»]*»")
# The link OPENER carries the fields; the display is the span's CONTENT and rides
# through to the passes below.  `[^|«]*` stops each field at the first marker, which
# is what makes the display's own markers (`«I»`, a nested «XL») someone else's job —
# the same grammar `render.inline._LN_OPEN_RE` uses, and for the same reason its
# comment gives: a display group that excluded « leaked whenever a link held a
# marker decoded after this pass.  A trailing second field marks the 3-part
# (resolved) form `«LN:filename|target|display«/LN»`.
_LN_OPEN = re.compile(r"«LN(?:\[[a-z_]*\])?:([^|«]*)\|(?:([^|«]*)\|)?")
_XL_OPEN = re.compile(r"«XL(?:\[[a-z_]*\])?:([^|«]*)(\|)?")
_TABLE_OPEN = re.compile(r"«TABLE\[[^\]]*\]»")
_TABLEBRACE_OPEN = re.compile(r"\{\{TABLEH?:")
_VERSE_OPEN = re.compile(r"\{\{VERSE(?:\[[^\]]*\])?:")
_IVERSE_OPEN = re.compile(r"\{\{IVERSE(?:\[[^\]]*\])?:")
_LEGEND_OPEN = re.compile(r"\{\{LEGEND:")
# The two outline forms — block and in-cell — differ only in where they may
# appear; both carry the same «OLI:depth» items and render the same list.
# NOT `PLATE_OUTLINE`: no producer emits it.  It survives only in two consumers'
# patterns (the old `_OUTLINE_RE` here, and `markers._DROP_MARKER_RE`), both
# spelling it with a colon that could never have matched anyway.  A form kept "just
# in case" is how the dead `«OUTLINE:` pattern lived for five weeks; if one is ever
# emitted the leak oracle reports it, which is the point of the oracle.
_LIST_FORMS = ((re.compile(r"«OL(?:\[type:[\w-]+\])?»"), "«/OL»", True),
               (re.compile(r"«UL»"), "«/UL»", False))
_LI_OPEN = re.compile(r"«LI»")
_CTR_SC_OPEN = re.compile(r"«CTR»\s*«SC»")
_SC_OPEN = re.compile(r"«SC»")
_CAPTION_OPEN = re.compile(r"«CAPTION»")

_SEC_RE = re.compile(r"«SEC:[^|»]*\|([^»]*)»")
_ANCHOR_RE = re.compile(r"«ANCHOR:[^»]*»")
_IMG_RE = re.compile(r"\{\{IMG:([^|}]+)((?:\|[^{}]*?)*?)\}\}")

# emphasis that maps to real Markdown
_WRAP = {"I": ("*", "*"), "B": ("**", "**"), "STK": ("~~", "~~"),
         "SS": ("<sub>", "</sub>"), "SR": ("<sup>", "</sup>")}
# presentation that SHEDS to its inner content (open+close both → "")
_SHED = ("SC", "CTR", "MIRROR", "FL", "FR",
         "SM", "LG", "XS", "XXS", "XXL", "FS", "LH")

_RULE_RE = re.compile(r"«BAR(?:\[\d+\])?»|«DHR(?:\[[^\]]*\])?»|«DHRI(?:\[[^\]]*\])?»")
_BRACE2_RE = re.compile(r"«BRACE2\[[^\]]*\]»")
# open/close tokens for the SHED family and the styled DIV/SPAN (carry an [attr]).
# `MIRROR` also has a SPLIT form (`«MIRROR:…«/MIRROR»`, ALPHABET's reversed glyphs)
# which the split-marker pass handles; this catches the bare wrapper form.
# The attribute run excludes only the GUILLEMETS: a real `«SPAN[title:…]»` carries
# nested brackets (`farm [tribute] of the county`), and `[^\]]*` left three articles
# showing the raw marker.  Same correction as `render.inline._SPAN_TITLE_RE`.
_SHED_RE = re.compile(
    r"«/?(?:" + "|".join(_SHED) + r")»|«/?(?:DIV|SPAN)(?:\[[^«»]*\])?»")
_MIRROR_OPEN = re.compile(r"«MIRROR:")


_WS_RUN = re.compile(r"[ \t\r\n]+")


def _flatten(s: str) -> str:
    """Collapse LAYOUT whitespace to single spaces, leaving other spaces alone.

    A bare ``" ".join(s.split())`` also eats U+00A0 and the thin/en spaces, which
    are CONTENT here — `W. S. Jevons` carries a non-breaking space so the
    initials don't split across a line, and 1911 typography uses the thin space
    deliberately.  Dropping them is silent, irreversible loss
    ([[feedback_when_in_doubt_carry]]).
    """
    return _WS_RUN.sub(" ", s).strip()


class _Ctx:
    """Per-article footnote state — numbering + the collected note bodies."""

    def __init__(self):
        self.notes: list[str] = []
        self.named: dict[str, int] = {}


def _img_to_md(m: re.Match) -> str:
    filename = m.group(1).strip()
    meta = m.group(2) or ""
    caption = ""
    # caption is any trailing pipe-field that isn't an align=/width=/height= field
    for field in (f for f in meta.split("|") if f):
        if not re.match(r"(?:align|width|height)=", field):
            caption = field
    alt = caption or filename
    return f"![{alt}]({filename})"


def _link_md(target: str, display: str) -> str:
    """``[display](target)``, with the display left for the later passes to finish.

    Whitespace is collapsed because a newline inside `[...]` breaks the link
    syntax; the display's MARKERS are deliberately untouched here — they decode in
    the context-free passes after this one, which is what lets a link hold an
    «I» or a nested «XL» without this pass having to know about them.
    """
    display = _flatten(display or "") or target.strip()
    return f"[{display}]({target.strip()})"


def _list_md(inner: str, ctx: _Ctx, ordered: bool, level: int = 0) -> str:
    """A list body → a Markdown list, keeping the SOURCE's own nesting.

    A sublist rides inside its parent ``«LI»``, so this recurses into the item
    rather than densifying a depth ladder — which is what the outline it replaces
    had to do, its depths being inferred rather than stated.  An ordered list
    numbers with ``1.`` and Markdown renumbers it, exactly as the browser does
    from the list's own ``type``.
    """
    items: list[str] = []
    i = 0
    while True:
        m = _LI_OPEN.search(inner, i)
        if m is None:
            break
        end = _balanced_end(inner, m.end(), _LI_OPEN, "«/LI»")
        if end < 0:
            break
        items.append(inner[m.end():end - len("«/LI»")])
        i = end
    if not items:
        # No items: hand the inner back so whatever is there stays VISIBLE.
        return _convert(inner, ctx)
    pad = "  " * level
    lines: list[str] = []
    for n, item in enumerate(items, 1):
        sub = ""
        for opener, close, sub_ordered in _LIST_FORMS:
            mo = opener.search(item)
            if mo is None:
                continue
            sub_end = _balanced_end(item, mo.end(), opener, close)
            if sub_end < 0:
                continue
            sub = _list_md(item[mo.end():sub_end - len(close)],
                           ctx, sub_ordered, level + 1)
            item = item[:mo.start()] + item[sub_end:]
            break
        body = _flatten(_convert(item, ctx))
        lines.append(f"{pad}{n}. {body}" if ordered else f"{pad}- {body}")
        if sub:
            lines.append(sub)
    joined = "\n".join(lines)
    return joined if level else "\n\n" + joined + "\n\n"


def _table_to_gfm(opener: str, inner: str, ctx: _Ctx) -> str:
    """A carried ``«TABLE[…]»…«/TABLE»`` → a de-spanned GFM table.

    Expand colspan/rowspan by REPEATING the value into every cell it covered, so
    each row is self-contained (the shape RAG wants); the merge itself is pure
    presentation, shed.  Cells are recursively converted.  A nested table (plate
    grids) can't be expressed in GFM, so it falls back to HTML (which GFM allows
    and agents parse).
    """
    if _TABLE_OPEN.search(inner):
        # The HTML fallback keeps «CAPTION» as <caption>; only the GFM path below
        # has nowhere to put it.
        return "\n\n" + _nested_table_html(opener, inner, ctx).strip() + "\n\n"
    # GFM has no caption syntax, so the caption becomes an emphasized line ABOVE
    # the table.  Dropping it would be silent loss — BABYLONIA's "Kings of Assyria"
    # survived the old emitter only because its table parse failed and dumped the
    # caption as raw text ([[feedback_preserve_trivial_content]]).
    captions: list[str] = []

    def _take_caption(_m: re.Match, body: str) -> str:
        captions.append(_flatten(_convert(body, ctx)))
        return ""

    inner = _sub_balanced(inner, _CAPTION_OPEN, "«/CAPTION»", _take_caption)
    # Emitted as its own line, NOT re-emphasized: 1911 captions carry their own
    # «I», so wrapping again produced `**Kings of Assyria*.*` — malformed emphasis
    # from decorating text that was already decorated.
    lead = "".join(f"\n\n{c}" for c in captions if c)
    rows: list[list[str]] = []
    spans: dict[tuple[int, int], str] = {}   # (row, col) → carried rowspan value
    for r, (_tr_open, tr) in enumerate(_spans_of(inner, _TR_OPEN, "«/TR»")):
        row: list[str] = []
        c = 0
        for cell_open, content in _spans_of(tr, _CELL_OPEN, None):
            while (r, c) in spans:           # a rowspan from above lands here
                row.append(spans.pop((r, c)))
                c += 1
            payload = cell_open.group(2) or ""
            text = _flatten(_convert(content, ctx))
            cspan = int((re.search(r"colspan:(\d+)", payload) or [0, 1])[1])
            rspan = int((re.search(r"rowspan:(\d+)", payload) or [0, 1])[1])
            for _ in range(cspan):
                row.append(text)
                for rr in range(1, rspan):   # carry value down for rowspans
                    spans[(r + rr, c)] = text
                c += 1
        rows.append(row)
    rows = [row for row in rows if row]
    if not rows:
        # A caption with no rows still carries its text out.
        return (lead + "\n\n") if lead else ""
    width = max(len(row) for row in rows)
    for row in rows:                         # pad short rows (1-cell letter
        row.extend([""] * (width - len(row)))  # dividers, etc.) to a clean grid
    esc = lambda s: s.lstrip("|").replace("|", "\\|").strip()
    head = "| " + " | ".join(esc(x) for x in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = "\n".join("| " + " | ".join(esc(x) for x in row) + " |" for row in rows[1:])
    return lead + "\n\n" + "\n".join([head, sep, body]).rstrip() + "\n\n"


_TR_OPEN = re.compile(r"«TR(?:\[([^\]]*)\])?»")
_CELL_OPEN = re.compile(r"«(T[DH])(?:\[([^\]]*)\])?»")


def _spans_of(text: str, open_re: re.Pattern, close: str | None):
    """Yield ``(open_match, inner)`` for each balanced span of ``open_re``.

    ``close=None`` derives the close from the opener's own name (``«TD»`` → ``«/TD»``),
    which is what a cell needs: TD and TH share one scan but not one closer.
    """
    i = 0
    while True:
        m = open_re.search(text, i)
        if m is None:
            return
        tok = close or f"«/{m.group(1)}»"
        end = _balanced_end(text, m.end(), open_re, tok)
        if end < 0:
            return
        yield m, text[m.end():end - len(tok)]
        i = end


def _nested_table_html(opener: str, inner: str, ctx: _Ctx) -> str:
    """Nested tables → HTML (GFM has no nested-table syntax), markers → tags."""
    def tag(m: re.Match) -> str:
        name = (m.group(1) or "").lower()
        attrs = ""
        for field in (m.group(2) or "").split("|"):
            if not field:
                continue
            k, _, v = field.partition(":")
            if k == "cols":                  # metadata, not an attribute
                continue
            attrs += f' {k}="{v}"'
        return f"<{name}{attrs}>"

    h = opener + inner + "«/TABLE»"
    h = re.sub(r"«(TABLE|TR|TD|TH)(?:\[([^\]]*)\])?»", tag, h)
    for name in ("TABLE", "TR", "TD", "TH", "CAPTION"):
        h = h.replace(f"«/{name}»", f"</{name.lower()}>")
    h = h.replace("«CAPTION»", "<caption>")
    return _convert(h, ctx)


def _footnote_md(m: re.Match, inner: str, ctx: _Ctx) -> str:
    """``«FN[name]?:body«/FN»`` → an inline ``[^n]``; the body joins the notes block."""
    name = m.group(1)
    if name and name in ctx.named:
        return f"[^{ctx.named[name]}]"
    n = len(ctx.notes) + 1
    if name:
        ctx.named[name] = n
    ctx.notes.append("")                     # reserve the slot BEFORE recursing,
    ctx.notes[n - 1] = _flatten(_convert(inner, ctx))
    return f"[^{n}]"


def _drop_heading_echo(text: str) -> str:
    """Drop a ``«CTR»«SC»name«/SC»«/CTR»`` that merely re-shows the «SEC» heading.

    «SEC» is a POINT marker carrying the name; the visible heading render follows
    it as a centred small-caps run.  Emitting both would print the heading twice.
    """
    out, i = [], 0
    while True:
        m = _SEC_RE.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:m.start()])
        out.append(f"\n\n## {m.group(1).strip()}\n\n")
        j = m.end()
        echo = _CTR_SC_OPEN.match(text, j + (len(text[j:]) - len(text[j:].lstrip())))
        if echo is not None:
            # The echo is EXACTLY `«CTR»«SC»…«/SC»«/CTR»`.  Requiring the «/CTR» to
            # sit immediately after the balanced «/SC» is the whole discipline here:
            # scanning to the balanced «/CTR» alone swallows whatever lies between —
            # in VALVES that was two images and a heading, silently.  A shape that
            # isn't this one is not an echo, so it stays.
            sc_end = _balanced_end(text, echo.end(), _SC_OPEN, "«/SC»")
            if sc_end > 0 and text.startswith("«/CTR»", sc_end):
                j = sc_end + len("«/CTR»")
        i = j


def _convert(text: str, ctx: _Ctx) -> str:
    """Every balanced construct, then the context-free substitutions.

    Recursive: a cell, a footnote body and an outline item all come back through
    here, so a link inside a table cell is a link, not residue.
    """
    text = _sub_balanced(text, _TITLE_OPEN, "«/TITLE»", lambda m, s: "")
    text = _sub_balanced(text, _FN_OPEN, "«/FN»",
                         lambda m, s: _footnote_md(m, s, ctx))
    text = _sub_balanced(text, _MATH_OPEN, "«/MATH»",
                         lambda m, s: (f"\n\n$$\n{_strip_math(s, ctx)}\n$$\n\n"
                                       if m.group(1) else f"${_strip_math(s, ctx)}$"))
    text = _sub_balanced(text, _EQN_OPEN, "«/EQN»",
                         lambda m, s: f"\n\n$$\n{_strip_math(s, ctx)}\n$$\n\n")
    # images BEFORE tables — so a cell's {{IMG:…|width=N}} becomes ![…](file)
    # (no pipe) before the table's cell-escaper would mangle its `|`.
    text = _IMG_RE.sub(_img_to_md, text)
    text = _sub_balanced(text, _TABLE_OPEN, "«/TABLE»",
                         lambda m, s: _table_to_gfm(m.group(0), s, ctx))
    text = _sub_balanced(text, _TABLEBRACE_OPEN, "}TABLE}",
                         lambda m, s: _table_to_gfm("«TABLE[]»", s, ctx))
    for opener, close, ordered in _LIST_FORMS:
        text = _sub_balanced(text, opener, close,
                             lambda m, s, o=ordered: _list_md(s, ctx, o))
    for opener, close in ((_VERSE_OPEN, "}VERSE}"), (_IVERSE_OPEN, "}IVERSE}")):
        text = _sub_balanced(
            text, opener, close,
            lambda m, s: "\n\n" + _convert(s, ctx).strip().replace("\n", "  \n") + "\n\n")
    text = _sub_balanced(text, _LEGEND_OPEN, "}LEGEND}",
                         lambda m, s: "\n\n*" + _convert(s, ctx).strip() + "*\n\n")
    text = _sub_balanced(text, _SH_OPEN, "«/SH»",
                         lambda m, s: f"\n\n### {_convert(s, ctx).strip()}\n\n")
    # LN target = the FIRST field: the article filename on a resolved 3-part link,
    # the raw target on an unresolved 2-part one — the same identifier the bundle's
    # xref edges use.  XL with no pipe has no display, so the url is the text.
    text = _sub_balanced(text, _LN_OPEN, "«/LN»",
                         lambda m, s: _link_md(m.group(1), s))
    text = _sub_balanced(text, _XL_OPEN, "«/XL»",
                         lambda m, s: _link_md(m.group(1),
                                               s if m.group(2) else m.group(1)))
    # «MIRROR:…«/MIRROR» — the SPLIT form (reversed glyphs); shed to its content,
    # like the bare wrapper.  Both flat converters wrote their rule for `«MIRROR»`
    # alone and left 19 raw markers in one article.
    text = _sub_balanced(text, _MIRROR_OPEN, "«/MIRROR»",
                         lambda m, s: _convert(s, ctx))
    text = _drop_heading_echo(text)
    text = _ANCHOR_RE.sub("", text)
    # ── context-free from here: an independent open/close substitution IS the
    #    tree walk for a marker whose translation cannot depend on its position.
    for name, (o, c) in _WRAP.items():
        text = text.replace(f"«{name}»", o).replace(f"«/{name}»", c)
    text = _SHED_RE.sub("", text)                # presentation → its content
    text = _RULE_RE.sub("\n\n---\n\n", text)
    text = _BRACE2_RE.sub("", text)
    return text.replace("«P»", "\n\n").replace("«BR»", "  \n")


def _strip_math(s: str, ctx: _Ctx) -> str:
    """A math/EQN inner → its LaTeX body (nested «MATH» wrappers peeled)."""
    s = _sub_balanced(s, _MATH_OPEN, "«/MATH»", lambda m, inner: inner)
    # `strip_markers`, not a local `«[^«»]*»`: this runs on MATH inner, which is
    # exactly where the unproofread OCR keeps its stray guillemets, and a span
    # there eats LaTeX ([[feedback_verify_the_counter]]).
    return strip_markers(s, "").strip()


def body_to_markdown(body: str) -> str:
    """Render a marker-stream ``body`` to GitHub-Flavored Markdown."""
    ctx = _Ctx()
    text = _convert(body, ctx)
    text = re.compile(r"\n{3,}").sub("\n\n", text).strip()
    if ctx.notes:
        text += "\n\n" + "\n".join(f"[^{i + 1}]: {b}"
                                   for i, b in enumerate(ctx.notes))
    return text
