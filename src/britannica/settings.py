from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # `book.env` is COMMITTED by the book's repository and names its book;
        # `.env` is private (credentials) and may override it.
        env_file=("book.env", ".env"),
        env_prefix="BRITANNICA_",
        extra="ignore",
    )

    env: str = "development"
    data_dir: Path = Path("./data")
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/britannica"
    log_level: str = "INFO"
    #: Which book this process is building, by import path —
    #: ``britannica.books.eb1911:EB1911`` — see ``britannica.corpora``.  It sits
    #: beside ``database_url`` on purpose: one run reads one database and
    #: therefore one book, so the two are chosen together or not at all.
    #: NO DEFAULT: it was "eb1911", which was the engine naming a book.  The
    #: book's repository names itself in its committed ``book.env``.
    corpus: str


settings = Settings()