from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from britannica.db.base import Base


class ArticleSegment(Base):
    """Where each source page BEGINS inside its article's body — a page KEY.

    Formerly this held the article's text cut into per-page pieces, which every
    consumer then glued back together — and glued back DIFFERENTLY: `""` in
    `transform_articles`, `"\\n"` in `author_links`, `"\\n\\n"` in half the
    diagnostics, and `" "`-unless-the-next-piece-starts-with-an-image in
    `DetectedArticle.body`.  Four answers, because the cut destroyed the
    information needed to invert it.  That round trip is what leaked `:` into
    BIRD's taxonomy, ate ROOFS' page marker, and blocked dehyphenation at every
    seam ([[project_page_position_out_of_band]]).

    The body now lives whole in `Article.body`, exactly as sliced from the clean
    volume stream and never edited.  This table keeps only the RELATION that was
    always right — which pages an article spans, in order — plus the offset where
    each one starts.  Nothing is reassembled, so nothing can be reassembled
    wrongly.
    """

    __tablename__ = "article_segments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id"),
        nullable=False,
    )

    source_page_id: Mapped[int] = mapped_column(
        ForeignKey("source_pages.id"),
        nullable=False,
    )

    sequence_in_article: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    # Byte offset into `Article.body` where this page's text begins.  The first
    # key of an article is 0 by construction (the body starts on its first page);
    # `Article.body[key.offset:]` is that page onward, so "which article is page
    # 740 in, and where" is a plain query — no scan, no re-derivation.
    offset: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )