"""A book's site may live under a path of its host (the DNB, at
reference.aaronhaspel.com/dnb), so every root-relative address the engine
writes — in its pages, in the export, and in the local server's routing —
starts with the site's root.  At a domain's root that is "" and the addresses
are exactly what they were."""
from types import SimpleNamespace

import pytest

from fixture_book import BARE, UNDER_A_PATH
from wikikit.render.inline import _article_url, commons_url, images_url, site_url
from wikikit.site.build import fill
from wikikit.site.serve import Handler

UNDER = ["fixture_book:UNDER_A_PATH"]


def test_the_root_is_the_path_of_the_site_address():
    assert BARE.root == ""
    assert UNDER_A_PATH.root == "/fixture"


def test_at_a_domains_root_addresses_are_unchanged():
    assert _article_url("01-0001-abc123.json") == "/article/01-0001-abc123"
    assert images_url() == "/data/images/"
    assert fill('<a href="@@root@@/index.html">', "t") == '<a href="/index.html">'


def test_a_resolved_link_lands_on_its_section():
    from wikikit.render.inline import decode_inline
    assert _article_url("25-0547-dd33a0.json", section="history") == \
        "/article/25-0547-dd33a0#section-history"
    html = decode_inline("see «LN:25-0547-dd33a0.json|Spain#history|Spain«/LN», and "
                         "«LN:18-0882-dd33a0.json|Morocco|Morocco«/LN»")
    assert 'href="/article/25-0547-dd33a0#section-history"' in html
    assert 'href="/article/18-0882-dd33a0"' in html


def test_an_apostrophe_is_no_section():
    # escaped for the page, the apostrophe is `&#39;`: its `#` read as the
    # section's sent 262 links to "#section-39-s" (2026-11-04)
    from wikikit.render.inline import decode_inline
    html = decode_inline("«LN:14-0234-a17a53.json|Ibn Khallikān's|Ibn Khallikān's«/LN»", escape=True)
    assert 'href="/article/14-0234-a17a53"' in html
    html = decode_inline("«LN:24-0802-e2cb62.json|Shakespeare#love's-labour's-lost|it«/LN»", escape=True)
    assert 'href="/article/24-0802-e2cb62#section-love-s-labour-s-lost"' in html


@pytest.mark.parametrize("corpus", UNDER, indirect=True)
def test_under_a_path_every_address_carries_it(corpus):
    assert _article_url("01-0001-abc123.json") == "/fixture/article/01-0001-abc123"
    assert commons_url("A b.png").startswith("/fixture/data/images/")
    assert site_url("/contributors.html#x") == "/fixture/contributors.html#x"
    assert fill('<script src="@@root@@/article-urls.js"></script>', "t") == \
        '<script src="/fixture/article-urls.js"></script>'
    assert fill('const B = "@@root|js@@/data/articles";', "t") == \
        'const B = "/fixture/data/articles";'


@pytest.mark.parametrize("path, under", [
    ("/fixture", ("/", "", "")),
    ("/fixture/", ("/", "", "")),
    ("/fixture/article/01-0001-abc123/x?q=y", ("/article/01-0001-abc123/x", "?", "q=y")),
    ("/fixturex/index.html", (None, "", "")),       # a sibling, not the site
    ("/index.html", (None, "", "")),                # outside the root
])
def test_the_server_answers_only_under_the_root(path, under):
    handler = SimpleNamespace(root="/fixture", path=path)
    assert Handler._site_path(handler) == under


def test_at_a_domains_root_the_server_sees_every_path():
    handler = SimpleNamespace(root="", path="/article/x?q=1")
    assert Handler._site_path(handler) == ("/article/x", "?", "q=1")
