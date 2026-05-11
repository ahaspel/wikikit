"""Per-article processing context threaded through element handlers.

Replaces the loose ``context: dict`` that used to be passed around.
Carries the small amount of cross-element state a handler may need:

  * ``volume`` / ``page_number`` — for score / chart-image lookups that
    key on physical location.
  * ``ref_bodies`` — name → resolved-footnote-body map, built once per
    article by ``_resolve_ref_bodies`` and consumed by ``<ref name=X/>``
    anchors.
  * ``djvu_crop_counters`` — per ``(volume, page)`` running index so
    successive ``{{Css image crop}}`` templates get distinct local
    filenames matching ``download_djvu_crops.py`` ordering.  Mutated
    in place by ``_process_djvu_crop``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ElementContext:
    volume: int = 0
    page_number: int = 0
    ref_bodies: dict[str, str] = field(default_factory=dict)
    djvu_crop_counters: dict[tuple[int, int], int] = field(default_factory=dict)
