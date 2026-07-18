"""Link wraps — the `«LN:target|display»` family, folded into the peel/recurse/wrap mechanism.

Every link emits `«LN:target|display»`; they differ only in how the TARGET is parsed from the raw
(display-first vs target-first vs `#fragment` vs `Author:` vs the `1911 Enc…/` prefix strip). The
DISPLAY is the recursed slot: `_link_display` peels it (the PEEL side of the mechanism), the
classifier decomposes it to child nodes, and the wrap here parses the target from `raw` and wraps
the substituted `body`. So the seven old producers are seven `_PR_WRAP` rows on one shared peel —
no bespoke producer functions, no `_classify_link_composite`.
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


def _link_args(raw: str) -> str:
    """The inner args of a `{{…link…}}` template (delimiters peeled) — the wrap's
    target/positional parse reads this (`inner` carries the classified DISPLAY, not the args)."""
    return re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))


_LINK_LABELS = frozenset({
    "EB1911_ARTICLE_LINK", "TARGET_FIRST_LINK", "EB1911_SELFREF",
    "AUTHOR_LINK", "FRAGMENT_LINK", "INTRA_ARTICLE_LINK", "WIKILINK",
})


def _link_display(raw: str, label: str) -> str:
    """The DISPLAY slot for `label`'s link form — the one arg the mechanism recurses (the PEEL
    side).  Mirrors each wrap's own display parse so the classified display matches what the wrap
    emits: `[[…|Display]]` forms take the post-pipe slot; the `{{…}}` forms take the display-first
    (ARTICLE) or display-second (TARGET_FIRST / INTRA) positional."""
    if raw.startswith("[["):
        body = raw[2:-2] if raw.endswith("]]") else raw
        return body.partition("|")[2].strip()
    pos = [p.strip() for p in _split_top_pipes(_link_args(raw))]
    pos = [p for p in pos[1:] if "=" not in p and p]
    if not pos:
        return ""
    if label == "EB1911_ARTICLE_LINK":
        return pos[0]
    return pos[1] if len(pos) > 1 else pos[0]


def _strip_link_prefix(t: str) -> str:
    """Drop a single leading `word:` namespace/interwiki prefix (w:/Portal:/…); the
    colon must be followed by a non-space, so the section colon form `Europe: History`
    is NOT treated as a prefix.  Mirrors the resolver's normalizer strip."""
    i = t.find(":")
    if 0 < i < len(t) - 1 and " " not in t[:i] and not t[i + 1].isspace():
        return t[i + 1:].strip()
    return t


# ── The seven link wraps (rows in `_PR_WRAP`; `body` = the substituted display) ─────────────
def _wrap_article_link(raw, body, ctx):
    """`{{EB1911 article link|Display|Target}}` → «LN:Target|display».  Name + target from raw; a
    subpage target (`Article/Section`) with a roman-numeral display is a plain section label."""
    parts = [p.strip() for p in _split_top_pipes(_link_args(raw))]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if len(positional) >= 2:
        display, target = positional[0], positional[1]
    elif len(positional) == 1:
        display = target = positional[0]
    else:
        return ""
    disp = body.strip()
    if "/" in target:
        if re.match(r"^[IVXLC]+\.", display):
            return disp
        return f"«LN:{display}|{disp}«/LN»"
    return f"«LN:{target}|{disp}«/LN»"


def _wrap_target_first(raw, body, ctx):
    """`{{lkpl|Target|Display}}` / `{{1911link|…}}` / `{{EB1911 link|…}}` — target-first: target
    is the first positional (after the name)."""
    parts = [p.strip() for p in _split_top_pipes(_link_args(raw))]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if not positional:
        return ""
    return f"«LN:{positional[0]}|{body.strip()}«/LN»"


def _wrap_selfref(raw, body, ctx):
    """`[[1911 Encyclopædia Britannica/Article#Section|Display]]` → «LN:Article#Section|display».
    Strip the `1911 Encyclopædia Britannica/` prefix, KEEP the `#Section` fragment.  A bare ref
    to the work (no `/Article`) has no target → emit the display as prose."""
    b = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target_raw, _sep, display = b.partition("|")
    target_raw = target_raw.strip()
    disp = body.strip() if display.strip() else ""
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


def _wrap_author_link(raw, body, ctx):
    """`[[Author:Name|Display]]` — carried through the walk NEUTRALLY as
    `«AL:name|display»`.  The signature-vs-reference decision is DEFERRED to 6b4,
    where the finished roster resolves it ([[project_roster_from_author_links]]):
    a display that is a known contributor's initials becomes the bare-initials
    signoff, otherwise an `«LN»` xref.  The walk no longer needs a roster, and
    render + binding become one roster-driven decision.  Output uses `body`."""
    b = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target_raw, _s, _disp = b.partition("|")               # target from raw
    target = re.sub(r"^\s*Author:\s*", "", target_raw, flags=re.IGNORECASE).strip()
    disp_out = body.strip() or target
    return f"«AL:{target}|{disp_out}«/AL»"


def _wrap_fragment_link(raw, body, ctx):
    """`[[#Section]]` / `[[#Section|Display]]` → «LN:#Section|display» (display defaults to the
    section name)."""
    b = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target, _sep, display = b.partition("|")
    section = target.strip().lstrip("#").strip()
    disp = body.strip() if display.strip() else section
    if not section:
        return disp
    return f"«LN:#{section}|{disp}«/LN»"


def _wrap_intra_link(raw, body, ctx):
    """`{{EB1911 intra-article link|Section[|Display]}}` → «LN:#Section|display» — the template
    twin of `[[#Section]]`; the section is the first positional."""
    parts = [p.strip() for p in _split_top_pipes(_link_args(raw))]
    positional = [p for p in parts[1:] if "=" not in p and p]
    if not positional:
        return ""
    return f"«LN:#{positional[0]}|{body.strip()}«/LN»"


def _wrap_wikilink(raw, body, ctx):
    """`[[Target]]` / `[[Target|Display]]` → «LN:Target|display».  With no display, show the bare
    name (w:/Portal: prefix stripped as noise); the target keeps its prefix for the resolver."""
    b = raw[2:-2] if raw.startswith("[[") and raw.endswith("]]") else raw
    target, _sep, display = b.partition("|")
    target = target.strip()
    display = display.strip()
    disp = body.strip() if display else _strip_link_prefix(target)
    if not target:
        return disp
    return f"«LN:{target}|{disp}«/LN»"
