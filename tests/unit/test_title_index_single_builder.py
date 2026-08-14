"""There is ONE title→filename index, and one builder for it.

The export built it and the post-export xref pass built its own "mirror", kept
in step by a comment.  They had already drifted — the mirror kept plates and
skipped `article_sort_key` — and when the index grew a second question
(`get_as_written`), the mirror was still a plain dict and the rebuild died 47
minutes in, inside `swapped_link`.

Nothing here can prove a third one will never appear, but it can insist that the
two known consumers get theirs from the builder.
"""
import re
from pathlib import Path

import pytest

from britannica.export.article_json import build_title_index
from britannica.xrefs.normalizer import NormalizedIndex

ROOT = Path(__file__).resolve().parents[2]
CONSUMERS = [
    ROOT / "src" / "britannica" / "export" / "article_json.py",
    ROOT / "tools" / "pipeline" / "resolve_xrefs_post.py",
]

# `{...}.setdefault(a.title.upper(), …)` and friends — a title map being built
# by hand rather than asked for.
BY_HAND = re.compile(r"\.setdefault\(\s*\w+\.title\.(?:strip\(\)\.)?upper\(\)")


def _art(title, atype="article", page=1):
    from britannica.db.models import Article
    return Article(title=title, article_type=atype, volume=1,
                   page_start=page, page_end=page, body="", section_name=None)


def test_the_builder_returns_the_index_type():
    idx = build_title_index([_art("BAG-PIPE")])
    assert isinstance(idx, NormalizedIndex)
    assert hasattr(idx, "get_as_written"), "the swap asks this question"


def test_plates_do_not_own_titles():
    """The export excluded them; the mirror did not, so the two disagreed about
    which article a title belongs to."""
    idx = build_title_index([_art("ATOM", "plate"), _art("ATOM", page=2)])
    assert idx.get("ATOM") is not None
    assert len(idx) == 1


# `_resolve_bio_articles` keeps its own map on purpose: it PREFIX-SCANS titles
# (`title_map.items()`, `startswith`) rather than looking one up, which is not
# what NormalizedIndex offers.  It is counted, not excused — a SECOND hand-built
# map in that file fails this, and folding the bio matcher onto a shared index
# is its own piece of work.
BY_HAND_ALLOWED = {"article_json.py": 1, "resolve_xrefs_post.py": 0}


@pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
def test_no_new_title_map_is_built_by_hand(path):
    source = path.read_text(encoding="utf-8")
    assert "build_title_index" in source, f"{path.name} should ask for the index"
    found = len(BY_HAND.findall(source))
    allowed = BY_HAND_ALLOWED[path.name]
    assert found <= allowed, (
        f"{path.name} builds {found} title maps by hand, {allowed} known. "
        "Call build_title_index — a second index drifts from the first.")
