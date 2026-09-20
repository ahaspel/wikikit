"""Per-article processing context threaded through element handlers.

Replaces the loose ``context: dict`` that used to be passed around.
Carries the small amount of cross-element state a handler may need:

  * ``volume`` — for score / chart-image lookups that key on physical location.
  * ``ref_bodies`` — name → resolved-footnote-body map, built once per
    article by ``resolve_ref_bodies`` and consumed by ``<ref name=X/>``
    anchors.

The Author-link producer no longer needs a roster here: it classifies an
``[[Author:…]]`` display by PATTERN (is it an initials form?), so the roster can
be built after the walk ([[project_roster_from_author_links]]).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ElementContext:
    volume: int = 0
    ref_bodies: dict[str, str] | None = None
    # PER-NODE (not article-constant): the label of the node this one hangs under,
    # threaded down by ``produce_tree`` on an immutable per-level copy.  Lets a
    # producer read its own parent — the BODY producer keys on it to tell a verse
    # line break (parent POEM/PPOEM → «BR») from a prose soft-wrap (→ space).
    parent_label: str | None = None
    # PER-NODE, threaded the same way ``parent_label`` is: the immediately
    # PRECEDING sibling's label and produced marker.  Siblings are produced in
    # order and each marker is populated before the next handler runs, so the
    # left neighbour is genuinely available — no look-ahead, no second pass.
    #
    # The BODY producer is the only consumer.  It owns the "blank line → «P»"
    # decision, and that decision is not knowable from the run alone: a blank
    # line separating a display block from the prose after it says nothing about
    # whether a paragraph starts there, because a transcriber types one around a
    # template either way (measured: after `{{center|…}}` a blank line precedes a
    # sentence-continuation 26% of the time and a single newline 41% — both
    # buckets mixtures).  What the sentence was doing when the block interrupted
    # it is the fact that settles it, and only the left neighbour carries it.
    prev_label: str | None = None
    prev_marker: str = ""
    # The left neighbour's RAW too, because one shape does not identify itself in
    # its marker: `{{EB1911 fine print/e}}` is its own element and produces bare
    # «/DIV», indistinguishable from `{{outdent/e}}`.  Which wrapper closed is
    # only in the raw, and the BODY producer needs it to tell a NOTE ending from
    # any other div ending.
    prev_raw: str = ""
    # STICKY (set once a TABLE/REF ancestor is entered, inherited by every descendant):
    # this node's content is decoded wholesale by ``decode_inline`` (a table is one
    # decode pass; a footnote body is another), so a verse / outline here must render in
    # its INLINE form (span / plain <li>), never a top-level block (blockquote / <p>-item).
    # The producer stamps the block-vs-inline marker off this; the render decodes it
    # mechanically, no context re-inference.  Threaded by ``produce_tree``.
    inline: bool = False
