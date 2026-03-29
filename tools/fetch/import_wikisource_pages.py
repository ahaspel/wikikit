#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from britannica.db.session import SessionLocal
from britannica.db.models import SourcePage


def load_payload(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_existing(session, volume: int, page_number: int) -> SourcePage | None:
    return (
        session.query(SourcePage)
        .filter(
            SourcePage.volume == volume,
            SourcePage.page_number == page_number,
        )
        .one_or_none()
    )


def build_page(payload: dict) -> SourcePage:
    return SourcePage(
        source_name="wikisource",
        volume=payload["volume"],
        page_number=payload["page_number"],
        raw_text=payload["cleaned_preview"],   # ← important choice
        cleaned_text=None,                     # pipeline will populate this
    )


def update_page(existing: SourcePage, payload: dict) -> None:
    existing.raw_text = payload["cleaned_preview"]
    existing.source_name = "wikisource"
    existing.cleaned_text = None  # force re-clean if re-importing


def import_file(path: Path, overwrite: bool) -> str:
    payload = load_payload(path)

    volume = payload["volume"]
    page_number = payload["page_number"]

    session = SessionLocal()

    try:
        existing = get_existing(session, volume, page_number)

        if existing:
            if not overwrite:
                return f"SKIP   vol={volume} page={page_number} {path.name}"

            update_page(existing, payload)
            session.commit()
            return f"UPDATE vol={volume} page={page_number} {path.name}"

        page = build_page(payload)
        session.add(page)
        session.commit()

        return f"INSERT vol={volume} page={page_number} {path.name}"

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--indir", type=Path, required=True)
    parser.add_argument("--volume", type=int)
    parser.add_argument("--overwrite", action="store_true")

    args = parser.parse_args()

    files = sorted(args.indir.glob("*.json"))
    if not files:
        raise SystemExit(f"No JSON files found in {args.indir}")

    inserted = 0
    skipped = 0

    for path in files:
        payload = load_payload(path)

        if args.volume and payload["volume"] != args.volume:
            continue

        result = import_file(path, overwrite=args.overwrite)
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