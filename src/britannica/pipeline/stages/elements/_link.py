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


def process_eb1911_article_link(inner: str) -> str:
    """`{{EB1911 article link|Display|Target}}` → `«LN:Target|»{recursed display}«/LN»`.

    `inner` is the already-bounded template body the walker handed us; slot 0 is the
    template name.  Display first, target second.  A nested `{{sc|…}}` in the
    display is walker-extracted + substituted as «SC», not stripped.  A subpage target
    (`Article/Section`) with a roman-numeral display is a plain section label, else a
    self-link."""
    parts = [p.strip() for p in _split_top_pipes(inner)]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if len(positional) >= 2:
        display, target = positional[0], positional[1]
    elif len(positional) == 1:
        display = target = positional[0]
    else:
        return ""
    disp = display
    if "/" in target:
        if re.match(r"^[IVXLC]+\.", display):
            return disp
        return f"«LN:{display}|{disp}«/LN»"
    return f"«LN:{target}|{disp}«/LN»"


def process_target_first_link(inner: str) -> str:
    """`{{EB1911/DNB lkpl|Target|Display}}` / `{{1911link|Target|Display}}` /
    `{{EB1911 link|…}}` — the TARGET-first cross-reference convention (vs the article
    link's display-first).  Slot 0 is the template name; target is the first positional,
    display the second (falls back to the target).  Display's nested elements are
    walker-extracted, same «LN» marker — no flat pre-unwrap, no re-parse of the raw."""
    parts = [p.strip() for p in _split_top_pipes(inner)]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if not positional:
        return ""
    target = positional[0]
    display = positional[1] if len(positional) > 1 else target
    return f"«LN:{target}|{display}«/LN»"


def process_eb1911_selfref_link(inner: str) -> str:
    """`[[1911 Encyclopædia Britannica/Article#Section|Display]]` — an internal EB1911
    cross-reference in raw bracket form (the `{{EB1911 article link}}` template's twin).
    Emit «LN:Article|Display»: strip the `1911 Encyclopædia Britannica/` prefix and the
    `#Section` anchor (the export resolves «LN» targets by article name; there is no
    anchor resolution yet).  A bare `[[1911 Encyclopædia Britannica|Disp]]` (the work as
    a whole, no article) has no target → emit the display as plain prose."""
    target_raw, _sep, display = inner.partition("|")
    target_raw = target_raw.strip()
    display = display.strip()
    rest = re.sub(r"^1911\s+[Ee]ncyclop[^/]*/", "", target_raw, flags=re.IGNORECASE)
    if rest == target_raw:                      # no `/Article` — a ref to the work itself
        return display or target_raw
    article = rest.split("#", 1)[0].strip()
    display = display or article
    if not article:
        return display
    return f"«LN:{article}|{display}«/LN»"


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
    body = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    _t, _s, disp_raw = body.partition("|")                 # literal display
    target_raw, _s2, disp_out = inner.partition("|")        # recursed display
    target = re.sub(r"^\s*Author:\s*", "", target_raw, flags=re.IGNORECASE).strip()
    disp_out = disp_out.strip() or target
    key = _normalize_initials(disp_raw.strip("() "))
    if key and key in ctx.contributor_initials:
        return disp_out                                    # contributor → initials
    return f"«LN:{target}|{disp_out}«/LN»"                  # reference → xref
