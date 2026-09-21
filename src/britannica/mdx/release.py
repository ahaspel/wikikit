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
            with original.open(name) as incoming, target.open(name, 'w', force_zip64=True) as outgoing:
                while chunk := incoming.read(1024*1024):
                    digest.update(chunk)
                    outgoing.write(chunk)
            checksums[name] = digest.hexdigest()
        readme = edition_readme(sample, False).encode('utf-8')
        target.writestr('README.md', readme)
        checksums['README.md'] = hashlib.sha256(readme).hexdigest()
        target.writestr('SHA256SUMS', format_checksums(checksums))


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
        assert manifest['article_count'] == (13 if sample else 37225)
        assert not any(Path(name).name.lower() == 'goldendict.exe' for name in names)
        if enhanced:
            assert {'Britannica11.mdx', 'Britannica11.mdd', 'Install Britannica 11.exe',
                    'search/titles.json', 'search/articles.sqlite', 'search/runtime/node.exe'} <= set(names)
            assert json.loads(z.read('installation.json'))['edition'] == manifest['edition']
        else:
            base = 'Britannica11-sample' if sample else 'Britannica11'
            assert set(names) == {base+'.mdx', base+'.mdd', 'README.md', 'LICENSE',
                                  'manifest.json', 'source-link-issues.json', 'SHA256SUMS'}
        return manifest


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
    packages = [
        ('Britannica11-MDX.zip', standard/'Britannica11.zip', False, False),
        ('Britannica11-Enhanced-Windows.zip', builds/'enhanced-windows.zip', False, True),
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
    introduction = '''# Britannica 11 dictionary downloads

**Choose the standard MDX edition for ordinary dictionary use.** Extract its MDX
and MDD together and add their folder to your existing reader. There is no
installer or additional runtime. GoldenDict is not included in any download.

| Download | What it provides |
|---|---|
| [Standard MDX](Britannica11-MDX.zip) | The complete dictionary with conventional lookup aliases. |
| [Enhanced search for Windows](Britannica11-Enhanced-Windows.zip) | The complete dictionary with one canonical title per article in GoldenDict's title suggestions. |

The standard format is independent of OS. Both editions have been tested with
GoldenDict-ng 26.8.0 on Windows; other readers and operating systems are not
verified. The enhanced package currently supports portable GoldenDict on 64-bit
Windows and includes its own search runtime. Python and Node need not be installed.

Both complete editions contain the same 37,225 articles and plates, illustrations,
mathematics, contributor and topic navigation, and Reader's Guide. The difference
is lookup: the standard edition exposes ordinary aliases, which the reader may
show separately; enhanced search matches aliases internally and displays canonical
titles. GoldenDict controls final result ordering and full-text presentation.

**Choose one complete edition.** Enhanced search includes a different MDX plus
its helper; it is a complete alternative, not a helper-only add-on. Do not load
both editions at once. Its installer replaces the standard pair if installed in
the same portable reader's content directory. Remove any other copy from your
reader's sources to avoid duplicate dictionaries.

To return from enhanced search to the standard MDX, disable the “Britannica title
search” and “Britannica 11” entries under GoldenDict's Programs sources, replace the
MDX/MDD pair, and rescan. Keep the actual Britannica MDX dictionary enabled.

Each ZIP contains instructions, attribution and file checksums. Known unavailable
source links are inventoried in each edition. Reading and internal resources work
offline; explicitly external links require a connection. The Chrome selection-menu
extension is optional and distributed separately.

'''
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
