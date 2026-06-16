"""Shared string utilities."""

import re


def section_slug(name: str) -> str:
    """URL-safe slug from a wikisource section name (or any string).

    Preserves ASCII letters/digits, lowercases, collapses runs of other
    chars to a single hyphen. Strips surrounding hyphens.
    """
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


_MARKER_RE = re.compile(r"«/?[A-Za-z]+(?:\[[^\]]*\])?»")


def strip_markers(s: str) -> str:
    """Drop `«…»`-style markers, leaving the plain display text.

    Shared by the shoulder producer (to mint a slug from a heading's text)
    and export (to read a heading's title) so both see the same plain text —
    one regex, not a copy per caller.
    """
    return _MARKER_RE.sub("", s)
