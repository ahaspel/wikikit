"""`{{DNB lkpl}}` cites another book, so it prints — it does not link here.

The defect this locks down reached production. `{{DNB lkpl|Walsh, Peter|Dict.
Nat. Biog}}` sat in the TARGET_FIRST_LINK family, which is harvested
`display -> target` into the EB1911 alias table. A DNB citation is written the
other way round — the target is the PERSON, the display is always the same fixed
phrase — so the table learnt that the words "Dict. Nat. Biog." mean Peter Walsh,
and then linked that phrase to him in every article citing the DNB. Sixteen
articles shipped with a citation pointing at a stranger, each also listing
WALSH, PETER in its cross-reference panel.

Which stranger was decided by punctuation: `alias_table` drops any display
containing an apostrophe, so instances with the italics INSIDE the braces were
skipped and ones with them outside were kept; the trailing-period spelling went
to WALSINGHAM instead.

Both spellings below are verbatim from the scans (vol 22 p. 123, vol 28 p. 309).
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from britannica.pipeline.stages.elements import ElementContext, process_elements

PORSON = "See also R. C. Jebb in {{DNB lkpl|Porson, Richard|''Dict. Nat. Biog.''}}, and"
WALSH = "T. Carte, ''Life of Ormonde''; ''{{DNB lkpl|Walsh, Peter|Dict. Nat. Biog}}''. lix."
WALSINGHAM = ("evidence for the statement "
              "(''{{DNB lkpl|Walsingham, Francis (1530?-1590)|Dict. Nat. Biog.}}'')")
# What the walk actually receives: quote-runs converts '' to «I» in preprocess,
# so the display slot carries MARKUP by the time the classifier sees it.
PORSON_WALKED = ("See also R. C. Jebb in "
                 "{{DNB lkpl|Porson, Richard|«I»Dict. Nat. Biog.«/I»}}, and")


def walk(raw: str, volume: int = 22) -> str:
    return process_elements(raw, ElementContext(volume=volume))


@pytest.mark.parametrize("raw,gone", [
    (PORSON, "Porson, Richard"),
    (WALSH, "Walsh, Peter"),
    (WALSINGHAM, "Walsingham"),
    (PORSON_WALKED, "Porson, Richard"),
])
def test_dnb_citation_does_not_become_a_link(raw, gone):
    out = walk(raw)

    assert "«LN" not in out, f"DNB citation still emits a link marker: {out}"
    assert gone not in out, f"DNB page name leaked into the reader's text: {out}"


@pytest.mark.parametrize("raw", [PORSON, WALSH, WALSINGHAM, PORSON_WALKED])
def test_the_printed_citation_survives(raw):
    """The words on the page stay; only the link goes."""
    assert "Dict. Nat. Biog" in walk(raw)


def test_display_markup_is_preserved():
    """Italics are the page's own emphasis, not link chrome.

    [[feedback_when_in_doubt_carry]] — the display slot is recursed, so its
    markup must survive the unlinking.
    """
    assert "«I»" in walk(PORSON_WALKED)


def test_eb1911_lkpl_still_links():
    """Control: the fix narrows ONE template, not the whole target-first family."""
    out = walk("see {{EB1911 lkpl|Aachen|Aix-la-Chapelle}} there", volume=1)

    assert "«LN" in out and "Aix-la-Chapelle" in out


def test_alias_table_ignores_dnb_templates():
    """The alias harvest is EB1911-only; a DNB citation teaches it nothing."""
    from britannica.xrefs.alias_table import _extract_aliases_from_wikitext

    aliases = defaultdict(list)
    _extract_aliases_from_wikitext(
        "''{{DNB lkpl|Walsh, Peter|Dict. Nat. Biog}}''", aliases)

    assert not aliases, f"DNB citation still feeds the EB1911 alias table: {dict(aliases)}"


def test_alias_table_still_learns_eb1911_aliases():
    """Guard on the guard: the harvest is narrowed, not disabled."""
    from britannica.xrefs.alias_table import _extract_aliases_from_wikitext

    aliases = defaultdict(list)
    _extract_aliases_from_wikitext(
        "{{EB1911 lkpl|Aachen|Aix-la-Chapelle}}", aliases)

    assert dict(aliases) == {"AIX-LA-CHAPELLE": ["AACHEN"]}
