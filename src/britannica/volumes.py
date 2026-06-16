"""Per-volume article ranges — the static, scan-verified source of truth.

Each EB1911 volume opens with front matter (half-title, title page, copyright,
the contributor / "initials and headings" list) and closes with the last
article; some carry trailing blank or plate leaves. The article/plate walk must
process ONLY the article span, so front matter never enters the element walk and
no downstream pass has to neutralise it.

This is a STATIC fact about a fixed source, recorded once. The first and last
article LEAF of every volume were verified by eye against the page scans
(2026-06-16, via ``tools/viewer/leaf_check.html``). The walk reads ws/djvu page
numbers (``SourcePage.page_number``), so the values below are the ws projection
of those verified leaves; the verified leaf pair is kept in the trailing comment
for human re-verification.

Vol 29 (the Index) is not an article volume and has no entry.
"""
from __future__ import annotations

# volume -> (first_article_ws, last_article_ws), inclusive — ws/djvu page space
# (``SourcePage.page_number``).  Pages outside the span are front matter (before
# first) or trailing blank/plate/back matter (after last) and are NOT walked.
# Trailing comment = the verified IA-scan leaf pair (leaf_check.html).
ARTICLE_WS_RANGE: dict[int, tuple[int, int]] = {
    1:  (32, 1029),   # leaves 39..1044
    2:  (12, 1027),   # leaves 19..1048
    3:  (15, 1015),   # leaves 21..1026
    4:  (14, 1031),   # leaves 23..1048
    5:  (12, 1002),   # leaves 21..1024
    6:  (14, 1017),   # leaves 23..1032
    7:  (14, 1008),   # leaves 21..1016
    8:  (15, 1027),   # leaves 21..1034
    9:  (13, 997),    # leaves 21..1020
    10: (13, 967),    # leaves 23..984
    11: (13, 968),    # leaves 21..982
    12: (14, 985),    # leaves 21..994
    13: (14, 985),    # leaves 21..998
    14: (13, 953),    # leaves 19..980
    15: (14, 994),    # leaves 21..1030
    16: (15, 1016),   # leaves 21..1024
    17: (15, 1039),   # leaves 21..1058
    18: (15, 1000),   # leaves 19..1018
    19: (15, 1034),   # leaves 21..1062
    20: (19, 1048),   # leaves 21..1048
    21: (15, 1019),   # leaves 21..1034
    22: (15, 993),    # leaves 21..1002
    23: (14, 1069),   # leaves 21..1088
    24: (15, 1100),   # leaves 19..1118
    25: (15, 1090),   # leaves 21..1112
    26: (17, 1104),   # leaves 21..1118
    27: (15, 1092),   # leaves 21..1110
    28: (16, 1091),   # leaves 21..1106
}


def article_ws_range(volume: int) -> tuple[int, int] | None:
    """The inclusive ``(first_ws, last_ws)`` article span for ``volume``,
    or ``None`` for a volume with no recorded range (e.g. vol 29, the Index)."""
    return ARTICLE_WS_RANGE.get(volume)


def in_article_range(volume: int, page_number: int) -> bool:
    """True if ``page_number`` is within ``volume``'s article span.  A volume
    with no recorded range admits every page (no front/back matter to exclude)."""
    span = ARTICLE_WS_RANGE.get(volume)
    if span is None:
        return True
    return span[0] <= page_number <= span[1]
