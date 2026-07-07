"""Browser-equivalent HTML normalization — the render port's "browser fixup".

The viewer emits open-only wrapper tags («P»→<p>, independent «DIV»/«SPAN» open/close)
and leans on the browser's HTML5 tree-construction to pair, nest, and close them; the
golden is `app.innerHTML` read back — that serialization.  The Python renderer builds the
SAME open-only template, then normalizes here.  html5lib is HTML5-spec (the same spec as
jsdom's parse5), so its parse+serialize matches the golden's tree-construction (auto-close,
nesting, entity normalization) — and yields valid closed markup for EPUB XHTML.

Dev/verify dependency only (see pyproject [project.optional-dependencies].dev); the core
render.article module stays importable without html5lib.
"""
import html5lib
from html5lib.serializer import HTMLSerializer
from html5lib.treewalkers import getTreeWalker

_WALKER = getTreeWalker("etree")


def normalize_html(fragment):
    """Parse an HTML fragment and re-serialize it the way a browser would."""
    dom = html5lib.parseFragment(fragment, treebuilder="etree")
    ser = HTMLSerializer(quote_attr_values="always", omit_optional_tags=False)
    return ser.render(_WALKER(dom))
