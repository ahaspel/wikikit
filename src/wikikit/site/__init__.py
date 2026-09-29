"""The reading site: the engine's pages, filled with the book's names, served
beside the book's own pages.

The ENGINE owns the pages that read what it exports — the article viewer (the
sole decoder of the export's markup), the article index and search, the
contributors list, the page-scan viewer, and their scripts.  They live in
`pages/` with `@@name@@` where the book's names go.  The BOOK owns everything
else a reader sees (its home page, prefaces, guides) in its `site_dir`.

A served file is always produced by `page(name)`.  `assemble_site()` writes
every one of them to the directory deploy uploads; the development server
calls `page()` per request.  One path, so what the development server shows
is what deploy ships.
"""
