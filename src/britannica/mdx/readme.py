"""Reader-facing instructions shared by complete and sample dictionary builds.

The SENTENCES are the engine's — how an MDX is installed, what GoldenDict does,
what works offline — and are the same for every book.  The book's own nouns come
from its declared `mdx_phrases.json`: its accent example, its name-order example,
what its sample exercises and which titles to try, and what an edition holds
besides its articles.  A whole-README template per book would copy these
instructions into every book, where they would drift apart.
"""


def phrases() -> dict[str, str]:
    """The book's phrases.  Asking for one the book lacks is a KeyError — a
    missing phrase must not become a hole in a shipped README."""
    import json
    from britannica.corpora import current_corpus
    return json.loads(current_corpus().data("mdx_phrases.json").read_text(encoding="utf-8"))


def edition_readme(sample=False, native_search=False, *, article_count):
    from britannica.corpora import brand
    from britannica.mdx.build import dictionary_basename, help_word as _help_word
    p = phrases()
    label = 'sample' if sample else 'complete edition'
    basename = dictionary_basename(sample)
    help_word = _help_word(sample)
    text = f'# {brand("short_name")} — {label}\n\n'
    if native_search:
        text += ('This is the dictionary component of the optional enhanced-search edition. '
                 'Its alias matching requires the accompanying search helper. '
                 'For ordinary MDX installation, choose the standard MDX download instead.\n\n')
    else:
        text += ('This is the standard MDX edition for an existing dictionary reader. '
                 'No installer, search helper, Python, or Node is required. '
                 'The reader application is not included.\n\n')
    text += (f'Extract the ZIP. Keep `{basename}.mdx` and `{basename}.mdd` together, '
             'add their folder to your reader’s dictionary sources, and rescan. '
             f'Look up `{help_word}` for contents and navigation.\n\n'
             'Validated in GoldenDict-ng 26.8.0 on Windows. The files use the standard MDX/MDD '
             'format and are not tied to Windows; other readers and operating systems have '
             'not been verified. Try the sample in your own reader before choosing the full edition.\n\n')
    if sample:
        text += (f'This {article_count:,}-article sample exercises {p["sample_exercises"]}. '
                 f'Try {p["sample_try"]}. Links outside the sample are explicitly marked online. '
                 'Remove the sample from your reader when installing the complete edition.\n\n')
    else:
        text += (f'Includes all {article_count:,} nonempty article and plate records, '
                 f'{p["edition_contents"]}.\n\n')
    text += (f'Accent-free spellings are indexed directly, so {p["accent_example"]} with no '
             'setting changed; GoldenDict-ng’s Ignore diacritics option additionally folds '
             'accents in what you type. '
             'Full-text search is available after initial indexing (Ctrl+Shift+F). '
             'Search presentation and ordering depend on the reader. ')
    if not native_search:
        text += ('Almost every article has a single headword. A few carry one extra spelling '
                 'deliberately — an accent-free form, or a name order the book does not print, '
                 f'such as {p["name_order_example"]} — so that typing it in full still finds '
                 'the article. ')
    text += ('Reading, illustrations, mathematics and internal navigation work offline. '
             'Explicitly external links require an internet connection.\n\n'
             'See LICENSE for attribution, manifest.json for coverage, SHA256SUMS for file '
             'checksums, and source-link-issues.json for the known unavailable source links.\n')
    if native_search:
        text += ('\nAdvanced setup: with Python 3 and Node.js 22.13+ installed, close GoldenDict '
                 'and run `python search/install.py --reader "PORTABLE GOLDENDICT FOLDER" '
                 '--node "PATH TO node.exe"`. The enhanced Windows download packages this '
                 'setup separately with a private runtime and graphical installer.\n')
    return text
