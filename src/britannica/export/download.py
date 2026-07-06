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

from britannica.export.markdown import body_to_markdown
from britannica.markers import IMG_PARTS_RE

_SITE = "https://www.britannica11.org"
_ASSETS = Path(__file__).parent / "download_assets"   # README / LICENSE / schema


def _id(filename: str) -> str:
    """Stable public id = the filename stem (vol-page-slug-TITLE)."""
    return filename[:-5] if filename.endswith(".json") else filename


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
                    nodes[tuple(path)]["articles"].append(_id(fn))
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


def build_download(articles_dir: str = "data/derived/articles",
                   classified_toc: str = "data/derived/classified_toc.json",
                   out_dir: str = "data/derived/download",
                   version: str = "1.0",
                   limit: int | None = None) -> dict:
    arts = Path(articles_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    toc = json.loads(Path(classified_toc).read_text(encoding="utf-8"))
    topic_nodes, reverse = _topic_index(toc)
    (out / "topics.json").write_text(
        json.dumps(topic_nodes, ensure_ascii=False, indent=1), encoding="utf-8")

    roster: dict[str, dict] = {}          # initials → {name, credentials, bio, articles}
    n_arts = n_edges = 0
    files = sorted(arts.glob("*.json"))
    with (out / "articles.jsonl").open("w", encoding="utf-8") as af, \
         (out / "xref_edges.jsonl").open("w", encoding="utf-8") as ef:
        for fp in files:
            if fp.name in ("index.json", "contributors.json"):
                continue
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(d, dict) or not d.get("body"):
                continue
            aid = _id(fp.name)
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
                to = _id(x["target_filename"])
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
    for name in ("README.md", "LICENSE", "schema.json"):
        shutil.copyfile(_ASSETS / name, out / name)
    manifest = {
        "name": "encyclopaedia-britannica-11th-edition",
        "version": version,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    archive = out.parent / "eb1911-corpus.tar.gz"   # stable name; version in manifest
    with tarfile.open(archive, "w:gz") as tar:
        for fp in sorted(out.glob("*")):
            tar.add(fp, arcname=f"eb1911/{fp.name}")
    (out.parent / f"{archive.name}.sha256").write_text(
        f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")

    return {"articles": n_arts, "xref_edges": n_edges,
            "topic_nodes": len(topic_nodes), "contributors": len(contributors),
            "version": version, "archive": str(archive), "out_dir": str(out)}


if __name__ == "__main__":
    import sys
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(build_download(limit=lim), indent=2))
