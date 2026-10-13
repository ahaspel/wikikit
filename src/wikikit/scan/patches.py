"""Patches: corrections of words and formulas laid on a FETCHED page, and
nothing else.

A book's own transcription may be far more accurate than the page it would
replace in its words and formulas, and far poorer in everything a person put
on that page: tables with their rules and spans, links, footnotes, images
(2026-10-12: 335 figures and 39 footnotes lost to wholesale replacement).  A
patch takes only the first: one span of the fetched page's raw wikitext —
`old`, at `start`..`end` — replaced by `new`, our word or our formula.

The book declares its patches in `patches.json`:

  {"<volume>:<page>": [{"start": int, "end": int, "old": str, "new": str,
                        "kind": "word" | "formula"}, ...]}

They are laid on the fetched page as it is loaded (`source_pages.load_payloads`),
so the page enters the pipeline complete and corrected, like a supplied page.
A patch is applied only where its `old` text stands exactly at its place — a
fetched page that has changed underneath makes the load FAIL, never patch the
wrong place — and a patched page must keep every piece of the fetched page's
structure (`structure`): the gate checks it."""
from __future__ import annotations

import json
import re

_cache: dict | None = None


class PatchError(ValueError):
    """A patch whose text no longer stands where it was made."""


def load_patches() -> dict:
    """The book's `patches.json`, loaded once; a book without one has none."""
    global _cache
    if _cache is not None:
        return _cache
    from wikikit.corpora import current_corpus
    book = current_corpus()
    _cache = {}
    if book.has_data("patches.json"):
        with book.data("patches.json").open(encoding="utf-8") as f:
            _cache = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    return _cache


def reset_cache() -> None:
    global _cache
    _cache = None


def patches_for(volume: int, page: int) -> list[dict]:
    return load_patches().get(f"{volume}:{page}", [])


def apply_patches(raw: str, patches: list[dict]) -> str:
    """`raw` with every patch laid on it.  Each patch's `old` must stand at
    its place exactly; patches may not overlap."""
    out, pos = [], 0
    for p in sorted(patches, key=lambda x: (x["start"], x["end"])):
        if p["start"] < pos:
            raise PatchError(f"patches overlap at {p['start']}")
        if raw[p["start"]:p["end"]] != p["old"]:
            raise PatchError(f"patch at {p['start']} expects {p['old'][:40]!r}, "
                             f"page has {raw[p['start']:p['end']][:40]!r}")
        out.append(raw[pos:p["start"]])
        out.append(p["new"])
        pos = p["end"]
    out.append(raw[pos:])
    return "".join(out)


def balanced(text: str, i: int, open_: str, close: str) -> int:
    """The index just past the close of the pair `open_` opens at `i`
    (nesting counted); the text's end when it never closes."""
    depth, j = 0, i
    while j < len(text):
        if text.startswith(open_, j):
            depth += 1
            j += len(open_)
        elif text.startswith(close, j):
            depth -= 1
            j += len(close)
            if depth == 0:
                return j
        else:
            j += 1
    return len(text)


_TAG = re.compile(r"</?[a-zA-Z]")
_INLINE = re.compile(r"'{2,5}|</?su[bp]>")
#: A fetched page's formula-making templates: what a formula patch may replace.
FORMULA_TEMPLATES = {
    "polytonic", "greek", "eb1911 tfrac", "sfrac", "tfrac", "frac", "sub", "sup", "overline", "em",
    "gap", "ne", "uc", "brace2", "=", "...", "spaces", "nop", "hi", "fs", "lb-", "asc", "csc",
    "subsup", "su", "sqrt", "radic", "math", "mvar", "var", "ell", "prime", "√", "abs", "e", "dual line"}
#: Presentation wrappers: their syntax is structure, their contents text.
WRAPPERS = {"center", "c", "centre", "block center", "nowrap", "sc", "smaller", "larger", "x-larger",
            "small", "x-smaller", "fine block", "smaller block"}


def structure(text: str) -> list[str]:
    """Every piece of structure on a page, in order, as written: templates
    (a wrapper's syntax, not its contents; never a formula-making template),
    links, footnotes, tables, tags, the noinclude header.  A patched page's
    list must equal the fetched page's — nothing a person built is touched.
    A template's extent is measured with formulas blanked: TeX's `}}`
    (`\\frac{a}{b}}`) closes no template."""
    blank = re.sub(r"(<math[^>]*>)([\s\S]*?)(</math>)",
                   lambda m: m.group(1) + " " * len(m.group(2)) + m.group(3), text)
    out, i = [], 0
    while i < len(text):
        if text.startswith("<math", i):
            j = text.find("</math>", i)
            i = len(text) if j < 0 else j + 7
        elif text.startswith("<noinclude>", i):
            j = text.find("</noinclude>", i)
            j = len(text) if j < 0 else j + 12
            out.append(text[i:j])
            i = j
        elif text.startswith("<ref", i):
            j = text.find("</ref>", i)
            k = text.find("/>", i)
            j = (j + 6) if j >= 0 and (k < 0 or j < k or text[i:k].count(">") > 0) else (k + 2 if k >= 0 else len(text))
            out.append(text[i:j])
            i = j
        elif text.startswith("{{{", i):
            i += 1                             # a brace of the text before a template
        elif text.startswith("{{", i):
            j = balanced(blank, i, "{{", "}}")
            name = re.match(r"\{\{\s*([^|{}]*?)\s*(?:\||\}\})", text[i:j])
            name = name.group(1).lower() if name else ""
            bar = blank.find("|", i, j)
            if name in FORMULA_TEMPLATES:
                pass
            elif name in WRAPPERS and 0 < bar < j - 2:
                out.append(text[i:bar + 1])
                out.extend(structure(text[bar + 1:j - 2]))
                out.append("}}")
            else:
                out.append(text[i:j])
            i = j
        elif text.startswith("[[", i):
            j = balanced(text, i, "[[", "]]")
            out.append(text[i:j])
            i = j
        elif text.startswith("{|", i) and (i == 0 or text[i - 1] == "\n"):
            j = text.find("\n|}", i)
            j = len(text) if j < 0 else j + 3
            out.append(text[i:j])
            i = j
        elif text[i] == "<" and not _INLINE.match(text, i) and _TAG.match(text[i:i + 3]):
            j = text.find(">", i)
            j = len(text) if j < 0 else j + 1
            out.append(text[i:j])
            i = j
        else:
            i += 1
    return out


#: Wrappers that display a formula: a formula patch sets its own.
DISPLAY = {"center", "c", "centre", "block center", "nowrap", "x-larger", "larger"}
#: html a fetched page sets a formula with — giving way to ours, as an
#: unruled table laying out a formula does (the formula exception, 2026-10-12)
_FORMULA_TAGS = {"br", "span", "sub", "sup", "big", "small", "center", "i", "b", "div", "tr", "td", "th"}
_HTML = _FORMULA_TAGS | {"table", "ref", "references", "section", "noinclude", "includeonly", "poem", "p",
                         "math", "pre", "blockquote", "ol", "ul", "li", "dl", "dd", "dt", "font", "u", "s",
                         "tt", "code", "score", "chem", "ce", "hr", "gallery", "imagemap", "timeline"}
_RULED = re.compile(r"border|rules|class", re.I)


def _formula_piece(piece: str) -> bool:
    """A piece a formula is SET with, not one a person built around it."""
    if piece.startswith("<"):
        m = re.match(r"</?\s*([a-zA-Z][a-zA-Z0-9]*)", piece)
        if not m or m.group(1).lower() not in _HTML:
            return True                        # OCR's "<A *Bl": no tag
        name = m.group(1).lower()
        if name == "table":                    # an unruled table's open; any close
            return piece.startswith("</") or not _RULED.search(piece)
        return name in _FORMULA_TAGS
    if piece.startswith("{|"):                 # unruled, no image, no note
        return not _RULED.search(piece.split("\n", 1)[0]) \
            and not re.search(r"\[\[|Css image crop|raw image|<ref", piece, re.I)
    return False


def _built(pieces: list[str]) -> list[str]:
    """The pieces a person built: `structure` less what a formula is set
    with — a display wrapper's syntax (its own `}}` paired with it), the
    html and the unruled tables formulas are laid out in."""
    out, stack = [], []
    for x in pieces:
        if x == "}}":
            if not (stack and stack.pop()):
                out.append(x)
            continue
        m = re.match(r"\{\{\s*([^|{}]*?)\s*\|$", x)
        if m and m.group(1).lower() in WRAPPERS:
            stack.append(m.group(1).lower() in DISPLAY)
            if stack[-1]:
                continue
        elif _formula_piece(x):
            continue
        out.append(x)
    return out


def check(raw: str, patches: list[dict]) -> list[str]:
    """What is wrong with laying `patches` on `raw`: a patch not at its place,
    or structure a person built changed (`_built`: a formula's own display
    and layout give way to ours).  Empty: the page may be patched."""
    try:
        new = apply_patches(raw, patches)
    except PatchError as e:
        return [str(e)]
    a, b = _built(structure(raw)), _built(structure(new))
    if a != b:
        k = next((n for n, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        return [f"structure changed at piece {k}: {a[k][:50] if k < len(a) else '-'!r} -> "
                f"{b[k][:50] if k < len(b) else '-'!r}"]
    return []
