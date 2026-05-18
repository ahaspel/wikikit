"""ElementRegistry — placeholder bookkeeping for extracted elements.

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
