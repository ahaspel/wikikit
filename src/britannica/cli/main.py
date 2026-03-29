from pathlib import Path

import typer

from britannica.db.base import Base
from britannica.db.session import SessionLocal, engine
from britannica.pipeline.stages.clean_pages import clean_pages
from britannica.db.models import Article, ArticleSegment, CrossReference, SourcePage
from britannica.pipeline.stages.detect_boundaries import detect_boundaries
from britannica.export.article_json import export_articles_to_json
from britannica.pipeline.stages.extract_contributors import extract_contributors_for_volume
from britannica.pipeline.stages.extract_images import extract_images_for_volume
from britannica.pipeline.stages.extract_xrefs import extract_xrefs_for_volume
from britannica.pipeline.stages.resolve_xrefs import resolve_xrefs_for_volume, resolve_xrefs_all
from britannica.review.reports import (
    get_unresolved_xrefs_report,
    get_backlinks_report,
)

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
        
@app.command("list-segments")
def list_segments() -> None:
    session = SessionLocal()
    try:
        segments = (
            session.query(ArticleSegment)
            .order_by(ArticleSegment.article_id, ArticleSegment.sequence_in_article)
            .all()
        )

        for seg in segments:
            print(
                f"article_id={seg.article_id} "
                f"seq={seg.sequence_in_article} "
                f"text={seg.segment_text[:60]!r}"
            )
    finally:
        session.close()
        
@app.command("detect-boundaries")
def detect_boundaries_cmd(volume: int = typer.Argument(...)) -> None:
    count = detect_boundaries(volume)
    print(f"Created {count} articles for volume {volume}.")


@app.command("list-articles")
def list_articles(volume: int = typer.Option(None)) -> None:
    session = SessionLocal()
    try:
        query = session.query(Article)
        if volume is not None:
            query = query.filter(Article.volume == volume)

        for article in query.order_by(Article.volume, Article.page_start).all():
            body = repr(article.body[:60])
            print(
                f"vol={article.volume} pages={article.page_start}-{article.page_end} "
                f"title={article.title!r} body={body}"
            )
    finally:
        session.close()
        
@app.command("extract-xrefs")
def extract_xrefs_cmd(volume: int = typer.Argument(...)) -> None:
    count = extract_xrefs_for_volume(volume)
    print(f"Extracted {count} cross-references for volume {volume}.")


@app.command("list-xrefs")
def list_xrefs(volume: int = typer.Option(None)) -> None:
    session = SessionLocal()
    try:
        query = session.query(CrossReference, Article).join(
            Article, CrossReference.article_id == Article.id
        )
        if volume is not None:
            query = query.filter(Article.volume == volume)

        rows = query.order_by(Article.title, CrossReference.id).all()

        for xref, article in rows:
            print(
                f"article={article.title!r} "
                f"type={xref.xref_type!r} "
                f"surface={xref.surface_text!r} "
                f"target={xref.normalized_target!r} "
                f"status={xref.status!r}"
            )
    finally:
        session.close()
        
@app.command("resolve-xrefs")
def resolve_xrefs_cmd(volume: int = typer.Argument(...)) -> None:
    count = resolve_xrefs_for_volume(volume)
    print(f"Resolved {count} cross-references for volume {volume}.")


@app.command("resolve-xrefs-all")
def resolve_xrefs_all_cmd() -> None:
    count = resolve_xrefs_all()
    print(f"Resolved {count} cross-references across all volumes.")


@app.command("extract-images")
def extract_images_cmd(volume: int = typer.Argument(...)) -> None:
    count = extract_images_for_volume(volume)
    print(f"Extracted {count} images for volume {volume}.")


@app.command("extract-contributors")
def extract_contributors_cmd(volume: int = typer.Argument(...)) -> None:
    count = extract_contributors_for_volume(volume)
    print(f"Extracted {count} article-contributor links for volume {volume}.")


@app.command("report-unresolved-xrefs")
def report_unresolved_xrefs(volume: int = typer.Argument(...)) -> None:
    report = get_unresolved_xrefs_report(volume)

    if not report:
        print(f"No unresolved cross-references for volume {volume}.")
        return

    total = 0

    for article_title, xrefs in report.items():
        print(f"{article_title}:")
        for xref in xrefs:
            print(
                f"  - type={xref.xref_type!r} "
                f"surface={xref.surface_text!r} "
                f"target={xref.normalized_target!r}"
            )
            total += 1

    print(f"\nTotal unresolved cross-references: {total}")

@app.command("report-backlinks")
def report_backlinks(volume: int = typer.Argument(...)) -> None:
    report = get_backlinks_report(volume)

    if not report:
        print(f"No backlinks for volume {volume}.")
        return

    total = 0

    for target_title, xrefs in report.items():
        print(f"{target_title} ←")
        for xref in xrefs:
            print(
                f"  - from_article_id={xref.article_id} "
                f"type={xref.xref_type!r} "
                f"surface={xref.surface_text!r}"
            )
            total += 1

    print(f"\nTotal backlinks: {total}")

@app.command("export-articles")
def export_articles_cmd(
    volume: int = typer.Argument(...),
    out_dir: str = typer.Option("data/derived/articles"),
) -> None:
    count = export_articles_to_json(volume, out_dir)
    print(f"Exported {count} articles for volume {volume} to {out_dir}.")


def run() -> None:
    app(prog_name="britannica")