"""One extracted cross-reference, in flight.

An xref is TRANSIENT: the extractor reads it out of a body, the resolver binds a
target onto it, the panel + the body-linker read it, and it is then serialized
into the article JSON and ``xref_resolution.jsonl``.  It is never persisted.

It used to be a SQLAlchemy model (``cross_references``) from a design where
resolution happened in the database.  That design is gone — the whole xref graph
lives in the exported JSONs and the resolution snapshot — and the table sat empty
through every rebuild while the pipeline still truncated it per volume.  The
table is deleted; this is the value object that was always the real thing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Xref:
    """A reference found in ``article_id``'s body, with whatever the resolver
    bound onto it.  ``target_article_id``/``target_section`` are None until
    resolution; ``status`` is ``resolved`` | ``unresolved``."""

    article_id: int
    surface_text: str
    normalized_target: str
    xref_type: str
    target_article_id: int | None = None
    target_section: str | None = None
    status: str = "unresolved"
