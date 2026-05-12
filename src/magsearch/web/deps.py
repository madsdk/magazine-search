from collections.abc import Iterator
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from magsearch.db import make_engine, make_session_factory
from magsearch.settings import Settings, get_settings


@lru_cache(maxsize=1)
def _session_factory_for(database_url: str) -> sessionmaker[Session]:
    return make_session_factory(make_engine(database_url))


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[Session]:
    factory = _session_factory_for(settings.database_url)
    session = factory()
    try:
        yield session
    finally:
        session.close()
