"""Dictionary navigation from the existing roster, taxonomy and EPUB pages."""
from __future__ import annotations

from collections import defaultdict
import html
import json
import re
from urllib.parse import unquote, parse_qs, urlsplit

from britannica.epub import front_matter as FM, readers_guide as RG
from britannica.export.article_json import stable_id_from_filename
from britannica.export.download import _topic_index
from britannica.markers import strip_title_markers
from britannica.util.strings import fold_accents
from britannica.xrefs.normalizer import normalize_xref_target
from britannica.mdx.build import ROOT, SITE, PREFIX, article_key, topic_key, volume_key, entry_url, list_links, wrap, bundle_body, _section_slug, digest, add_article_topics, HREF_ATTR_RE


def add_reference_aliases(articles, aliases):
    """Use resolved TARGET spellings, never arbitrary link display text."""
    accepted, rejected = defaultdict(set), []
    for stem, a in articles.items():
        for x in a.get("xrefs") or []:
            target = stable_id_from_filename(x.get("target_filename") or "")
            if x.get("status") != "resolved" or target not in articles or x.get("target_section"):
                continue
            name = (x.get("normalized_target") or "").strip()
            if len(name) > 100:
                rejected.append({"from": stem, "target": target, "spelling": name, "reason": "Exceeds tested reader's 100-character lookup limit"})
                continue
            if not name or any(c in name for c in ("«", "<", ">", "#", "\n")) or "://" in name:
                rejected.append({"from": stem, "target": target, "spelling": name, "reason": "Not a plain article lookup target"})
                continue
            # A `/` is a Wikisource PAGE PATH, so the name addresses another work
            # and is not an EB1911 lookup name — `BIBLE (KING JAMES)/NUMBERS: 17:8`
            # and `UNITED STATES STATUTES AT LARGE/VOLUME 1/1ST CONGRESS/...` were
            # both indexed as headwords, stacking rows in the reader's list under
            # an article whose name they are not.  The same cross-work leak as
            # `{{DNB lkpl}}`, arriving by a different route.
            # No EB1911 title contains a `/` (checked against the whole corpus and
            # the shipped index), so this can only remove foreign addresses.
            if "/" in name:
                rejected.append({"from": stem, "target": target, "spelling": name,
                                 "reason": "Wikisource page path, not an EB1911 article name"})
                continue
            for spelling in {name, normalize_xref_target(fold_accents(name))}:
                aliases[spelling].add(target)
                accepted[spelling].add(target)
    return {"accepted": {k: sorted(v) for k, v in sorted(accepted.items())}, "rejected": rejected}


def load_contributors(articles, contributors):
    roster = json.loads((ROOT / "data/derived/articles/contributors.json").read_text(encoding="utf-8"))
    for c in roster:
        slug = _section_slug(c["full_name"])
        entry = contributors.setdefault(slug, {"person": c, "articles": []})
        if entry["person"]["full_name"] != c["full_name"]:
            raise ValueError(f"Contributor key collision: {slug}")
        entry["person"] = c  # roster carries biography links and display names
        for a in c.get("articles") or []:
            stem = stable_id_from_filename(a["filename"])
            if stem not in articles:
                raise ValueError(f"Contributor article outside edition: {stem}")
            if stem not in entry["articles"]:
                entry["articles"].append(stem)


def full_help(count):
    return (f"<h1>Britannica 11</h1><p>Complete offline reference edition: {count:,} articles and plates. "
            "Type an article title in the reader’s lookup box. Use its full-text search to find words within articles; initial indexing may take time.</p>"
            + list_links((label, entry_url(PREFIX + key)) for label, key in [
                ("Introduction and prefaces", "introduction"), ("Volumes", "volumes"),
                ("Topics", "topics"), ("Contributors", "contributors"),
                ("Reader’s Guide", "page:guide.xhtml")])
            + '<p>Encyclopædia Britannica, Eleventh Edition (1910–1911). Transcription from Wikisource; '
              'digital edition by britannica11.org. See the accompanying license and build manifest. '
              '<a href="https://britannica11.org">Website (online)</a>.</p>')


def add_navigation(entries, articles, contributors, ct, policy, resources, source_assets):
    # _topic_index owns the topic IDs and membership mapping. The original tree
    # additionally carries notes, emphasis and unresolved printed index entries.
    flat, memberships = _topic_index(ct)
    by_path = {n["path"]: n for n in flat}

    def topic_link(n):
        return entry_url(topic_key(n["id"]))

    def nodes(node, parents):
        path = parents + [node["name"]]
        info = by_path[" > ".join(path)]
        body = "<h1>" + html.escape(" › ".join(path)) + "</h1>"
        if parents:
            body += list_links([("Parent topic", topic_link(by_path[" > ".join(parents)]))])
        for note in node.get("notes") or []:
            text, pos, parts = note.get("text", ""), 0, []
            for link in sorted(note.get("links") or [], key=lambda l: l["start"]):
                start, end = link["start"], link["end"]
                if not (pos <= start <= end <= len(text)):
                    raise ValueError("Invalid topic note offsets")
                parts.append(html.escape(text[pos:start]))
                label = html.escape(text[start:end])
                anchor = link.get("anchor") or ""
                if anchor.startswith("art:"):
                    url = policy.url_for(stable_id_from_filename(anchor[4:]))
                elif anchor in by_path:
                    url = topic_link(by_path[anchor])
                else:
                    raise ValueError(f"Unknown topic note target: {anchor}")
                parts.append(f'<a href="{url}">{label}</a>')
                pos = end
            parts.append(html.escape(text[pos:]))
            body += "<p><i>" + "".join(parts) + "</i></p>"
        children = (node.get("subsections") or []) + (node.get("children") or [])
        body += list_links((c["name"], topic_link(by_path[" > ".join(path + [c["name"]])])) for c in children)
        body += "<ul>"
        for a in node.get("articles") or []:
            label = html.escape(a.get("display") or a.get("target") or "")
            if a.get("emphasized") in (True, "True", "true"):
                label = "<b>" + label + "</b>"
            if a.get("filename"):
                stem = stable_id_from_filename(a["filename"])
                label = f'<a href="{policy.url_for(stem)}">{label}</a>'
            body += "<li>" + label + "</li>"
        entries[topic_key(info["id"])] = wrap(body + "</ul>")
        for child in children:
            nodes(child, path)

    for node in ct["categories"]:
        nodes(node, [])
    entries[PREFIX + "topics"] = wrap("<h1>Topics</h1>" + list_links(
        (n["name"], topic_link(by_path[n["name"]])) for n in ct["categories"]))
    topic_by_id = {n["id"]: n for n in flat}
    for stem, a in articles.items():
        ids = memberships.get(stem + ".json", [])
        if ids:
            links = [(topic_by_id[i]["path"], topic_link(topic_by_id[i])) for i in ids]
            entries[article_key(stem)] = add_article_topics(entries[article_key(stem)], links)

    entries[PREFIX + "contributors"] = wrap("<h1>Contributors</h1>" + list_links(
        (c["person"].get("display_name") or c["person"]["full_name"], entry_url(PREFIX + "contributor:" + slug))
        for slug, c in sorted(contributors.items(), key=lambda pair: pair[1]["person"].get("display_name") or pair[1]["person"]["full_name"])))
    volumes = defaultdict(list)
    for stem, a in articles.items():
        volumes[a["volume"]].append(stem)
    for volume, stems in sorted(volumes.items()):
        stems.sort(key=lambda s: (articles[s]["page_start"], articles[s].get("page_end") or 0, articles[s]["title"], s))
        entries[volume_key(volume)] = wrap(f"<h1>Volume {volume}</h1>" + list_links(
            (strip_title_markers(articles[s]["title"]), entry_url(article_key(s))) for s in stems))
    entries[PREFIX + "volumes"] = wrap("<h1>Volumes</h1>" + list_links(
        (f"Volume {v}", entry_url(volume_key(v))) for v in sorted(volumes)))

    FM.DROPPED_HREFS.clear()
    front = FM.pages()
    guide, images = RG.pages()
    if FM.DROPPED_HREFS:
        raise ValueError(f"Ancillary extraction dropped malformed source links: {FM.DROPPED_HREFS}")
    page_map = {f: PREFIX + "page:" + f for f, *_ in front + guide}
    signature_map = {c["person"].get("slug"): slug for slug, c in contributors.items()}
    name_map = {c["person"]["full_name"]: slug for slug, c in contributors.items()}

    def internalize(body):
        def href(m):
            url = html.unescape(m[2])
            u = urlsplit(url)
            if not u.netloc or u.netloc == "britannica11.org":
                base = u.path.lstrip("/")
                if base in page_map:
                    url = entry_url(page_map[base], unquote(u.fragment))
                elif base.startswith("article/"):
                    stem = base.split("/")[1]
                    url = policy.url_for(stem)
                    if u.fragment:
                        url += "#" + u.fragment
                elif base == "contributors.html":
                    query_name = parse_qs(u.query).get("q", [""])[0]
                    slug = signature_map.get(u.fragment) or name_map.get(query_name)
                    url = entry_url(PREFIX + "contributor:" + slug) if slug else entry_url(PREFIX + "contributors")
            return 'href=' + m[1] + html.escape(url, quote=True) + m[1]
        return HREF_ATTR_RE.sub(href, body)

    for name, path in images.items():
        with open(path, "rb") as source:
            data = source.read()
        resources["images/" + name] = data
    for filename, title, body, *_ in front + guide:
        body = internalize(body)
        body = bundle_body(body, resources, source_assets, sample=False)
        if not re.search(r"<h1\b", body, re.I):
            body = "<h1>" + html.escape(title) + "</h1>" + body
        entries[page_map[filename]] = wrap('<div class="frontmatter">' + body + "</div>")
    entries[PREFIX + "introduction"] = wrap("<h1>Introduction and prefaces</h1>" + list_links(
        (title, entry_url(page_map[filename])) for filename, title, _ in front))
    sources = [ROOT / "docs/introduction.txt", ROOT / "data/derived/articles/contributors.json"]
    sources += list((ROOT / "tools/viewer").glob("readers-guide*.html"))
    sources += [ROOT / "tools/viewer/preface.html", ROOT / "tools/viewer/ancillary-prefatory-note.html"]
    return {"topic_count": len(flat), "front_matter_count": len(front), "guide_page_count": len(guide),
            "volume_count": len(volumes), "source_sha256": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sources},
            "guide_image_sha256": {n: digest(resources["images/" + n]) for n in images}}
