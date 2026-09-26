"""Build an offline MDX/MDD edition from canonical exports.

    python -m britannica.mdx.build --sample --output mdx/sample
    python -m britannica.mdx.build --all --output mdx/complete

The sample stages a fixed selection and loads it with load_corpus. The corpus
index supplies the destinations outside the sample; it never supplies bodies.
No source rebuild, database access, or source edits are involved.
"""
from __future__ import annotations

import argparse
import gc
from collections import defaultdict
from datetime import datetime, timezone
import html
from html.parser import HTMLParser
from importlib.metadata import version
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import quote, unquote, urljoin
import zipfile

from britannica.epub import math_assets
from britannica.epub.images import diet_image
from britannica.export.corpus import load_corpus
from britannica.export.article_json import stable_id_from_filename
from britannica.export.download import _topic_index
from britannica import provenance as _prov
from britannica.markers import strip_title_markers
from britannica.render.article import render_article, _section_slug
from britannica.render.inline import _article_url
from britannica.util.strings import fold_accents, strip_html_tags
from britannica.xrefs.normalizer import normalize_xref_target
from britannica.provenance import digest

ROOT = Path(__file__).resolve().parents[3]
SITE = "https://britannica11.org"
PREFIX = "EB1911:"
SAMPLE = Path(__file__).with_name("sample.json")


def article_key(stem: str) -> str:
    return PREFIX + "article:" + stem


def topic_key(topic_id: str) -> str:
    # GoldenDict-ng truncates lookup keys longer than 100 characters. Deep
    # taxonomy paths exceed that limit; retain their full names in the page.
    return PREFIX + "topic:" + digest(topic_id.encode("utf-8"))[:24]


def volume_key(volume) -> str:
    return PREFIX + f"volume:{volume}"


# `href="…"` / `src="…"` with the quote captured so the rewrite can hand back the
# SAME one.  Both the article body rewriter here and the navigation pages rewrite
# these attributes; spelling the pattern twice is what the dup-constants ratchet
# caught.
HREF_ATTR_RE = re.compile(r'''\bhref=(["'])(.*?)\1''')
SRC_ATTR_RE = re.compile(r'''\bsrc=(["'])(.*?)\1''')


def entry_url(key: str, fragment: str = "") -> str:
    return "entry://" + quote(key, safe="") + ("#" + quote(fragment, safe="") if fragment else "")


def lookup_fold(text: str) -> str:
    # Deliberately conservative collision grouping, including reader punctuation
    # folding. Preserve the original spelling as a lookup key as well.
    return "".join(c for c in fold_accents(text).casefold() if c.isalnum())


def add_article_topics(body, paths):
    """Place topic navigation after the byline, or the unsigned citation line.

    Both the POSITION and the MARKUP are shared with the EPUB and the site —
    `paths` is a list of paths, each a list of `(label, url)` segments.
    """
    from britannica.render.article import insert_after_byline, topic_trail_html
    return insert_after_byline(body, topic_trail_html(paths))


class Links:
    def __init__(self, selected: set[str], known: set[str]):
        self.selected, self.known = selected, known

    def url_for(self, stem, section_slug=None):
        if stem not in self.known:
            raise ValueError(f"Unknown article target: {stem}")
        fragment = "section-" + section_slug if section_slug else ""
        if stem in self.selected:
            return entry_url(article_key(stem), fragment)
        return SITE + _article_url(stem + ".json") + ("#" + quote(fragment) if fragment else "")

    def contrib_url(self, slug):
        return entry_url(PREFIX + "contributor:" + slug)


class Inventory(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.assets, self.tags = set(), [], [], defaultdict(int)
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags[tag] += 1
        if a.get("id"):
            if a["id"] in self.ids:
                raise ValueError(f"Duplicate HTML id: {a['id']}")
            self.ids.add(a["id"])
        if tag in ("a", "area") and a.get("href"):
            self.links.append(a["href"])
        if tag == "link" and a.get("href"):
            self.assets.append(a["href"])
        if a.get("src"):
            self.assets.append(a["src"])
        if any(k.startswith("on") for k in a) or tag == "script":
            raise ValueError("Dictionary content must contain no scripts/event handlers")

    handle_startendtag = handle_starttag


def stylesheet() -> str:
    css = (ROOT / "src/britannica/epub/epub.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    if "@" in css or "url(" in css:
        raise ValueError("EPUB stylesheet changed: review CSS scoping/resources")

    def scope(m):
        selectors = [s.strip() for s in m[1].split(",")]
        return ",".join(".eb1911" if s == "body" else ".eb1911 " + s for s in selectors) + "{"

    css = re.sub(r"([^{}]+)\{", scope, css)
    return css + """
.eb1911 {line-height:1.5; color:inherit; background:transparent;}
.eb1911 .contributors {color:inherit; opacity:.8;}
.eb1911 svg.math-display {display:block; max-width:100%; height:auto; margin:1em auto;}
.eb1911 svg.math-inline {max-width:100%;}
.eb1911 .wide-table-inline, .eb1911 .wide-table-wrap {overflow-x:auto;}
.eb1911 .sample-note {font-size:.85em; border-bottom:1px solid; padding:.4em 0;}
.eb1911 .outside-sample {font-size:.8em;}
"""


def wrap(body: str) -> str:
    return '<link rel="stylesheet" href="britannica.css"><div class="eb1911">' + body + "</div>"


def list_links(items):
    return "<ul>" + "".join(f'<li><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></li>'
                            for label, url in items) + "</ul>"


def bundle_body(body: str, resources: dict[str, bytes], source_assets: dict, *, sample=True) -> str:
    def image_src(m):
        url = html.unescape(m[2])
        if not url.startswith("/data/images/"):
            if url in resources or url.startswith("data:") or url.startswith("#"):
                return m[0]
            raise ValueError(f"Unbundled image/resource: {url}")
        name = unquote(url.removeprefix("/data/images/"))
        # Hand back the quote character the source used, both ways out.
        def attr(value):
            return f'src={m[1]}{value}{m[1]}'

        if name in source_assets:
            return attr(source_assets[name]["resource"])
        base = (ROOT / "data/images").resolve()
        path = (base / name).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"Image escapes asset directory: {name}")
        raw = path.read_bytes()  # missing images fail explicitly
        data, ext = diet_image(str(path))
        dest = "images/" + digest(data) + ext
        resources[dest] = data
        source_assets[name] = {"sha256": digest(raw), "resource": dest}
        return attr(dest)

    body = SRC_ATTR_RE.sub(image_src, body)

    def href(m):
        url = html.unescape(m[2])
        if url.startswith(("entry://", "#", "https://", "http://", "mailto:")):
            return m[0]
        return 'href=' + m[1] + html.escape(urljoin(SITE + "/", url), quote=True) + m[1]

    body = HREF_ATTR_RE.sub(href, body)
    # An outside destination must be apparent before clicking, not just in help.
    if sample:
        body = re.sub(r'(<a\b[^>]*href="https://britannica11.org/article/[^>]*>.*?</a>)',
                      r'\1 <span class="outside-sample">[online; outside sample]</span>', body, flags=re.S)
    if "«MATHPH»" in body:
        raise ValueError("Missing rendered mathematics")
    return body


def compact_aliases(articles, aliases):
    """Keep one displayed spelling for redundant routes to identical targets.

    First preserve all destinations across existing punctuation/accent folds.
    Preserve word order: natural-name aliases are needed for exact lookup.
    Canonical titles win over harvested spellings; retain substantive synonyms.
    Accent-free lookup is the reader's Ignore diacritics preference.
    """
    titles = {strip_title_markers(a["title"]) for a in articles.values()}
    def preferred(spellings):
        return min(spellings, key=lambda s: (s not in titles,
            -sum(a != b for a, b in zip(s, fold_accents(s))), s))
    folded = defaultdict(lambda: {"spellings": set(), "targets": set()})
    for spelling, stems in aliases.items():
        group = folded[lookup_fold(spelling)]
        group["spellings"].add(spelling)
        group["targets"].update(stems)
    compact, removed = {}, []
    for group in folded.values():
        spellings, stems = group["spellings"], sorted(group["targets"])
        keep = preferred(spellings)
        compact[keep] = set(stems)
        # The accent-free spelling is not a redundant route: it is the only one
        # a reader can TYPE.  Ignore diacritics is off by default and a
        # hand-copied .mdx never meets the installer that turns it on, so
        # dropping ABABDA as a duplicate of ABĀBDA is what made the reviewer's
        # lookup fail.  Every other fold this groups by — case, punctuation,
        # word order — the reader can reproduce from the keyboard; accents are
        # the one that they cannot.
        plain = fold_accents(keep)
        if plain != keep:
            compact[plain] = set(stems)
        for spelling in sorted(spellings - {keep, plain}):
            removed.append({"spelling": spelling, "retained": keep, "targets": list(stems)})
    return compact, sorted(removed, key=lambda r: r["spelling"])


def add_headwords(entries, articles, aliases):
    groups = defaultdict(set)
    spellings = defaultdict(set)
    for spelling, stems in aliases.items():
        folded = lookup_fold(spelling)
        if not folded:
            raise ValueError(f"Empty lookup spelling: {spelling!r}")
        groups[folded].update(stems)
        spellings[folded].add(spelling)
    choices = 0
    for folded, stems in sorted(groups.items()):
        if len(stems) == 1:
            target = article_key(next(iter(stems)))
        else:
            choices += 1
            target = PREFIX + "choice:" + digest(folded.encode())[:16]
            items = []
            for stem in sorted(stems):
                a = articles[stem]
                label = f'{strip_title_markers(a["title"])} — volume {a["volume"]}, page {a["page_start"]}'
                # Same-page homonyms need a little context, not merely a page number.
                from britannica.markers import markers_to_text
                label += ": " + markers_to_text(a["body"])[:140].strip()
                items.append((label, entry_url(article_key(stem))))
            entries[target] = wrap("<h1>Choose an article</h1>" + list_links(items))
        for spelling in sorted(spellings[folded]):
            if spelling.startswith(PREFIX) or spelling in entries:
                raise ValueError(f"Reserved or duplicate headword: {spelling}")
            entries[spelling] = "@@@LINK=" + target
    return choices


# The image a plate shows, which is the only thing that tells two plates of one
# article apart: they share their article's title and carry no words at all.
_PLATE_ALT_RE = re.compile(r'<img\b[^>]*\balt="([^"]*)"', re.I)
_BODY_TEXT_RE = re.compile(r'<div\b[^>]*\bclass="body-text"[^>]*>(.*)', re.S | re.I)
# Sample-edition furniture on every link that leaves the sample; it is not the
# book's words and must not become a headword.
_OUTSIDE_SAMPLE_RE = re.compile(r'<span\b[^>]*\bclass="outside-sample".*?</span>', re.S | re.I)
_SPACE_BEFORE_CLOSE = re.compile(r"\s+([)\]},.;:!?’”])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[{“‘])\s+")
_LEADING_PUNCT = re.compile(r"^[\s,.;:—–-]+")


def _after_title(text: str, title: str) -> str:
    """What a name says BEYOND the title, with the punctuation that joined them.

    A gloss and a plate's image name are the same question asked of different
    text, so they strip the echoed title the same way.
    """
    if title and text.upper().startswith(title.upper()):
        text = text[len(title):]
    return _LEADING_PUNCT.sub("", text).strip()
GLOSS_WORDS = 8


def display_gloss(body, article, *, words=GLOSS_WORDS):
    """The few words that tell one homonym from another, in the book's voice.

    ELEVEN articles are titled JOHN.  `JOHN (vol. 15, p. 440)` distinguishes
    them and tells a reader nothing; `JOHN — (1296–1346), king of Bohemia` is
    how the book itself distinguishes them, and it is what an INDEX entry has
    always looked like.  Measured over the corpus, title + eight words leaves
    two genuine collisions out of 37,226 (HERZBERG and WILMINGTON, whose
    openings really do start alike), against 1,183 colliding bare titles.

    It also has the property the printed location has and an ordinal `(2)` does
    not: it is SOURCE-derived, so it moves only when the text moves.  That is
    what makes it safe to address a link to.

    A plate has no words — its body is one `<img>` — so it is named by the image
    it shows, `NEUROPATHOLOGY — Plate II`.
    """
    if (article or {}).get("article_type") == "plate":
        alt = _PLATE_ALT_RE.search(body)
        if alt:
            name = re.sub(r"\.(?:jpg|jpeg|png|gif|svg)$", "", alt[1], flags=re.I)
            name = re.sub(r"^EB1911\b[\s\-—–]*", "", name, flags=re.I)
            name = _after_title(name, strip_title_markers((article or {}).get("title") or ""))
            if name:
                return name
    # `body-text` is the RENDERER's own name for the article's prose, shared
    # with the site.  Reading from `</h1>` instead swept up the furniture
    # between them and produced `MERCURY — vol. 18, p. 154 · 521 words In:`,
    # which disambiguates by exactly the printed location the gloss exists to
    # replace.
    rest = _BODY_TEXT_RE.search(body)
    text = _OUTSIDE_SAMPLE_RE.sub("", rest[1] if rest else body)
    text = " ".join(html.unescape(strip_html_tags(text, " ")).split())
    # A tag boundary is a word boundary, so the separator has to be a space —
    # but `(<span>Mercurius</span>)` then reads `( Mercurius )` in the headword
    # list.  Closing up around punctuation is display work, and this is the
    # display key.
    text = _SPACE_AFTER_OPEN.sub(r"\1", _SPACE_BEFORE_CLOSE.sub(r"\1", text))
    text = _after_title(text, strip_title_markers((article or {}).get("title") or ""))
    return " ".join(text.split()[:words])


def label_content_entries(entries, articles):
    """Put readable keys on HTML records: GoldenDict uses them in FTS results.

    The stable identifier does NOT survive as a redirect.  Every key in an MDX
    file is a headword — a redirect is indexed exactly like a body — so keeping
    `EB1911:article:01-0036-dd33a0` beside each article put a machine id in the
    reader's headword list for every article in the book.  Links are repointed
    at the display key instead, which is why this returns a rewritten mapping
    rather than editing in place.
    Reuse an unambiguous title key; qualify homonyms with the book's own opening
    words, then the printed location, and only last with an ordinal.
    Never merge bodies or change a choice page's existing lookup spelling.
    """
    output, names = dict(entries), {}
    owners, headwords = defaultdict(set), defaultdict(list)
    for key, value in entries.items():
        target = value[8:] if value.startswith("@@@LINK=") else key
        owners[lookup_fold(key)].add(target)
        if value.startswith("@@@LINK="):
            headwords[target].append(key)
    for key, body in sorted(entries.items()):
        if body.startswith("@@@LINK="):
            continue
        heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", body, re.S | re.I)
        if not heading:
            raise ValueError(f"Content entry has no display heading: {key}")
        title = " ".join(html.unescape(strip_html_tags(heading[1], " ")).split())
        article = None
        if key.startswith(PREFIX + "article:"):
            article = articles[key.removeprefix(PREFIX + "article:")]
            title = strip_title_markers(article["title"])
        elif key.startswith(PREFIX + "choice:"):
            title = "Articles named " + min(headwords[key], key=lambda s: (len(s), s))
        elif key.startswith(PREFIX + "topic:"):
            title = "Topic: " + title
        elif key.startswith(PREFIX + "contributor:"):
            title = "Contributor: " + title
        # The bare title FIRST, which absorbs the alias that already points here
        # and so costs a headword rather than adding one.  Then the book's own
        # disambiguation, then the printed location, and only then an ordinal —
        # which is a last resort because it is the one form that can MOVE when
        # an unrelated article is added, silently repointing every link to it.
        forms = [title]
        if article is not None:
            gloss = display_gloss(body, article)
            if gloss:
                forms.append(f"{title} — {gloss}")
            forms.append(f'{title} (vol. {article["volume"]}, p. {article["page_start"]})')
        candidate = None
        for form in forms:
            form = form if len(form) <= 90 else form[:87] + "…"
            if not owners[lookup_fold(form)] - {key}:
                candidate = form
                break
        if candidate is None:
            base = forms[-1]
            base = base if len(base) <= 90 else base[:87] + "…"
            candidate, number = base, 1
            while owners[lookup_fold(candidate)] - {key}:
                number += 1
                candidate = base + f" ({number})"
        if len(candidate) > 100:
            raise ValueError(f"Display key exceeds the reader's limit: {candidate}")
        output[candidate] = body
        # An entry already keyed by its own display title relabels to itself;
        # deleting the old key would delete the body just written.  The old code
        # rejected that case because it would have made a self-redirect.
        if candidate != key:
            del output[key]
        # An alias whose fold is a PREFIX of the display key's is a second line
        # in every list where the key already appears, and there is nothing a
        # reader can type to reach it that does not also reach the key: VOLTAIRE
        # beside VOLTAIRE, FRANÇOIS MARIE AROUET DE.  Dropping it costs no
        # lookup at all.
        #
        # The other direction is NOT the same trade and is left alone.  DANTE
        # ALIGHIERI is a prefix sibling of DANTE, and dropping the longer one
        # would mean typing the poet's full name finds nothing — the same
        # failure as the ABABDA report.  One redundant line is the lesser
        # defect, which is the ruling the accent variants already got.
        folded = lookup_fold(candidate)
        for alias in headwords.get(key, ()):
            spelt = lookup_fold(alias)
            if spelt != folded and folded.startswith(spelt):
                output.pop(alias, None)
        owners[lookup_fold(candidate)].add(key)
        names[key] = candidate
    return _repoint(output, names), names


def _repoint(output, names):
    """Send every link at the display key, now that the identifier is not one.

    Both halves of the graph carry identifiers: `entry://` hrefs inside bodies
    and the `@@@LINK=` value of every alias.  Anything missed here has nothing
    left to resolve to, so `validate` reports it as a missing entry rather than
    shipping a dead headword.
    """
    def repoint(url):
        raw, _, fragment = url[8:].partition("#")
        key = unquote(raw)
        return entry_url(names.get(key, key), unquote(fragment)) if key else url

    def href(m):
        url = html.unescape(m[2])
        if not url.startswith("entry://"):
            return m[0]
        return "href=" + m[1] + html.escape(repoint(url), quote=True) + m[1]

    return {key: "@@@LINK=" + names.get(body[8:], body[8:])
            if body.startswith("@@@LINK=") else HREF_ATTR_RE.sub(href, body)
            for key, body in output.items()}


def check_headwords(entries):
    """Nothing in the list a reader scrolls may be an identifier or a free duplicate.

    Both failures shipped, and neither is visible to `validate`, because both
    resolve perfectly.  They are only visible in the headword list itself:

      * ~40,000 `EB1911:article:…` keys sat there beside the articles they
        named, because an MDX redirect is indexed exactly like a body.
      * an alias whose fold is a PREFIX of its own article's key put that
        article on the list twice for one query, and no query could reach the
        alias without also reaching the key.

    The converse — a key that is a prefix of one of its own aliases, DANTE
    beside DANTE ALIGHIERI — is deliberately allowed: removing it would cost a
    lookup rather than a line.
    """
    shadows = []
    for key, body in entries.items():
        if key.startswith(PREFIX):
            raise ValueError(f"Machine identifier left in the headword list: {key}")
        if not body.startswith("@@@LINK="):
            continue
        spelt, folded = lookup_fold(key), lookup_fold(body[8:])
        if spelt != folded and folded.startswith(spelt):
            shadows.append(f"{key} -> {body[8:]}")
    if shadows:
        raise ValueError(f"{len(shadows)} alias(es) shadow their own article: "
                         + ", ".join(shadows[:10]))
    return {"headwords": len(entries),
            "redirects": sum(v.startswith("@@@LINK=") for v in entries.values())}


def validate(entries, resources, *, report_path=None):
    inventories = {k: Inventory(v) for k, v in entries.items() if not v.startswith("@@@LINK=")}
    def canonical(key):
        seen = set()
        while key in entries and entries[key].startswith("@@@LINK="):
            if key in seen:
                raise ValueError("Alias loop")
            seen.add(key)
            key = entries[key][8:]
        if key not in inventories:
            raise ValueError(f"Missing entry: {key}")
        return key
    for key in entries:
        canonical(key)
    links, errors = 0, []
    for key, inv in inventories.items():
        for url in inv.links:
            if url.startswith("entry://"):
                target, _, fragment = url[8:].partition("#")
                try:
                    target = canonical(unquote(target)) if target else key
                except ValueError as exc:
                    errors.append(f"{key}: {exc}")
                    continue
            elif url.startswith("#"):
                target, fragment = key, url[1:]
            else:
                if not url.startswith(("https://", "http://", "mailto:")):
                    errors.append(f"Unresolved link {key} -> {url}")
                continue
            if fragment and unquote(fragment) not in inventories[target].ids:
                errors.append(f"Missing fragment: {key} -> {url}")
            links += 1
        for url in inv.assets:
            if url.startswith("data:"):
                continue
            if unquote(url) not in resources:
                errors.append(f"Missing resource: {key} -> {url}")
    if errors:
        if report_path:
            report_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ValueError(f"{len(errors)} link/resource failures: " + "\n".join(errors[:10]))
    return {"internal_links": links, "html_entries": len(inventories)}


def compile_package(folder: Path, entries, resources, basename, *, sample=True):
    from mdict_utils import writer
    from mdict_utils.base.readmdict import MDX, MDD
    db = folder / "stage.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE mdx(entry TEXT PRIMARY KEY, paraphrase TEXT NOT NULL)")
        conn.execute("CREATE TABLE mdd(entry TEXT PRIMARY KEY, file BLOB NOT NULL)")
        conn.executemany("INSERT INTO mdx VALUES (?,?)", sorted(entries.items()))
        conn.executemany("INSERT INTO mdd VALUES (?,?)", [("\\" + k.replace("/", "\\"), v)
                                                           for k, v in sorted(resources.items())])
    conn.close()
    try:
        writer.pack(str(folder / (basename + ".mdx")), writer.pack_mdx_db(str(db)),
                    title="Britannica 11" + (" — compatibility sample" if sample else " — complete edition"),
                    description="Offline edition from britannica11.org; CC BY-SA 4.0.")
        writer.pack(str(folder / (basename + ".mdd")), writer.pack_mdd_db(str(db)), is_mdd=True)
    finally:
        for obj in writer.MDICT_OBJ.values():
            obj.close()
        writer.MDICT_OBJ.clear()
        # mdict-utils 1.3.14's pack_*_db helpers use SQLite context managers
        # without closing the connections. Release them before Windows cleanup.
        gc.collect()
    # Stream verification: avoid making another full dictionary/resource copy.
    seen = set()
    for key, value in MDX(str(folder / (basename + ".mdx"))).items():
        key = key.decode("utf-8")
        if key in seen or key not in entries or value.decode("utf-8").rstrip("\0") != entries[key]:
            raise ValueError(f"Compiled MDX differs: {key}")
        seen.add(key)
    if seen != set(entries):
        raise ValueError("Compiled MDX has missing keys")
    seen = set()
    for key, value in MDD(str(folder / (basename + ".mdd"))).items():
        key = key.decode("utf-8").lstrip("\\").replace("\\", "/")
        if key in seen or key not in resources or value != resources[key]:
            raise ValueError(f"Compiled MDD differs: {key}")
        seen.add(key)
    if seen != set(resources):
        raise ValueError("Compiled MDD has missing resources")


def build_edition(output: Path, *, sample=True, native_search=False):
    started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=output) as tmp:
        stage = Path(tmp)
        selected = json.loads(SAMPLE.read_text(encoding="utf-8")) if sample else None
        source = ROOT / "data/derived/articles"
        index_raw = (source / "index.json").read_bytes()
        index = json.loads(index_raw)
        known = {stable_id_from_filename(a["filename"]) for a in index}
        inputs = stage / "inputs" if sample else source
        input_hashes = {}
        if sample:
            inputs.mkdir()
            for stem in selected:
                raw = (source / (stem + ".json")).read_bytes()
                input_hashes[stem] = digest(raw)
                (inputs / (stem + ".json")).write_bytes(raw)
        print("Loading canonical corpus" if not sample else "Loading sample", flush=True)
        payloads, _ = load_corpus(inputs, require=("id", "body", "title", "stable_id"))
        articles = {p.stem: a for p, a in payloads.items()}
        excluded = {}
        if not sample:
            if set(articles) != known:
                raise ValueError(f"Index/corpus identity mismatch: {sorted(set(articles) ^ known)}")
            empty = {s for s, a in articles.items() if not a["body"]}
            if empty != {"25-0483-dc502a"}:
                raise ValueError(f"Changed empty-record disposition: {sorted(empty)}")
            excluded = {s: "Empty source plate, also excluded by the corpus download" for s in empty}
            for s in empty:
                del articles[s]
            selected = {s: a["title"] for s, a in articles.items()}
            input_hashes = {s: digest(json.dumps(a, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                            for s, a in articles.items()}
        print(f"Loaded {len(articles)} nonempty records in {time.perf_counter()-started:.1f}s", flush=True)
        for stem, a in articles.items():
            if not a["body"] or a["stable_id"] != stem or stem not in known:
                raise ValueError(f"Invalid corpus record {stem}")
        policy = Links(set(articles), known)
        math_assets.start_collect()
        for a in articles.values():
            if "«MATH" in a["body"] or "«EQN" in a["body"]:
                render_article(a, target="epub", epub_bundled=policy)
        math_assets.generate(math_assets.take_collected(), svg=True, png=False)
        resources = {"britannica.css": stylesheet().encode("utf-8")}
        entries, source_assets, contributors = {}, {}, {}
        aliases = defaultdict(set)
        failures = []
        for number, (stem, a) in enumerate(articles.items(), 1):
            if sample or number % 1000 == 0:
                print("Rendering", number, stem, a["title"], flush=True)
            title = strip_title_markers(a["title"])
            aliases[title].add(stem)
            # Do not require the reader's optional ignore-diacritics setting.
            aliases[normalize_xref_target(fold_accents(title))].add(stem)
            try:
                body = render_article(a, target="epub", epub_bundled=policy)
                body = bundle_body(body, resources, source_assets, sample=sample)
                Inventory(body)
            except Exception as exc:
                failures.append({"article": stem, "title": a["title"], "error": str(exc)})
                continue
            note = '<p class="sample-note">Britannica 11' + (" compatibility sample" if sample else " complete edition") + ' · <a href="' + entry_url(PREFIX + "help") + '">Contents and help</a></p>'
            entries[article_key(stem)] = wrap(note + body)
            for c in a.get("contributors") or []:
                if c.get("full_name"):
                    slug = _section_slug(c["full_name"])
                    info = contributors.setdefault(slug, {"person": c, "articles": []})
                    info["articles"].append(stem)
        # Explicit, source-attested pilot alias. Broad alias harvesting belongs
        # to the complete-export phase, not an indiscriminate display-text index.
        if failures:
            (output / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
            raise ValueError(f"{len(failures)} rendering/resource failures; see {output / 'failures.json'}")
        if sample:
            aliases["Continued Fraction"].add("07-0045-9b7a0f")
        else:
            from britannica.mdx.navigation import add_reference_aliases, load_contributors
            alias_report = add_reference_aliases(articles, aliases)
            (output / "alias_report.json").write_text(json.dumps(alias_report, ensure_ascii=False, indent=2), encoding="utf-8")
            load_contributors(articles, contributors)
        for slug, info in contributors.items():
            c = info["person"]
            body = "<h1>" + html.escape(c["full_name"]) + "</h1><p>" + html.escape(c.get("credentials", "")) + "</p><p>" + html.escape(c.get("description", "")) + "</p><h2>Articles" + (" in this sample" if sample else "") + "</h2>"
            body += list_links((articles[s]["title"], entry_url(article_key(s))) for s in info["articles"])
            bio = c.get("bio_article_filename")
            if bio:
                body += list_links([("Biographical article", policy.url_for(stable_id_from_filename(bio)))])
            entries[PREFIX + "contributor:" + slug] = wrap(body)
        ct_raw = (ROOT / "data/derived/classified_toc.json").read_bytes()
        topics, _ = _topic_index(json.loads(ct_raw))
        topic_count = 0
        sample_memberships = defaultdict(list)
        for topic in topics if sample else []:
            members = [s for s in topic["articles"] if s in articles]
            if not members:
                continue
            topic_count += 1
            key = topic_key(topic["id"])
            label = " › ".join(topic["path"]) if isinstance(topic["path"], list) else topic["path"]
            entries[key] = wrap("<h1>" + html.escape(label) + "</h1><p>Articles in this sample</p>" + list_links((articles[s]["title"], entry_url(article_key(s))) for s in members))
            for stem in members:
                # One (label, url) per SEGMENT; the sample bundles only the leaf
                # topic page, so every segment points at it rather than pretending
                # an ancestor page exists in a sample that does not carry one.
                segs = topic["path"] if isinstance(topic["path"], list) else topic["path"].split(" > ")
                sample_memberships[stem].append([(s, entry_url(key)) for s in segs])
        for stem, links in sample_memberships.items():
            entries[article_key(stem)] = add_article_topics(entries[article_key(stem)], links)
        ancillary = {}
        if not sample:
            from britannica.mdx.navigation import add_navigation
            ancillary = add_navigation(entries, articles, contributors, json.loads(ct_raw), policy, resources, source_assets)
            topic_count = ancillary["topic_count"]
        aliases, redundant_aliases = compact_aliases(articles, aliases)
        (output / "redundant-aliases.json").write_text(json.dumps(redundant_aliases, ensure_ascii=False, indent=2), encoding="utf-8")
        choices = add_headwords(entries, articles, aliases)
        if sample:
            body = "<h1>Britannica 11 — compatibility sample</h1><p>This is a small test selection, not the complete edition. Article text, illustrations and mathematics are bundled offline. Destinations outside this sample open the website. Front matter and the Reader’s Guide are not included in this pilot.</p>" + list_links((a["title"], entry_url(article_key(s))) for s, a in articles.items())
            # Use a real canonical section in the pilot's reader test.
            section_ids = sorted(i for i in Inventory(entries[article_key("01-0639-46474b")]).ids if i.startswith("section-"))
            if not section_ids:
                raise ValueError("Algebra sample has lost its section anchors")
            body += "<h2>Section-link test</h2>" + list_links([
                ("Open a section within Algebra", entry_url(article_key("01-0639-46474b"), section_ids[0]))])
            entries[PREFIX + "help"] = wrap(body)
            entries["Britannica 11 sample"] = "@@@LINK=" + PREFIX + "help"
        else:
            from britannica.mdx.navigation import full_help
            entries[PREFIX + "help"] = wrap(full_help(len(articles)))
            entries["Britannica 11"] = "@@@LINK=" + PREFIX + "help"
        print("Validating complete link/resource graph", flush=True)
        from britannica.mdx.link_exceptions import mark_unavailable
        source_link_issues = mark_unavailable(entries)
        if source_link_issues:
            entries[PREFIX + "help"] += wrap(f"<p>{len(source_link_issues)} pre-existing source links have unavailable section destinations. Their labels remain visible and are marked unavailable. See source-link-issues.json in the distribution.</p>")
        qa_entries = entries
        entries, display_keys = label_content_entries(entries, articles)
        # Before the native edition deliberately re-hides its choice pages under
        # their identities: this asks what the STANDARD edition puts in front of
        # a reader, which is the edition the reviewer installed.
        headword_checks = check_headwords(entries)
        search_files = []
        if native_search:
            from britannica.mdx.native import package_search
            entries, search_files = package_search(output, entries, display_keys, articles, aliases, stylesheet(), ROOT)
        checks = validate(entries, resources, report_path=output / "link_failures.json")
        basename = "Britannica11-sample" if sample else "Britannica11"
        print("Compiling", len(entries), "keys and", len(resources), "resources", flush=True)
        compile_package(stage, entries, resources, basename, sample=sample)
        preview = output / "preview"
        preview.mkdir(exist_ok=True)
        for key, body in entries.items() if sample else []:
            if not body.startswith("@@@LINK="):
                (preview / (digest(key.encode())[:16] + ".html")).write_text(body, encoding="utf-8")
        # Preview is an inspection aid; real-reader QA must use the MDX/MDD pair.
        for name, raw in resources.items() if sample else []:
            dest = preview / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        if sample:
            (output / "entries.json").write_text(json.dumps(qa_entries, ensure_ascii=False), encoding="utf-8")
        else:
            # Small read-back fixture for the same reader QA, without serializing
            # the full HTML corpus a second time into the distribution directory.
            qa_stems = json.loads(SAMPLE.read_text(encoding="utf-8"))
            (output / "entries.json").write_text(json.dumps({article_key(s): qa_entries[article_key(s)] for s in qa_stems}, ensure_ascii=False), encoding="utf-8")
        manifest = {"built_utc": datetime.now(timezone.utc).isoformat(), "compiler": "mdict-utils " + version("mdict-utils"),
                    "article_count": len(articles), "alias_count": sum(v.startswith("@@@LINK=") for v in entries.values()),
                    "choice_count": choices, "contributor_count": len(contributors), "topic_count": topic_count,
                    "resource_count": len(resources), "checks": {**checks, **headword_checks},
                    "roundtrip": "exact entries and resource bytes",
                    "edition": "sample" if sample else "complete", "excluded": excluded,
                    "native_search": native_search,
                    "unavailable_source_link_count": len(source_link_issues),
                    "redundant_alias_count": len(redundant_aliases),
                    "display_keys": display_keys,
                    "sample": json.loads(SAMPLE.read_text(encoding="utf-8")),
                    "input_sha256": input_hashes, "input_hash_mode": "raw bytes" if sample else "sorted-key JSON payload",
                    "index_sha256": digest(index_raw), "ancillary": ancillary,
                    "topics_sha256": digest(ct_raw), "source_assets": source_assets,
                    # Same rule as before, now owned by britannica.provenance so
                    # the EPUB records the identical thing rather than a second
                    # implementation of it.
                    "export_code_sha256": _prov.source_files(),
                    "provenance": _prov.fingerprint(),
                    "sample_spec_sha256": digest(SAMPLE.read_bytes()),
                    "reader_verification": "pending; see separate reader QA report"}
        for ext in (".mdx", ".mdd"):
            shutil.copyfile(stage / (basename + ext), output / (basename + ext))
        # Shipped text goes out LF on every platform, and the SHA256SUMS is
        # written by its one owner — see britannica.mdx.checksums.
        from britannica.mdx.checksums import write_checksums, write_shipped_text
        write_shipped_text(output / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        write_shipped_text(output / "source-link-issues.json", json.dumps(source_link_issues, ensure_ascii=False, indent=2))
        shutil.copyfile(ROOT / "src/britannica/export/download_assets/LICENSE", output / "LICENSE")
        from britannica.mdx.readme import edition_readme
        write_shipped_text(output / "README.md", edition_readme(sample, native_search))
        shipped = [basename + ".mdx", basename + ".mdd", "README.md", "LICENSE", "manifest.json", "source-link-issues.json"] + search_files
        write_checksums(output, shipped)
        with zipfile.ZipFile(output / (basename + ".zip"), "w", zipfile.ZIP_DEFLATED) as z:
            for name in shipped + ["SHA256SUMS"]:
                z.write(output / name, name)
        print(f"Finished in {time.perf_counter()-started:.1f}s", flush=True)
        print(json.dumps({k: v for k, v in manifest.items() if k.endswith("count") or k in ("checks", "roundtrip")}, indent=2))
        return manifest


def build_sample(output: Path):
    return build_edition(output, sample=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sample", action="store_true")
    mode.add_argument("--all", action="store_true")
    ap.add_argument("--output", type=Path)
    ap.add_argument("--native-search", action="store_true", help="Package canonical-title native search helpers")
    args = ap.parse_args()
    output = args.output or Path("mdx/sample" if args.sample else "mdx/complete")
    build_edition(output.resolve(), sample=args.sample, native_search=args.native_search)


if __name__ == "__main__":
    main()
