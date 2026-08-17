"""A measurement cache's key and path have ONE owner, shared by object.

Two content-addressed caches decide what the reader sees: the measured math
widths (`«MATH[fs=N]` / popout) and the measured table widths (the Expand
figure).  Each is WRITTEN by a build-time tool and READ by the pipeline, and
each used to spell its own key function and its own cache path on both sides.
Nothing failed loudly when a pair drifted — the reader simply looked up keys
the writer had never written, found nothing, and every hint quietly vanished.
That is not hypothetical: the 2026-08-16 rebuild lost 194 tables' Expand hints
to a key that no longer addressed what had been measured.

So the ratchet is identity, not equality: the writer must hold the SAME object
as the reader.  Two functions that merely compute the same digest today are
exactly the state this campaign removed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in ("src", "tools/diagnostics", "tools/pipeline"):
    sys.path.insert(0, str(ROOT / _p))

from britannica import markers, math_widths, table_widths  # noqa: E402


def test_math_writer_and_reader_share_one_key_and_path():
    import measure_math_widths as writer
    assert writer.cache_key is math_widths.cache_key
    assert writer.CACHE_PATH is math_widths.CACHE_PATH


def test_table_writer_and_reader_share_one_key_and_path():
    import annotate_table_markers as annotator
    import measure_table_widths as writer
    import post_export
    assert writer.span_key is table_widths.span_key
    assert annotator.span_key is table_widths.span_key
    assert writer.CACHE is table_widths.CACHE_PATH
    assert annotator.CACHE is table_widths.CACHE_PATH
    assert post_export.TABLE_WIDTHS_CACHE is table_widths.CACHE_PATH


def test_span_key_is_stable_under_annotation():
    """THE property the cache rests on: annotating a corpus must not move the
    keys it was measured under, or the next measure re-measures everything and
    the annotator strips every hint it can no longer look up."""
    span = "«TABLE[cols:7|style:x»«TR»«TD»a«/TD»«/TR»«/TABLE»"
    stamped = markers.set_table_wide(span, True)
    assert "|wide" in stamped and stamped != span
    assert table_widths.span_key(stamped) == table_widths.span_key(span)
    assert markers.set_table_wide(stamped, False) == span
    assert markers.set_table_wide(stamped, True) == stamped   # idempotent


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
    assert not markers.table_is_wide(span)


def test_unterminated_open_does_not_hide_a_later_table():
    body = "«TABLE[cols:3» oops, never closed «TABLE[cols:4»«TR»«/TR»«/TABLE»"
    spans = [s for _o, s in markers.iter_table_spans(body)]
    assert len(spans) == 1 and markers.table_cols(spans[0]) == 4
