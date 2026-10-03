#!/usr/bin/env python3
"""Import a book's pages into the database: one volume, or every volume.

The pages are read through `wikikit.source_pages.load_payloads` — the book's
fetched pages with its supplied ones (transcribed from the scans) laid over
them — so the database holds exactly what every other reader reads.  Each
page records where it came from (`source_name`: "wikisource" or "scan").

    uv run python -m wikikit.pipeline.import_wikisource_pages --volume 3 [--overwrite]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from wikikit.db.models import SourcePage
from wikikit.db.session import SessionLocal
from wikikit.source_pages import load_payloads


def _source(payload: dict) -> str:
    return payload.get("source") or "wikisource"


def get_existing(session, volume: int, page_number: int) -> SourcePage | None:
    return (
        session.query(SourcePage)
        .filter(SourcePage.volume == volume, SourcePage.page_number == page_number)
        .one_or_none()
    )


def build_page(payload: dict) -> SourcePage:
    return SourcePage(
        source_name=_source(payload),
        volume=payload["volume"],
        page_number=payload["page_number"],
        raw_text=payload["raw_text"],          # raw wikitext; the walk's preprocess
        wikitext=payload["raw_text"],          # does the cleaning the old cleaned_preview used to
    )


def update_page(existing: SourcePage, payload: dict) -> None:
    existing.raw_text = payload["raw_text"]
    existing.source_name = _source(payload)
    existing.wikitext = payload["raw_text"]


def import_payload(path: Path, payload: dict, overwrite: bool) -> str:
    volume, page_number = payload["volume"], payload["page_number"]
    session = SessionLocal()
    try:
        existing = get_existing(session, volume, page_number)
        if existing:
            if not overwrite:
                return f"SKIP   vol={volume} page={page_number} {path.name}"
            update_page(existing, payload)
            session.commit()
            return f"UPDATE vol={volume} page={page_number} {path.name}"
        session.add(build_page(payload))
        session.commit()
        return f"INSERT vol={volume} page={page_number} {path.name}"
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--volume", type=int, help="one volume (default: every volume)")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    payloads, _ = load_payloads(args.volume)          # strict: an unreadable page stops the import
    if not payloads:
        raise SystemExit(f"No pages for volume {args.volume}" if args.volume else "No pages")
    inserted = skipped = 0
    for path, payload in payloads:
        result = import_payload(path, payload, overwrite=args.overwrite)
        print(result)
        if result.startswith("SKIP"):
            skipped += 1
        else:
            inserted += 1
    print()
    print(f"Imported/updated: {inserted}")
    print(f"Skipped:          {skipped}")


if __name__ == "__main__":
    main()
