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
