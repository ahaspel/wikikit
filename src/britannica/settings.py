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


settings = Settings()