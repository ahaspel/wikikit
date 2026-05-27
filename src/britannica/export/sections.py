"""Detect section headings in an article body.

Mirrors the viewer's `detectSections` (tools/viewer/viewer.html). The
two implementations must produce the same set of slugs so that
deep-section URLs generated from the article JSON resolve to the
anchors the viewer emits at render time.

Detected signals:
  • «SEC:name«/SEC»  — pipeline-emitted absorbed-subsection marker.
                       Already a stable raw-source signal.
  • «SC»...«/SC»     — small-caps section heading. Two paragraph
                       shapes: standalone (whole paragraph is just SC)
                       and with-inline-body (heading followed by
                       paragraph prose after the closer).  Optional
                       Roman prefix may sit OUTSIDE «SC» or INSIDE.
  • «SH»...«/SH»     — inline shoulder heading (margin label, level 2).

Section ids are slug-derived (`section-<slug>`) with `-2`, `-3`, …
suffix on collisions, so adding/removing one section never renumbers
the others.

Margin-pointer SHs — those whose dehyphenated text matches the most
recent top-level heading — are decorative margin labels for their
containing section, not real subsections, and are omitted from the
returned list (the viewer assigns them ids but doesn't expose them in
the TOC; the article JSON sections list mirrors the TOC).
"""
from __future__ import annotations

import re

_SEC_RE = re.compile(r"^«SEC:([^«]+)«/SEC»$")
_SC_RE = re.compile(
    r"^(?:([IVXLCA-Z]+\.)[\s—]*)?"
    r"«SC»"
    r"(([IVXLCA-Z]+[.—]\s*)?.*?)"
    r"«/SC»"
    r"[.:]?—?(.*)$",
    re.DOTALL,
)
_SH_RE = re.compile(r"«SH»(.*?)«/SH»", re.DOTALL)
_FN_RE = re.compile(r"«FN(?:\[[^\]]+\])?:.*?«/FN»", re.DOTALL)
_ROMAN_PREFIX_RE = re.compile(r"^[IVXLCA-Z]+\.[\s—]*")
_EXCLUDED_RE = re.compile(
    r"^(?:(?:References|Authorities|Bibliography|See also)\b|Fig\.)",
    re.IGNORECASE,
)

# A standalone SC paragraph immediately followed by an extracted table
# marker is a table caption (LIGHTHOUSE "Table II.", UNITED STATES
# "West Central Massachusetts"), not a section heading.  Transcribers
# are inconsistent: some pages center the caption as `{{c|{{sc|Table}}
# I.—prose}}` (numeral outside SC, no detection issue); others as
# `{{c|{{sc|Table II.}}}}` (standalone SC, trips heading detection
# even though it's a caption).  The structural signal — caption
# immediately precedes its table — is reliable across phrasings, so we
# don't have to enumerate the title patterns ("Table N.", place names,
# etc.) the inconsistency can produce.
_TABLE_MARKER_PREFIX_RE = re.compile(
    r"^(?:\{\{TABLE(?:\[style:[^\]]*\])?:|«HTMLTABLE:)"
)
_DEHYPH_RE = re.compile(r"([a-z])-\s*([a-z])")


def _section_key(name: str) -> str:
    """Lowercase, alphanumerics only — collapses punctuation/spacing
    so 'Poly-morphism.' matches 'Polymorphism'.  Used for dedup."""
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())


from britannica.util.strings import section_slug as _section_slug


def _display_section_name(name: str) -> str:
    """Insert space at lowercase→uppercase transitions so source
    artifacts like 'BrewingTrade' render as 'Brewing Trade'."""
    return re.sub(r"([a-z])([A-Z])", r"\1 \2", name)


def _dehyphenate_shoulder(text: str) -> str:
    """Strip line-wrap hyphens between two lowercase letters: 'Poly-
    morphism.' → 'Polymorphism.'"""
    return _DEHYPH_RE.sub(r"\1\2", text)


def detect_sections(body: str) -> list[dict]:
    """Return the section list for `body`, in document order.

    Each entry:
      {"title": str, "slug": str, "id": str, "level": 1|2, "kind": "sec"|"sc"|"sh"}

    `id` is what the viewer emits as the `<h3>` / `<span>` `id` attribute
    so URLs of the form `…#{id}` resolve at render time.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\n+", body) if p.strip()]
    sections: list[dict] = []
    used_keys: set[str] = set()
    used_ids: set[str] = set()
    counter = 0
    active_key = ""

    def reserve_id(slug: str, fallback: str) -> str:
        nonlocal counter
        base = f"section-{slug}" if slug else fallback
        if base not in used_ids:
            used_ids.add(base)
            return base
        n = 2
        while f"{base}-{n}" in used_ids:
            n += 1
        cand = f"{base}-{n}"
        used_ids.add(cand)
        return cand

    for i, p in enumerate(paragraphs):
        next_p = paragraphs[i + 1] if i + 1 < len(paragraphs) else ""
        # Signal A — SEC marker. Whole paragraph is just the marker;
        # no SH possible inside, so skip the SH scan.
        m = _SEC_RE.match(p)
        if m:
            name = _display_section_name(m.group(1).strip())
            key = _section_key(name)
            if key in used_keys:
                continue
            slug = _section_slug(name)
            counter += 1
            sec_id = reserve_id(slug, f"section-{counter}")
            used_keys.add(key)
            sections.append({
                "title": name, "slug": slug, "id": sec_id,
                "level": 1, "kind": "sec",
            })
            active_key = key
            continue

        # Signal B — SC heading.  Detect first so any SH on the same
        # paragraph evaluates margin-pointer status against the new
        # heading's key, not the previous section's.
        if "{{IMG:" not in p and "{{TABLE" not in p:
            m = _SC_RE.match(p)
            if m:
                outer_roman = m.group(1)
                inner = m.group(2).strip()
                inner_roman = m.group(3)
                tail = (m.group(4) or "").strip()
                # For with-inline-body shape, require a Roman prefix
                # and limit SC inner content to <60 chars.  Without
                # those gates, an inline «SC» label embedded in prose
                # ("century «SC»A.D.«/SC» …") would render as a
                # heading.  Standalone shape (whole paragraph = SC) is
                # signal-strong enough on its own.
                has_numeral = bool(outer_roman) or bool(
                    inner_roman and inner_roman.strip()
                )
                passes_gate = (has_numeral and len(inner) < 60) if tail else True
                # Standalone SC followed by a table marker is a table
                # caption, not a section heading.
                is_caption = (
                    not tail
                    and _TABLE_MARKER_PREFIX_RE.match(next_p)
                )
                if passes_gate and not is_caption:
                    raw_title = f"{outer_roman}—{inner}" if outer_roman else inner
                    # Strip FN markers from the title we'll display +
                    # slug.  Without this, an SC heading wrapping a
                    # footnote — ARACHNIDA "Tabular Classification«FN:
                    # …Pocock…«/FN» of the Arachnida" — produces a
                    # 1000-char slug containing the entire footnote
                    # text and a TOC entry that's all noise.  The
                    # heading itself still renders the FN as a
                    # superscript footnote link via the inline-marker
                    # pipeline, since renderParagraph runs on `raw_title`
                    # (passed separately).
                    title = _FN_RE.sub("", raw_title).strip()
                    if not _EXCLUDED_RE.match(title):
                        name_only = _ROMAN_PREFIX_RE.sub(
                            "", _FN_RE.sub("", inner),
                        ).strip()
                        key = _section_key(name_only)
                        if key not in used_keys:
                            slug = _section_slug(title)
                            counter += 1
                            sec_id = reserve_id(slug, f"section-{counter}")
                            used_keys.add(key)
                            sections.append({
                                "title": title, "slug": slug, "id": sec_id,
                                "level": 1, "kind": "sc",
                                "raw_title": raw_title,
                            })
                            active_key = key

        # Signal C — SH markers (inline, level 2).  Margin pointers
        # (text matches active section's key) are decorative and
        # omitted from the returned list.
        for sh in _SH_RE.finditer(p):
            display = _dehyphenate_shoulder(sh.group(1).strip())
            is_pointer = bool(active_key) and _section_key(display) == active_key
            slug = _section_slug(display)
            counter += 1
            sec_id = reserve_id(slug, f"section-{counter}")
            if not is_pointer:
                sections.append({
                    "title": display, "slug": slug, "id": sec_id,
                    "level": 2, "kind": "sh",
                })

    return sections
