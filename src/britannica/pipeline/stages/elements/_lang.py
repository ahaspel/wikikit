"""Language / script producer — `{{greek|…}}`, `{{polytonic|…}}`, `{{hebrew|…}}`, ….

A script wrapper carries no presentation we render: the Unicode glyphs ARE the
content and the browser shapes them, so the producer's whole job is to unwrap to
its content and recurse.  That is the SAME output the old `{}` registry strip
gave — but here it is an explicit DECISION ("the glyphs are the text"), scoped to
the known script names, not a blind strip-by-name.  A wrapper that is NOT a known
script never reaches this producer; it leaks, which is the signal it needs a
producer of its own.
"""
from __future__ import annotations


def process_lang(raw: str, context) -> str:
    """Unwrap a script template to its bare content — the glyphs are the text."""
    from britannica.pipeline.stages.elements import process_elements
    body = raw.strip()
    if body.startswith("{{"):
        body = body[2:]
    if body.endswith("}}"):
        body = body[:-2]
    _name, _sep, content = body.partition("|")
    return process_elements(content, context).strip()
