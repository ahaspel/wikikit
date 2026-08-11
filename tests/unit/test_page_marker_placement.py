"""Page markers render in PAGE ORDER — always.

Placement has two sources of position: a signature LOCATED in the rendered text,
and a proportional fallback for a page whose signature could not be found.  The
located ones are monotonic by construction (`locate` is a monotonic DP).  The
fallback was not, and it is a weak estimate — raw offsets do not map linearly
onto rendered letters, so GYROSCOPE AND GYROSTAT's page 773 was located at letter
19,167 while its prior said 11,287.  An unclamped fallback for page 774 landed at
16,790, i.e. BEHIND page 773, and the two margin numbers rendered swapped.  30 of
the longest articles did this.

Page order is not an estimate, so it bounds the estimate: a fallback is clamped
between the previous marker and the next page actually located.  That creates
ties — a clamped fallback sits exactly on its predecessor — and back-to-front
insertion reverses whatever shares an index, so `inject` breaks ties by page
descending.  Both halves are needed; either alone still renders a swap.
"""
from __future__ import annotations

import re

from britannica.render.page_markers import _place, inject, marker_positions

_MARK = re.compile(r'data-page="(\d+)"')


class _Ctx:
    volume = 12
    unproofed_pages: dict = {}
    epub_bundled = None
    scan_url = "scans.html"


def _keys(pages_offsets_sigs):
    return [{"page": p, "offset": o, "sig": s} for p, o, s in pages_offsets_sigs]


def test_fallback_never_precedes_a_located_predecessor():
    """The GYROSCOPE shape: page N located late, page N+1 unfindable and its
    prior pointing behind N."""
    letters = "abcdefghij" * 40                      # 400 letters
    at = list(range(len(letters)))
    ordered = [(0, 769), (100, 773), (200, 774), (300, 775)]
    found = [0, 300, None, 350]                      # 774 unlocated
    expected = [0, 120, 150, 380]                    # its prior says 150 — behind 300
    placed = _place(ordered, found, expected, letters, at)
    positions = [p for p, _pg in placed]
    assert positions == sorted(positions), (
        f"markers not in non-decreasing position order: {placed}")
    assert [pg for _p, pg in placed] == [769, 773, 774, 775]


def test_fallback_is_bounded_by_the_next_located_page():
    """A wildly high prior must not jump the page that follows it."""
    letters = "abcdefghij" * 40
    at = list(range(len(letters)))
    ordered = [(0, 10), (100, 11), (200, 12)]
    found = [0, None, 150]                           # 11 unlocated
    expected = [0, 399, 150]                         # prior overshoots past 12
    placed = _place(ordered, found, expected, letters, at)
    positions = [p for p, _pg in placed]
    assert positions == sorted(positions), placed
    assert positions[1] <= positions[2], (
        f"fallback jumped past the next LOCATED page: {placed}")


def test_inject_breaks_position_ties_by_page():
    """Two markers at one index: back-to-front insertion reverses them unless
    the tie is broken by page descending."""
    html = "<p>" + ("word " * 40) + "</p>"
    # Force a genuine tie: EQUAL offsets (so equal priors) and signatures the
    # locator cannot find, so both fall back to the same letter.
    keys = _keys([(769, 0, ""), (773, 50, "zzzzzzzzzzzz"),
                  (774, 50, "zzzzzzzzzzzz")])
    out = inject(html, keys, _Ctx(), body_span=100)
    pages = [int(p) for p in _MARK.findall(out)]
    assert pages == sorted(pages), f"markers rendered out of page order: {pages}"


def test_located_pages_are_already_monotonic():
    """Guard on the premise the clamp relies on: `locate`'s finds never go
    backwards, so the ceiling built from them is well-formed."""
    letters = "alpha" * 20 + "beta" * 20 + "gamma" * 20
    at = list(range(len(letters)))
    ordered = [(0, 1), (10, 2), (20, 3)]
    found = [0, 100, 180]
    expected = [0, 100, 180]
    placed = _place(ordered, found, expected, letters, at)
    assert [p for p, _pg in placed] == [0, 100, 180]


def test_marker_positions_returns_one_spot_per_key_in_order():
    html = "<p>" + ("alpha bravo charlie delta " * 30) + "</p>"
    keys = _keys([(1, 0, ""), (2, 300, "bravocharlie"), (3, 600, "")])
    spots = marker_positions(html, keys, body_span=900)
    assert len(spots) == 3
    assert [pg for _p, pg in spots] == [1, 2, 3]
    positions = [p for p, _pg in spots]
    assert positions == sorted(positions), spots
