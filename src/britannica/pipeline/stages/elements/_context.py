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
    # STICKY (set once a TABLE/REF ancestor is entered, inherited by every descendant):
    # this node's content is decoded wholesale by ``decode_inline`` (a table is one
    # decode pass; a footnote body is another), so a verse / outline here must render in
    # its INLINE form (span / plain <li>), never a top-level block (blockquote / <p>-item).
    # The producer stamps the block-vs-inline marker off this; the render decodes it
    # mechanically, no context re-inference.  Threaded by ``produce_tree``.
    inline: bool = False
