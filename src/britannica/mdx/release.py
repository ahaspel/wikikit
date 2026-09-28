"""Assemble the standard and enhanced downloads.

Reads existing validated builds; does not rebuild source data or publish files.
python -m britannica.mdx.release
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from britannica.mdx.checksums import (
    format_checksums, sha, write_checksums, write_shipped_text)
from britannica.mdx.windows import build as build_windows
from britannica.mdx.readme import edition_readme


def package_standard(source, destination, sample):
    """Refresh reader instructions without recompiling the verified MDX/MDD."""
    checksums = {}
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as target:
        for name in original.namelist():
            if name in ('README.md', 'SHA256SUMS'):
                continue
            digest = hashlib.sha256()
            # Carry each member's timestamp from the verified build.  Opening a
            # member by NAME stamped it 1980-01-01 (zipfile's floor date), so
            # every file in the download claimed to predate the reader's index
            # of the previous release, and GoldenDict kept that index: the
            # reviewer saw the new icon but the OLD "About dictionary" text.
            info = zipfile.ZipInfo(name, date_time=original.getinfo(name).date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            with original.open(name) as incoming, target.open(info, 'w', force_zip64=True) as outgoing:
                while chunk := incoming.read(1024*1024):
                    digest.update(chunk)
                    outgoing.write(chunk)
            checksums[name] = digest.hexdigest()
        # The count of what THIS archive holds, from its own manifest.
        count = json.loads(original.read('manifest.json'))['article_count']
        readme = edition_readme(sample, False, article_count=count).encode('utf-8')
        target.writestr('README.md', readme)
        checksums['README.md'] = hashlib.sha256(readme).hexdigest()
        target.writestr('SHA256SUMS', format_checksums(checksums))


def expected_article_count(sample):
    """How many articles an edition must hold — from the book, never from the
    archive: the sample's declared selection, or the exported corpus index less
    the records the source leaves empty (which the dictionary excludes)."""
    from britannica.corpora import current_corpus
    from britannica.mdx.build import ROOT, _sample_spec
    book = current_corpus()
    if sample:
        return len(json.loads(_sample_spec().read_text(encoding='utf-8'))['articles'])
    index = json.loads((ROOT / book.derived('articles', 'index.json')).read_text(encoding='utf-8'))
    return len(index) - len(book.empty_records)


def verify_archive(archive, *, sample, enhanced):
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate ZIP members: '+str(archive))
        listed = {}
        for line in z.read('SHA256SUMS').decode('utf-8').splitlines():
            expected, name = line.split('  ', 1)
            if name in listed or name.startswith(('/', '\\')) or '..' in Path(name).parts:
                raise ValueError('Invalid archive member: '+name)
            with z.open(name) as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError('Archive checksum mismatch: '+name)
            listed[name] = expected
        if set(names) != set(listed) | {'SHA256SUMS'}:
            raise ValueError('Unlisted archive files')
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['edition'] == ('sample' if sample else 'complete')
        assert bool(manifest.get('native_search')) == enhanced
        assert manifest['article_count'] == expected_article_count(sample)
        assert not any(Path(name).name.lower() == 'goldendict.exe' for name in names)
        # EXPECTED names come from the book being built, never from the archive's
        # own manifest: an archive checked against itself would always agree.
        from britannica.corpora import brand
        from britannica.mdx.build import book_identity, dictionary_basename
        from britannica.mdx.windows import installer_exe
        assert manifest['book'] == book_identity(sample)
        if enhanced:
            stem = brand('file_stem')
            assert {stem+'.mdx', stem+'.mdd', installer_exe(book_identity(sample)),
                    'search/titles.json', 'search/articles.sqlite', 'search/book.json',
                    'search/runtime/node.exe'} <= set(names)
            assert json.loads(z.read('installation.json'))['edition'] == manifest['edition']
        else:
            base = dictionary_basename(sample)
            assert set(names) == {base+'.mdx', base+'.mdd', base+'.png', 'README.md', 'LICENSE',
                                  'manifest.json', 'source-link-issues.json', 'SHA256SUMS'}
        return manifest


_RELEASE_README = '''# $short_name dictionary downloads

**Choose the standard MDX edition for ordinary dictionary use.** Extract its MDX
and MDD together and add their folder to your existing reader. There is no
installer or additional runtime. GoldenDict is not included in any download.

| Download | What it provides |
|---|---|
| [Standard MDX]($file_stem-MDX.zip) | The complete dictionary with conventional lookup aliases. |
| [Enhanced search for Windows]($file_stem-Enhanced-Windows.zip) | The complete dictionary with one canonical title per article in GoldenDict's title suggestions. |

The standard format is independent of OS. Both editions have been tested with
GoldenDict-ng 26.8.0 on Windows; other readers and operating systems are not
verified. The enhanced package currently supports portable GoldenDict on 64-bit
Windows and includes its own search runtime. Python and Node need not be installed.

Both complete editions contain the same $article_count articles and plates, $contents.
The difference is lookup: the standard edition exposes ordinary aliases, which the reader may
show separately; enhanced search matches aliases internally and displays canonical
titles. GoldenDict controls final result ordering and full-text presentation.

**Choose one complete edition.** Enhanced search includes a different MDX plus
its helper; it is a complete alternative, not a helper-only add-on. Do not load
both editions at once. Its installer replaces the standard pair if installed in
the same portable reader's content directory. Remove any other copy from your
reader's sources to avoid duplicate dictionaries.

To return from enhanced search to the standard MDX, disable the
“$search_name” and “$short_name” entries under GoldenDict's Programs sources, replace the
MDX/MDD pair, and rescan. Keep the actual $short_name MDX dictionary enabled.

Each ZIP contains instructions, attribution and file checksums. Known unavailable
source links are inventoried in each edition. Reading and internal resources work
offline; explicitly external links require a connection. The Chrome selection-menu
extension is optional and distributed separately.

'''


def release_readme(article_count):
    """The release folder's README: the engine's account of the two editions,
    with the book's names and its `release_contents` phrase filled in.  The table
    of archives is appended by `assemble`."""
    from string import Template
    from britannica.corpora import brand
    from britannica.mdx.readme import phrases
    return Template(_RELEASE_README).substitute(
        short_name=brand('short_name'), file_stem=brand('file_stem'),
        search_name=brand('search_name'), article_count=f'{article_count:,}',
        contents=phrases()['release_contents'])


def assemble(standard, enhanced, output, node):
    # 13-article samples were dropped from the release 2026-09-15: the downloads
    # page should not hand a buyer a four-way choice.  `build.py --sample` still
    # builds one for QA; it is simply not a published download.
    a, b = [json.loads((p/'manifest.json').read_text(encoding='utf-8')) for p in (standard,enhanced)]
    for key in ('input_sha256','index_sha256','topics_sha256','article_count'):
        if a[key] != b[key]:
            raise ValueError(f'Editions use different source inputs: {standard}, {enhanced}: {key}')
    builds = output.parent/'release-builds'
    build_windows(enhanced, builds/'enhanced-windows', node)
    output.mkdir(parents=True, exist_ok=True)
    from britannica.corpora import brand
    stem = brand('file_stem')
    packages = [
        (f'{stem}-MDX.zip', standard/f'{stem}.zip', False, False),
        (f'{stem}-Enhanced-Windows.zip', builds/'enhanced-windows.zip', False, True),
    ]
    catalog = []
    for name, source, sample, enhanced_search in packages:
        manifest = verify_archive(source, sample=sample, enhanced=enhanced_search)
        target = output/name
        if enhanced_search:
            shutil.copy2(source, target)
        else:
            package_standard(source, target, sample)
        verify_archive(target, sample=sample, enhanced=enhanced_search)
        catalog.append(dict(file=name, bytes=target.stat().st_size, sha256=sha(target),
                            edition='enhanced-windows' if enhanced_search else 'standard-mdx',
                            sample=sample, article_count=manifest['article_count']))
        print('Verified', name, flush=True)
    introduction = release_readme(catalog[0]['article_count'])
    introduction += '| Archive | Size (MB) |\n|---|---:|\n' + ''.join(
        f'| {r["file"]} | {r["bytes"]/1_000_000:.1f} |\n' for r in catalog)
    write_shipped_text(output/'README.md', introduction)
    write_shipped_text(output/'release.json',
                       json.dumps({'downloads':catalog,'published':False},indent=2))
    files = [r['file'] for r in catalog] + ['README.md','release.json']
    write_checksums(output, files)
    print('Release downloads ready in', output, flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    # The DIRECTORIES say which edition they hold, and these defaults must
    # follow them.  They used to read `standard -> mdx/standard`, `enhanced ->
    # mdx/complete`, which was the original scheme; the builds moved to
    # `mdx/complete` (standard) and `mdx/complete-enhanced` (native search) and
    # these did not.  A default-args run would therefore have packaged the
    # STANDARD edition as "Enhanced" and taken "standard" from a directory
    # nothing builds into any more.  The releases actually shipped were correct
    # because they were run with explicit paths — which is luck, not a design.
    # Each edition's own `manifest.json` carries `native_search`; that is the
    # thing to check if these ever look wrong again.
    for name, default in [('standard', 'mdx/complete'),
                          ('enhanced', 'mdx/complete-enhanced'),
                          ('output', 'mdx/releases')]:
        parser.add_argument('--'+name,type=Path,default=Path(default))
    parser.add_argument('--node',type=Path,default=shutil.which('node'),required=not bool(shutil.which('node')))
    args=parser.parse_args()
    assemble(args.standard,args.enhanced,args.output,args.node)
