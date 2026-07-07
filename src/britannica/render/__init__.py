"""Marker → HTML rendering in Python (the render-to-Python arc, project_render_to_python).

One marker parser + per-target emitters, reproducing the viewer's decode exactly
(verified by corpus diff against the jsdom golden, UNEXPECTED=0), so the same renderer
can emit site HTML and EPUB XHTML.  `inline` is the port of the viewer's
`decodeInlineMarkers`; the block/shell layers build on it.
"""
from britannica.render.inline import decode_inline, escape_html

__all__ = ["decode_inline", "escape_html"]
