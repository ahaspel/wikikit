import re

from britannica.db.models import (
    Article, ArticleContributor, Contributor, ContributorInitials,
)
from britannica.db.session import SessionLocal

# Matches: {{EB1911 footer initials|Full Name|Initials|name2=Name2|initials2=Init2}}
#   or:    {{EB1911 footer double initials|Name1|Init1|Name2|Init2}}
# The "double" variant uses four positional args instead of named
# parameters; _parse_contributors handles both shapes.
_FOOTER_PATTERN = re.compile(
    r"\{\{\s*EB1911\s+footer(?:\s+double)?\s+initials\s*\|([^}]+)\}\}",
    re.IGNORECASE,
)

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
    # Unwrap wiki templates (e.g. {{small-caps|He}} → He)
    s = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}", r"\1", s)
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
    s = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}", r"\1", initials)
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


def extract_contributors_for_volume(volume: int) -> int:
    """Bind each article to its contributors by scanning its WALKED body for
    rendered initials signoffs — no raw re-parse, no position matching.

    The body↔contributor link can't drift: the signoff renders inside the
    article's own body, so one ``(…)`` scan gated on the vol-29 / front-matter
    index picks up footers, Author signoffs, and bare parentheticals uniformly
    and attributes each to the article it sits in.  Assumes a clean slate
    (``rebuild_contributors`` truncates ``article_contributors`` first)."""
    from britannica.db.models import ContributorInitials
    from britannica.pipeline.stages.transform_articles import walk_article

    session = SessionLocal()
    try:
        initials_map = {
            ci.initials: ci.contributor_id
            for ci in session.query(ContributorInitials).all()
        }
        articles = (
            session.query(Article)
            .filter(Article.volume == volume,
                    Article.article_type != "plate")
            .order_by(Article.page_start, Article.id)
            .all()
        )
        created = 0
        for article in articles:
            body, _disp = walk_article(session, article)
            for seq, cid in enumerate(
                    _harvest_signature_contributors(body, initials_map),
                    start=1):
                session.add(ArticleContributor(
                    article_id=article.id, contributor_id=cid, sequence=seq))
                created += 1
        session.commit()
        return created

    finally:
        session.close()


