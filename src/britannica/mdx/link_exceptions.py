"""Reviewed pre-existing missing destinations; never silently discard a link.

These are present in canonical exports, independently of the MDX container.
Do not guess replacement articles from labels or synthesize section positions.
Only the exact audited links below may become visibly unresolved; new failures
still stop the build. A repaired source anchor automatically retires its entry.
"""
import html
import re
from urllib.parse import unquote

KNOWN = {
    '20-0065-dd33a0': 'oil-testing',
    '22-0312-420571': 'phenomenon',
    '23-0289-33f6a9': 'givors',
    '24-0213-9266e5': 'brazil',
    '24-0213-a26d04': 'france',
    '24-0239-9f5d78': 'le-mans',
    '24-0263-f5bfef': 'chambery',
    '24-0617-181431': 'france',
    '24-0617-832b6f': 'france',
    '24-0618-4cc1a4': 'france',
    '24-0618-fd3431': 'france',
    '25-0408-55c98e': 'france',
    '26-0025-dd33a0': 'agriculture',
    '26-1087-ed99ba': 'japan-09-domestic-history',
    '28-0070-049d78': 'france',
    '28-0070-1880ab': 'france',
    '28-0071-f44e53': 'france',
    '28-0950-cdab30': 'france',
}


def mark_unavailable(entries):
    from britannica.mdx.build import Inventory, article_key, entry_url
    report = []
    for stem, slug in KNOWN.items():
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
