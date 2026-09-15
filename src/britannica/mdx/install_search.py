"""Install a native-search edition in a closed portable GoldenDict-ng reader.

Distributed as search/install.py; only Python's standard library is required.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree as ET


PROGRAM_IDS = ('eb1911-title-search', 'eb1911-name-lookup')


def install(package, reader, node):
    package, reader, node = Path(package).resolve(), Path(reader).resolve(), Path(node).resolve()
    config = reader / 'portable/config'
    if not config.is_file() or not (reader / 'goldendict.exe').is_file():
        raise ValueError('Expected an initialized portable GoldenDict-ng directory')
    if os.name == 'nt':
        processes = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq goldendict.exe', '/FO', 'CSV', '/NH'],
                                   capture_output=True, text=True, check=True)
        if 'goldendict.exe' in processes.stdout.lower():
            raise RuntimeError('Close GoldenDict before installing so it can save its settings')
    # Verify runtime capabilities before changing the installed edition.
    subprocess.run([str(node), '--no-warnings', '-e',
                    "const {DatabaseSync}=require('node:sqlite');new DatabaseSync(':memory:').close()"], check=True)
    manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('native_search'):
        raise ValueError('Build this edition with --native-search first')
    basename = 'Britannica11' if manifest['edition'] == 'complete' else 'Britannica11-sample'
    content = reader / 'content'
    content.mkdir(exist_ok=True)
    for ext in ('mdx', 'mdd'):
        source, dest = package / (basename+'.'+ext), content / (basename+'.'+ext)
        if source != dest:
            shutil.copy2(source, dest)
    folder = content / 'search'
    if package / 'search' != folder:
        shutil.copytree(package / 'search', folder, dirs_exist_ok=True)
    # GoldenDict 26.8 makeDictionaryId: sorted UTF-8 paths relative to portable
    # content/, each terminated by NUL. This also keeps resources movable.
    dictionary_id = hashlib.md5(b''.join((basename+'.'+ext+'\0').encode('utf-8')
                                        for ext in ('mdd','mdx'))).hexdigest()
    binding = folder / 'binding.json'
    binding.write_text(json.dumps({'dictionaryId': dictionary_id}), encoding='utf-8')
    tree = ET.parse(config)
    root = tree.getroot()
    programs = root.find('programs')
    if programs is None:
        programs = ET.SubElement(root, 'programs')
    for child in list(programs):
        if child.get('id') in PROGRAM_IDS:
            programs.remove(child)
    command = f'"{node}" --no-warnings "{folder / "lookup.cjs"}" "{folder / "titles.json"}"'
    # No query substitution: GoldenDict writes UTF-8 to stdin safely.
    for ident, kind, name, suffix in [
        (PROGRAM_IDS[0], '3', 'Britannica title search', ''),
        (PROGRAM_IDS[1], '2', 'Britannica 11', f' --article "{folder / "articles.sqlite"}" "{binding}"'),
    ]:
        ET.SubElement(programs, 'program', dict(id=ident, type=kind, name=name,
                      enabled='1', commandLine=command+suffix, icon=''))
    pref = root.find('preferences/ignoreDiacritics')
    if pref is not None:
        pref.text = '1'
    shutil.copy2(config, config.with_name('config.before-britannica-search'))
    temporary = config.with_name('config.britannica-new')
    tree.write(temporary, encoding='utf-8')
    temporary.replace(config)
    print(f'Installed {manifest["article_count"]:,} searchable articles in {reader}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reader', type=Path, required=True, help='Close this portable reader before installing')
    parser.add_argument('--node', type=Path, default=shutil.which('node'), required=not bool(shutil.which('node')))
    args = parser.parse_args()
    install(Path(__file__).resolve().parent.parent, args.reader, args.node)
