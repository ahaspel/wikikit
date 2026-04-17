from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from britannica.db.base import Base


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    article_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="article", server_default="article"
    )
    # Wikisource <section begin="X"> name for this article's starting
    # segment. Used alongside (volume, page_start) as the stable-ID
    # tiebreaker so URLs don't rot when page sequencing shifts.
    section_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )