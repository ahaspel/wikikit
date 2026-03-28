from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from britannica.settings import settings

engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)