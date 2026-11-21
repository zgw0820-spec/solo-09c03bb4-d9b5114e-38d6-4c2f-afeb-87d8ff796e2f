"""Database access helpers.

Every write path opens its own connection; nothing is cached on the module so
tests can point ``DATABASE_URL`` at a scratch database.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


DEFAULT_DATABASE_URL = "postgresql://alarm:alarm@db:5432/alarms"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Yield a connection; commit on clean exit, roll back on exception."""
    with psycopg.connect(
        database_url(), row_factory=dict_row, autocommit=autocommit
    ) as conn:
        yield conn


SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "schema.sql")


def init_schema() -> None:
    """Apply the idempotent schema.  Called on application startup."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with connect(autocommit=True) as conn:
        # Serialise concurrent startups (e.g. multiple web replicas).
        conn.execute("SELECT pg_advisory_lock(918273645)")
        try:
            conn.execute(sql)
        finally:
            conn.execute("SELECT pg_advisory_unlock(918273645)")
