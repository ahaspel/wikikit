from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from britannica.db.base import Base

# Import models so they are registered on Base.metadata
from britannica.db.models import Article, ArticleSegment, SourcePage  # noqa: F401


@pytest.fixture()
def test_session_local(tmp_path: Path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    TestingSessionLocal = sessionmaker(bind=engine)

    Base.metadata.create_all(bind=engine)

    try:
        yield TestingSessionLocal
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()