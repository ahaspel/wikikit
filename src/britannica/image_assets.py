"""Static lookup tables for pre-rendered / pre-cropped image assets.

This module imports nothing from the pipeline — it's a leaf, safe to
import eagerly from anywhere.
"""

from __future__ import annotations

import re


def normalize_score_content(s: str) -> str:
    """Whitespace-normalize a LilyPond block for content-keyed lookup.

    Collapses every run of whitespace (spaces, tabs, newlines) to a
    single space and strips ends.  LilyPond is whitespace-insensitive
    so this is safe, and it makes the lookup robust to incidental
    formatting differences between the SCORE_IMAGES keys and the tag
    contents the element extractor delivers.
    """
    return re.sub(r"\s+", " ", s).strip()


# ── <score> musical-notation tags → pre-rendered Wikimedia PNGs ─────────
#
# Wikisource ``<score>`` tags carry LilyPond source; the live wiki
# renders them server-side to PNGs on upload.wikimedia.org.  We can't
# run LilyPond here, so each tag's content is mapped to its pre-fetched
# rendered URL.  Content-addressable (like math/poem) — the element
# handler does a single dict lookup, no positional context required.

SCORE_IMAGES: dict[str, str] = {
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine } \\relative <<{f'_0 g_1 a_2 \\stemUp b_3 "
        "\\stemNeutral c_4 d_5 e_6 f_7 g_8}>> }"
    ): "https://upload.wikimedia.org/score/h/z/hzcdxxolvqb8f88rf1kv99xpbkz4fhl/hzcdxxol.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine } \\relative <<{ gis' \\stemUp bes "
        "\\stemNeutral cis ees fis gis a}>> }"
    ): "https://upload.wikimedia.org/score/8/m/8muj660hon0gdc23klev5oueja71g2e/8muj660h.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine } \\relative <<{gis'' a ais b c d}>> }"
    ): "https://upload.wikimedia.org/score/l/e/le30qszwd023fbi5l5p72zzgp0qif4z/le30qszw.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine } \\relative <<{\\clef bass \\stemDown c g' c "
        "\\clef treble \\stemNeutral g' c}>> }"
    ): "https://upload.wikimedia.org/score/t/a/ta4vp64mow2a4xgtut6yrr587tjqvwq/ta4vp64m.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine } \\relative <<{ g'_1 a_2 \\stemUp bes_3 "
        "\\stemNeutral c_4 d_5 e_6 g_7}>> }"
    ): "https://upload.wikimedia.org/score/l/e/lexak41zsl71g5wdztqfen2titdlq9w/lexak41z.png",
    normalize_score_content(
        "{ \\new Staff \\with {\\clef bass \\omit Score.TimeSignature "
        "\\omit Score.BarLine } \\relative <<{g}>> }"
    ): "https://upload.wikimedia.org/score/c/6/c6ls5kqiltjw1nu8qc0qh3r85v60gdx/c6ls5kqi.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine} \\relative { e''1 d g, b} }"
    ): "https://upload.wikimedia.org/score/9/i/9iavthct92fgw9tjxwi3s57m4yb9fw0/9iavthct.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine} \\relative {\\clef bass e' d g, a} }"
    ): "https://upload.wikimedia.org/score/i/y/iy808fgeauppb3nth1mdmfhjgi6rpy3/iy808fge.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine} \\relative {\\clef bass e' d g, b} }"
    ): "https://upload.wikimedia.org/score/5/y/5ytbiminte2jgttcfyl2swwg254v2i0/5ytbimin.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine} \\relative {\\clef bass g a b c d e} }"
    ): "https://upload.wikimedia.org/score/a/o/ao59rovwbfgbmxdrbumkluxcgxg7qoj/ao59rovw.png",
    normalize_score_content(
        "{ \\new Staff \\with { \\omit Score.TimeSignature "
        "\\omit Score.BarLine } \\relative  { c' e g c e g} }"
    ): "https://upload.wikimedia.org/score/7/y/7yn0x3i1tb37t4fg4v68yj3n4pnh1vi/7yn0x3i1.png",
}


# ── {{chart2}} genealogical trees → pre-cropped local page scans ────────
#
# The 5 ``{{chart2/start}}…{{chart2/end}}`` blocks have been manually
# cropped from DjVu page scans and saved as ``chart2_volNN_pageNNNN.jpg``.
# Keyed by ``(volume, page_number)``.

CHART2_IMAGES: dict[tuple[int, int], str] = {
    (1, 124): "chart2_vol01_page0124.jpg",
    (21, 573): "chart2_vol21_page0573.jpg",
    (23, 945): "chart2_vol23_page0945.jpg",
    (24, 271): "chart2_vol24_page0271.jpg",
    (28, 952): "chart2_vol28_page0952.jpg",
}

# Sibling tree macros of chart2 — same treatment (unrenderable grid macro →
# manually-cropped page scan).  ``{{familytree/start}}…{{familytree/end}}`` and
# ``{{Tree chart/start}}…{{Tree chart/end}}``.  Keyed by ``(volume, page_number)``.
#   * COWPER, WILLIAM (vol 7, p369) — the Cowper-family genealogy.
#   * SOLOMON, PSALMS OF (vol 25, p382) — the manuscript stemma.
# TODO(crops): produce these two .jpg crops from the DjVu scans (as for chart2);
# until then the substitution emits an IMG marker pointing at the pending file
# (a visible placeholder) instead of the catch-all silently deleting the tree.
TREE_IMAGES: dict[tuple[int, int], str] = {
    (7, 369): "familytree_vol07_page0369.jpg",
    (25, 382): "treechart_vol25_page0382.jpg",
}
