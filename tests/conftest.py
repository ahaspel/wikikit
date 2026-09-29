from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from wikikit import settings as settings_module
from wikikit.db.base import Base


@pytest.fixture
def corpus(request):
    """Build as another book for one test, then put it back.

    The corpus is process-wide by design — one run reads one database and
    therefore one book — so a test that changes it has to restore it, or the
    next test inherits a different book.
    """
    before = settings_module.settings.corpus
    settings_module.settings.corpus = request.param
    try:
        yield request.param
    finally:
        settings_module.settings.corpus = before

# Import models so they are registered on Base.metadata
from wikikit.db.models import Article, ArticleSegment, SourcePage  # noqa: F401


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