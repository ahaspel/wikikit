from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from britannica.db.base import Base


class SourcePage(Base):
    __tablename__ = "source_pages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    cleaned_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    wikitext: Mapped[str | None] = mapped_column(Text, nullable=True)