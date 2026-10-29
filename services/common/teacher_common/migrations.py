"""Minimal schema migrations on top of ``Base.metadata.create_all``.

``create_all`` creates any missing table, so adding a new table needs no migration.
Changes it cannot express (new columns on existing tables, data fixes) are added as
a numbered function to ``MIGRATIONS``. Applied versions are recorded in
``schema_migrations`` so each runs once; the api and ingestion services both call
``init_db`` on startup, so runs are serialised per version by the primary key.
"""

import logging
from collections.abc import Callable

from sqlalchemy import Column, DateTime, Engine, Integer, String, Table, func, insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from teacher_common.db import Base

logger = logging.getLogger("teacher.migrations")

Migration = tuple[int, str, Callable[[Connection], None]]


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version = Column(Integer, primary_key=True, autoincrement=False)
    name = Column(String(255), nullable=False)
    applied_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _baseline_study_tables(connection: Connection) -> None:
    """Quiz and flashcard tables are created by create_all; nothing else to do."""


# Append only. Never edit or renumber a released migration.
MIGRATIONS: list[Migration] = [
    (1, "baseline: quiz and flashcard tables", _baseline_study_tables),
]


def run_migrations(engine: Engine, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations in order; return the versions applied."""
    table: Table = SchemaMigration.__table__  # type: ignore[assignment]
    table.create(bind=engine, checkfirst=True)
    applied: list[int] = []
    for version, name, func_ in sorted(migrations if migrations is not None else MIGRATIONS, key=lambda m: m[0]):
        with engine.begin() as connection:
            if connection.execute(select(table.c.version).where(table.c.version == version)).first():
                continue
            func_(connection)
            try:
                connection.execute(insert(table).values(version=version, name=name))
            except IntegrityError:
                # another service applied it concurrently
                continue
            applied.append(version)
            logger.info("applied schema migration %s: %s", version, name)
    return applied
