"""Snapshot regression test for the Python article renderer.

For each seed under ``tests/snapshots/render/`` — a frozen export payload
(``<stem>.input.json``) paired with its rendered HTML (``<stem>.html``) —
re-render through ``render_article`` and assert the output matches the golden.

The golden IS the renderer's own output: Python is the sole renderer now (the
viewer inserts this HTML verbatim), and it was adjudicated byte-identical to the
retired jsdom/.js baseline (the flip, 22/22; the corpus diff, 277/277).  So the
golden is a self-snapshot that catches any *unintended* future render change.

Both sides are normalised through html5lib (``normalize_html``) before comparing
— identical footing, robust to entity/whitespace canonicalisation.

When you intentionally change the renderer:
  1. Rebaseline from Python — ``render_article(json.load(<stem>.input.json))``
     re-written to ``<stem>.html`` (see ``_rebaseline`` below).
  2. Inspect the diffs to confirm ONLY the intended cases changed.
  3. Commit the new goldens alongside the behaviour change.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from britannica.render.article import render_article
from britannica.render.normalize import normalize_html


SNAPSHOT_DIR = Path("tests/snapshots/render")


def _first_divergence(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


def _snapshot_pairs() -> list[tuple[str, Path, Path]]:
    """Discover (stem, input, golden) pairs.  The ``.html`` IS the snapshot; its
    sibling ``.input.json`` is the frozen export payload it was rendered from."""
    if not SNAPSHOT_DIR.exists():
        return []
    pairs = []
    for html_path in sorted(SNAPSHOT_DIR.glob("*.html")):
        stem = html_path.name.removesuffix(".html")
        pairs.append((stem, SNAPSHOT_DIR / f"{stem}.input.json", html_path))
    return pairs


@pytest.mark.parametrize("stem,input_path,golden_path",
                         _snapshot_pairs(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_render_snapshot(stem, input_path, golden_path):
    article = json.loads(input_path.read_text(encoding="utf-8"))
    # Defaults match how the golden was produced: is_local=True stub URLs,
    # target="site" (the production render the viewer inserts).
    got = normalize_html(render_article(article))
    exp = normalize_html(golden_path.read_text(encoding="utf-8"))

    if got != exp:
        i = _first_divergence(got, exp)
        pytest.fail(
            f"render output diverged for {stem} @ char {i}\n"
            f"  golden: ...{exp[max(0, i - 30):i + 60]!r}\n"
            f"  actual: ...{got[max(0, i - 30):i + 60]!r}"
        )


def _rebaseline(stems: list[str] | None = None) -> None:
    """Rewrite goldens from the Python renderer (the source of truth).  Run
    manually after an intended render change, then adjudicate the git diff:

        python -c "from tests.regression.test_render_snapshot import _rebaseline; _rebaseline()"
    """
    for stem, input_path, golden_path in _snapshot_pairs():
        if stems and stem not in stems:
            continue
        article = json.loads(input_path.read_text(encoding="utf-8"))
        golden_path.write_text(render_article(article), encoding="utf-8", newline="\n")
