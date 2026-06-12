"""New table producer — three concepts (table, row, cell), one recognizer.

chop → recurse → reassemble.  A single nesting-aware top-level recognizer
chops a table's inner into rows and cells, recognizing both `{|` and
`<table>` spellings at once (no flavor branch), skipping any divider that
sits inside a nested construct ({{…}}, [[…]], {|…|}, <table>, <math>, a
walker placeholder, …) by stepping over it with the walker's `_construct_end`.

The dividers die at the chop: the output is divider-blind structure, the
canonical `(sep, attr_part, content)` cell shape that `_table_decompose`'s
`produce_table_rows` recurses and reassembles.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._walker import _construct_end

# (sep, attr_part, content); sep '|' = data cell, '!' = header cell.
Cell = tuple[str, str, str]
Row = tuple[str, list[Cell]]  # (row_attr_part, cells)

_TAG_ROW = re.compile(r"<tr\b([^>]*)>", re.IGNORECASE)
_TAG_CELL = re.compile(r"<(t[dh])\b([^>]*)>", re.IGNORECASE)
_TAG_CAPTION = re.compile(r"<caption\b[^>]*>(.*?)</caption\s*>",
                          re.IGNORECASE | re.DOTALL)
_TAG_CLOSE = re.compile(r"</(?:td|th|tr|caption)\s*>", re.IGNORECASE)

# A wiki cell's attr/content boundary: the part before the first top-level
# `|` is the attr slot IFF it looks like attributes (an attr keyword, an
# HTML style/align/etc., or pure `{{Ts|…}}` styling) — MediaWiki's own rule.
_CELL_ATTR_RE = re.compile(
    r'^\s*(?:[A-Za-z_:-]+\s*=|colspan|rowspan|align|valign|style|class|'
    r'width|height|scope|bgcolor|nowrap|abbr|id|{{[Tt]s\b)', re.IGNORECASE)


def _skip(inner: str, i: int) -> int:
    """End of the nested construct opening at `i`, or `i` if none opens
    here.  `<td>`/`<tr>`/`<th>`/`<i>`/… return `None` from `_construct_end`
    (not constructs the walker bounds), so they are left for the divider
    scan; `{{…}}`/`[[…]]`/`<table>`/`<math>`/comments are stepped over whole.

    A nested `{|…|}` is depth-counted HERE rather than via `_construct_end`,
    because an UNCLOSED `{|` (common in the corpus, where the nested table
    shares the outer's `|}`) makes `_construct_end` return `None` — which
    would let the scanner descend and hoist the nested table's cells (the
    flatten bug).  Run it to its matching `|}` or to end-of-string, whole."""
    if inner.startswith("{|", i):
        n = len(inner)
        depth, j = 1, i + 2
        while j < n and depth:
            if inner.startswith("{|", j):
                depth += 1
                j += 2
            elif inner.startswith("|}", j):
                depth -= 1
                j += 2
            else:
                j += 1
        return j
    e = _construct_end(inner, i)
    return e if (e is not None and e > i) else i


def _peel_caption(inner: str) -> tuple[str, str]:
    """Pull a top-level caption (`|+ …` wiki line, or `<caption>…</caption>`)
    out of the inner so it doesn't masquerade as a cell.  Returns
    `(caption, inner_without_caption)`."""
    # HTML caption — match at top level (skip nested).
    n = len(inner)
    i = 0
    while i < n:
        j = _skip(inner, i)
        if j > i:
            i = j
            continue
        m = _TAG_CAPTION.match(inner, i)
        if m:
            return m.group(1).strip(), inner[:m.start()] + inner[m.end():]
        i += 1
    # Wiki caption — a `|+` at line start.
    m = re.search(r"(?:^|\n)[ \t]*\|\+([^\n]*)", inner)
    if m:
        return m.group(1).strip(), inner[:m.start()] + inner[m.end():]
    return "", inner


def _split_rows(inner: str) -> list[tuple[str, str]]:
    """Top-level split of (caption-free) inner into `[(row_attr, row_body)]`
    on `|-` (wiki) and `<tr>` (html) dividers.  The pre-first-divider
    segment is the implicit first row."""
    n = len(inner)
    dividers: list[tuple[int, int, str]] = []  # (div_start, body_start, attr)
    i = 0
    line_start = True
    while i < n:
        j = _skip(inner, i)
        if j > i:
            i = j
            line_start = False
            continue
        m = _TAG_ROW.match(inner, i)
        if m:
            dividers.append((i, m.end(), m.group(1).strip()))
            i = m.end()
            line_start = False
            continue
        if line_start and inner.startswith("|-", i):
            eol = inner.find("\n", i)
            eol = n if eol < 0 else eol
            dividers.append((i, eol, inner[i + 2:eol].strip()))
            i = eol
            line_start = True
            continue
        ch = inner[i]
        line_start = ch == "\n" or (line_start and ch in " \t")
        i += 1

    rows: list[tuple[str, str]] = []
    first = dividers[0][0] if dividers else n
    rows.append(("", inner[:first]))
    for k, (_ds, bs, attr) in enumerate(dividers):
        nxt = dividers[k + 1][0] if k + 1 < len(dividers) else n
        rows.append((attr, inner[bs:nxt]))
    return rows


def _split_cells(row_body: str) -> list[Cell]:
    """Top-level split of a row body into `[(sep, attr_part, content)]`.
    Cell starts: line-start `|`/`!` (wiki), inline `||`/`!!` (wiki), and
    `<td>`/`<th>` (html).  A close tag (`</td>`/`</tr>`) ends the current
    cell's content; a wiki cell self-closes at the next cell.  Nested
    constructs are skipped whole.  Each cell's content runs to the next
    EVENT (cell start or close), not the next cell start."""
    n = len(row_body)
    events: list[tuple] = []  # ("cell", pos, content_start, sep, html_attr) | ("close", pos, …)
    i = 0
    line_start = True
    while i < n:
        j = _skip(row_body, i)
        if j > i:
            i = j
            line_start = False
            continue
        m = _TAG_CELL.match(row_body, i)
        if m:
            sep = "!" if m.group(1).lower() == "th" else "|"
            events.append(("cell", i, m.end(), sep, m.group(2).strip()))
            i = m.end()
            line_start = False
            continue
        m = _TAG_CLOSE.match(row_body, i)
        if m:
            events.append(("close", i, None, None, None))
            i = m.end()
            line_start = False
            continue
        two = row_body[i:i + 2]
        if not line_start and two in ("||", "!!"):
            sep = "|" if two == "||" else "!"
            events.append(("cell", i, i + 2, sep, None))
            i += 2
            line_start = False
            continue
        if line_start and row_body[i:i + 1] in ("|", "!"):
            sep = row_body[i]
            events.append(("cell", i, i + 1, sep, None))
            i += 1
            line_start = False
            continue
        ch = row_body[i]
        line_start = ch == "\n" or (line_start and ch in " \t")
        i += 1

    cells: list[Cell] = []
    for idx, ev in enumerate(events):
        if ev[0] != "cell":
            continue
        _, _mark, cs, sep, html_attr = ev
        content_end = events[idx + 1][1] if idx + 1 < len(events) else n
        raw = row_body[cs:content_end]
        if html_attr is not None:          # html cell — attr was in the tag
            attr, content = html_attr, raw
        else:                              # wiki cell — split attr|content
            attr, content = _wiki_attr_split(raw)
        cells.append((sep, attr.strip(), content.strip()))
    return cells


def _wiki_attr_split(raw: str) -> tuple[str, str]:
    """Split a wiki cell body at its first TOP-LEVEL `|`: the prefix is the
    attr slot iff it looks like attributes, else the whole body is content."""
    n = len(raw)
    i = 0
    while i < n:
        j = _skip(raw, i)
        if j > i:
            i = j
            continue
        if raw[i] == "|":
            prefix = raw[:i]
            if _CELL_ATTR_RE.match(prefix):
                return prefix, raw[i + 1:]
            return "", raw
        i += 1
    return "", raw


def recognize_table(inner: str) -> tuple[str, list[Row]]:
    """Chop a table's inner into `(caption, [(row_attr, cells)])` — ONE
    nesting-aware top-level recognizer, both syntaxes, divider-blind output."""
    caption, body = _peel_caption(inner)
    rows: list[Row] = []
    for row_attr, row_body in _split_rows(body):
        cells = _split_cells(row_body)
        if cells:
            rows.append((row_attr, cells))
    return caption, rows


# ── Attr-slot, recursed (no whitelist) ──────────────────────────────────
#
# The cell/row/table attribute slot has ONE nested construct, `{{Ts|…}}`,
# which is recursed through its producer (`_parse_ts_codes` — the existing
# Ts-code→CSS map, which also passes inline CSS through).  EVERYTHING else —
# `style="…"`, `align=`, `width=`, `bgcolor=`, bare CSS, an attribute we
# don't recognise — is CARRIED, never dropped.  This is the faithful
# replacement for `_cell_styles`'s whitelist: recursion never loses.

_TS_TMPL_RE = re.compile(r"\{\{[Tt]s\s*((?:\|[^{}]*)?)\}\}")
_KV_RE = re.compile(r'([A-Za-z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|(\S+))')
_ATTR_CSS = {"valign": "vertical-align", "bgcolor": "background-color",
             "color": "color", "width": "width", "height": "height"}


def fold_cell_styles(attr_part: str) -> list[str]:
    """Total attr handling — the attr slot recursed, nothing dropped."""
    from britannica.pipeline.stages.elements._tables import _parse_ts_codes
    rules: list[str] = []
    slot = attr_part or ""
    for m in _TS_TMPL_RE.finditer(slot):          # {{Ts|…}} → CSS (its producer)
        rules += _parse_ts_codes(m.group(1).lstrip("|"))
    slot = _TS_TMPL_RE.sub(" ", slot)
    for m in _KV_RE.finditer(slot):               # key="val" or key=val attributes
        k = m.group(1).lower()
        v = m.group(2) if m.group(2) is not None else m.group(3)
        if k in ("colspan", "rowspan"):           # structural — emit owns these
            continue
        if k == "style":
            rules += [d.strip() for d in v.split(";") if d.strip()]
        elif k == "align":
            rules.append(f"text-align:{'center' if v.startswith('cent') else v}")
        elif k in _ATTR_CSS:
            rules.append(f"{_ATTR_CSS[k]}:{v}")
        else:                                      # unrecognised — CARRY it
            rules.append(f"{k}:{v}")
    slot = _KV_RE.sub(" ", slot)
    for frag in slot.split(";"):                  # leftover bare CSS (width:50%)
        frag = frag.strip()
        if ":" in frag and not frag.startswith(("|", "{")):
            rules.append(frag.rstrip(";").strip())
    seen: dict[str, int] = {}                      # dedupe, later wins
    out: list[str] = []
    for r in rules:
        p = r.split(":", 1)[0].strip()
        if p in seen:
            out[seen[p]] = r
        else:
            seen[p] = len(out)
            out.append(r)
    return out
