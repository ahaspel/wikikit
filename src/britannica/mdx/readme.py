"""Reader-facing instructions shared by complete and sample dictionary builds."""


def edition_readme(sample=False, native_search=False):
    label = 'sample' if sample else 'complete edition'
    basename = 'Britannica11-sample' if sample else 'Britannica11'
    help_word = 'Britannica 11 sample' if sample else 'Britannica 11'
    text = f'# Britannica 11 — {label}\n\n'
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
        text += ('This 13-article sample exercises illustrations, equations, tables, Unicode, '
                 'footnotes and ambiguous titles. Try ALGEBRA, ALPHABET, MERCURY and '
                 'Continued Fraction. Links outside the sample are explicitly marked online. '
                 'Remove the sample from your reader when installing the complete edition.\n\n')
    else:
        text += ('Includes all 37,225 nonempty article and plate records, contributors, topics, '
                 'volume lists, front matter and the Reader’s Guide.\n\n')
    text += ('In GoldenDict-ng, enable Ignore diacritics for accent-free lookup. '
             'Full-text search is available after initial indexing (Ctrl+Shift+F). '
             'Search presentation and ordering depend on the reader. ')
    if not native_search:
        text += ('Alternative spellings and name orders are ordinary dictionary aliases; '
                 'your reader may show more than one alias for the same article. ')
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
