"""Reviewed pre-existing missing destinations; never silently discard a link.

These are present in canonical exports, independently of the MDX container.
Do not guess replacement articles from labels or synthesize section positions.
Only the exact audited links in the book's `link_exceptions.json` may become
visibly unresolved; new failures still stop the build. A repaired source anchor
automatically retires its entry.
"""
import html
import json
import re
from urllib.parse import unquote


def known() -> dict[str, str]:
    """The book's audited exceptions, ``{stable_id: missing section slug}``,
    from its declared ``link_exceptions.json``.  A book that declares none has
    none — every missing destination then stops the build, which is the rule."""
    from britannica.corpora import current_corpus
    book = current_corpus()
    if not book.has_data("link_exceptions.json"):
        return {}
    return json.loads(book.data("link_exceptions.json").read_text(encoding="utf-8"))


def mark_unavailable(entries):
    from britannica.mdx.build import Inventory, article_key, entry_url
    report = []
    for stem, slug in known().items():
        key, fragment = article_key(stem), 'section-' + slug
        if key not in entries or fragment in Inventory(entries[key]).ids:
            continue
        url = entry_url(key, fragment)

        def replace(m):
            if html.unescape(m[2]) != url:
                return m[0]
            report.append({'entry': key, 'destination': url, 'label_html': m[3],
                           'reason': 'Section destination absent from canonical export',
                           'treatment': 'Label retained; visibly marked unavailable'})
            return '<span class="unavailable-source-link">' + m[3] + ' <small>[source link unavailable]</small></span>'

        entries[key] = re.sub(r'''<a\b[^>]*\bhref=(["'])(.*?)\1[^>]*>(.*?)</a>''', replace, entries[key], flags=re.S)
    return report
