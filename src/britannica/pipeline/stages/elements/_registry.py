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

    Uses a module-level counter so keys are unique across every
    registry instance in a processing run.  Per-instance counters
    caused silent collisions: an inner registry's ``ELEM:1``
    matched the outer registry's ``ELEM:1``, so if a stale inner
    placeholder escaped unreplaced (e.g. ``<ref>`` inside ``<poem>``
    inside a table), the outer substitution pass would substitute
    the outer ``ELEM:1``'s processed content into the inner's
    location — duplicating content across the article.
    """
    elements: dict[str, tuple[str, str]] = field(default_factory=dict)

    def add(self, element_type: str, raw: str) -> str:
        """Add an element to the registry, return its placeholder."""
        counter = _next_placeholder_id()
        key = f"{_PH}ELEM:{counter}{_PH}"
        self.elements[key] = (element_type, raw)
        return key
