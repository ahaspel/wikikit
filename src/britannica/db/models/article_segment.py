from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from britannica.db.base import Base


class ArticleSegment(Base):
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

    segment_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )