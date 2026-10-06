from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from wikikit.settings import settings

if not settings.database_url:
    raise RuntimeError("no database: set WIKIKIT_DATABASE_URL in this repository's private .env "
                       "(transcribing scans needs none; building the book does)")
engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)