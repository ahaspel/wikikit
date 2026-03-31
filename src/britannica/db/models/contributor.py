from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from britannica.db.base import Base


class Contributor(Base):
    __tablename__ = "contributors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    initials: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArticleContributor(Base):
    __tablename__ = "article_contributors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id"),
        nullable=False,
    )

    contributor_id: Mapped[int] = mapped_column(
        ForeignKey("contributors.id"),
        nullable=False,
    )

    sequence: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
