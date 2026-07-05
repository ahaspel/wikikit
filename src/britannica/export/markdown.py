"""Marker stream → GitHub-Flavored Markdown — the agent/download emitter.

The article ``body`` is a marker stream (the viewer's HTML input).  This is its
Markdown sibling of :func:`britannica.markers.markers_to_text`: where that STRIPS
to plain text, this TRANSLATES to clean GFM — keeping the semantic structure an
LLM/agent consumer wants (headings, emphasis, links, tables, math, footnotes) and
shedding only presentation (sizes, small-caps, centering, floats, styled
divs/spans), which to a model is pure token noise.

Same three-phase shape as ``markers_to_text`` and driven off the same marker
constants, so it is TOTAL by construction: every marker in
``RENDERED_GUILLEMET_MARKER_NAMES`` / ``RENDERED_MARKER_OPENS`` has an explicit
rule here.  A marker with no rule falls through the final inline sweep as bare
text — visible, not silently dropped — the same discipline as the classifier.

Policy per marker family:
  * headings   «SEC:slug|name» → ``## name``   ; «SH:slug»…«/SH» → ``### …``
  * emphasis   «I»→``*…*``  «B»→``**…**``  «STK»→``~~…~~``  «SS»/«SR»→``<sub>``/``<sup>``
  * presentation (SHED to content)  «SC» «CTR» «DIV[…]» «SPAN[…]» «FL» «FR»
                 «MIRROR» and the size family «SM»/«LG»/«XS»/«XXS»/«XXL»/«FS»/«LH»
  * links      «LN:target|display«/LN» / «XL:url|display«/XL» → ``[display](target)``
  * footnotes  «FN[name]?:body«/FN» → ``[^n]`` inline + a collected notes block
  * math       «MATH:…«/MATH» → ``$…$`` (display → ``$$…$$``) ; «EQN»/«EQNGROUP» → ``$$``
  * images     {{IMG:file|meta|caption}} → ``![caption](file)`` reference
  * tables     «HTMLTABLE:<table>…»  →  a de-spanned GFM table (see _table_to_gfm)
  * structural drop (carried in the record's own fields, not the prose):
                 «TITLE» (the title field), «ANCHOR» (a bare nav target),
                 the «PAGE» stream markers.
"""
from __future__ import annotations

import re

from britannica.markers import strip_page_markers

# ── phase 1: split / block markers (they nest a «, so they go first) ──────────

_MATH_RE = re.compile(r"«MATH(\[[^\]]*\])?:([\s\S]*?)«/MATH»")
_EQN_RE = re.compile(r"«EQN:[^»]*»([\s\S]*?)«/EQN»")
_EQNGROUP_RE = re.compile(r"«EQNGROUP»([\s\S]*?)«/EQNGROUP»")
_IMG_RE = re.compile(r"\{\{IMG:([^|}]+)((?:\|[^{}]*?)*?)\}\}")
_VERSE_RE = re.compile(r"\{\{VERSE(?:\[[^\]]*\])?:([\s\S]*?)\}VERSE\}")
_LEGEND_RE = re.compile(r"\{\{LEGEND:([\s\S]*?)\}LEGEND\}")
_TABLEBRACE_RE = re.compile(r"\{\{TABLEH?:([\s\S]*?)\}TABLE\}")
_OUTLINE_RE = re.compile(r"«(?:OUTLINE|PLATE_OUTLINE):([\s\S]*?)«/(?:OUTLINE|PLATE_OUTLINE)»")
_TITLE_RE = re.compile(r"«TITLE:[\s\S]*?«/TITLE»")

# ── phase 2: links ───────────────────────────────────────────────────────────

_LINK_RE = re.compile(r"«(?:LN|XL):([\s\S]*?)«/(?:LN|XL)»")


def _link_md(m: re.Match) -> str:
    # target is the first `|`-field, display the last (mirrors markers._link_display);
    # a display carrying nested inline markers (`«I»…«/I»`) rides through and is
    # translated by the emphasis pass that runs after this in _inline.
    parts = m.group(1).split("|")
    target = parts[0].strip()
    display = (parts[-1] if len(parts) > 1 else parts[0]).strip()
    return f"[{display}]({target})"

# ── phase 3: inline (paired «X»…«/X» and point markers) ──────────────────────

# headings — «SEC» is a POINT marker carrying the name; the visible heading render
# (a following «CTR»«SC»name«/SC»«/CTR») is a duplicate we swallow with it.
_SEC_RE = re.compile(
    r"«SEC:[^|»]*\|([^»]*)»(?:\s*«CTR»«SC»[\s\S]*?«/SC»«/CTR»)?")
_SH_RE = re.compile(r"«SH:[^»]*»([\s\S]*?)«/SH»")
_ANCHOR_RE = re.compile(r"«ANCHOR:[^»]*»")

# emphasis that maps to real Markdown
_WRAP = {"I": ("*", "*"), "B": ("**", "**"), "STK": ("~~", "~~"),
         "SS": ("<sub>", "</sub>"), "SR": ("<sup>", "</sup>")}
# presentation that SHEDS to its inner content (open+close both → "")
_SHED = ("SC", "CTR", "MIRROR", "FL", "FR",
         "SM", "LG", "XS", "XXS", "XXL", "FS", "LH")

_RULE_RE = re.compile(r"«BAR(?:\[\d+\])?»|«DHR(?:\[[^\]]*\])?»")
_BRACE2_RE = re.compile(r"«BRACE2\[[^\]]*\]»")
# open/close tokens for the SHED family and the styled DIV/SPAN (carry a [attr])
_SHED_RE = re.compile(
    r"«/?(?:" + "|".join(_SHED) + r")»|«/?(?:DIV|SPAN)(?:\[[^\]]*\])?»")


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


def _table_to_gfm(inner: str) -> str:
    """A carried ``<table>…</table>`` → a de-spanned GFM table.

    Expand colspan/rowspan by REPEATING the value into every cell it covered, so
    each row is self-contained (the shape RAG wants); the merge itself is pure
    presentation, shed.  Cells are recursively rendered to inline Markdown.  A
    non-rectangular table (ragged after de-span) falls back to the raw HTML,
    which GFM allows and agents parse — the rare escape hatch.
    """
    if "«HTMLTABLE:" in inner or "«CHEM:" in inner or inner.count("<table") > 1:
        # a NESTED table (plate grids) — GFM can't express nesting, so emit the
        # whole thing as HTML (which GFM allows and agents parse), dropping the
        # «HTMLTABLE»/«CHEM» wrapper tokens and decoding the inline markers.
        html = (inner.replace("«HTMLTABLE:", "").replace("«/HTMLTABLE»", "")
                     .replace("«CHEM:", "").replace("«/CHEM»", ""))
        return "\n\n" + _inline(html).strip() + "\n\n"
    rows: list[list[str]] = []
    spans: dict[tuple[int, int], str] = {}  # (row, col) → carried rowspan value
    tr_cells = re.findall(r"<tr\b[^>]*>([\s\S]*?)</tr>", inner, re.I)
    for r, tr in enumerate(tr_cells):
        row: list[str] = []
        c = 0
        for cell in re.finditer(r"<(t[dh])\b([^>]*)>([\s\S]*?)</\1>", tr, re.I):
            while (r, c) in spans:              # a rowspan from above lands here
                row.append(spans.pop((r, c))); c += 1
            attrs, content = cell.group(2), cell.group(3)
            text = _inline(content).replace("\n", " ").strip()
            cspan = int((re.search(r'colspan="?(\d+)', attrs, re.I) or [0, 1])[1])
            rspan = int((re.search(r'rowspan="?(\d+)', attrs, re.I) or [0, 1])[1])
            for _ in range(cspan):
                row.append(text)
                for rr in range(1, rspan):      # carry value down for rowspans
                    spans[(r + rr, c)] = text
                c += 1
        rows.append(row)
    rows = [row for row in rows if row]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    for row in rows:                            # pad short rows (1-cell letter
        row.extend([""] * (width - len(row)))   # dividers, etc.) to a clean grid
    esc = lambda s: s.lstrip("|").replace("|", "\\|").strip()
    head = "| " + " | ".join(esc(x) for x in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = "\n".join("| " + " | ".join(esc(x) for x in row) + " |" for row in rows[1:])
    return "\n\n" + "\n".join([head, sep, body]).rstrip() + "\n\n"


def _inline(text: str) -> str:
    """Phase-3 inline pass: links, headings, emphasis, shed-to-content, rules."""
    text = _LINK_RE.sub(_link_md, text)
    text = _SEC_RE.sub(lambda m: f"\n\n## {m.group(1).strip()}\n\n", text)
    text = _SH_RE.sub(lambda m: f"\n\n### {m.group(1).strip()}\n\n", text)
    text = _ANCHOR_RE.sub("", text)
    for name, (o, c) in _WRAP.items():
        text = text.replace(f"«{name}»", o).replace(f"«/{name}»", c)
    text = _SHED_RE.sub("", text)               # presentation → its content
    text = _RULE_RE.sub("\n\n---\n\n", text)
    text = _BRACE2_RE.sub("", text)
    text = text.replace("«P»", "\n\n").replace("«BR»", "  \n")
    return text


def _render_tables(text: str) -> str:
    """Replace every balanced «HTMLTABLE:…«/HTMLTABLE» / «CHEM:…«/CHEM» with its
    GFM rendering — DEPTH-aware, so a nested table (plate grids) is matched whole
    instead of truncating at the inner close (which a non-greedy regex would)."""
    for open_tok, close_tok in (("«HTMLTABLE:", "«/HTMLTABLE»"), ("«CHEM:", "«/CHEM»")):
        out, i = [], 0
        while True:
            s = text.find(open_tok, i)
            if s < 0:
                out.append(text[i:]); break
            out.append(text[i:s])
            depth, j = 1, s + len(open_tok)
            while depth:
                no, nc = text.find(open_tok, j), text.find(close_tok, j)
                if nc < 0:
                    j = len(text) + len(close_tok); break   # unbalanced: to end
                if 0 <= no < nc:
                    depth += 1; j = no + len(open_tok)
                else:
                    depth -= 1; j = nc + len(close_tok)
            out.append(_table_to_gfm(text[s + len(open_tok): j - len(close_tok)]))
            i = j
        text = "".join(out)
    return text


def body_to_markdown(body: str) -> str:
    """Render a marker-stream ``body`` to GitHub-Flavored Markdown."""
    footnotes: list[str] = []
    named: dict[str, int] = {}

    def _fn(m: re.Match) -> str:
        name, inner = m.group(1), m.group(2)
        if name and name in named:
            return f"[^{named[name]}]"
        n = len(footnotes) + 1
        if name:
            named[name] = n
        footnotes.append(_inline(inner).strip())
        return f"[^{n}]"

    text = strip_page_markers(body, replacement="")
    text = _TITLE_RE.sub("", text)
    # block markers first (they nest a «)
    text = re.compile(r"«FN(?:\[([^\]]*)\])?:([\s\S]*?)«/FN»").sub(_fn, text)
    text = _MATH_RE.sub(
        lambda m: (f"\n\n$$\n{m.group(2).strip()}\n$$\n\n" if m.group(1)
                   else f"${m.group(2).strip()}$"), text)
    text = _EQNGROUP_RE.sub(lambda m: f"\n\n$$\n{_strip_math(m.group(1))}\n$$\n\n", text)
    text = _EQN_RE.sub(lambda m: f"\n\n$$\n{_strip_math(m.group(1))}\n$$\n\n", text)
    # images BEFORE tables — so a cell's {{IMG:…|width=N}} becomes ![…](file)
    # (no pipe) before the table's cell-escaper would mangle its `|`.
    text = _IMG_RE.sub(_img_to_md, text)
    text = _render_tables(text)                 # depth-aware «HTMLTABLE»/«CHEM»
    text = _TABLEBRACE_RE.sub(lambda m: _table_to_gfm(m.group(1)), text)
    text = _VERSE_RE.sub(
        lambda m: "\n\n" + m.group(1).strip().replace("\n", "  \n") + "\n\n", text)
    text = _LEGEND_RE.sub(lambda m: "\n\n*" + _inline(m.group(1)).strip() + "*\n\n", text)
    text = _OUTLINE_RE.sub(
        lambda m: "\n\n" + "\n".join(
            f"- {_inline(ln).strip()}" for ln in m.group(1).split("\n") if ln.strip())
        + "\n\n", text)
    # inline pass
    text = _inline(text)
    # collapse 3+ blank lines, tidy edges
    text = re.compile(r"\n{3,}").sub("\n\n", text).strip()
    if footnotes:
        text += "\n\n" + "\n".join(f"[^{i+1}]: {b}" for i, b in enumerate(footnotes))
    return text


def _strip_math(s: str) -> str:
    """EQN/EQNGROUP inner → the LaTeX body (drop any nested «MATH» wrappers)."""
    s = _MATH_RE.sub(lambda m: m.group(2), s)
    return re.compile(r"«[^«»]*»").sub("", s).strip()
