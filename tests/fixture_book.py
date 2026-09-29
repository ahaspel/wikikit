"""The engine's own test book: a profile with nothing of any real book's.

No data files, no hooks, no branding — the engine's tests must pass against
it, which is how we know they test the ENGINE and not EB1911.  (It is the book
wikikit's own `book.env` names; in a book's repository the engine tests run
under that book instead, and must pass there too.)

`NOT_A_BOOK` exists so the refusal of a non-profile can be tested without
naming a real book's internals.
"""
from wikikit.corpora import Corpus

BARE = Corpus(
    key="fixture",
    title="Fixture Book",
    scan_name=lambda v: f"Fixture Volume {v:02d}.djvu",
    pages={1: 10, 2: 12},
    raw_dir="fixture",
    derived_dir="data/fixture/derived",
    images_dir="data/fixture/images",
)

NOT_A_BOOK = {1: 10}
