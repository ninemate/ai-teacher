from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from teacher_common.config import get_settings

Base = declarative_base()

settings = get_settings()
# The api and ingestion containers write to the same SQLite file (sessions, study data, runs, documents).
# Wait for the other writer's short transactions instead of failing after sqlite's default 5 seconds.
_connect_args = {"timeout": 30} if settings.metadata_db_url.startswith("sqlite") else {}
engine = create_engine(settings.metadata_db_url, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    from teacher_common import models, study_models  # noqa: F401
    from teacher_common.migrations import run_migrations

    try:
        Base.metadata.create_all(bind=engine)
    except OperationalError as exc:
        # Both services start together on a fresh volume: the other one may have created a table
        # between our check and our CREATE. Checking again skips what exists now.
        if "already exists" not in str(exc):
            raise
        Base.metadata.create_all(bind=engine)
    run_migrations(engine)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
