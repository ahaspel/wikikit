"""Figure anchors and the prose references that land on them — post-walk.

A figure is an image leaf followed by its caption ([[feedback_no_caption_concept]]):
`{{IMG:…}}«BR»«CTR»«SC»Fig. 1«/SC».«/CTR»`, or the caption a table row below
(`«/TD»«/TR»«TR»«TD»«SC»Fig.«/SC» 3.—Diagram of …`), or after a credit line, or
inside the image template's own caption field.  No producer sees the image AND
its caption — they are siblings, often in different cells — so this runs over
the walked body beside `stamp_section_anchors`, the post-walk family where the
article's whole structure is in view.

Two stamps, in order:

``stamp_figure_anchors`` puts ``«ANCHOR:fig-N|Fig. N»`` before each image whose
caption prints a figure number.  Captions pair with images IN ORDER: two images
side by side are followed by "Fig. 9. Fig. 10." in the row below (MAP), so the
k-th caption after a run of images is the k-th image's.  A paragraph break
outside a table ends the run — a sentence opening "Fig. 3 shows …" claims no
image.  A number printed twice (MAP restarts its numbering for Map Projections;
PALAEOBOTANY for its Mesozoic flora) names its later anchors "Fig. N second",
"third" …, so every anchor's NAME is unique and a link matches exactly one
(`match_section` abstains on a tie).

``stamp_figure_refs`` links the text's "fig. 4" / "figs. 2 and 3" / "figs. 11,
65" / "figs. 2-4" to those anchors, one link per number, to the NEAREST figure
of that number.  Only a number some figure of THIS article prints is linked, and
never one that names a plate ("Plate II. fig. 3") or another work ("Gymnoblastic
Hydroids, fig. 3"; "op. cit. p. 40, fig. 2").  Each link is a same-article
`«LN:#Fig. N|…»`, bound by the export like every `#section` link and gated by
the section-link check (7.7).  2026-11-06: 4,345 captioned figures had 10
anchors; ~6,000 references pointed at them, a median 300 words away.
"""
from __future__ import annotations

import re

from wikikit.export.sections import section_key
from wikikit.markers import IMG_PARTS_RE, strip_marker_tokens
from wikikit.pipeline.stages.elements._anchor import anchor_marker
from wikikit.pipeline.stages.elements._link import ln_marker
from wikikit.util.strings import anchor_slug

# The image marker's own grammar: filename, meta block, caption field.
_IMG = IMG_PARTS_RE
# A caption's figure number: "Fig. 3", "Fig 3", "Figure 3", "Fig. 2 a" / "2b".
_CAPTION_NUM = re.compile(r"\bFig(?:ure)?\s?\.?\s*(\d{1,3})(?:\s?([a-z])\b)?")
_PARA = re.compile(r"«P»")
# The figure number a Wikisource transcriber filed an EB1911 image under
# ("EB1911 Breakwater - Fig. 2 — Table Bay Breakwater.jpg").  Borrowed files
# ("Cambridge Natural History Mammalia Fig 077.png") number another book's.
_FILE_NUM = re.compile(r"(?i)\bfig(?:ure)?[\s_.%-]*(\d{1,3})(?!\d)")
_TABLE_OPEN, _TABLE_CLOSE = "«TABLE[", "«/TABLE»"
_ORDINALS = ("second", "third", "fourth", "fifth", "sixth")

# A prose reference.  The numbers run "3", "3, 4", "3 and 4", "3-5", "2 a".
_REF = re.compile(
    r"\b(?P<cue>figs?\.)\s?(?P<nums>\d{1,3}(?:\s?[a-z]\b)?"
    r"(?:\s*(?:,|and|&|-|–|to)\s*\d{1,3}(?:\s?[a-z]\b)?)*)")
_ONE_NUM = re.compile(r"(\d{1,3})(?:\s?([a-z])\b)?")
# Spans a reference never links inside: an image marker (its caption field), a
# link, a marker token's own fields.
_PROTECT = re.compile(_IMG.pattern + r"|«LN[^»]*?:.*?«/LN»|«[A-Z_]+:[^»]*»", re.S)
# What, just before a reference, makes it someone else's figure.
_PLATE_BEFORE = re.compile(r"(?:\b[Pp]l(?:ate)?s?\.?\s+[IVXLCivxlc\d]+\.?,?\s*)$")
# …or after it: "(fig. 120, Plate XXV.)" — SHIPBUILDING.
_PLATE_AFTER = re.compile(r"[,\s]*(?:on\s+)?[Pp]l(?:ate)?s?\.?\s+[IVXLCivxlc\d]")
_CITED_BEFORE = re.compile(
    r"(?:«/I»[^.;()«]{0,12}|\bop\.\s?cit\.[^;()«]{0,20}|\bp{1,2}\.\s?\d+[^;()«]{0,6}"
    r"|\bvol\.\s?[ivxlc\d]+[^;()«]{0,20})[,\s]*$")


def _caption_number(text: str):
    """(number, letter) of the first figure number in caption text, else None."""
    m = _CAPTION_NUM.search(text)
    return (int(m.group(1)), m.group(2) or "") if m else None


def _figures(body: str):
    """[(img_start, number, letter)] — each image paired with its caption, in order."""
    tokens = []                                   # (pos, kind, payload)
    for m in _IMG.finditer(body):
        tokens.append((m.start(), "img", m))
    for m in _PARA.finditer(body):
        tokens.append((m.start(), "p", None))
    tokens.sort(key=lambda t: t[0])
    depth_at = _table_depth(body)
    out, pending = [], []
    for i, (pos, kind, m) in enumerate(tokens):
        if kind == "p":
            if depth_at(pos) == 0:
                pending.clear()
            continue
        # the image's own caption field
        own = _caption_number(strip_marker_tokens(m.group(3), "")) if m.group(3) else None
        if own:
            out.append((pos, *own))
            continue
        fname = m.group(1)
        fm = _FILE_NUM.search(fname.replace("%20", " ")) if "1911" in fname else None
        pending.append((pos, int(fm.group(1)) if fm else None))
        # the text after this image, up to the next image or paragraph break
        nxt = tokens[i + 1][0] if i + 1 < len(tokens) else len(body)
        stretch = body[m.end():min(nxt, m.end() + 1200)]
        text = strip_marker_tokens(stretch, " ")
        if i + 1 < len(tokens) and tokens[i + 1][1] == "img" and not _caption_number(text):
            continue                              # side by side: captions follow the run
        out += _pair_run(pending, [(int(c.group(1)), c.group(2) or "")
                                   for c in _CAPTION_NUM.finditer(text)])
        pending.clear()
    return sorted(out)


def _pair_run(images, captions):
    """A run of images and the captions printed after it → [(img_start, n, letter)].

    In order, except where the image's own FILE names its number: a run's
    captions need not follow its images' order (CERAMICS, PLATE IV prints 67,
    66, 68 under images filed 66, 67, 68), and one image's caption may be
    missing, which would shift every later pairing by one.  A file whose number
    matches no caption in the run is not anchored — the file and the page
    disagree (BREAKWATER's Table Bay figure is filed "Fig. 2", printed "Fig.
    1"; CHEMISTRY's "Fig. 9" file sits over a "Fig. 10" caption) and nothing
    here says which is right.  Images with no number in their file name take
    the remaining captions in order."""
    out, left = [], list(captions)
    for pos, fnum in images:
        if fnum is None:
            continue
        hit = next((c for c in left if c[0] == fnum), None)
        if hit:
            out.append((pos, *hit))
            left.remove(hit)
    for pos, fnum in images:
        if fnum is None and left:
            out.append((pos, *left.pop(0)))
    return out


def _table_depth(body: str):
    opens = [m.start() for m in re.finditer(re.escape(_TABLE_OPEN), body)]
    closes = [m.start() for m in re.finditer(re.escape(_TABLE_CLOSE), body)]

    def depth(p: int) -> int:
        return sum(1 for o in opens if o < p) - sum(1 for c in closes if c < p)
    return depth


def _figure_name(num: int, letter: str, nth: int) -> str:
    base = f"Fig. {num}{letter}"
    return base if nth == 0 else f"{base} {_ORDINALS[min(nth, len(_ORDINALS)) - 1]}"


def figure_anchors(body: str):
    """[(img_start, number, letter, name)] — the anchors `stamp_figure_anchors`
    places, without placing them (the reference pass and the census read this)."""
    seen: dict = {}
    out = []
    # the source's own anchors, by the key `match_section` compares: its
    # `{{anchor|fig87}}` and our "Fig. 87" are one name, and two anchors of
    # one name tie and abstain (CRYSTALLOGRAPHY, 3 links)
    existing = {section_key(n) for _p, n in _source_anchors(body)}
    for pos, num, letter in _figures(body):
        key = (num, letter)
        nth = seen.get(key, 0)
        seen[key] = nth + 1
        name = _figure_name(num, letter, nth)
        out.append((pos, num, letter, name, section_key(name) in existing))
    return out


_ANCHOR_ANY = re.compile(r"«ANCHOR:[^|»]*\|([^»]*)»")
# A figure anchor's KEY: "fig87", "fig3", "figure3", "fig2b", "fig2second".
_FIG_KEY = re.compile(r"^fig(?:ure)?(\d{1,3})([a-z]?)(" + "|".join(_ORDINALS) + r")?$")


def _source_anchors(body: str):
    return [(m.start(), m.group(1)) for m in _ANCHOR_ANY.finditer(body)]


def stamp_figure_anchors(body: str) -> str:
    """``«ANCHOR:fig-N|Fig. N»`` before each captioned image (none where the
    source's own ``{{anchor|Fig. N}}`` already names it)."""
    for pos, _num, _letter, name, has in reversed(figure_anchors(body)):
        if not has:
            body = body[:pos] + anchor_marker(anchor_slug(name), name) + body[pos:]
    return body




def _ref_numbers(nums: str):
    """[(start, end, number, letter)] within the numbers run; a range links
    its two ends."""
    return [(m.start(), m.end(), int(m.group(1)), m.group(2) or "")
            for m in _ONE_NUM.finditer(nums)]


def stamp_figure_refs(body: str, stats: dict | None = None) -> str:
    """Link "fig. N" / "figs. N and M" to the nearest anchor of that number."""
    anchors: dict = {}                            # (num, letter) -> [(pos, name)]
    for pos, name in _source_anchors(body):
        k = _FIG_KEY.match(section_key(strip_marker_tokens(name, "")))
        if k:
            anchors.setdefault((int(k.group(1)), k.group(2)), []).append((pos, name))
    if not anchors:
        return body
    protected = [(m.start(), m.end()) for m in _PROTECT.finditer(body)]

    def shielded(p: int) -> bool:
        return any(a <= p < b for a, b in protected)

    def tally(k):
        if stats is not None:
            stats[k] = stats.get(k, 0) + 1

    edits = []
    for m in _REF.finditer(body):
        if shielded(m.start()):
            continue
        before = strip_marker_tokens(body[max(0, m.start() - 60):m.start()], "")
        before_raw = body[max(0, m.start() - 60):m.start()]
        after = strip_marker_tokens(body[m.end():m.end() + 30], "")
        if _PLATE_BEFORE.search(before) or _PLATE_AFTER.match(after):
            tally("skipped: plate")
            continue
        if _CITED_BEFORE.search(before_raw) or _CITED_BEFORE.search(before):
            tally("skipped: cited work")
            continue
        nums_at = m.start("nums")
        parts = []
        numbers = _ref_numbers(m.group("nums"))
        # Only "figs." lists figures.  After "fig." a second number is a part
        # OF the figure — ECHINODERMA's "(fig. 19, 2)" is item 2 of fig. 19.
        if m.group("cue") == "fig.":
            numbers = numbers[:1]
        for s, e, num, letter in numbers:
            # "fig. 2 a" is sub-figure 2a; "fig. 2 i" a label INSIDE fig. 2;
            # a bare "fig. 2" with only 2a/2b printed means the first.
            cands = (anchors.get((num, letter)) or anchors.get((num, ""))
                     or anchors.get((num, "a")))
            if not cands:
                tally("skipped: no such figure")
                continue
            at = nums_at + s
            name = min(cands, key=lambda c: abs(c[0] - at))[1]
            parts.append((at, nums_at + e, name))
            tally("linked")
        if not parts:
            continue
        if len(numbers) == 1:                     # "fig. 3" whole, cue and number
            end = parts[0][1]
            edits.append((m.start(), end, parts[0][2], body[m.start():end]))
        else:
            for a, b, name in parts:
                edits.append((a, b, name, body[a:b]))
    for a, b, name, shown in reversed(edits):
        body = body[:a] + ln_marker("#" + name, shown) + body[b:]
    return body
