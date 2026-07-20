"""The exported corpus as a collection — ONE loader, ONE writer, ONE error posture.

Every post-export phase (math annotation, contributor binding, xref resolution +
render, the download bundle, the audits) walks ``data/derived/articles/*.json``.
Each used to roll its own ``for fn in glob(): try: json.loads() except: continue``
loop, and that ``continue`` is a sweeper in the highest-stakes place we have: a
corrupt article silently ships STALE (no xrefs, no ``rendered_html``) or silently
vanishes from the public download bundle, and every count downstream still looks
right ([[feedback_sweepers_hide_bugs]], [[feedback_honesty_surface_failures]]).

So loading is total: a payload that won't parse, or that lacks the fields a phase
needs, is collected and reported — ALL of them, not just the first — and then
raises.  There is no partial-corpus success mode.  A caller that genuinely wants
to survive damage (an audit run against a half-built corpus) passes
``strict=False`` and gets the failures back to report itself.
"""
from __future__ import annotations

import json
from pathlib import Path

ARTICLES_DIR = Path("data/derived/articles")
# Sidecar files in the articles dir that are NOT articles.
NON_ARTICLE = frozenset({"index.json", "contributors.json"})


class CorpusLoadError(RuntimeError):
    """One or more article payloads could not be loaded."""

    def __init__(self, failures: list[tuple[Path, str]]):
        self.failures = failures
        listed = "\n".join(f"    {p.name}: {why}" for p, why in failures[:20])
        more = "" if len(failures) <= 20 else f"\n    … and {len(failures) - 20} more"
        super().__init__(
            f"{len(failures)} article payload(s) failed to load — refusing to run a "
            f"phase over a partial corpus:\n{listed}{more}")


def load_corpus(art_dir: Path | str = ARTICLES_DIR, *,
                require: tuple[str, ...] = ("id", "body"),
                strict: bool = True) -> tuple[dict[Path, dict], list]:
    """Load every article payload → ``({path: payload}, failures)``.

    ``require`` names the fields a payload must carry to be an article; a file
    missing them is a FAILURE, not a silent skip (that is how an article with no
    ``id`` used to drop out of a phase unnoticed).  ``strict`` (default) raises
    ``CorpusLoadError`` when anything failed.
    """
    art_dir = Path(art_dir)
    payloads: dict[Path, dict] = {}
    failures: list[tuple[Path, str]] = []
    for fn in sorted(art_dir.glob("*.json")):
        if fn.name in NON_ARTICLE:
            continue
        try:
            d = json.loads(fn.read_text(encoding="utf-8"))
        except Exception as e:
            failures.append((fn, f"unreadable/unparseable: {e}"))
            continue
        if not isinstance(d, dict):
            failures.append((fn, f"not a JSON object ({type(d).__name__})"))
            continue
        missing = [k for k in require if k not in d]
        if missing:
            failures.append((fn, f"missing field(s): {', '.join(missing)}"))
            continue
        payloads[fn] = d
    if failures and strict:
        raise CorpusLoadError(failures)
    return payloads, failures


def write_payload(path: Path, payload: dict) -> None:
    """Write one article payload back in the canonical on-disk form (the shape
    every phase and every golden compares against)."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def write_corpus(payloads: dict[Path, dict]) -> int:
    """Write every payload back.  The single write point for a post-export phase,
    so a phase's output is all-or-nothing rather than half-applied."""
    for path, payload in payloads.items():
        write_payload(path, payload)
    return len(payloads)
