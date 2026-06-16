from pathlib import Path

import typer

from britannica.db.base import Base
from britannica.db.session import SessionLocal, engine
from britannica.db.models import (
    Article, ArticleSegment, SourcePage)
from britannica.pipeline.stages.detect_boundaries import (
    persist_articles, wipe_articles)
from britannica.pipeline.stages.super_detect import detect_boundaries
from britannica.export.article_json import export_articles_to_json
from britannica.pipeline.assemble import assemble_and_export
from britannica.pipeline.stages.extract_contributor_bios import extract_contributor_bios

app = typer.Typer(no_args_is_help=True)


@app.command("init-db")
def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    print("Database initialized.")

@app.command("reset-volume")
def reset_volume(volume: int = typer.Argument(...)) -> None:
    session = SessionLocal()
    try:
        page_ids = [
            pid
            for (pid,) in session.query(SourcePage.id)
            .filter(SourcePage.volume == volume)
            .all()
        ]

        if page_ids:
            session.query(ArticleSegment).filter(
                ArticleSegment.source_page_id.in_(page_ids)
            ).delete(synchronize_session=False)

        session.query(Article).filter(Article.volume == volume).delete(
            synchronize_session=False
        )

        session.query(SourcePage).filter(SourcePage.volume == volume).delete(
            synchronize_session=False
        )

        session.commit()
    finally:
        session.close()

    print(f"Reset data for volume {volume}.")


@app.command("import-sample-pages")
def import_sample_pages(
    file: Path = typer.Argument(..., exists=True, dir_okay=False),
    volume: int = typer.Option(1),
) -> None:
    text = file.read_text(encoding="utf-8")
    pages = text.split("===PAGE===")

    session = SessionLocal()
    try:
        for i, page_text in enumerate(pages, start=1):
            page = SourcePage(
                source_name="sample",
                volume=volume,
                page_number=i,
                raw_text=page_text.strip(),
            )
            session.add(page)

        session.commit()
    finally:
        session.close()

    print(f"Imported {len(pages)} pages.")


@app.command("list-pages")
def list_pages(volume: int = typer.Option(None)) -> None:
    session = SessionLocal()
    try:
        query = session.query(SourcePage)
        if volume is not None:
            query = query.filter(SourcePage.volume == volume)

        for page in query.order_by(SourcePage.volume, SourcePage.page_number).all():
            raw = repr(page.raw_text[:60])
            wiki = repr((page.wikitext or "")[:60])
            print(
                f"vol={page.volume} page={page.page_number} "
                f"raw='{raw}' wiki='{wiki}'"
            )
    finally:
        session.close()
        
@app.command("detect-boundaries")
def detect_boundaries_cmd(volume: int = typer.Argument(...)) -> None:
    wipe_articles(volume)
    detected = detect_boundaries(volume)
    count = persist_articles(detected)
    print(f"Detected and created {count} articles for volume {volume}.")


@app.command("extract-contributor-bios")
def extract_contributor_bios_cmd() -> None:
    count = extract_contributor_bios()
    print(f"Updated {count} contributors with biographical data.")


@app.command("export-articles")
def export_articles_cmd(
    volume: int = typer.Argument(...),
    out_dir: str = typer.Option("data/derived/articles"),
) -> None:
    count = assemble_and_export(out_dir, only_volume=volume)
    print(f"Exported {count} articles for volume {volume} to {out_dir}.")


@app.command("corpus-export")
def corpus_export_cmd(
    out_dir: str = typer.Option("data/derived/articles"),
) -> None:
    count = assemble_and_export(out_dir)
    print(f"Assembled + exported {count} articles to {out_dir}.")


def run() -> None:
    app(prog_name="britannica")