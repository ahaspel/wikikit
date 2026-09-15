"""Wrap an already validated native-search edition in a Windows installation ZIP.

python -m britannica.mdx.windows --edition mdx/complete --node C:/nvm4w/nodejs/node.exe
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

from britannica.mdx.checksums import sha, write_checksums, write_shipped_text


def build(edition, output, node):
    edition, output, node = edition.resolve(), output.resolve(), node.resolve()
    manifest = json.loads((edition/'manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('native_search'):
        raise ValueError('A --native-search edition is required')
    sample = manifest['edition'] == 'sample'
    source_basename = 'Britannica11-sample' if sample else 'Britannica11'
    # Refuse stale or incomplete source artifacts before compiling/copying.
    for line in (edition/'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        expected, name = line.split('  ', 1)
        if sha(edition/name) != expected:
            raise ValueError('Edition checksum mismatch: '+name)
    runtime_version = subprocess.check_output([str(node), '--version'], text=True).strip()
    # Duplicated in `install_search.py` ON PURPOSE: that file is copied into the
    # shipped package as `install.py` and runs on a machine with no `britannica`
    # package, so it cannot import a shared helper.  Accepted in the dup-constants
    # baseline for that reason rather than consolidated.
    subprocess.run([str(node), '--no-warnings', '-e',
                    "const {DatabaseSync}=require('node:sqlite');new DatabaseSync(':memory:').close()"], check=True)
    output.mkdir(parents=True, exist_ok=True)
    payload = ['Britannica11.mdx', 'Britannica11.mdd', 'manifest.json', 'LICENSE', 'source-link-issues.json',
               'search/titles.json', 'search/articles.sqlite', 'search/lookup.cjs', 'search/search-api.js']
    for name in payload:
        dest = output/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        source_name = name.replace('Britannica11.', source_basename+'.') if name.endswith(('.mdx','.mdd')) else name
        shutil.copy2(edition/source_name, dest)
    runtime = output/'search/runtime'
    runtime.mkdir(exist_ok=True)
    shutil.copy2(node, runtime/'node.exe')
    shutil.copy2(node.parent/'LICENSE', runtime/'LICENSE')
    payload += ['search/runtime/node.exe', 'search/runtime/LICENSE']
    engine = Path(__file__).resolve().parents[3]/'tools/mdx-installer'
    if not (engine/'node_modules/@xmldom/xmldom/package.json').exists():
        raise ValueError('Run npm ci --prefix tools/mdx-installer --ignore-scripts first')
    for name in ['install.cjs', 'package.json', 'package-lock.json']:
        dest = output/'installer'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(engine/name,dest)
        payload.append('installer/'+name)
    dependency = engine/'node_modules/@xmldom/xmldom'
    for source_file in dependency.rglob('*'):
        if source_file.is_file():
            name = 'installer/node_modules/@xmldom/xmldom/'+source_file.relative_to(dependency).as_posix()
            dest = output/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file,dest)
            payload.append(name)
    source = Path(__file__).with_name('windows')/'InstallBritannica.cs'
    source_dest = output/'installer-source/InstallBritannica.cs'
    source_dest.parent.mkdir(exist_ok=True)
    shutil.copy2(source, source_dest)
    compiler = Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    executable = output/'Install Britannica 11.exe'
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/optimize+', '/utf8output',
                    '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                    '/out:'+str(executable), str(source)], check=True)
    payload += ['Install Britannica 11.exe', 'installer-source/InstallBritannica.cs']
    write_shipped_text(output/'README.txt',
        ('BRITANNICA 11 — ENHANCED SEARCH FOR WINDOWS' + (' — SAMPLE' if sample else '') + '\n\n') +
        'This optional edition displays one canonical title per matching article.\n'
        'It contains its own MDX/MDD pair and search helper: install it instead of the\n'
        'standard MDX edition, not as a second copy beside it. GoldenDict is not bundled.\n'
        'For ordinary MDX installation without helpers, choose Britannica11-MDX.zip.\n\n' +
        ('This is a 13-article trial of the same enhanced-search setup used by the full\n'
         'edition. Try ALGEBRA, ALPHABET, MERCURY and Continued Fraction. Links to\n'
         'articles outside the sample are marked online. Use a separate test reader\n'
         'folder if the complete edition is already installed.\n\n' if sample else '') +
        '1. Extract this entire ZIP to a folder. Do not run setup from inside the ZIP.\n'
        '2. Close GoldenDict using File > Quit.\n'
        '3. Double-click Install Britannica 11.exe. Browse to your portable GoldenDict folder\n'
        '   (the folder containing goldendict.exe), then click Install.\n' +
        ('4. Open GoldenDict and search for '+('Britannica 11 sample' if sample else 'Britannica 11')+' to see the contents.\n\n') +
        'GoldenDict-ng is required and is not included. This package is tested with\n'
        'GoldenDict-ng 26.8.0 on 64-bit Windows. Use a writable portable reader folder,\n'
        'such as one under Documents, rather than Program Files. Setup creates portable\n'
        'configuration if the folder has not been initialized. Python and Node do not\n'
        'need to be installed: the search runtime is included privately with this edition.\n\n'
        'The first full-text index may take several minutes. Title lookup is available\n'
        'sooner. GoldenDict controls\n'
        'result ordering. Reading, search, images and mathematics work offline.\n\n'
        'To update, quit GoldenDict and run the new installer against the same folder.\n'
        'Existing settings are preserved and backed up. If you move the reader folder,\n'
        'run setup again to update the search paths. Keep content/search with the book.\n'
        'You may remove the extracted setup folder after installation.\n\n'
        'The Chrome selection menu is a separate optional extension; setup does not\n'
        'change your browser or the registered goldendict:// handler.\n\n'
        'See LICENSE for edition attribution and search/runtime/LICENSE for the bundled\n'
        'Node runtime notices. Package identity and checksums are included.\n')
    write_shipped_text(output/'installation.json', json.dumps({
        'platform':'windows-x64', 'runtime_version':runtime_version, 'edition':manifest['edition'],
        'runtime_sha256':sha(runtime/'node.exe'), 'edition_manifest_sha256':sha(edition/'manifest.json'),
        'installer_source_sha256':sha(source), 'reader_tested':'GoldenDict-ng 26.8.0',
    }, indent=2))
    payload += ['README.txt', 'installation.json']
    write_checksums(output, payload)
    archive = output.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in payload+['SHA256SUMS']:
            z.write(output/name, name)
    print('Windows installation package:', archive, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', type=Path, default=Path('mdx/complete'))
    parser.add_argument('--output', type=Path, default=Path('mdx/Britannica11-Windows'))
    parser.add_argument('--node', type=Path, default=shutil.which('node'), required=not bool(shutil.which('node')))
    args = parser.parse_args()
    build(args.edition, args.output, args.node)
