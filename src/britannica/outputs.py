"""Every finished output of the marker stream, and the format each one emits.

The article ``body`` is ONE marker stream with several converters over it.  This is
the list of them — the single place that answers "what comes out of the stream?" —
so the leak oracle and the quality report scan the same set and a converter cannot
be audited by one and missed by the other.

Adding a converter means adding it HERE.  That is the whole drift defence: the
oracle consults no handled-marker manifest, but it still has to be pointed at an
output, and scanning only ``rendered_html`` is the same blindness one level up.
`markdown.py` shipped a raw `«OUTLINE»` for five weeks because nothing looked at
what it emitted ([[project_leaked_markup_queue]]).

An output is text a READER or an AGENT consumes.  That is the line, and it is what
puts a contributor bio in and leaves an xref's surface out.

NOT here, deliberately:

  * ``xrefs[].surface_text`` — 8,072 raw `«LN»` and 674 `«AL»` in a 6,000-article
    sample, and correctly so: it is the source surface an xref was extracted FROM,
    kept as provenance for matching.  Nothing renders it.  Scanning it would report
    thousands of leaks that are the field doing its job.
  * ``epub.fts.tokens`` folds to ``[a-z0-9]+`` tokens that CANNOT carry a marker;
    its `«[^»]*»` sweep spans to the first `»`, so on a split marker
    (`«TITLE:Aardvark«/TITLE»`) it eats the payload instead of leaking it — LOSS,
    not leak ([[feedback_loss_vs_leak]]), which wants a token-preservation gate.
  * ``embeddings._clean_lead`` runs over a lead and feeds a vector, not a reader.
    Same loss-shaped risk.
"""
from britannica.export.markdown import body_to_markdown
from britannica.markers import markers_to_text


def outputs_for(payload: dict) -> tuple[tuple[str, str, str], ...]:
    """``(consumer, fmt, text)`` for one exported article payload.

    ``fmt`` is what :func:`britannica.render.leaks.find_leaks` needs to pick the
    checks that hold for this output.  ``rendered_html`` is READ rather than
    recomputed — re-rendering needs resolved xrefs, and the persisted field is the
    artifact that actually ships, so it is the one to judge.  The rest are pure
    functions of ``body`` and are run for real here, never approximated.
    """
    body = payload.get("body") or ""
    out = [
        ("rendered_html", "html", payload.get("rendered_html") or ""),
        ("markdown", "markdown", body_to_markdown(body)),
        ("search_text", "text", markers_to_text(body)),
        ("title", "text", payload.get("title") or ""),
    ]
    # Contributor bios: displayed prose, carried per article, and the ONE output
    # that isn't a converter over `body` — a `«BIOLINK:target|display«/BIOLINK»`
    # reaches it from the bio harvest and is meant to be reduced to its display.
    # An article can carry several, so they are separate entries under one name.
    for c in payload.get("contributors") or []:
        if c.get("description"):
            out.append(("contributor_bio", "text", c["description"]))
    return tuple(out)
