"""A measurement cache's key and path have ONE owner, shared by object.

ONE content-addressed cache is left: the measured math widths
(`«MATH[fs=N]` / popout).  It is WRITTEN by a build-time tool and READ by the
pipeline, and each side used to spell its own key function and its own cache
path.  Nothing failed loudly when a pair drifted — the reader looked up keys the
writer had never written, found nothing, and every hint quietly vanished.

THE TABLE CACHE IS GONE, and its removal is the better lesson.  It kept failing
in a way identity could not fix: it keyed on the SPAN'S BYTES, so every producer
change that rewrote a table invalidated it — 194 hints lost in the 2026-08-16
rebuild, 47 more in August 2026 — and it answered at one viewport, which is
simply wrong on a phone.  Whether a table overflows is a LAYOUT fact the browser
owns, so the viewer now decides per element and per viewport (`wrapWideTables`)
and there is no cache to keep honest.  Math survives because its key is the
LATEX — which comes from the static source, not from our output — and because
the EPUB targets have no JS and genuinely need a baked hint.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in ("src", "tools/diagnostics", "tools/pipeline"):
    sys.path.insert(0, str(ROOT / _p))

from britannica import markers, math_widths  # noqa: E402



def test_math_writer_and_reader_share_one_key_and_path():
    import measure_math_widths as writer
    assert writer.cache_key is math_widths.cache_key
    assert writer.CACHE_PATH is math_widths.CACHE_PATH


def test_nested_table_spans_are_walked_by_depth():
    """A lazy close would end the outer span at the INNER «/TABLE», handing the
    key a half table and the render a torn one."""
    inner = "«TABLE[cols:1»«TR»«TD»i«/TD»«/TR»«/TABLE»"
    body = f"x«TABLE[cols:2»«TR»«TD»{inner}«/TD»«/TR»«/TABLE»y"
    spans = list(markers.iter_table_spans(body))
    assert len(spans) == 1, "nested table must not surface as a second span"
    off, span = spans[0]
    assert body[off:off + len(span)] == span
    assert span.endswith("«/TR»«/TABLE»") and inner in span
    assert markers.table_cols(span) == 2      # the OUTER table's params


def test_unterminated_open_does_not_hide_a_later_table():
    body = "«TABLE[cols:3» oops, never closed «TABLE[cols:4»«TR»«/TR»«/TABLE»"
    spans = [s for _o, s in markers.iter_table_spans(body)]
    assert len(spans) == 1 and markers.table_cols(spans[0]) == 4
