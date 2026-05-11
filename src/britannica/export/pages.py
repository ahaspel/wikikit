"""Printed-page / scan-leaf lookups used by the article exporter.

Loads two JSON tables at first use:
- ``data/derived/printed_pages.json``: ws-page → printed page number, per volume.
- ``data/derived/scan_map.json``: ws-page → physical scan leaf, per volume.

Both are produced earlier in the rebuild (Phase 3c / scan map build).
A fallback offset table covers volumes whose scan_map lacks an entry.
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_printed_pages() -> dict:
    """Load the printed page number lookup (leaf → printed per volume)."""
    path = Path("data/derived/printed_pages.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _load_scan_map() -> dict:
    """Load the ws → leaf mapping per volume."""
    path = Path("data/derived/scan_map.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


# Fallback ws → leaf offset when scan_map has no entry.
_LEAF_OFFSET = {
    1: 7, 2: 7, 3: 9, 4: 9, 5: 12, 6: 12, 7: 7, 8: 7,
    9: 9, 10: 10, 11: 8, 12: 7, 13: 7, 14: 6, 15: 17, 16: 6,
    17: 9, 18: 6, 19: 7, 20: 0, 21: 6, 22: 6, 23: 7, 24: 4,
    25: 8, 26: 4, 27: 6, 28: 5, 29: 6,
}


_PRINTED_PAGES = None
_SCAN_MAP = None


def _get_printed_pages() -> dict:
    global _PRINTED_PAGES
    if _PRINTED_PAGES is None:
        _PRINTED_PAGES = _load_printed_pages()
    return _PRINTED_PAGES


def _get_scan_map() -> dict:
    global _SCAN_MAP
    if _SCAN_MAP is None:
        _SCAN_MAP = _load_scan_map()
    return _SCAN_MAP


def _leaf_for_ws(volume: int, ws_page: int) -> int:
    """Translate a Wikisource page index to its physical scan leaf."""
    sm = _get_scan_map().get(str(volume), {})
    leaf = sm.get(str(ws_page))
    if leaf is not None:
        return int(leaf)
    return ws_page + _LEAF_OFFSET.get(volume, 0)


def _printed_page(volume: int, ws_page: int) -> int:
    """Look up the printed page number for a Wikisource page.

    printed_pages.json is ws-keyed (heading-sourced with monotonic
    interpolation for gaps). Stays in ws space — no scan_map detour.

    If the exact ws page has no printed mapping (it's a plate/blank),
    walk backward up to 10 pages to find the nearest numbered
    predecessor.  SHIPBUILDING ends on a plate at ws 1057 with no
    printed number; before this fallback its page_end was reported as
    1057 (a ws index) instead of 981 (the last numbered page).
    """
    pp = _get_printed_pages()
    vol_map = pp.get(str(volume), {})
    printed = vol_map.get(str(ws_page))
    if printed is not None:
        return printed
    for back in range(1, 11):
        printed = vol_map.get(str(ws_page - back))
        if printed is not None:
            return printed
    return ws_page  # last-resort fallback
