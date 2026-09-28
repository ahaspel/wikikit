"""EPUB emitter — package the corpus-proven Python render as a browsable EPUB.

The content reuses render.article (the shared, corpus-proven renderer); this layer adds the
EPUB-target concerns: XML-well-formed XHTML, image bundling, the two-TOC navigation (volume
browse + topic hierarchy) over a volume/page-ordered spine, and the OPF/nav/container package.
"""
