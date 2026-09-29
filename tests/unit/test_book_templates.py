"""`Corpus.template`: how the engine fills a book's prose — `$name`
placeholders, strict substitution, any braces allowed.  Split from
test_tei_readme.py, whose book half checks EB1911's README itself; these run
against the fixture book with a template written for the test."""
import dataclasses

import pytest

from fixture_book import BARE

README = "templates/tei_readme.md"


def test_prose_may_contain_any_braces(tmp_path):
    """The property, not the instance: a template may hold any braces."""
    (tmp_path / "templates").mkdir()
    hostile = 'Count $article_count.\n{"json": {"nested": [1, 2]}} \\cite{foo{bar}} {n} {0} {}'
    (tmp_path / "templates" / "tei_readme.md").write_text(hostile, encoding="utf-8")
    book = dataclasses.replace(BARE, data_files=frozenset({README}), data_dir=str(tmp_path))
    out = book.template(README, article_count="37,225")
    assert out.startswith("Count 37,225.")
    assert "{n}" in out and "{}" in out and "foo{bar}" in out


def test_an_unsupplied_placeholder_is_an_error_not_a_hole(tmp_path):
    """Strict substitution: a placeholder the engine does not fill must raise,
    never reach a reader as a literal `$site`."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "tei_readme.md").write_text("See $site.", encoding="utf-8")
    book = dataclasses.replace(BARE, data_files=frozenset({README}), data_dir=str(tmp_path))
    with pytest.raises(KeyError):
        book.template(README)
