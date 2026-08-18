"""Executable enforcement of the two-places rule ([[feedback_transform_only_two_places]]).

The rule has been re-broken repeatedly because it was only DECLARED (memory, code
comments), never ENFORCED — and the standing leak audit REWARDS a sweeper (writing
one makes the measured number go down).  These tests make the discipline
fail-closed instead: the pre-walker source-cleaning chain is FROZEN, and every
function in it must be pure cruft removal.

Two independent layers ([[feedback_audit_code_discipline]]):

1. FROZEN ALLOWLIST — the exact ordered chain in `_source_clean`, read from its
   AST.  Adding, removing, or reordering a step changes the extracted list and
   fails this test, forcing the change to the surface as a reviewable edit of the
   frozen tuple.  Each junk removal in docs/sweeper_removal.md SHRINKS this tuple,
   which is the mechanical proof the item is gone.

2. WORD-PRESERVATION — a cruft-removal function may only DELETE content or insert
   WHITESPACE; it must never introduce a new non-whitespace token.  A conversion
   (`<bdo>`→`<span>` introduces "span") fails this by construction, so a construct
   transform can never masquerade as cruft removal.  (Subsequence-at-char-level was
   rejected: `strip_html_comments` legitimately inserts a space, which is not a
   subsequence — whitespace-insertion must be allowed, new WORDS must not.)
"""
from __future__ import annotations

import ast
import inspect
import re

import pytest

from britannica.pipeline.stages import preprocess as P


# ── Layer 1: the pre-walker chain, CLASSIFIED (not snapshotted) ───────────────
#
# A frozen SNAPSHOT (`chain == tuple`) is only a change-notifier: every planned
# removal changes the chain, so it would be re-baselined at each step — ceremony,
# not audit.  Instead every step is CLASSIFIED into one of three sets, and the
# invariant is `set(chain) == VETTED | JUNK | UNDECIDED` — every step is accounted
# for, and it holds THROUGHOUT the campaign, not just at a snapshot.
#
# How it audits the removals we are about to do:
#   * Remove J3 → delete `_normalize_bdo` from the chain (real code) AND from JUNK,
#     in ONE commit.  Both sides of the invariant drop it → still equal.
#   * Delete it from JUNK but NOT the chain → invariant fails (can't fake removal).
#   * Delete it from the chain but NOT JUNK → invariant fails (forces the ledger
#     edit, so the diff records what left).
#   * Add ANY new step without classifying it → invariant fails (no silent sweeper).
# JUNK is the public ledger of what remains; the campaign is DONE when JUNK and
# UNDECIDED are empty and the chain is exactly VETTED — a POSITIVE cleanliness
# claim, not "matches a tuple I just edited".

# Vetted pure cruft removal (word-preservation-clean; growing this set requires
# justifying a step is removal, not conversion — see Layer 2).
_VETTED = frozenset({
    "_TRAILING_WS.sub",             # trailing whitespace
    "strip_noinclude_blocks",       # noinclude removal (the J1 rescue is GONE;
                                    #   plain wholesale strip)
    # The EXACT COMPLEMENT of the line above, and vetted on the same grounds:
    # `<includeonly>` is transclusion chrome, so MediaWiki keeps the content and
    # drops the tags while `<noinclude>` drops both.  Deletion-shaped under
    # word-preservation — it removes tags and introduces nothing.  THEATRE was
    # shipping a visible `&lt;includeonly&gt;`.
    #
    # `<chem>` was briefly bundled into this step and was moved OUT by this very
    # test: mhchem is EB1911 PRESENTATION, not Wikisource chrome, so removing it
    # here would have been a construct decision wearing a cruft-removal shape.
    # It is recognized in `quote_runs._INLINE_TAG_MARKERS` instead.
    "strip_includeonly_tags",
    "strip_html_comments",          # comments
    "_strip_chrome_furniture",      # running head / pagenum / ambox
    "_EDITORIAL_DEL.sub",           # <del> correction
    "_EDITORIAL_INS.sub",           # <ins> correction
    # J8, RULED VETTED 2026-07-24 (user): TRANSPORT DECODING, not construct
    # conversion.  An entity is the source's SPELLING of a character (MediaWiki
    # renders `&mdash;` as "—" in every view); decoding it is reading the
    # source, and the character identity is needed by EVERY downstream matcher
    # (titles, classifier names, xref normalization) — so there is no natural
    # single owner to relocate to, which was never true of J3–J5.  The one
    # interpretation hazard (decoding `&lt;`/`&gt;` would forge a tag) is
    # handled by the keep-rule.  Deletion-shaped under word-preservation.
    "_decode_entities",
})
# The junk ledger — shrinks to ∅ as docs/sweeper_removal.md is worked.
_JUNK = frozenset()
# THE LEDGER IS EMPTY (2026-07-23).  What left, and where each went:
# J3 `_normalize_bdo` + J4 `_normalize_size_tags`: `<bdo>`, `<small>`, `<big>`
#   are walker-lifted TAG-IMPLIED stylers (`_TAG_STYLER_RE` → HTML_STYLE).
# J5 `_resolve_param_defaults`: every article-space `{{{name|default}}}` sits in
#   a table attr slot; the decode moved to `_table_fold.fold_cell_attrs`.
# J1 (inside `strip_noinclude_blocks`): the `{|`/`|}` keep-branch deleted —
#   plain wholesale strip.
# J2 `close_unclosed_attr_quotes`: unterminated-attr-quote tolerance moved to
#   the attr READERS (`_KV_RE`, `_SPAN_TITLE_OPEN_RE`).
# J8 ruled (see _VETTED) — nothing undecided remains.
_UNDECIDED = frozenset()


def _extract_chain(fn) -> tuple[str, ...]:
    """Read the ordered `stream = OP(stream)` operations out of `fn`'s AST.

    Deliberately structural, not a call to the function: it reports what the SOURCE
    does, so a new transform cannot be added without this list changing."""
    tree = ast.parse(inspect.getsource(fn))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == fn.__name__)
    ops: list[str] = []
    for node in func.body:
        if not (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "stream"
                and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        f = call.func
        if isinstance(f, ast.Name):                       # bare function call
            ops.append(f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            ops.append(f"{f.value.id}.{f.attr}")          # e.g. _TRAILING_WS.sub
        else:                                             # unrecognised call shape
            ops.append(ast.dump(f))
    return tuple(ops)


def test_every_prewalker_step_is_classified():
    """The standing invariant: every step in the chain is VETTED, JUNK, or
    UNDECIDED — nothing unaccounted for.  Holds throughout the campaign; a removal
    that edits both the chain and the ledger keeps it true, a faked or silent change
    breaks it."""
    assert _VETTED.isdisjoint(_JUNK) and _VETTED.isdisjoint(_UNDECIDED) \
        and _JUNK.isdisjoint(_UNDECIDED), "a step is classified two ways"
    chain = set(_extract_chain(P._source_clean))
    classified = _VETTED | _JUNK | _UNDECIDED
    unclassified = chain - classified
    stale = classified - chain
    assert not unclassified, (
        f"unclassified pre-walker step(s): {unclassified}. Classify each as VETTED "
        "(pure cruft removal), JUNK (to remove), or UNDECIDED before it lands.")
    assert not stale, (
        f"declared step(s) not in the chain: {stale}. If removed, delete from the "
        "matching set — the ledger must match reality.")


def test_campaign_progress_is_visible():
    """The junk ledger only shrinks; when JUNK ∪ UNDECIDED is empty the chain is
    exactly VETTED — the positive cleanliness claim the campaign is driving toward.
    This test documents the remaining count; it does not fail on non-empty JUNK."""
    remaining = (_JUNK | _UNDECIDED) & set(_extract_chain(P._source_clean))
    if not remaining:
        assert set(_extract_chain(P._source_clean)) == _VETTED, \
            "campaign complete but chain != VETTED — reconcile the sets"


# ── Layer 2: cruft removal only deletes / inserts whitespace ──────────────────
#
# For each function CLAIMED to be pure cruft removal, a representative input
# carrying its target.  The assertion: no non-whitespace token in the output is
# absent from the input.  A conversion fails this; a removal (even one that inserts
# a separating space) passes.  The known junk (bdo/size = conversions,
# param-default = deletion-shaped) is deliberately NOT listed here — it is not
# cruft removal, and listing it would assert a falsehood.
# Word-CHARACTER tokens, not whitespace-split: `\S+` glues a word to adjacent
# markup (`a<!--`), so removing the markup would look like a new word.  Punctuation
# and tag syntax are not words; only the alphanumerics are, in both before and after.
_WORD = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+")

_CRUFT_REMOVERS = {
    "_TRAILING_WS.sub":
        (lambda s: P._TRAILING_WS.sub("", s), "a   \nb\t \nc"),
    "strip_noinclude_blocks":     # real noinclude is newline-delimited (page header/footer)
        (P.strip_noinclude_blocks, "keep\n<noinclude>{{rh|1|X|2}} drop me</noinclude>\ntail"),
    "strip_html_comments":
        (P.strip_html_comments, "a<!-- an invisible comment -->b"),
    "_strip_chrome_furniture":
        (P._strip_chrome_furniture, "text {{rh|left|CENTER|right}} more"),
    "_EDITORIAL_DEL.sub":
        (lambda s: P._EDITORIAL_DEL.sub("", s), "good <del>bogus OCR</del> text"),
    # Transport decoding is deletion-shaped: {a, mdash, nbsp, b} -> {a, b};
    # the kept &lt;/&gt; introduce nothing either.
    "_decode_entities":
        (P._decode_entities, "a&mdash;b&nbsp;&lt;tag&gt; &amp; more"),
}


@pytest.mark.parametrize("name", list(_CRUFT_REMOVERS))
def test_cruft_remover_introduces_no_new_word(name):
    """A cruft-removal function may delete content or insert whitespace, but must
    not introduce a new non-whitespace token — the tell of a construct conversion
    ([[feedback_transform_only_two_places]])."""
    fn, sample = _CRUFT_REMOVERS[name]
    before = set(_WORD.findall(sample))
    after = set(_WORD.findall(fn(sample)))
    new = after - before
    assert not new, f"{name} introduced non-whitespace tokens not in its input: {new}"


def test_the_invariant_would_catch_a_conversion():
    """Guard on the guard: the word-preservation check actually FIRES on a
    conversion.  The sample is SYNTHETIC (the shape `_normalize_bdo` had before
    J3 removed it) so the guard outlives the campaign — once every conversion is
    out of preprocess there is no live one left to point it at."""
    def fake_conversion(s: str) -> str:
        return re.sub(r"<bdo\b[^>]*>", '<span style="direction:ltr">', s)
    src = '<bdo dir="rtl">x</bdo>'
    new = set(_WORD.findall(fake_conversion(src))) - set(_WORD.findall(src))
    assert new, "word-preservation invariant failed to flag a bdo→span conversion"


# ── Page seams: a page break is EXACTLY ONE line break ───────────────────────
#
# `make_stream` says this already — it `.strip()`s each page and joins with "\n"
# — but it runs while the `<noinclude>` wrapper is still attached, so it cannot
# strip whitespace the noinclude removal later EXPOSES.  Both directions were
# broken ([[project_page_position_out_of_band]]):
#
#   * `_SECTION_TAG_RE` ends `[ \t]*\n?`, so a page ending
#     `…difference<section end="Pollination" />` lost the join newline and ran
#     "difference" into the next page's "between" — 10,385 of 26,931 seams.
#   * A page whose wikitext reads `<noinclude>…</noinclude>\ntext` kept that
#     leading newline, making the seam "\n\n" — a paragraph break mid-sentence
#     ("Prester John, and various ¶ expeditions had been sent"), 284 of 2,949
#     seams in vols 1/8/22.  Its neighbours in the SAME article are written
#     without the newline, so it is transcription style, not content.
#
# Both were invisible while the body was cut into per-page segments and glued
# back with `" "` — the reassembly normalised the seam by accident.  Storing the
# body whole removed the accident, so the rule is asserted here instead.
#
# A paragraph that genuinely breaks across a page is carried by `{{nop}}`, which
# is what Wikisource writes for it precisely because a page transition swallows a
# lone newline.  It is not whitespace, so the rule never touches it.

class _Pg:
    def __init__(self, page_number, wikitext):
        self.page_number = page_number
        self.wikitext = wikitext


_SEAM_CASES = {
    # POLLINATION ws 16/17 verbatim: the section tag is flush against the last
    # word, so its `\n?` reaches past the page's own end.
    "section_tag_flush_with_last_word": [
        _Pg(16, 'no difference<section end="Pollination" /><noinclude></noinclude>'),
        _Pg(17, '<noinclude>{{rh||x|3}}</noinclude>between the effects'),
    ],
    # The tag on its own line: dropping it takes the line's newline, and the
    # page's own trailing newline is what remains — nothing to supply.
    "section_tag_on_its_own_line": [
        _Pg(16, 'no difference\n<section end="X" />'),
        _Pg(17, 'between the effects'),
    ],
    # No chrome at all — the plain join, which was always correct.
    "no_chrome": [
        _Pg(16, 'no difference'),
        _Pg(17, 'between the effects'),
    ],
    # ABYSSINIA ws 120/121: the incoming page's own text starts with a newline
    # the `<noinclude>` hid from `make_stream`'s `.strip()`.
    "incoming_page_starts_with_a_newline": [
        _Pg(16, 'known as Prester John, and various'),
        _Pg(17, '<noinclude>{{rh|90|ABYSSINIA||x}}</noinclude>\n'
                'expeditions had been sent in quest of it'),
    ],
    # Whitespace exposed on BOTH sides at once.
    "whitespace_on_both_sides": [
        _Pg(16, 'end of the line\n\n<section end="X" />'),
        _Pg(17, '<noinclude>{{rh|1|X|2}}</noinclude>\n\ncontinues here'),
    ],
}


@pytest.mark.parametrize("name", list(_SEAM_CASES))
def test_page_break_is_exactly_one_newline(name):
    stream, page_keys, _sections = P.stream_with_keys(_SEAM_CASES[name])
    assert [pg for _off, pg in page_keys] == [16, 17]
    for off, pg in page_keys:
        if off == 0:                      # the stream's first page: no seam
            continue
        i = off
        while i > 0 and stream[i - 1] in " \t\n":
            i -= 1
        j = off
        while j < len(stream) and stream[j] in " \t\n":
            j += 1
        assert stream[i:j] == "\n", (
            f"page {pg}'s seam is {stream[i:j]!r}, not a single newline: "
            f"...{stream[max(0, i - 20):i]!r} ⟨key⟩ {stream[j:j + 20]!r}...")


def test_seam_never_glues_two_words():
    stream, _keys, _sections = P.stream_with_keys(
        _SEAM_CASES["section_tag_flush_with_last_word"])
    assert "differencebetween" not in stream
    assert "difference\nbetween" in stream


def test_seam_never_invents_a_paragraph_break():
    """The ABYSSINIA shape: a lone newline in the incoming page's wikitext is
    transcription style — its neighbours in the same article are written without
    one — so carrying it through would break a sentence into two paragraphs."""
    stream, _keys, _sections = P.stream_with_keys(
        _SEAM_CASES["incoming_page_starts_with_a_newline"])
    assert "\n\n" not in stream, f"invented a paragraph break: {stream!r}"
    assert "and various\nexpeditions" in stream


def test_nop_survives_the_seam_rule():
    """`{{nop}}` is how Wikisource carries a paragraph that genuinely breaks
    across a page — precisely because a page transition swallows a lone newline.
    It is not whitespace, so the seam rule must leave it alone."""
    stream, page_keys, _sections = P.stream_with_keys([
        _Pg(16, 'the paragraph ends here.\n{{nop}}'),
        _Pg(17, 'A new paragraph starts.'),
    ])
    assert "{{nop}}" in stream, f"the seam rule ate {{nop}}: {stream!r}"
    off = page_keys[1][0]
    assert stream[:off].rstrip("\n").endswith("{{nop}}")
