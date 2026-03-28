from pathlib import Path

import typer

from britannica.db.base import Base
from britannica.db.models import SourcePage
from britannica.db.session import SessionLocal, engine
from britannica.pipeline.stages.clean_pages import clean_pages

app = typer.Typer(no_args_is_help=True)


@app.command("init-db")
def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    print("Database initialized.")


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
                cleaned_text=None,
            )
            session.add(page)

        session.commit()
    finally:
        session.close()

    print(f"Imported {len(pages)} pages.")


@app.command("clean-pages")
def clean_pages_cmd(volume: int = typer.Argument(...)) -> None:
    count = clean_pages(volume)
    print(f"Cleaned {count} pages for volume {volume}.")


@app.command("list-pages")
def list_pages(volume: int = typer.Option(None)) -> None:
    session = SessionLocal()
    try:
        query = session.query(SourcePage)
        if volume is not None:
            query = query.filter(SourcePage.volume == volume)

        for page in query.order_by(SourcePage.volume, SourcePage.page_number).all():
            raw = repr(page.raw_text[:60])
            clean = repr((page.cleaned_text or "")[:60])
            print(
                f"vol={page.volume} page={page.page_number} "
                f"raw='{raw}' clean='{clean}'"
            )
    finally:
        session.close()


def run() -> None:
    app(prog_name="britannica")