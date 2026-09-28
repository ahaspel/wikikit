"""Build the public download bundle from the derived corpus — the corpus and its
three knowledge graphs, none of which exist in the Wikisource source:

    articles.jsonl    one agent-ready record per article: the Markdown body
                      (``body_to_markdown``) + metadata, with denormalized
                      ``categories`` / ``xrefs`` / ``contributors`` so each record
                      is self-contained.
    xref_edges.jsonl  the cross-reference graph — one ``{from, to, display}`` per
                      RESOLVED internal link.
    topics.json       the subject taxonomy (vol-29 classified index) as flat nodes
                      ``{id, name, path, parent, articles}`` — the reconstruction
                      that took the work.
    contributors.json the authorship graph — a scholar ROSTER (initials → name,
                      credentials, bio) each carrying the article ids they wrote.

One pass over the article JSONs; the taxonomy comes from ``classified_toc.json``.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from britannica.corpora import brand, current_corpus
from britannica.export.markdown import body_to_markdown
from britannica.markers import IMG_PARTS_RE
from britannica.export.article_json import stable_id_from_filename
from britannica.export.corpus import NON_ARTICLE
from britannica.export.tei import EDITION_DOI

# The CANONICAL host is the apex.  `www` had no DNS record at all until
# 2026-08-22, so every url in every published bundle — and in the HuggingFace
# dataset — pointed at a host that did not resolve.  `www` now exists and 301s
# here, which repairs copies ALREADY downloaded; this line stops new ones
# carrying the non-canonical form.
_SITE = brand("site")   # the book's; one owner in corpora
_ASSETS = Path(__file__).parent / "download_assets"   # README / LICENSE / schema


def _archive_name(part: str) -> str:
    """`<slug>-<part>.tar.gz` — the ONE naming rule for the three download
    archives (corpus, maps, TEI).  Each spelled it out separately."""
    return f"{brand('slug')}-{part}.tar.gz"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _topic_index(classified_toc: dict) -> tuple[list[dict], dict[str, list[str]]]:
    """Walk ``classified_toc`` → (flat topic nodes, filename→[node-id] reverse map).

    A category node carries ``name`` + ``subsections``; a leaf entry carries
    ``filename``.  Each node gets a path-slug id, its parent, and the article ids
    filed directly under it; the reverse map lets each article name its topics.
    """
    nodes: dict[tuple, dict] = {}         # path-tuple → node
    reverse: dict[str, list[str]] = {}

    def slug(path: list[str]) -> str:
        return "/".join(p.lower().replace(" ", "-").replace(">", "") for p in path)

    def ensure(path: list[str]) -> str:
        key = tuple(path)
        if key not in nodes:
            nodes[key] = {"id": slug(path), "name": path[-1],
                          "path": " > ".join(path),
                          "parent": slug(path[:-1]) if len(path) > 1 else None,
                          "articles": []}
        return nodes[key]["id"]

    def walk(node, path: list[str]) -> None:
        if isinstance(node, dict):
            fn = node.get("filename")
            if fn:                        # a LEAF entry — file it under this category
                if path:
                    nid = ensure(path)
                    nodes[tuple(path)]["articles"].append(stable_id_from_filename(fn))
                    reverse.setdefault(fn, [])
                    if nid not in reverse[fn]:
                        reverse[fn].append(nid)
                return
            name = node.get("name")
            here = path + [name] if name else path
            if name:
                ensure(here)
            for v in node.values():       # recurse EVERY child-bearing value
                if isinstance(v, (list, dict)):
                    walk(v, here)
        elif isinstance(node, list):
            for v in node:
                walk(v, path)

    for top in classified_toc.get("categories", []):
        walk(top, [])
    return list(nodes.values()), reverse


def build_download(articles_dir: str | None = None,
                   classified_toc: str | None = None,
                   out_dir: str | None = None,
                   version: str = "1.0",
                   limit: int | None = None) -> dict:
    arts = Path(articles_dir or current_corpus().derived("articles"))
    out = Path(out_dir or current_corpus().derived("download"))
    out.mkdir(parents=True, exist_ok=True)

    toc = json.loads(Path(classified_toc or current_corpus().derived("classified_toc.json")).read_text(encoding="utf-8"))
    topic_nodes, reverse = _topic_index(toc)
    (out / "topics.json").write_text(
        json.dumps(topic_nodes, ensure_ascii=False, indent=1), encoding="utf-8")

    roster: dict[str, dict] = {}          # initials → {name, credentials, bio, articles}
    n_arts = n_edges = 0
    # Total load: a payload that won't parse RAISES rather than being skipped —
    # a silent skip here drops an article from the PUBLIC dataset while every
    # count downstream still looks right ([[feedback_honesty_surface_failures]]).
    from britannica.export.corpus import load_corpus
    payloads, _ = load_corpus(arts, require=("body",))
    with (out / "articles.jsonl").open("w", encoding="utf-8") as af, \
         (out / "xref_edges.jsonl").open("w", encoding="utf-8") as ef:
        for fp, d in sorted(payloads.items()):
            if not d.get("body"):
                continue
            aid = stable_id_from_filename(fp.name)
            body = d["body"]

            contribs = [{"initials": c.get("initials"), "name": c.get("full_name")}
                        for c in d.get("contributors") or []]
            for c in d.get("contributors") or []:
                r = roster.setdefault(c.get("initials") or c.get("full_name") or "?", {
                    "initials": c.get("initials"), "name": c.get("full_name"),
                    "credentials": c.get("credentials"),
                    "bio": c.get("description"), "articles": []})
                r["articles"].append(aid)

            xrefs = []
            for x in d.get("xrefs") or []:
                if x.get("status") != "resolved" or not x.get("target_filename"):
                    continue
                to = stable_id_from_filename(x["target_filename"])
                disp = (x.get("normalized_target") or "").title()
                xrefs.append({"to": to, "display": disp})
                ef.write(json.dumps({"from": aid, "to": to, "display": disp},
                                    ensure_ascii=False) + "\n")
                n_edges += 1

            images = [{"file": m.group(1).strip()}
                      for m in IMG_PARTS_RE.finditer(body)]

            record = {
                "id": aid,
                "title": d.get("title"),
                "type": d.get("article_type"),
                "volume": d.get("volume"),
                "page_start": d.get("page_start"),
                "page_end": d.get("page_end"),
                "word_count": d.get("word_count"),
                "url": f"{_SITE}/article/{aid}",
                "categories": reverse.get(fp.name, []),
                "sections": [{"title": s.get("title"), "slug": s.get("slug"),
                              "level": s.get("level")} for s in d.get("sections") or []],
                "contributors": contribs,
                "images": images,
                "xrefs": xrefs,
                "markdown": body_to_markdown(body),
            }
            af.write(json.dumps(record, ensure_ascii=False) + "\n")
            n_arts += 1
            if limit and n_arts >= limit:
                break

    contributors = sorted(roster.values(),
                          key=lambda r: (r["name"] or r["initials"] or ""))
    (out / "contributors.json").write_text(
        json.dumps(contributors, ensure_ascii=False, indent=1), encoding="utf-8")

    # Validate: every articles.jsonl line must parse and the count must match —
    # catches truncation / encoding corruption before the bundle ships.
    seen = 0
    with (out / "articles.jsonl").open(encoding="utf-8") as f:
        for line in f:
            json.loads(line)
            seen += 1
    if seen != n_arts:
        raise RuntimeError(f"articles.jsonl has {seen} lines, expected {n_arts}")

    # Self-describing docs + a manifest with SHA-256 over the FINAL files.
    # The README is the HuggingFace dataset card — the ONE page a reader sees —
    # and HF surfaces update times only in the commit list, so the card itself
    # carries the build date: the {{GENERATED}} placeholder is filled with the
    # same instant the manifest records.
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in ("README.md", "LICENSE", "schema.json"):
        shutil.copyfile(_ASSETS / name, out / name)
    readme = out / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "{{GENERATED}}", generated[:10]),
        encoding="utf-8")
    manifest = {
        "name": "encyclopaedia-britannica-11th-edition",
        "version": version,
        "generated": generated,
        "license": "CC-BY-SA-4.0",
        "source": _SITE,
        "counts": {"articles": n_arts, "xref_edges": n_edges,
                   "topic_nodes": len(topic_nodes), "contributors": len(contributors)},
        "files": [{"name": fp.name, "bytes": fp.stat().st_size, "sha256": _sha256(fp)}
                  for fp in sorted(out.glob("*")) if fp.name != "manifest.json"],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    # A single gzip archive of the whole bundle — what the download link points at —
    # with its own checksum beside it for verification.
    slug = brand("slug")
    archive = out.parent / _archive_name("corpus")   # stable name; version in manifest
    with tarfile.open(archive, "w:gz") as tar:
        for fp in sorted(out.glob("*")):
            tar.add(fp, arcname=f"{slug}/{fp.name}")
    (out.parent / f"{archive.name}.sha256").write_text(
        f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")

    return {"articles": n_arts, "xref_edges": n_edges,
            "topic_nodes": len(topic_nodes), "contributors": len(contributors),
            "version": version, "archive": str(archive), "out_dir": str(out)}


def build_maps_bundle(maps_json: str | None = None,
                      images_dir: str | None = None,
                      out_dir: str | None = None) -> dict:
    """Archive the colour maps — the EB1911 plates and the Stieler originals —
    as a bundle of their own (eb1911-maps.tar.gz).  Kept separate from the
    corpus bundle: ~200MB of full-resolution JPGs would bloat the agent
    dataset for consumers who only want the text and graphs.

    The registry (maps.json) rides along as the bundle's own manifest; every
    file it names must exist — a missing referenced image RAISES rather than
    shipping a bundle that silently lacks it."""
    if maps_json is None:
        # The book's own map manifest.  A book without one has no map bundle
        # to build, and asking for it is a caller's mistake — `data()` raises.
        from britannica.corpora import current_corpus
        maps_json = current_corpus().data("maps.json")
    reg = json.loads(Path(maps_json).read_text(encoding="utf-8"))
    imgs = Path(images_dir or current_corpus().images("maps"))
    files: list[Path] = []
    for row in reg.get("maps", []):
        # A row's SIDES are recognised by shape — a dict carrying `sheets`,
        # `file` or `full` — not by name.  The names ("eb1911", "stieler": the
        # book's own map and the atlas original) are the book's data; the
        # engine naming them was the book leaking into the bundler.  Rows keep
        # their JSON order, so the archive's file order is unchanged.
        for side, block in row.items():
            if not (isinstance(block, dict) and
                    (block.get("sheets") or block.get("file") or block.get("full"))):
                continue
            sheets = block.get("sheets") or [block]
            for sheet in sheets:
                for key in ("file", "full"):
                    name = sheet.get(key)
                    if not name:
                        continue
                    fp = imgs / name
                    if not fp.is_file():
                        raise RuntimeError(f"maps.json names missing file: {fp}")
                    if fp not in files:
                        files.append(fp)
    out = Path(out_dir or current_corpus().derived())
    top = brand("slug") + "-maps"
    archive = out / _archive_name("maps")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(Path(maps_json), arcname=f"{top}/maps.json")
        for fp in sorted(files):
            tar.add(fp, arcname=f"{top}/{fp.name}")
    (out / f"{archive.name}.sha256").write_text(
        f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return {"maps": len(reg.get("maps", [])), "files": len(files),
            "bytes": archive.stat().st_size, "archive": str(archive)}


def build_tei_bundle(articles_dir: str | None = None,
                     out_dir: str | None = None) -> dict:
    """Write the TEI-P5 edition and archive it as a bundle of its OWN.

    SEPARATE FROM THE CORPUS BUNDLE, for the same reason the maps are: a reader
    who wants `articles.jsonl` for text-mining does not want a 37,000-file XML
    tree, and the TEI audience does not want the JSONL.  Separation also lets the
    edition carry its own DOI if it is deposited (Zenodo / TAPAS / the Oxford
    Text Archive), which a file buried inside another archive cannot.

    ONE DOCUMENT PER ARTICLE, named by stable_id, so a file's name is the article's
    URL tail and a `<ref target="#22-0987-b3f68e">` resolves by inspection.  A
    `teiCorpus.xml` binds them with XInclude — the standard way to present a TEI
    corpus without a single multi-gigabyte file.

    Not validated here: `tools/diagnostics/tei_validate.py` is the gate (rebuild
    phase 7.7) and it validates every article against the TEI Consortium's own
    schema.  Writing files is not the place to re-answer that question.
    """
    from britannica.export.tei import article_to_tei

    src = Path(articles_dir or current_corpus().derived("articles"))
    out = Path(out_dir or current_corpus().derived())
    tei_dir = out / "tei"
    if tei_dir.exists():
        shutil.rmtree(tei_dir)
    tei_dir.mkdir(parents=True)

    n = 0
    total_bytes = 0
    names: list[str] = []
    for fp in sorted(src.glob("*.json")):
        if fp.name in NON_ARTICLE:
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("body"):
            continue
        xml = article_to_tei(d)
        name = f"{d.get('stable_id') or fp.stem}.xml"
        (tei_dir / name).write_text(xml, encoding="utf-8")
        names.append(name)
        total_bytes += len(xml.encode("utf-8"))
        n += 1

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    includes = "\n".join(
        f'  <xi:include href="{nm}"/>' for nm in names)
    (tei_dir / "teiCorpus.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<teiCorpus xmlns="http://www.tei-c.org/ns/1.0"\n'
        '           xmlns:xi="http://www.w3.org/2001/XInclude">\n'
        "<teiHeader><fileDesc>\n"
        f"<titleStmt><title>{current_corpus().title} — "
        "a TEI-P5 edition</title>\n"
        '<respStmt xml:id="wikisource"><resp>transcription</resp>'
        "<orgName>the contributors to Wikisource</orgName></respStmt></titleStmt>\n"
        f"<publicationStmt><publisher>{_SITE.split('://', 1)[-1]}</publisher>\n"
        '<availability status="free"><licence '
        'target="https://creativecommons.org/licenses/by-sa/4.0/"/></availability>\n'
        # The catalogue has a better claim to the citation than any single
        # article: it is the document that stands for the whole edition.  The
        # CONCEPT DOI, as in the members — it follows the newest deposit rather
        # than freezing on the release current when the file was written.
        f'<idno type="DOI">{EDITION_DOI}</idno>\n'
        f"<date>{generated}</date></publicationStmt>\n"
        + current_corpus().template("templates/tei_corpus_source.xml") + "\n"
        "</fileDesc></teiHeader>\n"
        f"{includes}\n"
        "</teiCorpus>\n", encoding="utf-8")

    # IDS ARE DOCUMENT-SCOPED, and the README has to say so.  Each member is
    # SELF-CONTAINED — it declares its own <respStmt xml:id="wikisource"> and its
    # own <rendition> set, which is what lets a single file validate and be read
    # alone (all 37,225 do).  The cost is that resolving every XInclude into ONE
    # tree collides: 37,225 `wikisource` ids, 37,225 `sc` renditions, and
    # `section-history` in every article that has one.  That is ordinary for a
    # file-per-member TEI corpus — the teiCorpus is a CATALOGUE and tools process
    # members individually — but a consumer who tries to assemble the whole thing
    # should learn it from the README, not from a validator.
    (tei_dir / "README.md").write_text(
        # The BOOK's prose; the engine supplies the numbers and names.
        current_corpus().template("templates/tei_readme.md", article_count=f"{n:,}",
                                  site=_SITE, slug=brand("slug"), concept_doi=EDITION_DOI),
        encoding="utf-8")

    # The ODD ships WITH the edition: it documents which part of TEI this uses,
    # which is the first thing a TEI-literate reader looks for and the difference
    # between a data dump and an edition.  It is derived from the emitted corpus
    # rather than from intention, and validates as TEI in its own right.
    # The BOOK's file (`tei_odd.xml`): its header names and describes the
    # edition, as the README template does; `data()` raises if it is missing.
    odd = current_corpus().data("tei_odd.xml")
    shipped_odd = brand("slug") + ".odd.xml"
    shutil.copy(odd, tei_dir / shipped_odd)

    top = brand("slug") + "-tei"
    archive = out / _archive_name("tei")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tei_dir / "README.md", arcname=f"{top}/README.md")
        tar.add(tei_dir / shipped_odd, arcname=f"{top}/{shipped_odd}")
        tar.add(tei_dir / "teiCorpus.xml", arcname=f"{top}/teiCorpus.xml")
        for nm in names:
            tar.add(tei_dir / nm, arcname=f"{top}/{nm}")
    (out / f"{archive.name}.sha256").write_text(
        f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")

    return {"documents": n, "uncompressed_bytes": total_bytes,
            "archive": str(archive), "archive_bytes": archive.stat().st_size,
            "out_dir": str(tei_dir)}


if __name__ == "__main__":
    import sys
    # `name corpus` prints an archive's name — for deploy.sh, which must upload
    # under the same names and so asks the one naming rule instead of
    # re-spelling `<slug>-corpus.tar.gz` in shell.
    if sys.argv[1:2] == ["name"]:
        print(_archive_name(sys.argv[2]))
        raise SystemExit(0)
    if "maps" in sys.argv[1:]:
        print(json.dumps(build_maps_bundle(), indent=2))
    elif "tei" in sys.argv[1:]:
        print(json.dumps(build_tei_bundle(), indent=2))
    else:
        lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
        print(json.dumps(build_download(limit=lim), indent=2))
