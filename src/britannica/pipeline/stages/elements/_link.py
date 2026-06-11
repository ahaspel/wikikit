"""Link producers — recognized walker elements whose DISPLAY recurses.

The body-text link handlers flat-read their display and pre-unwrapped `{{sc|…}}`,
dropping the small-caps, because a `([^{}]*)` regex can't bound nested braces.  Here the
construct is bounded by the ONE balanced matcher (`_construct_end`, driven by an
opener-only recognizer — see `_walker._walk_balanced_shapes`), and the producer simply
splits its display slots and emits «LN» — a nested `{{sc|…}}` rides through as a
walker-extracted placeholder that `produce_tree` substitutes.  No re-parse of the raw:
the walker hands us the bounded `inner`.
"""

from __future__ import annotations

import re


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


def process_eb1911_article_link(inner: str, context) -> str:
    """`{{EB1911 article link|Display|Target}}` → `«LN:Target|»{recursed display}«/LN»`.

    Producer = transform the OUTER (template name + target → the `«LN:…«/LN»`
    scaffold), then RETURN the INNER (the display) through the loop by calling
    ``process_elements`` on it.  The args (`name|target|`) are outer, consumed
    here; the display is inner, so a `{{sc|…}}` or footnote in it is recursed by
    the same loop.  A subpage target (`Article/Section`) with a roman-numeral
    display is a plain section label, else a self-link."""
    from britannica.pipeline.stages.elements import process_elements
    parts = [p.strip() for p in _split_top_pipes(inner)]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if len(positional) >= 2:
        display, target = positional[0], positional[1]
    elif len(positional) == 1:
        display = target = positional[0]
    else:
        return ""
    disp = process_elements(display, context, _allow_figure=False).strip()
    if "/" in target:
        if re.match(r"^[IVXLC]+\.", display):
            return disp
        return f"«LN:{display}|{disp}«/LN»"
    return f"«LN:{target}|{disp}«/LN»"


def process_target_first_link(inner: str, context) -> str:
    """`{{EB1911/DNB lkpl|Target|Display}}` / `{{1911link|Target|Display}}` /
    `{{EB1911 link|…}}` — the TARGET-first cross-reference convention (vs the article
    link's display-first).  Slot 0 is the template name; target is the first positional,
    display the second (falls back to the target).  Transform the outer (target →
    `«LN:…`), return the inner (display) through the loop via ``process_elements`` so a
    nested `{{sc|…}}` is carried as «SC»."""
    from britannica.pipeline.stages.elements import process_elements
    parts = [p.strip() for p in _split_top_pipes(inner)]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if not positional:
        return ""
    target = positional[0]
    display = positional[1] if len(positional) > 1 else target
    disp = process_elements(display, context, _allow_figure=False).strip()
    return f"«LN:{target}|{disp}«/LN»"


def process_eb1911_selfref_link(raw: str, context) -> str:
    """`[[1911 Encyclopædia Britannica/Article#Section|Display]]` — an internal EB1911
    cross-reference in raw bracket form (the `{{EB1911 article link}}` template's twin).
    Emit «LN:Article#Section|Display»: strip the `1911 Encyclopædia Britannica/` prefix
    and KEEP the `#Section` fragment (resolve_one splits it → article + section, so the
    link lands on that subsection's «ANCHOR»).  A bare `[[1911 Encyclopædia Britannica|
    Disp]]` (the work as a whole, no article) has no target → emit the display as prose."""
    from britannica.pipeline.stages.elements import process_elements
    body = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target_raw, _sep, display = body.partition("|")
    target_raw = target_raw.strip()
    disp = process_elements(display.strip(), context, _allow_figure=False).strip() if display.strip() else ""
    rest = re.sub(r"^1911\s+[Ee]ncyclop[^/]*/", "", target_raw, flags=re.IGNORECASE)
    if rest == target_raw:                      # no `/Article` — a ref to the work itself
        return disp or target_raw
    article, _hash, fragment = rest.partition("#")
    article = article.strip()
    fragment = fragment.strip()
    disp = disp or article
    if not article:
        return disp
    target = f"{article}#{fragment}" if fragment else article
    return f"«LN:{target}|{disp}«/LN»"


def process_author_link(raw: str, inner: str, ctx) -> str:
    """`[[Author:Name|Display]]` — route on the display, element-alone.

    A contributor signature (the display's initials are a known contributor,
    per ``ctx.contributor_initials``) → render just the initials; everything
    else → `«LN:Name|Display»`, an xref to the *referenced* author.

    The index decision reads the RAW display so a `{{sc|…}}` shell or `(…)`
    parens fold to bare initials via `_normalize_initials`; the rendered output
    uses the recursed `inner`, so a contributor's small-caps survive.  No
    surrounding context is consulted — recursion already delivered a lone link.
    """
    from britannica.pipeline.stages.extract_contributors import _normalize_initials
    from britannica.pipeline.stages.elements import process_elements
    body = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target_raw, _s, disp_raw = body.partition("|")          # parse the outer from raw
    target = re.sub(r"^\s*Author:\s*", "", target_raw, flags=re.IGNORECASE).strip()
    # Return the inner (the display) through the loop; consume the rest as outer.
    disp_out = process_elements(disp_raw.strip(), ctx, _allow_figure=False).strip() or target
    key = _normalize_initials(disp_raw.strip("() "))
    if key and key in ctx.contributor_initials:
        return disp_out                                    # contributor → initials
    return f"«LN:{target}|{disp_out}«/LN»"                  # reference → xref


def process_fragment_link(raw: str, context) -> str:
    """`[[#Section]]` / `[[#Section|Display]]` — a bare same-article anchor link.

    Transform the outer (parse `#Section` from `raw` → `«LN:#Section`) and return the
    inner (display) through the loop.  The leading-`#` target the resolver reads as
    "this article, section Section"; display defaults to the section name."""
    from britannica.pipeline.stages.elements import process_elements
    body = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target, _sep, display = body.partition("|")
    section = target.strip().lstrip("#").strip()
    disp = (process_elements(display.strip(), context, _allow_figure=False).strip()
            if display.strip() else section)
    if not section:
        return disp
    return f"«LN:#{section}|{disp}«/LN»"


def process_intra_article_link(inner: str, context) -> str:
    """`{{EB1911 intra-article link|Section}}` / `{{…|Section|Display}}` — the template
    twin of the bare `[[#Section]]` anchor.  Slot 0 is the template name; the section is
    the first positional, display the second (falls back to the section).  Transform the
    outer (section → `«LN:#…`), return the inner (display) through the loop."""
    from britannica.pipeline.stages.elements import process_elements
    parts = [p.strip() for p in _split_top_pipes(inner)]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if not positional:
        return ""
    section = positional[0]
    display = positional[1] if len(positional) > 1 else section
    disp = process_elements(display, context, _allow_figure=False).strip()
    return f"«LN:#{section}|{disp}«/LN»"


def _strip_link_prefix(t: str) -> str:
    """Drop a single leading `word:` namespace/interwiki prefix (w:/Portal:/…); the
    colon must be followed by a non-space, so the section colon form `Europe: History`
    is NOT treated as a prefix.  Mirrors the resolver's normalizer strip."""
    i = t.find(":")
    if 0 < i < len(t) - 1 and " " not in t[:i] and not t[i + 1].isspace():
        return t[i + 1:].strip()
    return t


def process_wikilink(raw: str, context) -> str:
    """`[[Target]]` / `[[Target|Display]]` — a generic wiki cross-reference (anything
    not claimed by the File / Author / SELFREF / `#` recognizers).  Transform the outer
    (parse `Target` from `raw`, consuming it into the `«LN:…` scaffold) and return the
    inner (the display) through the loop via ``process_elements`` so a styler/footnote
    inside the link text is recursed, not swept.  The target's prefix is preserved (the
    resolver needs it); with no display, show the bare name (w:/Portal: prefix is noise)."""
    from britannica.pipeline.stages.elements import process_elements
    body = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target, _sep, display = body.partition("|")
    target = target.strip()
    display = display.strip()
    disp = (process_elements(display, context, _allow_figure=False).strip()
            if display else _strip_link_prefix(target))
    if not target:
        return disp
    return f"«LN:{target}|{disp}«/LN»"
