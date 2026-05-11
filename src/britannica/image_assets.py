"""Static lookup tables for pre-rendered / pre-cropped image assets.

Both maps key on a physical source location (volume, page) so the
element handlers can substitute the right asset without reaching
across pipeline stages.

This module imports nothing from the pipeline — it's a leaf, safe to
import eagerly from anywhere.
"""

from __future__ import annotations

import re


# ── <score> musical-notation tags → pre-rendered Wikimedia PNGs ─────────
#
# Wikisource ``<score>`` tags carry LilyPond source; the live wiki
# renders them server-side to PNGs on upload.wikimedia.org.  We can't
# run LilyPond here, so we map each occurrence — keyed by
# ``(volume, page_number, occurrence_index)`` — to the rendered PNG URL.

SCORE_IMAGES: dict[tuple[int, int, int], str] = {
    (3, 221, 0): "https://upload.wikimedia.org/score/h/z/hzcdxxolvqb8f88rf1kv99xpbkz4fhl/hzcdxxol.png",
    (3, 221, 1): "https://upload.wikimedia.org/score/8/m/8muj660hon0gdc23klev5oueja71g2e/8muj660h.png",
    (3, 221, 2): "https://upload.wikimedia.org/score/l/e/le30qszwd023fbi5l5p72zzgp0qif4z/le30qszw.png",
    (3, 221, 3): "https://upload.wikimedia.org/score/t/a/ta4vp64mow2a4xgtut6yrr587tjqvwq/ta4vp64m.png",
    (3, 971, 0): "https://upload.wikimedia.org/score/l/e/lexak41zsl71g5wdztqfen2titdlq9w/lexak41z.png",
    (3, 972, 0): "https://upload.wikimedia.org/score/c/6/c6ls5kqiltjw1nu8qc0qh3r85v60gdx/c6ls5kqi.png",
    (6, 415, 0): "https://upload.wikimedia.org/score/9/i/9iavthct92fgw9tjxwi3s57m4yb9fw0/9iavthct.png",
    (6, 416, 0): "https://upload.wikimedia.org/score/i/y/iy808fgeauppb3nth1mdmfhjgi6rpy3/iy808fge.png",
    (6, 416, 1): "https://upload.wikimedia.org/score/5/y/5ytbiminte2jgttcfyl2swwg254v2i0/5ytbimin.png",
    (6, 416, 2): "https://upload.wikimedia.org/score/a/o/ao59rovwbfgbmxdrbumkluxcgxg7qoj/ao59rovw.png",
    (6, 416, 3): "https://upload.wikimedia.org/score/7/y/7yn0x3i1tb37t4fg4v68yj3n4pnh1vi/7yn0x3i1.png",
}

SCORE_TAG = re.compile(r"<score[^>]*>.*?</score>", re.DOTALL)


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
