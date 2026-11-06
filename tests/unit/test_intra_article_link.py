"""`{{EB1911 intra-article link|Display|Target}}` links ANOTHER article.

Wikisource's template writes `[[1911 Encyclopædia Britannica/{2 or 1}|{1}]]` —
the same link as `{{EB1911 article link}}`, set in plain type rather than small
capitals.  "Intra" is within the encyclopaedia.  Read as a section of the page
it sits on, its 45 uses (12 pages) became `#Le Mans`, `#France`: SARTHE linked
its towns to itself at headings it lacks, and when those self-links were
dropped (2026-11-04) the towns lost their links altogether (2026-11-06).

Each call below is verbatim from its page.
"""
from __future__ import annotations

import pytest

from wikikit.markers import iter_ln_markers
from wikikit.pipeline.stages.elements import ElementContext, process_elements


def links(raw: str, volume: int) -> list[tuple[str, str]]:
    out = process_elements(raw, ElementContext(volume=volume))
    return [(ln.target, ln.display) for ln in iter_ln_markers(out)]


@pytest.mark.parametrize("raw,volume,want", [
    # SARTHE, vol 24 p. 239: one positional is both what is shown and followed
    ("The principal places are {{EB1911 intra-article link|Le Mans}}, which", 24,
     [("Le Mans", "Le Mans")]),
    # the second positional is the target, the first what the page prints
    ("{{EB1911 intra-article link|La Ferté Bernard|La Ferté-Bernard}}", 24,
     [("La Ferté-Bernard", "La Ferté Bernard")]),
    # SOMME, vol 25 p. 408
    ("{{EB1911 intra-article link|Ham|Ham (France)}}", 25, [("Ham (France)", "Ham")]),
    # PRESENTATIONISM, vol 22 p. 312: the name in other capitals
    ("see especially the article {{EB1911 Intra-Article Link|Psychology}}", 22,
     [("Psychology", "Psychology")]),
])
def test_it_links_the_article_it_names(raw, volume, want):
    assert links(raw, volume) == want


def test_a_subpage_target_names_the_article_and_its_section():
    # TOKUGAWA, vol 26 p. 1087: Wikisource's subpage of JAPAN, not a section of TOKUGAWA
    got = links("(See {{EB1911 intra-article link|{{sc|Japan}}: ''History.''"
                "|Japan/09 Domestic History}})", 26)
    assert len(got) == 1
    assert got[0][0] == "Japan#Domestic History"
    assert not got[0][0].startswith("#")
