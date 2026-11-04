"""Pre-deploy gate: every article link that names a section lands on it.

A link `/article/X#section-Y` must find an element `id="section-Y"` in X's
rendered page.  Nothing checked this, and on 2026-11-04 329 of the site's
2,033 section links named an anchor that was not there — each opened the
right article at its top, which nobody sees as broken.  Two causes: the
render read a section from the HTML-escaped title, where an apostrophe's
`&#39;` supplied the `#` (262 links to "#section-39-s"), and the resolver
handed out a fragment's heading without asking whether the article has it
(SARTHE's "#Le Mans", NUMBER#24: 67).  The MDX build's own link check is
what caught them, a derivative later and a deploy too late.

No exceptions file: an anchor that is not there is never right — the fix is
the binding (no section, or the right one), never a list of tolerated misses.

Usage:
    uv run python -m wikikit.diagnostics.check_section_links
"""
from __future__ import annotations

import collections
import html
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

from wikikit.export.corpus import load_corpus     # noqa: E402
from wikikit.render.inline import site_url        # noqa: E402

_ID_RE = re.compile(r'\bid="([^"]+)"')
_HREF_RE = re.compile(r'href="([^"#]*)#(section-[^"]*)"')


def audit():
    """-> (missing [(article title, target stem, fragment)], links checked)."""
    payloads, _ = load_corpus()
    ids = {p.stem: set(_ID_RE.findall(d.get("rendered_html") or "")) for p, d in payloads.items()}
    prefix = site_url("/article/")
    missing, n = [], 0
    for p, d in payloads.items():
        for path, frag in _HREF_RE.findall(d.get("rendered_html") or ""):
            if not path.startswith(prefix):
                continue
            n += 1
            stem, frag = path[len(prefix):], html.unescape(frag)
            if frag not in ids.get(stem, ()):
                missing.append((d.get("title") or p.stem, stem, frag))
    return missing, n


def main() -> int:
    missing, n = audit()
    print(f"  {n} section link(s) in the rendered corpus")
    if not missing:
        print("  OK: every section link lands on its anchor.")
        return 0
    by_article = collections.Counter(m[0] for m in missing)
    print(f"\n  {len(missing)} section link(s) name an anchor their article does NOT have, "
          f"in {len(by_article)} article(s):")
    for title, stem, frag in missing[:40]:
        print(f"    {title[:40]:<40} -> {stem}#{frag}")
    if len(missing) > 40:
        print(f"    … and {len(missing) - 40} more")
    print("\n  Fix the binding: a section is written only where the target has that heading.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
