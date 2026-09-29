# wikikit

An engine for turning a reference work transcribed at [Wikisource](https://wikisource.org)
into a faithful digital edition: a website's article corpus, TEI-XML, an EPUB,
and an offline dictionary for GoldenDict and other MDict readers.

It was extracted from [britannica11.org](https://britannica11.org), the edition
of the *Encyclopædia Britannica*, Eleventh Edition (1910–1911), where it was
built and where it is still exercised end to end.

## The rule

**The engine never asks which book it is building.**  Everything a particular
book does differently — how its pages are named, where its articles begin,
which leaves are plates, how it credits contributors, its front matter, its
name — is supplied by the book, through a profile (`wikikit.corpora.Corpus`)
and a handful of hooks.  A book is a Python package in its own repository,
selected by that repository's committed `book.env`:

```
WIKIKIT_CORPUS=eb1911:EB1911
```

## This repository's tests

The engine's own suite runs against `tests/fixture_book.py`, a profile that
carries nothing of any real book: no data, no hooks, no branding.  A test that
passes here tests the engine and nothing else.  The engine's integration tests
— boundary detection, the regression fixtures, the full builds — need a real
book's rules and data, and live in that book's repository.

```
uv sync
uv run pytest
```

## Licence

The code is released under the MIT licence (see `LICENSE`).

Some test fixtures and snapshots quote text transcribed at Wikisource from the
*Encyclopædia Britannica*, Eleventh Edition.  The 1911 text is in the public
domain; Wikisource's transcriptions are available under the Creative Commons
Attribution-ShareAlike licence (CC BY-SA 4.0), and those fixtures are provided
under the same terms.
