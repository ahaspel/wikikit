---
license: cc-by-sa-4.0
language:
- en
pretty_name: "Encyclopædia Britannica, 11th Edition (1911) — Full Corpus with Knowledge Graphs"
size_categories:
- 10K<n<100K
tags:
- encyclopedia
- history
- reference
- knowledge-graph
- graphrag
- rag
task_categories:
- text-retrieval
- question-answering
- text-generation
---

# Encyclopædia Britannica, 11th Edition (1911) — Full Corpus with Knowledge Graphs

The complete text of the 1911 *Encyclopædia Britannica* — all ~37,000 articles — as clean
Markdown, together with **three knowledge graphs** reconstructed from the edition and its
index volume: a cross-reference graph, a subject taxonomy, and an authorship graph. None of
the three exist in the source text; they were rebuilt from the printed classified index and
the contributor tables, and they are the reason this is more than a text dump.

Rendered from [britannica11.org](https://www.britannica11.org).

## What's in the bundle

| file | what it is |
|---|---|
| `articles.jsonl` | one record per article — the text as Markdown, plus metadata, sections, and denormalized categories / cross-references / contributors |
| `xref_edges.jsonl` | the **cross-reference graph** — one `{from, to, display}` edge per resolved internal link |
| `topics.json` | the **subject taxonomy** — the vol-29 classified index as nodes `{id, name, path, parent, articles}` |
| `contributors.json` | the **authorship graph** — the roster of scholars who wrote the edition, each with the article ids they contributed |
| `manifest.json` | version, exact counts, and SHA-256 checksums |
| `schema.json` | the JSON Schema for an `articles.jsonl` record |
| `LICENSE` | terms (CC-BY-SA 4.0) and attribution |

## The article record

```json
{
  "id": "21-0935-poland-POLAND",
  "title": "POLAND",
  "type": "article",
  "volume": 21,
  "page_start": 902,
  "word_count": 42817,
  "url": "https://www.britannica11.org/article/21-0935-poland-POLAND",
  "categories": ["history/europe-(continental)/general"],
  "sections": [{"title": "Polish Literature", "slug": "polish-literature", "level": 1}],
  "contributors": [{"initials": "R. N. B.", "name": "Robert Nisbet Bain"}],
  "images": [{"file": "..."}],
  "xrefs": [{"to": "21-0962-poland-russian-POLAND__RUSSIAN", "display": "Poland, Russian"}],
  "markdown": "(Polish *Polska* …)"
}
```

Field-by-field types are in `schema.json`. Images are carried as **references** (`file`),
not binaries — the corpus stays light for text pipelines, and vision pipelines can fetch the
image on demand.

## Why the graphs matter

Every existing digitization of the 1911 Britannica is flat OCR text. This one carries
structure that had to be *reconstructed*, not extracted:

- **Cross-reference graph** (`xref_edges.jsonl`) — every article joined to the articles it
  names, resolved and disambiguated (Zürich the city and Zürich the canton are distinct
  nodes). GraphRAG fuel.
- **Subject taxonomy** (`topics.json`) — all ~37,000 articles classified into the edition's
  own subject hierarchy, taken from the printed vol-29 index. Scope retrieval to a subtree
  ("everything under *Zoology*").
- **Authorship graph** (`contributors.json`) — the cryptic initials ("R. N. B.") resolved to
  named scholars with their credentials: a prosopography of 1911 scholarship, and an
  authority/provenance signal.

## Loading

```python
import json

articles = [json.loads(l) for l in open("articles.jsonl", encoding="utf-8")]
edges    = [json.loads(l) for l in open("xref_edges.jsonl", encoding="utf-8")]
topics   = json.load(open("topics.json", encoding="utf-8"))
authors  = json.load(open("contributors.json", encoding="utf-8"))
```

```python
from datasets import load_dataset
ds = load_dataset("britannica11/eb1911", data_files="articles.jsonl", split="train")
```

## Content note

This is a **1911** encyclopedia. Its science is a century out of date; its geography and
politics describe a vanished world; and it contains framings, terms, and views — on race,
empire, and much else — that were the educated consensus of its time and are offensive by
modern standards. It is preserved and distributed as a **historical document**, not a current
reference. If you train or ground a model on it, treat it accordingly.

## Provenance

The text is the 1911 *Encyclopædia Britannica* (public domain), transcribed by
[Wikisource](https://en.wikisource.org/wiki/1911_Encyclopædia_Britannica) and the Internet
Archive. This corpus renders that text through a faithful marker-encoded pipeline, and
reconstructs the three graphs from the printed vol-29 classified index and the per-volume
contributor tables. Full methodology: [britannica11.org/about](https://www.britannica11.org/about.html).

## License & attribution

Released under **CC-BY-SA 4.0**. The underlying 1911 text is in the public domain; the
transcription is courtesy of Wikisource; the Markdown rendering, the resolved cross-reference
and authorship graphs, and the machine-readable subject taxonomy are the contribution of this
project. Please attribute as:

> *Encyclopædia Britannica, 11th Edition corpus — britannica11.org — CC-BY-SA 4.0.*

Exact counts, version, and checksums are in `manifest.json`.
