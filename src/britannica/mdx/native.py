"""Package native GoldenDict search without exposing aliases as headwords."""
import json
import shutil
import sqlite3
import zlib


def package_search(output, entries, display_keys, articles, aliases, css, root):
    from britannica.mdx.build import PREFIX, article_key
    from britannica.markers import strip_title_markers
    names = {stem: {strip_title_markers(a['title']), display_keys[article_key(stem)]} for stem, a in articles.items()}
    for alias, stems in aliases.items():
        for stem in stems:
            names[stem].add(alias)
    records = [{'id': stem, 'title': display_keys[article_key(stem)], 'names': sorted(names[stem])}
               for stem in sorted(articles)]
    folder = output / 'search'
    folder.mkdir(exist_ok=True)
    (folder / 'titles.json').write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    db = folder / 'articles.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE IF EXISTS articles')
        conn.execute('DROP TABLE IF EXISTS metadata')
        conn.execute('CREATE TABLE articles (id TEXT PRIMARY KEY, body BLOB NOT NULL)')
        conn.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('INSERT INTO metadata VALUES (?,?)', ('css', css))
        conn.executemany('INSERT INTO articles VALUES (?,?)',
                         ((r['id'], zlib.compress(entries[r['title']].encode('utf-8'))) for r in records))
        for stem, body in conn.execute('SELECT id,body FROM articles'):
            assert zlib.decompress(body).decode('utf-8') == entries[display_keys[article_key(stem)]]
    conn.close()
    for source, target in [('tools/mdx-title-search.cjs','lookup.cjs'),
                           ('tools/viewer/search-api.js','search-api.js'),
                           ('src/britannica/mdx/install_search.py','install.py')]:
        shutil.copyfile(root/source, folder/target)
    # Internal identities remain addressable. Only public article aliases go away.
    clean = {key: body for key,body in entries.items()
             if not body.startswith('@@@LINK=') or key.startswith(PREFIX)
             or key in ('Britannica 11','Britannica 11 sample')}
    # Choice pages stay available through their stable identity, not as another
    # suggestion alongside each of their actual articles.
    for key, title in list(display_keys.items()):
        if key.startswith(PREFIX+'choice:'):
            clean[key] = clean.pop(title)
            display_keys[key] = key
    return clean, ['search/'+name for name in ('titles.json','articles.sqlite','lookup.cjs','search-api.js','install.py')]
