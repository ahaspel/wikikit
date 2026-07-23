"""Executable enforcement of the two-places rule ([[feedback_transform_only_two_places]]).

The rule has been re-broken repeatedly because it was only DECLARED (memory, code
comments), never ENFORCED — and the standing leak audit REWARDS a sweeper (writing
one makes the measured number go down).  These tests make the discipline
fail-closed instead: the pre-walker source-cleaning chain is FROZEN, and every
function in it must be pure cruft removal.

Two independent layers ([[feedback_audit_code_discipline]]):

1. FROZEN ALLOWLIST — the exact ordered chain in `_clean_and_heal`, read from its
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
    "strip_noinclude_blocks",       # noinclude removal (J1 rescue is IN its body —
                                    #   a behavioral pin, not a chain member)
    "strip_html_comments",          # comments
    "_strip_chrome_furniture",      # running head / pagenum / ambox
    "_EDITORIAL_DEL.sub",           # <del> correction
    "_EDITORIAL_INS.sub",           # <ins> correction
})
# The junk ledger — shrinks to ∅ as docs/sweeper_removal.md is worked.
_JUNK = frozenset({
    "close_unclosed_attr_quotes",   # J2 sweeper
    "_normalize_bdo",               # J3 misplaced transform
    "_normalize_size_tags",         # J4 misplaced transform
    "_resolve_param_defaults",      # J5 misplaced transform
})
# Borderline, owner's call (J8).
_UNDECIDED = frozenset({"_decode_entities"})


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
    chain = set(_extract_chain(P._clean_and_heal))
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
    remaining = (_JUNK | _UNDECIDED) & set(_extract_chain(P._clean_and_heal))
    if not remaining:
        assert set(_extract_chain(P._clean_and_heal)) == _VETTED, \
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
    """Guard on the guard: the word-preservation check actually FIRES on a known
    conversion — `_normalize_bdo` introduces `<span …>`, which `_normalize_size_tags`
    does too.  If this ever passes silently the invariant has gone blind."""
    src = '<bdo dir="rtl">x</bdo>'
    new = set(_WORD.findall(P._normalize_bdo(src))) - set(_WORD.findall(src))
    assert new, "word-preservation invariant failed to flag a bdo→span conversion"
