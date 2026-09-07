from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BRITANNICA_",
        extra="ignore",
    )

    env: str = "development"
    data_dir: Path = Path("./data")
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/britannica"
    log_level: str = "INFO"
    #: Which book this process is building — see ``britannica.corpora``.  It sits
    #: beside ``database_url`` on purpose: one run reads one database and
    #: therefore one book, so the two are chosen together or not at all.
    #: Defaults to the Britannica, so nothing that does not opt in can move.
    corpus: str = "eb1911"


settings = Settings()