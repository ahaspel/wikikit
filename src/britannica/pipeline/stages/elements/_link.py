"""Link producers — recognized walker elements whose DISPLAY recurses.

The body-text link handlers flat-read their display and pre-unwrapped `{{sc|…}}`,
dropping the small-caps, because a `([^{}]*)` regex can't bound nested braces.  Here the
construct is bounded by the ONE balanced matcher (`_construct_end`, driven by an
opener-only recognizer — see `_walker._walk_balanced_shapes`), and the producer simply
recurses its display slot through `tt` (=`_apply_markup`).  No brace-balancer, no
re-parse of the raw: the walker hands us the bounded `inner`, we split slots and recurse.
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


def process_eb1911_article_link(inner: str, tt) -> str:
    """`{{EB1911 article link|Display|Target}}` → `«LN:Target|»{recursed display}«/LN»`.

    `inner` is the already-bounded template body the walker handed us; slot 0 is the
    template name.  Display first, target second.  The display slot is RECURSED (`tt`)
    so a nested `{{sc|…}}` is carried as «SC», not stripped.  A subpage target
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
    disp = tt(display)
    if "/" in target:
        if re.match(r"^[IVXLC]+\.", display):
            return disp
        return f"«LN:{display}|{disp}«/LN»"
    return f"«LN:{target}|{disp}«/LN»"


def process_target_first_link(inner: str, tt) -> str:
    """`{{EB1911/DNB lkpl|Target|Display}}` / `{{1911link|Target|Display}}` /
    `{{EB1911 link|…}}` — the TARGET-first cross-reference convention (vs the article
    link's display-first).  Slot 0 is the template name; target is the first positional,
    display the second (falls back to the target).  Display RECURSED through `tt`, same
    «LN» marker — no flat pre-unwrap, no re-parse of the raw."""
    parts = [p.strip() for p in _split_top_pipes(inner)]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if not positional:
        return ""
    target = positional[0]
    display = positional[1] if len(positional) > 1 else target
    return f"«LN:{target}|{tt(display)}«/LN»"
