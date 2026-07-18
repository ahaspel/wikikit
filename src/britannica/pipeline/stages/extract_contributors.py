import re

# Matches the START of: {{EB1911 footer initials|Full Name|Initials|name2=…|initials2=…}}
#   or:                  {{EB1911 footer double initials|Name1|Init1|Name2|Init2}}
# The "double" variant uses four positional args instead of named
# parameters; _parse_contributors handles both shapes.
_FOOTER_START = re.compile(
    r"\{\{\s*EB1911\s+footer(?:\s+double)?\s+initials\s*\|", re.IGNORECASE)


def _iter_footers(text: str):
    """Yield the brace-balanced field content (``Name|Initials|…``) of every
    footer author-link.

    Balancing is load-bearing: the old ``([^}]+)`` capture stopped at the first
    ``}`` — which for a nested ``{{sc|Wi}}`` in the initials field lands INSIDE
    that template, truncating Pitcher's "C. {{sc|Wi}}." down to "C." and
    colliding him with Crewe.  Count braces and stop at the footer's OWN closing
    ``}}`` instead (same technique as the front-matter reader's _iter_entries)."""
    for mo in _FOOTER_START.finditer(text):
        depth, k, n = 1, mo.end(), len(text)
        start = mo.end()
        while k < n:
            two = text[k:k + 2]
            if two == "{{":
                depth += 1
                k += 2
            elif two == "}}":
                depth -= 1
                if depth == 0:
                    yield text[start:k]
                    break
                k += 2
            else:
                k += 1


def _first_footer(text: str):
    """The first footer's field content, or None — the single-match shape the
    element render producer wants."""
    return next(_iter_footers(text), None)

def _unwrap_templates(s: str) -> str:
    """Unwrap an inline presentation template to its DISPLAY text — the LAST
    positional arg: ``{{sc|Wi}}`` → ``Wi``, ``{{Fs|108%|K.}}`` → ``K.``.

    Taking the FIRST arg instead left the size string behind and, once the ``%``
    sentinel filter ran, dropped Kropotkin's size-wrapped ``K.`` — turning
    ``P. A. {{Fs|108%|K.}}`` into a bare ``K.`` instead of ``P. A. K.``.  The
    ``[^{}]`` classes only match a template with no nested braces, so callers that
    might see nesting strip any residue afterwards."""
    return re.sub(r"\{\{[^{}]*\|([^{}|]*)\}\}", r"\1", s)


def _clean_footer_initials(initials: str) -> list[str]:
    """Clean and split footer initials string.

    Handles compound entries (E. H. P.; X.), brackets, crosses,
    leaked markup, and HTML entities. Returns a list of clean
    initials strings (excluding anonymous X. markers).
    """
    s = initials.strip()
    # Strip brackets, parentheses, and crosses
    s = s.strip("[]()").lstrip("✠").strip()
    # Decode HTML entities
    s = s.replace("&thinsp;", "").replace("&nbsp;", " ")
    # Unwrap wiki templates (e.g. {{small-caps|He}} → He, {{Fs|108%|K.}} → K.)
    s = _unwrap_templates(s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\{\{[^{}]*", "", s)
    s = re.sub(r"\}\}", "", s)
    # Split on semicolons (compound entries like "E. H. P.; X.")
    parts = [p.strip() for p in s.split(";")]
    # Discard anonymous markers
    parts = [p for p in parts if p and p not in ("X.", "X")]
    # Source-typo fix: initials always end with a period in the DB,
    # but some article signatures are missing it (e.g. `A. H. J. G}}`
    # in AGRARIAN LAWS). Append the period when the string ends in a
    # letter — never when it ends in `.`, `*`, or other punct.
    parts = [p + "." if p and p[-1].isalpha() else p for p in parts]
    return parts


def _parse_contributors(template_content: str) -> list[dict[str, str]]:
    """Parse contributor names and initials from a footer template.

    Handles both the single-author form (positional name+init, with
    optional name2=/initials2=... named-parameter second author) and
    the `footer double initials` variant which uses four positional
    arguments (name1|init1|name2|init2).
    """
    results = []
    # Unwrap inner templates / wikilinks (e.g. {{sc|Wi}} -> Wi, [[A:X|Y]] -> Y)
    # BEFORE the positional split: their internal `|` otherwise splits a field
    # mid-token, so "C. {{sc|Wi}}." would parse as a bare "C." and collide
    # Pitcher with Crewe.  The content is brace-balanced upstream (_iter_footers),
    # so the whole template survives intact to be unwrapped here.
    template_content = _unwrap_templates(template_content)
    template_content = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", template_content)
    parts = template_content.split("|")

    # Positional args (skip font-size sentinels like "108%")
    positional = [p.strip() for p in parts if "=" not in p and "%" not in p]

    # First contributor: first two positional args.
    if len(positional) >= 2:
        for clean_init in _clean_footer_initials(positional[1]):
            results.append({
                "full_name": positional[0],
                "initials": clean_init,
            })

    # Second contributor: the `footer double initials` template uses
    # positional args 3 and 4 for (name2, init2). Detect by looking
    # for a name-shaped third arg (has a space / capital letters).
    if len(positional) >= 4:
        for clean_init in _clean_footer_initials(positional[3]):
            results.append({
                "full_name": positional[2],
                "initials": clean_init,
            })

    # Additional contributors via named params: name2=…|initials2=…
    named = {}
    for part in parts:
        if "=" in part:
            key, _, value = part.partition("=")
            named[key.strip()] = value.strip()

    for n in range(2, 10):
        name_key = f"name{n}"
        init_key = f"initials{n}"
        if name_key in named and init_key in named:
            for clean_init in _clean_footer_initials(named[init_key]):
                results.append({
                    "full_name": named[name_key],
                    "initials": clean_init,
                })

    return results


def _normalize_initials(initials: str) -> str:
    """Canonicalize a contributor signature so equivalent forms compare
    equal.

    Used both at storage (`build_contributor_table.py` cleans front-
    matter `initials` fields before grouping) and at lookup
    (`_find_contributor` matches body-footer signatures against
    ContributorInitials rows).  Anything that varies between print
    convention, wikitext markup, and OCR transcription gets folded out
    here so the same signature is stored and looked up under one key.

    Folds applied:
      - HTML/template markup stripped (`<small>`, `{{unicode|✠}}`, &c.)
      - HTML-entity spaces decoded (`&thinsp;`, `&nbsp;`, …)
      - Stray prefix sigils stripped (`✠ J. G.` → `J. G.`,
        `✝ J. C. H.` → `J. C. H.`)
      - Asterisk placement normalized to `.*` (e.g. `O*.` → `O.*`)
      - Repeat-punctuation collapsed (`**` → `*`, `..` → `.`)
      - Inter-letter spacing normalized (`A.N.` → `A. N.`)
      - Trailing period appended when the value ends in a letter
        (rescues `T. F. T` → `T. F. T.`; mirrors the same fix
        `_clean_footer_initials` already applies on the footer side).
    """
    import re
    # Strip leaked wiki/HTML markup.  Unwrap `{{X|Y}}` template forms
    # to their inner text first (so `{{unicode|✠}}` → `✠`), then drop
    # any remaining template residue and HTML tags.
    s = _unwrap_templates(initials)
    s = re.sub(r"\{\{[^{}]*", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\}\}", "", s)
    # Decode HTML-entity spaces used in some wikitext entries
    # (notably vol 6's `J.&thinsp;D.&thinsp;v.&thinsp;d.&thinsp;W.`
    # for Van Der Waals — the entities pushed the raw initials field
    # past the build script's 20-char filter).
    s = (s.replace("&thinsp;", " ")
           .replace("&nbsp;", " ")
           .replace("&emsp;", " ")
           .replace("&ensp;", " "))
    # Strip stray leading sigils used as deceased / honorific markers
    # in EB1911 print (cross, dagger, asterisk-as-marker).  These are
    # decorative at the contributor-table level but never appear in
    # body-footer signatures, so they must come off for matches to
    # work.
    s = re.sub(r"^[✠✝†*\s]+", "", s)
    # Normalize asterisk placement: "O*.", "O. *", "O.*" → "O.*"
    s = re.sub(r"\*\s*\.", ".*", s)
    s = re.sub(r"\.\s*\*", ".*", s)
    # Deduplicate repeated punctuation
    s = re.sub(r"\*+", "*", s)
    s = re.sub(r"\.+", ".", s)
    # Normalize spacing: "A.N." → "A. N.", but keep ".*" together
    s = re.sub(r"\.([A-Za-z])", r". \1", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    # Append trailing period if value ends in a letter — contributor
    # signatures always end with a period in the canonical form.
    if s and s[-1].isalpha():
        s += "."
    # Fold OCR all-caps variants of multi-letter initial tokens.
    # ``W. AY.`` and ``W. Ay.`` are the same person ("Wilfrid Airy")
    # but get stored as different ContributorInitials rows — and so
    # different Contributor records — without this fold.  Only
    # multi-letter alphabetic tokens with a trailing period are
    # title-cased; single-letter tokens (``W.`` vs ``w.``) keep
    # their case to avoid altering well-formed initials.
    parts = []
    for tok in s.split(" "):
        if (len(tok) >= 3
                and tok[-1] == "."
                and tok[:-1].isalpha()
                and tok[:-1].isupper()):
            parts.append(tok[0] + tok[1:-1].lower() + ".")
        else:
            parts.append(tok)
    s = " ".join(parts)
    return s


_SIGNATURE_RE = re.compile(r"\(([^()]{1,80})\)")
_SIG_MARKER_RE = re.compile(r"«/?[A-Za-z]+(?:\[[^\]]*\])?»")


def _harvest_signature_contributors(
    body: str, initials_map: dict[str, int]
) -> list[int]:
    """Ordered, de-duplicated contributor_ids from the rendered signoffs in a
    walked article body.

    The footer producer renders ``{{EB1911 footer …}}`` to ``(initials)`` and
    the Author-link producer renders a contributor signature to its initials, so
    footers, Author signoffs, and bare parentheticals all reduce to one shape: a
    ``(…)`` whose marker-stripped, normalized content is a known contributor's
    initials.  The index is the discriminator for MULTI-initial signatures
    (``(E. V.)``) — prose parentheticals (dates, ``op. cit.``) and reference «LN»
    name-displays never match.

    A SINGLE initial is explicitly NOT bound, even when it is in the index: a
    one-letter parenthetical ``(M.)``/``(B.)`` collides with figure-key labels
    (ABBEY's plan is ``A. Gateway … M. Tower``), cross-references, and abbreviations
    everywhere, so binding it attributes whole articles to whatever contributor
    happens to sign with that one letter (ABBEY → 11 bogus "authors").  Single-
    initial contributors are recovered from the front-matter / vol-29 attribution
    passes instead, where the article binding is explicit, not inferred."""
    found: list[int] = []
    seen: set[int] = set()
    for sig in _SIGNATURE_RE.finditer(body):
        for part in sig.group(1).split(";"):
            stripped = _SIG_MARKER_RE.sub("", part).strip()
            # Two structural gates, because a false attribution is far worse than
            # a miss (misses are recovered from the explicit front-matter passes):
            #   (1) the producer renders a signature as SPACED initials ("A. D.",
            #       "E. He."); a run-together form ("A.D.", "q.v.") is a source
            #       date / abbreviation, never a signoff — and this also drops the
            #       lone single initial ("M."), which has no space and collides
            #       with figure-key labels everywhere (ABBEY → 11 bogus authors);
            #   (2) a contributor's first initial is ALWAYS capitalized, so a
            #       lower-case lead ("q. v.", "l. c.") is not a signoff.
            if " " not in stripped:
                continue
            norm = _normalize_initials(stripped)
            if not norm[:1].isupper():
                continue
            cid = initials_map.get(norm)
            if cid is not None and cid not in seen:
                seen.add(cid)
                found.append(cid)
    return found


