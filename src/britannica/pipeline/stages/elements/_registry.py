"""Element bookkeeping: placeholder counter, today's three-pass
``ElementRegistry``, and the post-refactor ``ClassifiedElement``
record.

The counter lives at module scope, not on each registry instance, so
keys remain unique across every registry created in a single processing
run.  Per-instance counters caused silent collisions: an inner
registry's ``ELEM:1`` matched the outer registry's ``ELEM:1``, and a
stale inner placeholder (e.g. ``<ref>`` inside ``<poem>`` inside a
table) could be substituted with the wrong content.

This module is the single source of truth — every caller that imports
``ElementRegistry`` via ``britannica.pipeline.stages.elements`` shares
the same ``_placeholder_counter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Placeholder uses \x03 (ETX) which is stripped by clean_pages anyway,
# so any leaked placeholders won't survive to export.
_PH = "\x03"


_placeholder_counter = 0


def _next_placeholder_id() -> int:
    global _placeholder_counter
    _placeholder_counter += 1
    return _placeholder_counter


@dataclass
class ElementRegistry:
    """Stores extracted elements keyed by placeholder strings.

    The pipeline runs three passes over the tree of extracted
    elements, each pass populating a parallel field on this object:

      * `elements` — (element_type, raw bytes) — set by the walker
        when it first registers an extracted element.  This is the
        only field every caller has historically destructured, so the
        tuple shape is preserved.
      * `inners` and `inner_registries` — set by the walk pass when
        it recurses into a non-leaf element.  `inners[placeholder]`
        is the delimiter-stripped content with child placeholders;
        `inner_registries[placeholder]` is the child registry built
        by recursing on that inner content.
      * `labels` — set by the classify pass.  `labels[placeholder]`
        is the producer-dispatch label assigned by `classify()`
        after seeing the element and its already-classified children.
      * `markers` — set by the produce pass.  `markers[placeholder]`
        is the final marker string the producer emitted.

    Uses a module-level counter so placeholder keys are unique
    across every registry instance in a processing run.
    Per-instance counters caused silent collisions where an inner
    `ELEM:1` matched an outer `ELEM:1` and substitution swapped
    content between the two locations.
    """
    elements: dict[str, tuple[str, str]] = field(default_factory=dict)
    inners: dict[str, str] = field(default_factory=dict)
    inner_registries: dict[str, "ElementRegistry"] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    markers: dict[str, str] = field(default_factory=dict)

    def add(self, element_type: str, raw: str) -> str:
        """Add an element to the registry, return its placeholder."""
        counter = _next_placeholder_id()
        key = f"{_PH}ELEM:{counter}{_PH}"
        self.elements[key] = (element_type, raw)
        return key


# Producer-dispatch labels emitted by `_classify_table` for brace-pipe
# wikitable source-type elements.  Siblings that need to ask "is this
# child any kind of wikitable?" check label membership against this
# set — the shape itself is the walker's private business.
#
# Must contain EVERY label emitted by `_TABLE_PREDICATES`.  When you
# add a new wikitable sub-classification (CAPTIONED_FIGURE_GRID,
# LEGEND, etc.), add it here too — otherwise the legacy-registry
# bridge maps it to its own name rather than "TABLE", and parent
# wikitables miss it when checking for nested-table children.
TABLE_LABELS: frozenset[str] = frozenset({
    "LAYOUT_WRAPPER", "TABLE",
    "CHEMISTRY_LAYOUT", "DJVU_CROP",
    "CAPTIONED_FIGURE", "CAPTIONED_FIGURE_INLINE",
    "FIGURE_GROUP", "UNPAIRED_FIGURE_GROUP",
    "LEGENDED_FIGURE", "LEGENDED_FIGURE_BESIDE",
    "LEGENDED_FIGURE_CHILD",
})

# Block-level child labels — anything that renders as its own
# paragraph.  Used by table producers to decide whether to unwrap a
# single-row table to inline text.  ("TABLE" arrives via TABLE_LABELS
# and covers both `{|` and `<table>` — the old HTML_TABLE label is gone.)
BLOCK_LABELS: frozenset[str] = frozenset({"POEM"}) | TABLE_LABELS

# Image-shaped child labels — File-bracket image elements regardless of
# whether the walker recognised an inline-prose context (INLINE_IMAGE) or
# a block / structural context (IMAGE).  Figure-table producers and
# image-counters key off the SHAPE (image element), not the alignment;
# layout in a cell is the container's job, so both labels are equally
# "an image child" to anything iterating children.
IMAGE_LABELS: frozenset[str] = frozenset({"IMAGE", "INLINE_IMAGE", "DJVU_CROP"})


@dataclass
class ClassifiedElement:
    """The classifier's output for one element — replaces the
    parallel dicts of ``ElementRegistry`` once the walker / classifier
    / producer pipeline is restructured.

    Under the post-refactor architecture the walker emits
    ``(shape, raw_bytes)`` one level at a time and the classifier
    drives the recursion: at each level it strips the shape's outer
    delimiters, hands the inner back to the walker for next-level
    extraction, recursively classifies each child the walker returns,
    and finally decides its own ``label`` from the shape, the
    identifier in its bytes, and the labels of its children.  A
    ``ClassifiedElement`` is what the classifier returns from that
    recursion.

    Today's five parallel dicts collapse to one record per
    placeholder:

      * ``elements[ph] = (etype, raw)``           → ``raw`` + walker shape (gone)
      * ``inners[ph]``                              → ``inner_text``
      * ``inner_registries[ph]``                    → ``inner_registry``
      * ``labels[ph]``                              → ``label``
      * ``markers[ph]``                             → ``marker``

    The shape itself is the walker's private vocabulary and is not
    retained on the classifier's output — by the time a
    ``ClassifiedElement`` exists, the question "what is this" has
    been answered by ``label``.

    Producers run as a bottom-up pass over the
    ``ClassifiedElement`` tree and fill in ``marker``.
    """

    label: str
    raw: str
    inner_text: str
    inner_registry: dict[str, "ClassifiedElement"] = field(default_factory=dict)
    marker: str = ""
